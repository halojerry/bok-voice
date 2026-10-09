"""话轮流生命周期（2026-10-09 call-743064ad 断断续续根修，W8-C）。

根因：逐句开/关 bidi 流各付 2-3.5s「flush 握手+锁交接」税——碎片串起来=
顿挫+积压滚雪球（实弹 perceived 冲 19s）。话轮聚合=一条 say 的 gen 撑整个
话轮（jinxi beginText→sendChunk×N→endText 同形）：每句译文照旧逐 delta 进流
（服务端按标点攒句连续出声），FIFO 排干再撑 ``BOK_INTERP_TURN_HOLD_S`` 才
收口（收口=end_input→finalize flush，握手税每话轮只付一次）。

本模块自持：开流/进料/收口钟/收口、轮尾催尾（task_flush 催产）与 playout
观察者 belt。配套的 bidi 侧改动（livekit_plugins）：中途催尾 ack 经
``stream_ended`` 门禁无害化——开着的流收到催尾 ack 不收摊。
"""

from __future__ import annotations

import asyncio
import os

_TURN_COALESCE_ENV = "BOK_INTERP_TURN_COALESCE"


def _turn_coalesce_enabled() -> bool:
    """话轮聚合开流总闸（缺省 0=保守；B 线 worker env 缺省 1 见 bokctl
    ``_interp_env``——0 回逐句开流旧径）。"""
    return os.environ.get(_TURN_COALESCE_ENV, "0") == "1"


def _turn_hold_s() -> float:
    """话轮收口保持窗：FIFO 排干后再撑本窗（跨过句间自然停顿/限速间隔）才
    收口=说话确实停了；期间新单元到达即撤钟续流。缺省 1.8s>提交限速 1.5s
    （连续语流的合法最大 commit 间隙），坏值回缺省。"""
    try:
        v = float(os.environ.get("BOK_INTERP_TURN_HOLD_S", "1.8"))
    except Exception:  # noqa: BLE001 - 配错回默认
        v = 1.8
    return v if v > 0 else 1.8


class TurnStream:
    """一条话轮开流的进料队列与收口钟（owner=InterpPipeline，鸭子面互持）。

    状态：``q=None`` 无开流；收口（哨兵）/打断（gen finally）都归 None。
    哨兵只由 ``close_now`` 投（先摘引用再投——新单元在收口期间到达立即开
    新流，绝不把文本喂进垂死的流）。
    """

    def __init__(self, p) -> None:
        self._p = p  # owner：session.say/_arm_tail_flush/_cancel_active_pump
        self.q: asyncio.Queue | None = None
        self._timer: asyncio.TimerHandle | None = None

    @property
    def alive(self) -> bool:
        return self.q is not None

    def open(self) -> None:
        """确保本话轮的开流 say 在场（幂等）。"""
        if self.q is not None:
            return
        q: asyncio.Queue = asyncio.Queue()
        self.q = q
        p = self._p

        async def _gen():
            try:
                while True:
                    item = await q.get()
                    if item is None:
                        return
                    yield item
            finally:
                # 退场（收口哨兵/打断/异常）:摘引用让下一单元开新话轮流;
                # 打断时在途泵就地收线（其产物只会进死队列=丢，别白烧 MT）。
                # R5（V3 审计）：泵收线带 q 身份门——若 finally 晚于新话轮流开
                # （收线/teardown 竞态），self.q 已是新流的队列，绝不可误杀
                # 新话轮的在途 MT 泵（误杀=该句 error_pre 白烧一次整句重译）。
                if self.q is q:
                    self.q = None
                    p._cancel_active_pump()

        p._last_say = p.session.say(_gen())
        arm_tail_flush(p, p._last_say)

    def put(self, text: str) -> None:
        """译文片段进本话轮流。"""
        if self.q is None:  # 竞态兜底：流刚收口/被打断——重开再进，绝不丢段。
            self.open()
        assert self.q is not None
        self.q.put_nowait(text)

    def schedule_close(self) -> None:
        """FIFO 排干后启动收口钟（新单元到达由 ``cancel_close`` 撤；钟到点
        再核 FIFO/MT 闲，误排无害）。"""
        if self.q is None:
            return
        self.cancel_close()
        p = self._p

        def _close() -> None:
            self._timer = None
            if self.q is not None and p.q.empty() and not p.mt_busy["flag"]:
                self.close_now()

        try:
            self._timer = asyncio.get_running_loop().call_later(_turn_hold_s(), _close)
        except RuntimeError:  # noqa: BLE001 - 无运行循环（直调姿势）即不排程
            pass

    def cancel_close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def close_now(self) -> None:
        """收口：哨兵入队 → gen 收 → 框架 end_input → bidi finalize flush
        （握手税每话轮只付这一次）。先摘引用：收口后新单元立即开新话轮流。"""
        q = self.q
        self.q = None
        self.cancel_close()
        if q is not None:
            q.put_nowait(None)


# ---- 轮尾催尾（task_flush 催产）与 playout 观察者 belt -------------------------------


async def maybe_tail_flush(p) -> None:
    """催无标点短尾（P1 实测省 ~2.1s：免等 MiniMax 官方无标点兜底窗 ~2.4s）。

    四道闸缺一不发：通道在场（env/TTS 装配）、FIFO 空（有后句=下一句的
    continue 自然催）、真 MT 不在途、当前 say 未被取消（取消流 recv 已死）。
    发送本身由通道守卫（连接不在场/死亡/异常一律静默 no-op）。
    （2026-10-09 撤「播放观察者在途让位」闸：中途催尾 ack 已由 bidi 侧
    ``stream_ended`` 门禁无害化——开着的流收到催尾 ack 不收摊，话轮聚合档
    的逐句催产全靠本位直发。）"""
    flush = p._tail_flush
    if flush is None or not p.q.empty():
        return
    if p.mt_busy["flag"] or p.lane_dead["reason"]:
        return
    if getattr(p._last_say, "interrupted", False):
        return
    try:
        await flush()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 纯增益，绝不成为新故障源
        pass


def arm_tail_flush(p, handle) -> None:
    """播放落地观察者 belt（真 SpeechHandle 装配）：``wait_for_playout`` 面在场
    才武装。playout 落地再催一枚（run 位已在泵排干处催过——belt 只兜「落地
    后仍有无标点尾巴」的边角）；newest 护栏（被更新单元接棒即让位）+被打断
    不催。fake/无该面=不武装（测试与直嵌姿势零漂移）。"""
    wait = getattr(handle, "wait_for_playout", None)
    if p._tail_flush is None or not callable(wait):
        return
    p._playout_watchers["n"] += 1

    async def _watch() -> None:
        try:
            try:
                await wait()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 - 观察者纯增益，绝不外抛
                return
        finally:
            p._playout_watchers["n"] -= 1  # 先落计数再判闸（自身不挡自己）
        if handle is not p._last_say or getattr(handle, "interrupted", False):
            return  # 已被更新单元接棒/被掐：让位（四道闸语义与 run 旧位同判）
        await maybe_tail_flush(p)

    task = asyncio.create_task(_watch())
    p._flush_tasks.add(task)
    task.add_done_callback(p._flush_tasks.discard)
