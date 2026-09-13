"""PrefillSpeculator——out-of-band prefill 投机预热（2026-09-10 抢跑防抖替代件）。

背景：livekit-agents 框架抢跑（preemptive generation）在本仓 STT 架构下命中
结构性不可能（插件 PREFLIGHT 只发稳定前缀防幻觉，提交转写是全句 FINAL，
框架逐词等价比较恒假；实机 843 失效 / 0 命中），且失效的投机请求被 mlx
「无断连中止」解码到完才放锁，解码尾巴挤占真请求队列。框架抢跑已默认关
（PREEMPTIVE_GENERATION=0）。

本件接手同一目标（LLM prefill 与说话重叠），姿势不同：
- **只预热不出声**——按「下一条真实请求的严格前缀」组装 prompt，
  max_tokens=1 打到本地 LLM，mlx_lm server 把该前缀 KV 留在 prompt cache；
- 真轮提交时，真实请求只 prefill 稳定前缀之后的分叉尾巴（未讲完的几个字
  + 尾部变化），暖轮未缓存后缀从 ~240 tok 压到几十 tok；
- 零正确性风险：投机请求不调度任何语音，回复永远由真请求按 FINAL 文本生成。

投机 prompt 组装（严格前缀契约）：
  [上次真实请求 messages] + [assistant: 上轮回复历史原文] + [user: 稳定前缀+当前尾部]
上一条真实请求本身已在 cache（它跑过）；新增可暖的只有其后两段。assistant
文本取会话历史条目原文（_on_item_for_context 的 raw text，含 expr 标记），
保证与框架追加进历史的逐字节一致——用 last_reply（清洗后）会分叉白暖。

开关：BOK_PREFILL_SPEC=1（默认开，0 关）；BOK_PREFILL_SPEC_GAP_MS（同轮两次
开火最小间隔，默认 600）；BOK_PREFILL_SPEC_MAX（每通轮次开火上限，默认 2）。
"""

from __future__ import annotations

import asyncio
import os
import time


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


class PrefillSpeculator:
    """会话级（每通电话一个）。全部入口幂等、异常吞掉——绝不影响主链路。"""

    def __init__(self, prewarm, context_state) -> None:
        # prewarm: async (messages: list[dict]) -> None——MlxLlmLLM.prefix_prewarm
        # （client read=30s，fire-and-forget 不阻塞任何人）。
        self._prewarm = prewarm
        self._ctx = context_state
        self._last_request: list[dict] | None = None
        self._reply_text: str | None = None
        self._busy = False  # thinking/speaking 期间不开火（LLM 忙，抢不过还添堵）
        self._turn_fires = 0
        self._last_fire_ts = 0.0
        self._last_prefix = ""
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ 输入
    def on_request_messages(self, messages: list[dict]) -> None:
        """快照钩子（MlxLlmLLM.on_request_messages）：逐字节真实请求 messages。"""
        if messages:
            self._last_request = messages

    def on_reply_history_text(self, text: str) -> None:
        """上轮回复进会话历史的原文（含 expr 标记，与框架追加的逐字节一致）。"""
        t = str(text or "")
        if t.strip():
            self._reply_text = t

    def set_busy(self, busy: bool) -> None:
        self._busy = busy

    def new_turn(self) -> None:
        """新用户轮提交（on_user_turn_completed 调）：预算与去重态重置。"""
        self._turn_fires = 0
        self._last_prefix = ""

    def on_stable_prefix(self, text: str) -> None:
        """STT 稳定前缀回调：门控全过则异步开火预热。"""
        _dbg = os.environ.get("BOK_PREFILL_SPEC_DEBUG", "") == "1"
        if os.environ.get("BOK_PREFILL_SPEC", "1") != "1":
            return
        if self._busy or self._task is not None:
            if _dbg:
                print(f"BOK_PREFILL_SPEC skip busy={self._busy} inflight={self._task is not None}", flush=True)
            return
        if not self._last_request or text == self._last_prefix:
            if _dbg:
                print(f"BOK_PREFILL_SPEC skip snapshot={self._last_request is not None} same={text == self._last_prefix}", flush=True)
            return
        # 前缀必须比上次开火更长（≥2 字），同段文本不重复预热。
        if len(text) - len(self._last_prefix) < 2:
            return
        if self._turn_fires >= _env_int("BOK_PREFILL_SPEC_MAX", 2):
            return
        gap_ms = _env_int("BOK_PREFILL_SPEC_GAP_MS", 600)
        if gap_ms > 0 and (time.monotonic() - self._last_fire_ts) * 1000 < gap_ms:
            return
        prefix = str(text or "").strip()
        if len(prefix) < 6:
            return  # 与 PREFLIGHT 稳定前缀门槛一致，太短不值得一次请求
        tail = ""
        try:
            tail = self._ctx.render_context_tail() if self._ctx is not None else ""
        except Exception:  # noqa: BLE001 - 尾部渲染失败按无尾部预热
            tail = ""
        user_content = f"{prefix}\n\n{tail}" if tail else prefix
        msgs = list(self._last_request)
        if self._reply_text:
            msgs.append({"role": "assistant", "content": self._reply_text})
        msgs.append({"role": "user", "content": user_content})

        self._last_prefix = text
        self._last_fire_ts = time.monotonic()
        self._turn_fires += 1
        self._task = asyncio.create_task(self._fire(msgs, len(prefix)))

    # ------------------------------------------------------------------ 执行
    async def _fire(self, msgs: list[dict], prefix_chars: int) -> None:
        try:
            print(
                f"BOK_PREFILL_SPEC fire chars={prefix_chars} msgs={len(msgs)}",
                flush=True,
            )
            await self._prewarm(msgs)
            print("BOK_PREFILL_SPEC done", flush=True)
        except Exception as exc:  # noqa: BLE001 - 预热失败零影响
            print(f"BOK_PREFILL_SPEC failed: {exc!r}", flush=True)
        finally:
            self._task = None
