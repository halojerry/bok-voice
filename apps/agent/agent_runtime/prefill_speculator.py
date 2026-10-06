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

云车道（2026-10-06 A 线对偶件）：装配点判 lane="cloud"（DeepSeek 端点 +
BOK_PREFILL_SPEC_CLOUD=1 缺省，见 prefill_lane_for）时同一形状的 max_tokens=1
预热请求打向云——DeepSeek 服务端自动前缀缓存（相同前缀 10-15 分钟 TTL 内命中
价 ~1/10）使真回复命中 cached=N/M（LLM_TTFT_MS 直接可见，无需新打点）。
BOK_PREFILL_SPEC_CLOUD=0 回旧 host 门逐字节（云档零发射）。云档成本护栏
（lane=cloud 专属，本地车道零触碰）：prompt 折算字符数 >
BOK_PREFILL_SPEC_CLOUD_MAX_CHARS（缺省 16000≈4k token；<=0=关）跳过；每通
开火累计 > BOK_PREFILL_SPEC_CLOUD_MAX_PER_CALL（缺省 12；<=0=关）跳过——
每通上限跨轮累计、new_turn 不归还。既有每轮 2 次/间隔 600ms/busy/F6 门控
云臂照旧。abort 语义：云请求无 mlx abort 旗（plugins._mlx_abort_on_for 只认
loopback，云端零注入判例），FINAL 即断=客户端关连接，max_tokens=1 服务端自完。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from urllib.parse import urlparse

from bok_voice_core.deepseek_llm import is_deepseek_endpoint

from .slot_actor import compose_slot_user_message


def _env_int(name: str, default: int, env: Mapping[str, str] | None = None) -> int:
    src = os.environ if env is None else env
    try:
        return int(src.get(name, "") or default)
    except ValueError:
        return default


# ---- 云车道判定与成本护栏（2026-10-06 A 线对偶件；纯函数，单测直喂）-----------

# 本机 loopback 判据（agent._is_local_base_url / plugins._MLX_LOCAL_HOSTS 同形状;
# 各处自持 frozenset 字面量,刻意不跨模块 import 防 agent↔本模块环）。
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_CLOUD_MAX_CHARS_DEFAULT = 16000  # ≈4k token：超过=尾部占比过大，预热无账可算
_CLOUD_MAX_PER_CALL_DEFAULT = 12  # 云费保险丝：每通累计开火上限


def prefill_lane_for(base_url: str, env: Mapping[str, str] | None = None) -> str:
    """PrefillSpeculator 车道判定："local" | "cloud" | ""（不发预热）。

    - 本机 loopback → "local"（2026-09-10 起的旧 host 门行为逐字节，不看云开关）；
      base 空 → 同样 "local"（保守照旧——替身/嵌入方零行为变化，旧
      ``_llm_prewarm_local`` 对空/读不到返 True 同款）；
    - DeepSeek 云端点（``is_deepseek_endpoint`` 单点识别，勿复制其逻辑）且
      BOK_PREFILL_SPEC_CLOUD=1（缺省；任何非 "1" 值=回旧 host 门逐字节）→
      "cloud"；
    - 其余云端点（非 DeepSeek）→ ""（旧 host 门挡下）。
    """
    base = str(base_url or "")
    try:
        host = (urlparse(base).hostname or "").lower()
    except Exception:  # noqa: BLE001 - 坏 base=非本地，落云端分支
        host = ""
    if not base or host in _LOCAL_HOSTS:
        return "local"
    src = os.environ if env is None else env
    if str(src.get("BOK_PREFILL_SPEC_CLOUD", "1") or "").strip() != "1":
        return ""
    if is_deepseek_endpoint(base):
        return "cloud"
    return ""


def lane_for_llm_provider(llm_provider) -> str:
    """装配点入口：读内芯 ``_client.base_url`` 后转 ``prefill_lane_for``。

    读不到底（替身/嵌入方）→ "local"（旧 ``_llm_prewarm_local`` 保守 True 同款，
    本地为主零行为变化）。
    """
    try:
        base = str(
            getattr(getattr(llm_provider, "_client", None), "base_url", "") or ""
        )
    except Exception:  # noqa: BLE001 - 读不到=保守 local
        return "local"
    return prefill_lane_for(base)


