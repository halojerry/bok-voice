"""interp_lite 翻译管线 v2：话段连续流式（utterance-continuous SIMT）。

call-e376e7a9 翻案（2026-10-09，Ethan 实弹定调「句间隙可以不要、首声 ≤1.5s」）：
v1 逐子句 say 有两处结构病——①每个提交=一条独立 TTS 流，各自尾窗结算+锁串行+
播放队列 → 「半句 … 4-5s 洞 … 半句」（听感=被截断半句）；②首声=等提交闸
（说话 1.6-2s 才提交）+MT+TTS ≈ 3.2s。

v2 模型（一条话段一条流，链路口径不变：ASR 流式 → DeepSeek 流式 → bidi 攒句）：
- worker 把 FINAL（已提交子句=精确文本）与 INTERIM（未提交余段=不稳定显示）都喂
  进来（feed_final/feed_interim）；管线维护 ``committed+display`` 视图与单调水位
  ``src_sent``（永不回撤；修订分歧认账重锚=MT_CHUNK_DRIFT 观测，不重喂不双播）；
- 切块器在视图增量上切块（chunker.pick_cut）：首块 ≥3 字即出（抢首声）、常规块
  标点优先/12 字保险丝、interim 余段尾 2 字回扣（防重解修订）；每块带「本话段
  前文」进消息（上下文对追加，翻译衔接）；
- 块翻译单飞（逐块顺序消费）→ MT delta 直灌话段 ``tts_q`` → **单条
  ``session.say(generator)``** 逐片产出；服务端 bidi 攒句连续合成=块间零天窗；
- 话段收尾=静默钟（无事件 ``_UTT_QUIET_S``）→ 末块全出 → 关流（哨兵）。
  MT 全失败/零译文 → 收尾后目标语兜底句（绝不回放源文）；车道死=后续话段秒走
  兜底（fail-fast）。配对账本 RC-8：每话段恰好一次 done_mt（真译）或 drop_src。

带（证据支持的 correctness 件，单源复用旧线）：
- 首段语言门（``bok_voice_core.mt_lang_check``；闸键 ``BOK_INTERP_MT_LANGGUARD``）
- 402/鉴权黑洞短路（``interpret._mt_fatal_provider_error``，call-0bdf1392 实证）
- MT 超时/异常兜底台词（``interpret._mt_fail_line``）

不带（计划档 §8 审计表）：spec-mt 持有音频/碎片闸/背压/摘译/回声去重/润色。
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
from .chunker import common_prefix_len, content_len, pick_cut
from .providers.mt_deepseek import DeepSeekMT, build_messages
from .voice_tags import TagGate

_UTT_QUIET_S = 1.2  # 话段边界：无新 ASR 事件的静默时长（VAD 段会话间隙内不会触发）
_STREAM_DEADLINE_S = 12.0  # 单块 MT 流排干上限（超时保已收 delta，不阻后续块）
_HOLD_INTERIM = 2  # interim 余段尾部回扣字数（服务端重解多发生在尾）
_CTX_MAX_CHARS = 400  # 「本话段前文」上下文对的源文上限（防消息无限膨胀）
_FIRST_PIECE_MIN_CHARS = 2  # 首段语言门软证据窗（v2 块更小，窗跟着小）


class _GateFailError(Exception):
    """首段语言门违约（首 yield 前抛出=零播报，话段走整段回退）。"""


def _looks(text: str, lang: str) -> bool:
    from bok_voice_core.mt_lang_check import looks_like_language

    return looks_like_language(text, lang)


async def _collect(mt: DeepSeekMT, msgs: list[dict], timeout_s: float) -> str:
    """整段排干（超时也关流，不留僵尸解码——旧线 _mt_collect 同纪律）。"""
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
            with contextlib.suppress(Exception):
                await aclose()
    return "".join(parts).strip()


class _Utterance:
    """单话段（VAD 段内一组子句）的连续流状态。"""

    __slots__ = (
        "any_delta", "chunk_task", "chunks", "chunks_started", "closed",
        "committed", "display", "gate_failed", "mt_parts", "quiet_seq",
        "quiet_task", "src_final", "src_sent", "t0", "tts_q", "yielded",
    )

    def __init__(self, t0: float):
        self.t0 = t0
        self.committed = ""  # 已提交子句拼接（精确文本）
        self.display = ""    # 未提交余段（服务端显示，可能重解）
        self.src_sent = ""   # 已切块喂出的源文本（单调水位，永不回撤）
        self.chunks_started = False
        self.chunks: asyncio.Queue = asyncio.Queue()
        self.tts_q: asyncio.Queue = asyncio.Queue()
        self.mt_parts: list[str] = []
        self.chunk_task: asyncio.Task | None = None
        self.quiet_task: asyncio.Task | None = None
        self.quiet_seq = 0
        self.closed = False
        self.yielded = False
        self.any_delta = False
        self.gate_failed = False
        self.src_final = ""

    def view(self) -> str:
        return self.committed + self.display


class InterpPipeline:
    """话段连续流管线；``run()`` 由 worker 作为长任务拉起，shutdown 时 cancel。"""

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
        self.first_ms = first_ms  # {"ms": int} 逐话段覆写（t0=话段起→首个译 delta）
        self.last_ms = {"ms": 0}  # 逐话段 MT 总时长（worker 落库 latency_ms 消费）
        self.queue_wait_ms = {"ms": 0}  # v2 无 FIFO 等待（观测行兼容字段，恒 0）
        self.pairs: deque = deque(maxlen=8)  # (源,译) 滚动对（跨话段上下文）
        self.lane_dead = {"reason": ""}
        self.round = 0
        self.utt: _Utterance | None = None

    # ---- 入口（worker 的 user_input_transcribed 回调调用；原文落库在 worker 侧）----
    def feed_final(self, text: str) -> None:
        """已提交子句（精确文本）：committed 追加；display 清空（其后 interim 重建）。"""
        utt = self._ensure_utt()
        if utt is None:
            return
        utt.committed += text
        utt.display = ""
        self._maybe_cut(utt)
        self._arm_quiet(utt)

    def feed_interim(self, text: str) -> None:
        """未提交余段（服务端显示）：覆盖 display；视图增量切块（尾 2 字回扣）。"""
        utt = self._ensure_utt()
        if utt is None:
            return
        utt.display = text
        self._maybe_cut(utt)
        self._arm_quiet(utt)

    def _ensure_utt(self) -> _Utterance | None:
        if self.utt is not None and not self.utt.closed:
            return self.utt
        self.round += 1
        utt = _Utterance(time.perf_counter())
        utt.chunk_task = asyncio.create_task(self._chunk_runner(utt))
        try:
            # 「播报中照样听」不在 say 旗上实现：allow_interruptions=True 实证会被
            # 用户语音打断译文播放（canceled×3 零出声，call-f6d655fd）。正确开关=
            # worker 侧 turn_handling.discard_audio_if_uninterruptible=False（says
            # 保持不可打断=译文播完整，ASR 不被静音替换=播报期间照样听）。
            self.session.say(self._gen(utt))
        except Exception as exc:  # noqa: BLE001 - say 提交失败=本话段放弃（罕见）
            print(f"[interp-lite] MT_STREAM say submit failed {exc!r}", flush=True)
            utt.closed = True
            self.lag.note_src(utt.view())
            self._drop()
            utt.chunks.put_nowait(None)  # 让块协程收工
            return None
        self.utt = utt
        print(f"[interp-lite] UTT_STREAM start round={self.round}", flush=True)
        return utt

    # ---- 切块 ----
    def _maybe_cut(self, utt: _Utterance) -> None:
        if utt.closed:
            return
        view = utt.view()
        sent = utt.src_sent
        if len(view) < len(sent):
            return  # 视图暂缩（final 清 display 的空窗）：等增长
        if not view.startswith(sent):
            cp = common_prefix_len(view, sent)
            print(
                f"[interp-lite] MT_CHUNK_DRIFT cp={cp} sent={len(sent)} view={len(view)}",
                flush=True,
            )
            utt.src_sent = view[: len(sent)]  # 认账重锚：已喂文本不回撤（罕见修订）
            sent = utt.src_sent
            if len(view) <= len(sent):
                return
        avail = view[len(sent):]
        first = not utt.chunks_started
        # 首块 hold=1（抢首声：只回扣 1 字）；常规块 hold=2（标点边界前的重解防抖）
        hold = 1 if first else _HOLD_INTERIM
        usable = avail[: len(avail) - hold] if len(avail) > hold else ""
        cut = pick_cut(usable, first=first)
        if cut <= 0:
            return
        chunk = usable[:cut]
        utt.chunks_started = True
        utt.chunks.put_nowait((chunk, sent))
        utt.src_sent = sent + chunk
        print(f"[interp-lite] MT_CHUNK chars={len(chunk)} first={int(first)}", flush=True)

    # ---- 静默钟（话段收尾） ----
    def _arm_quiet(self, utt: _Utterance) -> None:
        utt.quiet_seq += 1
        if utt.quiet_task is not None and not utt.quiet_task.done():
            utt.quiet_task.cancel()
        utt.quiet_task = asyncio.create_task(self._quiet_wait(utt, utt.quiet_seq))

    async def _quiet_wait(self, utt: _Utterance, seq: int) -> None:
        try:
            await asyncio.sleep(_UTT_QUIET_S)
        except asyncio.CancelledError:
            return  # 新事件重置：静默钟重挂
        if utt.quiet_seq == seq and not utt.closed:
            self._close(utt)

    def _close(self, utt: _Utterance) -> None:
        """末块全出 + 关流哨兵；记账（note_src）在收尾配对前挂上（RC-8 同序）。"""
        if utt.closed:
            return
        utt.closed = True
        src_final = utt.src_final = utt.view()
        view, sent = utt.view(), utt.src_sent
        if len(view) > len(sent) and view.startswith(sent):
            rest = view[len(sent):].strip()
            if rest and content_len(rest) > 0:  # 纯标点尾巴无内容可译：丢
                utt.chunks.put_nowait((rest, sent))
                utt.src_sent = view
        self.lag.note_src(src_final)
        utt.chunks.put_nowait(None)
        print(
            f"[interp-lite] UTT_STREAM close src_chars={len(src_final)} "
            f"chunks={int(utt.chunks_started)}",
            flush=True,
        )

    # ---- say 生成器（框架消费端；一条话段一个） ----
    async def _gen(self, utt: _Utterance):
        gate = TagGate()
        head = ""
        first_done = False
        guard_on = _mt_lang_guard_enabled()
        lang = self.target_lang

        def _emit(piece: str):
            """首段过语言门后放行；None=证据不足继续攒；违约抛 _GateFailError。"""
            nonlocal head, first_done
            if first_done:
                return piece
            head += piece
            if len(head.strip()) < _FIRST_PIECE_MIN_CHARS:
                return None
            if guard_on and lang and not _looks(head, lang):
                raise _GateFailError(head)
            first_done = True
            return head

        try:
            while True:
                item = await utt.tts_q.get()
                if item is None:
                    break
                if isinstance(item, BaseException):
                    print(f"[interp-lite] MT_STREAM err {item!r}", flush=True)
                    break
                piece = gate.feed(item)
                if not piece:
                    continue
                verdict = _emit(piece)
                if verdict is None:
                    continue
                utt.yielded = True
                yield verdict
            tail = gate.flush()
            if tail:
                verdict = _emit(tail)
                if verdict is not None:
                    utt.yielded = True
                    yield verdict
        except _GateFailError:
            utt.gate_failed = True
            print("[interp-lite] MT_STREAM fallback state=gate", flush=True)

    # ---- 块翻译单飞（逐块顺序） ----
    async def _chunk_runner(self, utt: _Utterance) -> None:
        try:
            while True:
                item = await utt.chunks.get()
                if item is None:
                    break
                if self.lane_dead["reason"]:
                    continue  # 死道：不翻（收尾走兜底句）
                chunk, prior_src = item
                await self._translate_chunk(utt, chunk, prior_src)
        except asyncio.CancelledError:
            raise
        finally:
            self._finish_utt(utt)
            utt.tts_q.put_nowait(None)
        if utt.gate_failed:
            await self._retry_once_say(utt)
        elif self.lane_dead["reason"] and not utt.mt_parts:
            await self._fallback_say("lane_dead")

    def _ctx_pairs(self, utt: _Utterance, prior_src: str) -> list[tuple[str, str]]:
        pairs = list(self.pairs)
        mt_so_far = "".join(utt.mt_parts).strip()
        if prior_src and mt_so_far and len(prior_src) <= _CTX_MAX_CHARS:
            # 本话段前文进上下文对（时间序在跨话段对之后）：块间翻译衔接单点。
            pairs.append((prior_src, mt_so_far))
        return pairs

    async def _translate_chunk(self, utt: _Utterance, chunk: str, prior_src: str) -> None:
        msgs = build_messages(self.instructions, self._ctx_pairs(utt, prior_src), chunk)
        produced = False
        try:
            async with asyncio.timeout(_STREAM_DEADLINE_S):
                async for delta in self.mt.stream(msgs):
                    if not delta:
                        continue
                    if not utt.any_delta:
                        utt.any_delta = True
                        self.first_ms["ms"] = int((time.perf_counter() - utt.t0) * 1000)
                    produced = True
                    utt.mt_parts.append(delta)
                    utt.tts_q.put_nowait(delta)
        except asyncio.CancelledError:
            raise
        except TimeoutError:  # asyncio.timeout 超时（py3.11+ = 内建 TimeoutError）
            print(f"[interp-lite] MT_CHUNK_TIMEOUT chars={len(chunk)}", flush=True)
        except Exception as exc:  # noqa: BLE001 - 单块失败不阻后续
            fatal = _mt_fatal_provider_error(exc)
            if fatal and not self.lane_dead["reason"]:
                self.lane_dead["reason"] = fatal
                print(f"[interp-lite] MT_LANE_DEAD reason={fatal} round={self.round}", flush=True)
            if not produced and not fatal:
                # 零 delta 的瞬时失败：整块回退一次（同 v1 单次重试纪律）
                try:
                    translated = await _collect(self.mt, msgs, _STREAM_DEADLINE_S)
                except Exception as retry_exc:  # noqa: BLE001
                    print(f"[interp-lite] MT_CHUNK_RETRY_FAIL {retry_exc!r}", flush=True)
                    translated = ""
                if translated:
                    if not utt.any_delta:
                        utt.any_delta = True
                        self.first_ms["ms"] = int((time.perf_counter() - utt.t0) * 1000)
                    utt.mt_parts.append(translated)
                    utt.tts_q.put_nowait(translated)
        finally:
            self._cache_log()

    # ---- 收尾配对（RC-8：每话段恰一次 done/drop；回退路径自结对） ----
    def _finish_utt(self, utt: _Utterance) -> None:
        if utt.gate_failed or (self.lane_dead["reason"] and not utt.mt_parts):
            return
        mt_ms = int((time.perf_counter() - utt.t0) * 1000)
        translated = "".join(utt.mt_parts).strip()
        if not translated:
            self._drop()
            print(f"[interp-lite] mt empty for {len(utt.src_final)} chars, skipped", flush=True)
            return
        src = utt.src_final or utt.view()
        self.pairs.append((src, translated))
        self._done(mt_ms)

    def _done(self, mt_ms: int) -> None:
        """配对记账单点（同时覆写 last_ms 供 worker 落库 latency_ms）。"""
        self.last_ms["ms"] = int(mt_ms or 0)
        self.lag.done_mt(int(mt_ms or 0))

    def _drop(self) -> None:
        self.last_ms["ms"] = 0
        self.lag.drop_src()

    async def _retry_once_say(self, utt: _Utterance) -> None:
        """整段回退（语言门违约）：新开流排干→整段 say；至多一次，无循环重试。"""
        src = utt.src_final or utt.view()
        msgs = build_messages(self.instructions, list(self.pairs), src)
        translated = await _collect(self.mt, msgs, _STREAM_DEADLINE_S)
        if not translated:
            self._drop()
            print(f"[interp-lite] mt retry empty for {len(src)} chars", flush=True)
            return
        if _mt_lang_guard_enabled() and self.target_lang and not _looks(translated, self.target_lang):
            print(
                f"[interp-lite] MT_LANG_MISMATCH retry target={self.target_lang} emit-as-is",
                flush=True,
            )
        self.session.say(self._final_text(translated))
        self.pairs.append((src, translated))
        self.first_ms["ms"] = 0
        self._done(int((time.perf_counter() - utt.t0) * 1000))

    def _final_text(self, translated: str) -> str:
        """整段出声口径：门开=TagGate 全文过一遍（校验/归一）；门关=剥标记单源。"""
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

    # ---- supervisor ----
    async def run(self) -> None:
        """worker 持 task；取消=收线：尽力冲尾（有界）后退出。"""
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._shutdown_flush(), timeout=2.0)
            raise

    async def _shutdown_flush(self) -> None:
        utt = self.utt
        if utt is None or utt.closed:
            return
        self._close(utt)
        if utt.chunk_task is not None:
            with contextlib.suppress(Exception):
                await utt.chunk_task  # 末块排干（外层 wait_for 有界）
