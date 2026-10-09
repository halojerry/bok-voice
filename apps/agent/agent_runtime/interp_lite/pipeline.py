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

不带（计划档 §8 审计表）：碎片闸/背压/摘译/回声去重/润色——本地档补偿。
W8-A1 回归两件（全 env 门控，缺省保守）：
- **spec-mt**（``BOK_INTERP_SPEC_MT`` 缺省 1；装配见 ``spec_mt.py``——机器件单源
  import 旧线）：说话中稳定子句投机 MT+TTS 成 held PCM，final 前缀确认
  HIT=零合成直播；busy 闸让位真车道（FIFO 深度/真 MT 在途/死道）。
- **轮尾 task_flush**（``BOK_INTERP_TAIL_FLUSH`` 缺省 1）：FIFO 空+当前 say
  排干后向 MiniMax bidi 连接催一枚 task_flush（无标点短尾立即起合成，
  免等官方无标点兜底窗；通道见 ``providers/tts_minimax.tail_flush_channel``）。

W8-B 播放解耦（2026-10-09，目标=播放时间=准备时间）：MT 泵独立任务过语言门/
TagGate 排干进缓冲，say 的 gen 只消费缓冲（框架 speech 队列**播放时才拉取**
——串行保序与 bidi 单连接纪律仍由框架承担）；run 主循环等**泵排干（MT 完成）
即推进 FIFO**，播放期间下一单元 MT 并发在跑。取消纪律：泵的 cancel 只终结泵
（run 等 Event，永不接住泵的 cancel）；播放端提前退场（打断）由 gen finally
就地 cancel 泵。催尾触发点迁 **playout 观察者**（``wait_for_playout`` 面；
泵排干≠文本推送完成，提前催尾毒化 bidi flushed 握手）；fake/无该面=run 循环
旧位兜底（测试与直嵌姿势零漂移）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections import deque

from ..interpret import (
    _mt_fail_line,
    _mt_fatal_provider_error,
    _mt_lang_guard_enabled,
)
from .providers.mt_deepseek import DeepSeekMT, build_messages
from .turn_stream import (
    TurnStream,
    _turn_coalesce_enabled,
    arm_tail_flush,
    maybe_tail_flush,
)
from .voice_tags import TagGate

_SENT_TIMEOUT_S = 15.0
_SENT_TIMEOUT_GRACE_S = 3.0  # 泵排干兜底窗（pump 自身 deadline 外的悬挂余量；流挂死由 run cancel）
_QUEUE_MAX = 48
_FIRST_PIECE_MIN_CHARS = 4  # 首段语言门的软证据窗
_TAIL_FLUSH_ENV = "BOK_INTERP_TAIL_FLUSH"