def cloud_budget_verdict(
    total_chars: int, cloud_fires: int, env: Mapping[str, str] | None = None
) -> tuple[bool, str]:
    """云档成本护栏（纯函数）：返回 (放行?, 拦截原因)——本地车道不经此函数。

    - 长度护栏：prompt 折算字符数 > BOK_PREFILL_SPEC_CLOUD_MAX_CHARS（缺省
      16000≈4k token）→ skip——预热只对「静态前缀+早期稳定前缀」有账可算，
      尾部过长时 miss 面随体积涨，烧的是真金；
    - 每通上限：BOK_PREFILL_SPEC_CLOUD_MAX_PER_CALL（缺省 12）——跨轮累计、
      new_turn 不归还（与每轮 2 次的轮内预算不同层）；
    - 两键 <=0 =关该护栏（0=关，与 FINAL_QUIET_MS/GAP_MS 同款读法）；非整数
      回缺省（_env_int 纪律）。
    """
    max_chars = _env_int(
        "BOK_PREFILL_SPEC_CLOUD_MAX_CHARS", _CLOUD_MAX_CHARS_DEFAULT, env
    )
    if max_chars > 0 and total_chars > max_chars:
        return False, "max_chars"
    max_fires = _env_int(
        "BOK_PREFILL_SPEC_CLOUD_MAX_PER_CALL", _CLOUD_MAX_PER_CALL_DEFAULT, env
    )
    if max_fires > 0 and cloud_fires >= max_fires:
        return False, "max_per_call"
    return True, ""


