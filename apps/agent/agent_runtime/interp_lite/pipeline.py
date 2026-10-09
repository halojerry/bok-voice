"""interp_lite 翻译管线：单消费 FIFO + 流式 say（全流式直通管的最薄形态）。

链路（计划档 §2 官方替代表的落地）：
ASR definite（VAD 段会话+负 seq 定稿，provider 层）→ FIFO → DeepSeek 流式 MT
（thinking 显式关、静态系统前缀吃官方上下文缓存）→ **delta 逐字喂 session.say**
（TagGate 校验后下发）→ MiniMax bidi 服务端攒句立即合成（句末标点即起合成；
插件内 head-flush 催产无标点头段）。客户端零攒句、零提交闸、零语气词转换。

带（证据支持的 correctness 件，判据/台词/分类器全部单源复用旧线）：
- 首段语言门（``bok_voice_core.mt_lang_check`` 脚本级；闸键=既有
  ``BOK_INTERP_MT_LANGGUARD``）：违约在首 yield 前抛出=零播报 → 整句回退路径。
- 402/鉴权黑洞短路（``interpret._mt_fatal_provider_error``；call-0bdf1392 实证
  重试永不好）：首次命中标死道，后续句秒走目标语请示句兜底，零 provider 调用。
- MT 超时/异常兜底台词（``interpret._mt_fail_line``）：绝不回放源文。

不带（计划档 §8 审计表）：spec-mt/碎片闸/背压/摘译/回声去重/润色——本地档补偿。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque

from ..interpret import (
    _mt_fail_line,
    _mt_fatal_provider_error,
    _mt_lang_guard_enabled,
)
from .providers.mt_deepseek import DeepSeekMT, build_messages
from .voice_tags import TagGate

_SENT_TIMEOUT_S = 15.0
_QUEUE_MAX = 48
_FIRST_PIECE_MIN_CHARS = 4  # 首段语言门的软证据窗


class _GateFailError(Exception):
    """首段语言门违约（首 yield 前抛出=零播报，调用方走整句回退）。"""


def _looks(text: str, lang: str) -> bool:
    from bok_voice_core.mt_lang_check import looks_like_language

    return looks_like_language(text, lang)


async def _collect(mt: DeepSeekMT, msgs: list[dict], timeout_s: float) -> str:
    """整句排干（超时也关流，不留僵尸解码——旧线 _mt_collect 同纪律）。"""
    parts: list[str] = []
    stream = mt.stream(msgs)

    async def _drain() -> None:
        async for delta in stream:
            parts.append(delta)

    try:
        await asyncio.wait_for(_drain(), timeout=timeout_s)
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):  # 关流尽力而为
                await aclose()
    return "".join(parts).strip()


class InterpPipeline:
    """每方向一个实例；``run()`` 由 worker 作为长任务拉起，shutdown 时 cancel。

    配对账本纪律（旧线 RC-8 同款）：每句**恰好一次** ``done_mt``（真译/兜底句）或
    ``drop_src``（空译文/回退失败），否则 _on_item 会错弹下一句的 pending。
    """

    def __init__(
        self,
        session,
        mt: DeepSeekMT,
        instructions: str,
        *,
        target_lang: str,
        voice_tags: bool,
        lag,
        first_ms: dict,
    ):
        self.session = session
        self.mt = mt
        self.instructions = instructions
        self.target_lang = target_lang
        self.voice_tags = voice_tags
        self.lag = lag
        self.first_ms = first_ms  # {"ms": int} 逐句覆写（观测口径同旧线）
        self.last_ms = {"ms": 0}  # 逐句 MT 总时长（worker 落库 latency_ms 消费）
        self.q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._enq: deque = deque()  # 入队时刻（FIFO 与 q 同序；queue_wait_ms 观测）
        self.queue_wait_ms = {"ms": 0}  # 逐句 FIFO 等待（W6 刀3-lite 四段账）
        self.pairs: deque = deque(maxlen=8)  # (源,译) 滚动对
        self.lane_dead = {"reason": ""}
        self.round = 0

    # ---- 入口（worker 的 user_input_transcribed 回调调用；原文落库在 worker 侧）----
    def enqueue(self, text: str) -> None:
        """源句入队（满=摘最新句防雪崩，旧线同语义）；入队成功才记账（RC-8）。"""
        try:
            self.q.put_nowait(text)
        except asyncio.QueueFull:
            print("[interp-lite] source queue overflow, sentence dropped(摘译)", flush=True)
            return
        self._enq.append(time.perf_counter())
        self.lag.note_src(text)

    def _done(self, mt_ms: int) -> None:
        """配对记账单点（同时覆写 last_ms 供 worker 落库 latency_ms）。"""
        self.last_ms["ms"] = int(mt_ms or 0)
        self.lag.done_mt(int(mt_ms or 0))

    def _drop(self) -> None:
        self.last_ms["ms"] = 0
        self.lag.drop_src()

    # ---- 主循环 ----
    async def run(self) -> None:
        while True:
            text = await self.q.get()
            t_enq = self._enq.popleft() if self._enq else None
            self.queue_wait_ms["ms"] = (
                int((time.perf_counter() - t_enq) * 1000) if t_enq is not None else 0
            )
            self.round += 1
            t0 = time.perf_counter()
            try:
                if self.lane_dead["reason"]:
                    self.session.say(_mt_fail_line(self.target_lang))
                    self._done(0)
                    continue
                await self._translate_say(text, t0)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                await self._fallback_say("timeout")
            except Exception as exc:  # noqa: BLE001 - 单句失败不阻后续
                fatal = _mt_fatal_provider_error(exc)
                if fatal and not self.lane_dead["reason"]:
                    self.lane_dead["reason"] = fatal
                    print(
                        f"[interp-lite] MT_LANE_DEAD reason={fatal} round={self.round} "
                        f"(fast-fail till call end — 充值/切道后下一通恢复)",
                        flush=True,
                    )
                await self._fallback_say(f"error {exc!r}")
            finally:
                self.q.task_done()

    # ---- 单句：流式 say；gate/error_pre 回退整句重开流 ----
    async def _translate_say(self, text: str, t0: float) -> None:
        msgs = build_messages(self.instructions, list(self.pairs), text)
        out = {"state": "clean", "yielded": False}
        done = asyncio.Event()
        gate = TagGate()
        raw: list[str] = []
        guard_on = _mt_lang_guard_enabled()
        lang = self.target_lang
        head = ""  # 首段语言门的软证据累积（不碰 gate 内部状态）

        # MT 与 say 解耦（2026-10-09 流畅度收口）：框架 speech 队列串行拉生成器
        # ——上一段播完前下一个 say 的 gen 无人拉取=MT 流根本没起跑（实弹：尾巴
        # 单元 first_ms 6-7.8s = 上一段播报时长 + 真实 MT 0.6s，缓存全健康）。
        # pump 独立任务先把 DeepSeek 流拉进缓冲，gen 只消费：MT 全程并发，框架
        # 到点即有货可播。None=流尽哨兵；异常对象=流错误透传。
        buf: asyncio.Queue = asyncio.Queue()

        async def _pump():
            try:
                stream = self.mt.stream(msgs)
                async for delta in stream:
                    await buf.put(delta)
                await buf.put(None)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - 错误透传给 gen 分类
                await buf.put(exc)

        pump_task = asyncio.create_task(_pump())

        async def _gen():
            nonlocal head
            deadline = time.monotonic() + _SENT_TIMEOUT_S
            first = {"done": False}

            def _emit(piece: str) -> str | None:
                """首段过语言门后放行；返回 None=证据不足继续攒；违约直接抛 _GateFailError。"""
                nonlocal head
                if first["done"]:
                    return piece
                head += piece
                if len(head.strip()) < _FIRST_PIECE_MIN_CHARS:
                    return None
                if guard_on and lang and not _looks(head, lang):
                    out["state"] = "gate"
                    raise _GateFailError(head)
                first["done"] = True
                self.first_ms["ms"] = int((time.perf_counter() - t0) * 1000)
                return head

            try:
                while True:
                    item = await buf.get()
                    if item is None:
                        break
                    if isinstance(item, BaseException):
                        raise item
                    if time.monotonic() > deadline:
                        out["state"] = "error_mid" if out["yielded"] else "error_pre"
                        print(f"[interp-lite] MT_STREAM deadline state={out['state']}", flush=True)
                        break
                    if not item:
                        continue
                    raw.append(item)
                    piece = gate.feed(item)
                    if not piece:
                        continue
                    verdict = _emit(piece)
                    if verdict is None:
                        continue
                    out["yielded"] = True
                    yield verdict
                tail = gate.flush()
                if tail:
                    verdict = _emit(tail)
                    if verdict is not None:
                        out["yielded"] = True
                        yield verdict
            except _GateFailError:
                pass  # state 已置 gate；零播报，调用方回退
            except asyncio.CancelledError:
                out["state"] = "error_mid" if out["yielded"] else "error_pre"
                raise
            except Exception as exc:  # noqa: BLE001 - 流错误按 yield 前后分类
                out["state"] = "error_mid" if out["yielded"] else "error_pre"
                print(f"[interp-lite] MT_STREAM err state={out['state']} {exc!r}", flush=True)
            finally:
                pump_task.cancel()
                done.set()

        try:
            self.session.say(_gen())
        except Exception as exc:  # noqa: BLE001 - say 提交失败=error_pre 回退
            out["state"] = "error_pre"
            print(f"[interp-lite] MT_STREAM say submit failed {exc!r}", flush=True)
            return await self._retry_once_say(text, msgs, t0)

        # 等 gen 排干（say 消费端停拉时 finally 也会 set；外层兜 3s）。
        try:
            await asyncio.wait_for(done.wait(), timeout=_SENT_TIMEOUT_S + 3.0)
        except asyncio.TimeoutError:
            print("[interp-lite] MT_STREAM gen not drained in time", flush=True)

        if out["state"] in ("clean", "error_mid"):
            translated = "".join(raw).strip()
            self._cache_log()
            if not translated:
                self._drop()
                print(f"[interp-lite] mt empty for {len(text)} chars, skipped", flush=True)
                return
            if not out["yielded"]:
                # clean 但零 yield（极端短流兜底）：整句出声，一次配对。
                self.session.say(self._final_text(translated))
            self.pairs.append((text, translated))
            self._done(int((time.perf_counter() - t0) * 1000))
            return
        if out["state"] == "gate":
            print(f"[interp-lite] MT_STREAM fallback state=gate round={self.round}", flush=True)
            return await self._retry_once_say(text, msgs, t0)
        # error_pre：零播报 → 整句回退（error_mid 不会到这里）。
        print(f"[interp-lite] MT_STREAM fallback state={out['state']} round={self.round}", flush=True)
        await self._retry_once_say(text, msgs, t0)

    async def _retry_once_say(self, text: str, msgs: list[dict], t0: float) -> None:
        """整句回退（新开流排干→语言复查→整句 say；至多一次，无循环重试）。"""
        translated = await _collect(self.mt, msgs, _SENT_TIMEOUT_S)
        if not translated:
            self._drop()
            print(f"[interp-lite] mt retry empty for {len(text)} chars", flush=True)
            return
        if _mt_lang_guard_enabled() and self.target_lang and not _looks(translated, self.target_lang):
            print(
                f"[interp-lite] MT_LANG_MISMATCH retry target={self.target_lang} emit-as-is",
                flush=True,
            )
        self.session.say(self._final_text(translated))
        self.pairs.append((text, translated))
        self.first_ms["ms"] = 0
        self._done(int((time.perf_counter() - t0) * 1000))

    def _final_text(self, translated: str) -> str:
        """整句出声口径：门开=TagGate 全文过一遍（校验/归一）；门关=剥标记单源。"""
        if not self.voice_tags:
            from ..interpret import _speech_text

            return _speech_text(translated, False)
        g = TagGate()
        return g.feed(translated) + g.flush()

    async def _fallback_say(self, why: str) -> None:
        """超时/异常兜底：目标语请示句出声+配对记账（绝不回放源文）。"""
        print(f"[interp-lite] MT_FALLBACK {why} lang={self.target_lang}", flush=True)
        try:
            self.session.say(_mt_fail_line(self.target_lang))
            self._done(0)
        except Exception as say_exc:  # noqa: BLE001 - 兜底不出声也不阻后续
            self._drop()
            print(f"[interp-lite] fallback say failed: {say_exc!r}", flush=True)

    def _cache_log(self) -> None:
        """官方 prompt_cache_hit_tokens 回读（DeepSeek usage 字段，计划档 §2）。"""
        m = getattr(self.mt, "last_metrics", {}) or {}
        hit, total = m.get("prompt_cache_hit_tokens", 0), m.get("prompt_tokens", 0)
        if total:
            print(
                f"[interp-lite] MT_CACHE hit={hit} tok={total} pct={m.get('cached_pct', 0.0)}",
                flush=True,
            )