def _tail_flush_enabled() -> bool:
    """轮尾催尾总闸（默认开；0=旧路径逐字节——排干后零额外动作）。"""
    return os.environ.get(_TAIL_FLUSH_ENV, "1") == "1"


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
        tts_provider=None,
        stt_provider=None,
        stats: dict | None = None,
        tempo=None,
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
        self.mt_busy = {"flag": False}  # 真 MT 在途旗（spec busy 闸消费，旧线同构）
        self.round = 0
        self._tempo = tempo  # 播放背压 auto_tempo（W8-B；None=未装配=零开销）
        self._last_say = None  # 最近一次 say 的 SpeechHandle（轮尾催尾的取消门）
        self._spec = None
        self._tail_flush = None
        self._flush_tasks: set = set()  # 在途催尾观察者任务（shutdown 收线用）
        self._playout_watchers = {"n": 0}  # 播放观察者在途计数（belt 域见 turn_stream）
        self._active_pump: asyncio.Task | None = None  # 在途 MT 泵（收线 cancel 用）
        # 话轮聚合开流（2026-10-09 call-743064ad 断断续续根修）：一条 say 流吃
        # 整个话轮——逐句「flush 握手+锁交接」2-3.5s 税归零。机器件=turn_stream
        # 模块（开流/进料/收口钟/催产/belt）；BOK_INTERP_TURN_COALESCE=0 回旧径。
        self._turn_mode = _turn_coalesce_enabled()
        self._turn = TurnStream(self)
        # 轮尾催尾通道（TTS 在场才装配；MiniMax bidi 专用，其他 provider 无害跳过）
        if tts_provider is not None and _tail_flush_enabled():
            from .providers.tts_minimax import tail_flush_channel

            self._tail_flush = tail_flush_channel(tts_provider)
        # 投机翻译装配（机器件单源旧线，见 spec_mt.py；text-only/总闸关=None=旧路径）
        if tts_provider is not None:
            from . import spec_mt

            self._spec = spec_mt.build(
                self,
                tts_provider=tts_provider,
                run_mt=spec_mt.run_mt_factory(
                    mt, instructions, self.pairs, _collect, _SENT_TIMEOUT_S
                ),
                stats=stats,
            )
            if self._spec is not None and stt_provider is not None and hasattr(
                stt_provider, "raw_interim_listener"
            ):
                # 豆包原文挂点直喂（clause-commit 坐标系；旧线同判——挂点在位时
                # 会话级 interim 不再重复喂，防双喂坐标漂移）。
                stt_provider.raw_interim_listener = self._spec.on_interim
                print("[interp-lite] spec feed=raw-interim (clause-commit coords)", flush=True)
                print("[interp-lite] spec_mt armed (prewarm-and-confirm)", flush=True)

    # ---- 入口（worker 的 user_input_transcribed 回调调用；原文落库在 worker 侧）----
    def enqueue(self, text: str) -> None:
        """final 入口：先过投机确认（HIT=held PCM 直播+余段入队，跳过正常路径），
        未中/关闸=照旧入队（旧线 _on_user_input 同序）。"""
        if self._spec is not None and self._spec.on_final(text):
            return
        self.enqueue_raw(text)

    def enqueue_raw(self, text: str) -> None:
        """纯入队（spec 余段/defer 兜底共用；**不过确认门**——防 on_final 递归）。
        满=摘最新句防雪崩（旧线同语义）；入队成功才记账（RC-8）。"""
        try:
            self.q.put_nowait(text)
        except asyncio.QueueFull:
            print("[interp-lite] source queue overflow, sentence dropped(摘译)", flush=True)
            return
        self._enq.append(time.perf_counter())
        self.lag.note_src(text)

    def enqueue_precomputed_text(self, src: str, text: str) -> None:
        """spec HIT 预计算译文文本入队（FIFO 保序——call-d1cf9dc3 乱序修复终版）。

        2026-10-09 两轮修复收敛：HIT 不再走 say(audio=) 通道（与 say(text=)
        在框架 speech queue 内不保跨类型播放序——audio 零合成先出声=第二段
        先播）。改为预计算译文**文本**入 FIFO，run 循环跳过 MT 直接 say(text)
        走正常 TTS 合成管线——省 MT 时间（first_ms=0）+ 保播放序（同类型 say）。
        """
        try:
            self.q.put_nowait(("__precomputed__", text))
        except asyncio.QueueFull:
            print("[interp-lite] precomputed queue overflow, dropped(摘译)", flush=True)
            return
        self._enq.append(time.perf_counter())

    def shutdown(self) -> None:
        """收线卫生：话轮收口 + 投机在途任务 + MT 泵 + 催尾观察者 cancel（绝不
        外抛；无=no-op）。泵/观察者被 cancel 后由各自 finally 兜底（哨兵入 buf/
        关流/计数回落），绝不悬挂。"""
        self._turn.close_now()
        if self._spec is not None:
            self._spec.cancel("shutdown")
        self._cancel_active_pump()
        for t in tuple(self._flush_tasks):
            if not t.done():
                t.cancel()

    # ---- 出声单点路由（聚合开=进本话轮流；聚合关=旧径逐句 say）----
    def _say_text(self, text: str) -> None:
        if not self._turn_mode:
            self._last_say = self.session.say(text)
            arm_tail_flush(self, self._last_say)
            return
        self._turn.open()
        self._turn.put(text)

    def _cancel_active_pump(self) -> None:
        """在途 MT 泵就地收线（run 被 cancel/流挂死超时/say 提交失败）。

        cancel 只终结泵本身（哨兵+关流由 pump finally 兜）；绝不外抛。"""
        pump = self._active_pump
        self._active_pump = None
        if pump is not None and not pump.done():
            pump.cancel()

    def _done(self, mt_ms: int) -> None:
        """配对记账单点（同时覆写 last_ms 供 worker 落库 latency_ms）。"""
        self.last_ms["ms"] = int(mt_ms or 0)
        self.lag.done_mt(int(mt_ms or 0))

    def _drop(self) -> None:
        self.last_ms["ms"] = 0
        self.lag.drop_src()

    # ---- 主循环 ----
    def _tempo_tick(self) -> None:
        """播放水位→变速档决策点（每单元出队后一次；tempo 未装配=no-op 逐字节）。

        积压估计=FIFO 深度×近期句均时长（指数均值，TTS 侧逐流实测回灌，初值
        2.5s，见 auto_tempo.TempoController）；档位升级一次一档、降级需保持满
        hold_s 且积压低于进入阈值×0.7（退出滞回）。变速的音频应用在 TTS 侧
        （frame_transform 注入），本点只做决策与档位观测。"""
        tempo = self._tempo
        if tempo is None:
            return
        depth = self.q.qsize()
        prev_level = tempo.current.get("level", 0)
        snap = tempo.resolve(tempo.backlog_estimate_ms(depth), depth, time.monotonic())
        if snap["level"] != prev_level or snap["severe"]:
            print(
                f"[interp-lite] INTERP_TEMPO state={snap['state']} speed={snap['speed']} "
                f"backlog_ms={snap['backlog_ms']:.0f} depth={depth}",
                flush=True,
            )

    async def run(self) -> None:
        while True:
            item = await self.q.get()
            t_enq = self._enq.popleft() if self._enq else None
            self.queue_wait_ms["ms"] = (
                int((time.perf_counter() - t_enq) * 1000) if t_enq is not None else 0
            )
            self.round += 1
            self._turn.cancel_close()  # 新单元到达=话轮未完,撤收口钟续流
            self._tempo_tick()  # 变速背压:出队即按积压水位调档(armed 才动)
            t0 = time.perf_counter()
            try:
                if self.lane_dead["reason"]:
                    self._say_text(_mt_fail_line(self.target_lang))
                    self._done(0)
                    continue
                # spec HIT 预计算文本（跳 MT 直走 TTS 合成保播放序——同类型 say 串行）。
                if isinstance(item, tuple) and len(item) == 2 and item[0] == "__precomputed__":
                    _, pre_text = item
                    self._say_text(pre_text)
                    self.pairs.append((pre_text, pre_text))
                    continue
                text = item
                self.mt_busy["flag"] = True
                try:
                    await self._translate_say(text, t0)
                finally:
                    self.mt_busy["flag"] = False
                await maybe_tail_flush(self)
            except asyncio.CancelledError:
                self._cancel_active_pump()  # 收线：在途泵就地 cancel（哨兵/关流由 finally 兜）
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
                # 话轮收口排程（finally 位=lanedead/precomputed 的 continue 路也覆盖；
                # 钟到点再核 FIFO/MT 闲，误排无害；新单元到达由循环顶撤钟）。
                self._turn.schedule_close()
                self.q.task_done()

    # ---- 单句：流式 say；gate/error_pre 回退整句重开流 ----
    async def _translate_say(self, text: str, t0: float) -> None:
        if self._turn_mode:
            return await self._translate_say_turn(text, t0)
        msgs = build_messages(self.instructions, list(self.pairs), text)
        out = {"state": "clean", "yielded": False}
        gate = TagGate()
        raw: list[str] = []
        guard_on = _mt_lang_guard_enabled()
        lang = self.target_lang
        head = ""  # 首段语言门的软证据累积（不碰 gate 内部状态）
        # MT 与 say 解耦（W8-B 播放解耦）：pump 独立任务把 MT 流**过完语言门/
        # TagGate**后排进 buf（状态/raw 也归泵单写——run 只在泵排干后读，无竞态）；
        # say 的 gen 只是薄消费者（框架 speech 队列**播放时才拉取**，串行保序=
        # bidi 单连接纪律仍由框架承担）。buf 内唯一非 str 项=None 流尽哨兵
        # （pump finally 必投，gen 见之即收，任何路径永不悬挂）。
        buf: asyncio.Queue = asyncio.Queue()
        pump_done = asyncio.Event()
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

        async def _pump():
            stream = None
            try:
                stream = self.mt.stream(msgs)
                deadline = time.monotonic() + _SENT_TIMEOUT_S
                async for delta in stream:
                    if time.monotonic() > deadline:
                        out["state"] = "error_mid" if out["yielded"] else "error_pre"
                        print(f"[interp-lite] MT_STREAM deadline state={out['state']}", flush=True)
                        break
                    if not delta:
                        continue
                    raw.append(delta)
                    piece = gate.feed(delta)
                    if not piece:
                        continue
                    verdict = _emit(piece)
                    if verdict is None:
                        continue
                    out["yielded"] = True
                    buf.put_nowait(verdict)
                if out["state"] == "clean":
                    tail = gate.flush()
                    if tail:
                        verdict = _emit(tail)
                        if verdict is not None:
                            out["yielded"] = True
                            buf.put_nowait(verdict)
            except _GateFailError:
                pass  # state 已置 gate；零播报，调用方回退
            except asyncio.CancelledError:
                # cancel 只终结泵本身（state 照实分类）；run 等 Event，永不接住
                # 泵的 cancel——前兵版让 cancel 传播进 run=整条 FIFO 链死亡。
                out["state"] = "error_mid" if out["yielded"] else "error_pre"
                raise
            except Exception as exc:  # noqa: BLE001 - 流错误按 yield 前后分类
                out["state"] = "error_mid" if out["yielded"] else "error_pre"
                print(f"[interp-lite] MT_STREAM err state={out['state']} {exc!r}", flush=True)
            finally:
                buf.put_nowait(None)  # 流尽哨兵（gen 见之即收）
                pump_done.set()  # run 推进门（正常/异常/cancel 三路都到这）
                if stream is not None:
                    aclose = getattr(stream, "aclose", None)
                    if aclose is not None:
                        with contextlib.suppress(Exception):  # 关流尽力而为（cancel 同纪律）
                            await aclose()

        pump_task = asyncio.create_task(_pump())
        self._active_pump = pump_task

        async def _gen():
            try:
                while True:
                    item = await buf.get()
                    if item is None:
                        break
                    yield item
            finally:
                # 播放端退场（排干完或被打断）：泵未收尾就地 cancel——cancel 落在
                # 泵的 except/finally，绝不传播进 run 循环（排干完=泵已 set，免扰）。
                if not pump_done.is_set():
                    pump_task.cancel()

        try:
            self._last_say = self.session.say(_gen())
        except Exception as exc:  # noqa: BLE001 - say 提交失败=error_pre 回退
            out["state"] = "error_pre"
            print(f"[interp-lite] MT_STREAM say submit failed {exc!r}", flush=True)
            self._cancel_active_pump()  # 旧版此处漏 cancel=泵带流悬挂
            return await self._retry_once_say(text, msgs, t0)
        arm_tail_flush(self, self._last_say)

        # 等**泵排干（MT 完成）**即推进 FIFO（W8 播放解耦）——等 **Event** 而非
        # pump_task：泵被 gen 收尾/超时 cancel 时 CancelledError 只落在泵内，run
        # 恒常醒。流挂死（无增量不出泵内 deadline）由本超时兜底 cancel。
        try:
            await asyncio.wait_for(
                pump_done.wait(), timeout=_SENT_TIMEOUT_S + _SENT_TIMEOUT_GRACE_S
            )
        except asyncio.TimeoutError:
            out["state"] = "error_mid" if out["yielded"] else "error_pre"
            print(f"[interp-lite] MT_STREAM deadline state={out['state']}", flush=True)
            self._cancel_active_pump()
        self._active_pump = None

        if out["state"] in ("clean", "error_mid"):
            translated = "".join(raw).strip()
            self._cache_log()
            if not translated:
                self._drop()
                print(f"[interp-lite] mt empty for {len(text)} chars, skipped", flush=True)
                return
            if not out["yielded"]:
                # clean 但零 yield（极端短流兜底）：整句出声，一次配对。
                self._last_say = self.session.say(self._final_text(translated))
                arm_tail_flush(self, self._last_say)  # 接棒观察者（gen 观察者被 newest 护栏让位）
            self.pairs.append((text, translated))
            self._done(int((time.perf_counter() - t0) * 1000))
            return
        if out["state"] == "gate":
            print(f"[interp-lite] MT_STREAM fallback state=gate round={self.round}", flush=True)
            return await self._retry_once_say(text, msgs, t0)
        # error_pre：零播报 → 整句回退（error_mid 不会到这里）。
        print(f"[interp-lite] MT_STREAM fallback state={out['state']} round={self.round}", flush=True)
        await self._retry_once_say(text, msgs, t0)

    async def _translate_say_turn(self, text: str, t0: float) -> None:
        """话轮聚合档（BOK_INTERP_TURN_COALESCE=1）：与 `_translate_say` 同状态
        机（语言门/TagGate/超时分类/回退路径逐款同判），唯一分叉=产物不进本句
        私有 buf+逐句 say，而是直进**本话轮开着的流**（`_turn_put`）——一条
        bidi 流吃掉整个话轮，逐句握手税归零（call-743064ad 断断续续根修）。
        流尽哨兵不存在（收口由 run 循环的收口钟负责）。"""
        msgs = build_messages(self.instructions, list(self.pairs), text)
        out = {"state": "clean", "yielded": False}
        gate = TagGate()
        raw: list[str] = []
        guard_on = _mt_lang_guard_enabled()
        lang = self.target_lang
        head = ""
        self._turn.open()  # 先开流再起泵：首 delta 到时流已在等
        pump_done = asyncio.Event()
        first = {"done": False}

        def _emit(piece: str) -> str | None:
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

        async def _pump():
            stream = None
            try:
                stream = self.mt.stream(msgs)
                deadline = time.monotonic() + _SENT_TIMEOUT_S
                async for delta in stream:
                    if time.monotonic() > deadline:
                        out["state"] = "error_mid" if out["yielded"] else "error_pre"
                        print(f"[interp-lite] MT_STREAM deadline state={out['state']}", flush=True)
                        break
                    if not delta:
                        continue
                    raw.append(delta)
                    piece = gate.feed(delta)
                    if not piece:
                        continue
                    verdict = _emit(piece)
                    if verdict is None:
                        continue
                    out["yielded"] = True
                    self._turn.put(verdict)
                if out["state"] == "clean":
                    tail = gate.flush()
                    if tail:
                        verdict = _emit(tail)
                        if verdict is not None:
                            out["yielded"] = True
                            self._turn.put(verdict)
            except _GateFailError:
                pass  # state 已置 gate；已进流的片段照播（同旧档语义），余下走回退
            except asyncio.CancelledError:
                out["state"] = "error_mid" if out["yielded"] else "error_pre"
                raise
            except Exception as exc:  # noqa: BLE001
                out["state"] = "error_mid" if out["yielded"] else "error_pre"
                print(f"[interp-lite] MT_STREAM err state={out['state']} {exc!r}", flush=True)
            finally:
                pump_done.set()
                if stream is not None:
                    aclose = getattr(stream, "aclose", None)
                    if aclose is not None:
                        with contextlib.suppress(Exception):
                            await aclose()

        pump_task = asyncio.create_task(_pump())
        self._active_pump = pump_task

        try:
            await asyncio.wait_for(
                pump_done.wait(), timeout=_SENT_TIMEOUT_S + _SENT_TIMEOUT_GRACE_S
            )
        except asyncio.TimeoutError:
            out["state"] = "error_mid" if out["yielded"] else "error_pre"
            print(f"[interp-lite] MT_STREAM deadline state={out['state']}", flush=True)
            self._cancel_active_pump()
        self._active_pump = None

        if out["state"] in ("clean", "error_mid"):
            translated = "".join(raw).strip()
            self._cache_log()
            if not translated:
                self._drop()
                print(f"[interp-lite] mt empty for {len(text)} chars, skipped", flush=True)
                return
            if not out["yielded"]:
                # clean 但零 yield（极端短流兜底）：整句进本话轮流。
                self._say_text(self._final_text(translated))
            self.pairs.append((text, translated))
            self._done(int((time.perf_counter() - t0) * 1000))
            return
        if out["state"] == "gate":
            print(f"[interp-lite] MT_STREAM fallback state=gate round={self.round}", flush=True)
            return await self._retry_once_say(text, msgs, t0)
        print(f"[interp-lite] MT_STREAM fallback state={out['state']} round={self.round}", flush=True)
        await self._retry_once_say(text, msgs, t0)

    async def _retry_once_say(self, text: str, msgs: list[dict], t0: float) -> None:
        """整句回退（新开流排干→语言复查→整句出声；至多一次，无循环重试）。
        出声走 `_say_text` 单点路由（聚合开=进本话轮流，聚合关=逐句 say）。"""
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
        self._say_text(self._final_text(translated))
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
        """超时/异常兜底：目标语请示句出声+配对记账（绝不回放源文）。
        出声走 `_say_text` 单点路由（聚合档进本话轮流）。"""
        print(f"[interp-lite] MT_FALLBACK {why} lang={self.target_lang}", flush=True)
        try:
            self._say_text(_mt_fail_line(self.target_lang))
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