class PrefillSpeculator:
    """会话级（每通电话一个）。全部入口幂等、异常吞掉——绝不影响主链路。"""

    def __init__(self, prewarm, context_state, lane: str = "local") -> None:
        # prewarm: async (messages: list[dict]) -> None——MlxLlmLLM.prefix_prewarm
        # （client read=30s，fire-and-forget 不阻塞任何人）。
        self._prewarm = prewarm
        self._ctx = context_state
        # 车道（2026-10-06 云放行）：装配点 lane_for_llm_provider 判定；
        # "cloud" 臂加成本护栏+lane=cloud 打点，"local" 逐字节旧行为。
        self._lane = str(lane or "local")
        # 云档每通开火累计（new_turn 不归还——与 _turn_fires 的轮内预算不同层）。
        self._cloud_fires = 0
        self._last_request: list[dict] | None = None
        self._reply_text: str | None = None
        # F6 稳定性门（2026-09-28）：快照时刻的 context revision。真请求落地时尾部
        # 会按当前 revision 渲染；若快照之后 revision 已前进（换步/事实沉淀），投机
        # 组的 user 段必与真请求分叉=纯白烧 GPU，直接跳过开火。
        self._snapshot_revision: int | None = None
        self._busy = False  # thinking/speaking 期间不开火（LLM 忙，抢不过还添堵）
        self._turn_fires = 0
        self._last_fire_ts = 0.0
        self._last_final_ts = 0.0  # 最近一次 FINAL 提交时刻（new_turn 记）
        self._last_prefix = ""
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ 输入
    def on_request_messages(self, messages: list[dict]) -> None:
        """快照钩子（MlxLlmLLM.on_request_messages）：逐字节真实请求 messages。

        同时记下快照时刻的 context revision（F6），供 on_stable_prefix 判投机尾部
        是否已与真请求分叉。ctx 无 revision（测试替身）→ None=不启用本门。
        """
        if messages:
            self._last_request = messages
            self._snapshot_revision = getattr(self._ctx, "revision", None)

    def on_reply_history_text(self, text: str) -> None:
        """上轮回复进会话历史的原文（含 expr 标记，与框架追加的逐字节一致）。"""
        t = str(text or "")
        if t.strip():
            self._reply_text = t

    def set_busy(self, busy: bool) -> None:
        self._busy = busy

    def new_turn(self) -> None:
        """新用户轮提交（on_user_turn_completed 调）：预算与去重态重置 + FINAL 即断。

        FINAL 即断（2026-09-25 车道卫生）：真回复请求马上要进 :1235 reply 车道——
        在飞的投机预热继续跑只会与真回复抢槽（mlx 无抢占，客户端断连也不中止
        解码；llm.log 实证两个 prefill 窗同秒交错即此类残余）。取消任务=关掉
        httpx 连接，把残余解码尾巴压到最小。静默窗（BOK_PREFILL_SPEC_FINAL_
        QUIET_MS，默认 1000，0=关）拦住 FINAL 后即刻再开火的竞态窗。
        """
        self._turn_fires = 0
        self._last_prefix = ""
        self._last_final_ts = time.monotonic()
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    def on_stable_prefix(self, text: str) -> None:
        """STT 稳定前缀回调：门控全过则异步开火预热。"""
        _dbg = os.environ.get("BOK_PREFILL_SPEC_DEBUG", "") == "1"
        if os.environ.get("BOK_PREFILL_SPEC", "1") != "1":
            return
        if self._busy or self._task is not None:
            if _dbg:
                print(f"BOK_PREFILL_SPEC skip busy={self._busy} inflight={self._task is not None}", flush=True)
            return
        quiet_ms = _env_int("BOK_PREFILL_SPEC_FINAL_QUIET_MS", 1000)
        if quiet_ms > 0 and (time.monotonic() - self._last_final_ts) * 1000 < quiet_ms:
            if _dbg:
                print(f"BOK_PREFILL_SPEC skip final_quiet={quiet_ms}ms", flush=True)
            return
        if not self._last_request or text == self._last_prefix:
            if _dbg:
                print(f"BOK_PREFILL_SPEC skip snapshot={self._last_request is not None} same={text == self._last_prefix}", flush=True)
            return
        # F6 稳定性门：快照后尾部 revision 已前进（换步/事实沉淀）→ 投机 user 段必与
        # 真请求分叉，白烧 GPU，跳过（ctx 无 revision=测试替身，不启用）。
        _rev = getattr(self._ctx, "revision", None)
        if (
            self._snapshot_revision is not None
            and _rev is not None
            and _rev != self._snapshot_revision
        ):
            if _dbg:
                print(
                    f"BOK_PREFILL_SPEC skip revision_advanced "
                    f"snapshot={self._snapshot_revision} now={_rev}",
                    flush=True,
                )
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
        # prefix 过同步润色(2026-10-02 审计修):真请求的 user 消息经
        # _polish_body(CSC/吸附润色,BOK_ASR_POLISH 默认开)——旧版投机用
        # raw STT 前缀,凡润色有编辑的轮,投机 user 段与真请求字节分叉=整段
        # 白烧(还在单生成线程上排真请求)。sync_polish=确定性吸附(纯本地
        # ~1ms,润色 map 未填时它正是真请求的同款兜底路径)。两分支(slot/
        # legacy)的 user 消息都以润色后的 prefix 组装。
        from .asr_polish_runtime import sync_polish as _sync_polish
        _lang = getattr(self._ctx, "user_language", None)
        try:
            prefix = _sync_polish(prefix, _lang or None) or prefix
        except Exception:  # noqa: BLE001 - 润色失败=raw 前缀照旧
            pass
        if getattr(self._ctx, "slot_mode", False):
            # D1 槽位化（2026-10-01）：真请求 user 消息=任务块+客户话
            # （compose_slot_user_message 单一顺序源），投机预热必须同序——
            # 否则预热序列从任务块处与真请求分叉，白烧一次全量 prefill。
            user_content = compose_slot_user_message(tail, prefix)
        else:
            user_content = f"{prefix}\n\n{tail}" if tail else prefix
        msgs = list(self._last_request)
        if self._reply_text:
            msgs.append({"role": "assistant", "content": self._reply_text})
        msgs.append({"role": "user", "content": user_content})

        # 云档成本护栏（lane=cloud 专属，本地车道零触碰）：长度+每通上限一次判。
        # 拦截发生在状态提交前——不烧 _turn_fires/去重态/间隔时间戳。
        if self._lane == "cloud":
            total_chars = sum(len(str(m.get("content") or "")) for m in msgs)
            _ok, _why = cloud_budget_verdict(total_chars, self._cloud_fires)
            if not _ok:
                if _dbg:
                    print(
                        f"BOK_PREFILL_SPEC skip cloud_budget why={_why} chars={total_chars}",
                        flush=True,
                    )
                return
            self._cloud_fires += 1

        self._last_prefix = text
        self._last_fire_ts = time.monotonic()
        self._turn_fires += 1
        self._task = asyncio.create_task(self._fire(msgs, len(prefix)))

    # ------------------------------------------------------------------ 执行
    async def _fire(self, msgs: list[dict], prefix_chars: int) -> None:
        try:
            # lane=cloud 后缀（2026-10-06 云放行）：local 行逐字节不变（RUNBOOK
            # 日志口径不动），云臂可 grep「fire lane=cloud」对账每通云预热次数。
            _lane_tag = "" if self._lane == "local" else f" lane={self._lane}"
            print(
                f"BOK_PREFILL_SPEC fire{_lane_tag} chars={prefix_chars} msgs={len(msgs)}",
                flush=True,
            )
            await self._prewarm(msgs)
            print("BOK_PREFILL_SPEC done", flush=True)
        except asyncio.CancelledError:
            # FINAL 即断（new_turn cancel）：预热被真回复让路，重抛保持取消语义。
            print("BOK_PREFILL_SPEC aborted (final committed)", flush=True)
            raise
        except Exception as exc:  # noqa: BLE001 - 预热失败零影响
            print(f"BOK_PREFILL_SPEC failed: {exc!r}", flush=True)
        finally:
            _cur = asyncio.current_task()
            if _cur is None or self._task is _cur:
                self._task = None
