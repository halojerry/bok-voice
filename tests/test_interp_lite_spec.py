"""interp_lite 投机翻译（W8-A1）：单源 parity 钉 + 右上下文门 + HIT/MISS 流 + 轮尾催尾。

parity 立法（W8-A1 任务1）：机器件单源 import 旧线（``interpret._SpecMtController``
家族）——本文件把「稳定判据/确认相似度 0.85/忙闸」三语义钉死在**同一对象**上
（import identity + 行为同构双面），lite 侧任何复制第二份的实现都会被第一组测试打红。
"""

from __future__ import annotations

import asyncio

import agent_runtime.interpret as interpret_old
from agent_runtime.interp_lite import spec_mt
from agent_runtime.interp_lite.pipeline import InterpPipeline

# ---- 单源 parity：机器件与旧线同一对象（复制即红） ------------------------------


def test_spec_machine_single_source_identity():
    assert issubclass(spec_mt.LiteSpecController, interpret_old._SpecMtController)
    assert spec_mt._SpecMtDetector is interpret_old._SpecMtDetector
    assert spec_mt._SpecMtHold is interpret_old._SpecMtHold
    assert spec_mt._spec_busy_depth is interpret_old._spec_busy_depth
    assert spec_mt._spec_clause_prefix is interpret_old._spec_clause_prefix
    assert spec_mt._spec_mt_enabled is interpret_old._spec_mt_enabled
    # 确认相似度=0.85（与旧线同值，lite 端不得私调）
    assert spec_mt._SPEC_CONFIRM_SIM == interpret_old._SPEC_CONFIRM_SIM == 0.85
    # kill-switch 同键缺省 1
    assert interpret_old._SPEC_KILL_SWITCH_ENV == "BOK_INTERP_SPEC_MT"


def test_detector_stable_clause_semantics_isomorphic():
    """稳定判据（≥6 内容字候选 + 跨 ≥2 interim 不变）lite 与旧线逐拍同构。"""
    seq = ["我想请问一下", "我想请问一下，", "我想请问一下，", "我想请问一下，你们"]
    d_old, d_lite = interpret_old._SpecMtDetector(), spec_mt._SpecMtDetector()
    t = 100.0
    got: list = []
    for txt in seq:
        t += 0.1
        r_old = d_old.feed(txt, now=t)
        r_lite = d_lite.feed(txt, now=t)
        assert r_old == r_lite
        got.append(r_lite)
    assert got == [None, None, "我想请问一下，", None]  # 第 2 次目击开火；再开火须比上次长 ≥6 字


def test_detector_digit_run_veto_isomorphic():
    """数字串 span（≥4 位 run）永不投机——号码高危（镜像 flow 数字 run 族）。"""
    d = spec_mt._SpecMtDetector()
    for i in range(3):
        assert d.feed("单号是七八九零，", now=1.0 + i * 0.1) is None


# ---- 右上下文门（lite 专属，BOK_INTERP_SPEC_RIGHT_CTX 缺省 2） --------------------


def test_right_ctx_chars_pure(monkeypatch):
    monkeypatch.delenv("BOK_INTERP_SPEC_RIGHT_CTX", raising=False)
    assert spec_mt.spec_right_ctx_chars() == 2  # 缺省 2
    monkeypatch.setenv("BOK_INTERP_SPEC_RIGHT_CTX", "3")
    assert spec_mt.spec_right_ctx_chars() == 3
    monkeypatch.setenv("BOK_INTERP_SPEC_RIGHT_CTX", "abc")
    assert spec_mt.spec_right_ctx_chars() == 2  # 坏值回缺省
    monkeypatch.setenv("BOK_INTERP_SPEC_RIGHT_CTX", "-5")
    assert spec_mt.spec_right_ctx_chars() == 0  # 负数钳 0
    monkeypatch.setenv("BOK_INTERP_SPEC_RIGHT_CTX", "0")
    assert spec_mt.spec_right_ctx_chars() == 0  # 0=关


def _ctl(busy: bool = False):
    said: list[tuple] = []
    enq: list[str] = []

    async def _run_spec(span: str) -> tuple[str, bytes]:
        return f"TR:{span}", b"\x01" * 16

    ctl = spec_mt.LiteSpecController(
        enabled=True,
        detector=spec_mt._SpecMtDetector(),
        hold=spec_mt._SpecMtHold(),
        run_spec=_run_spec,
        busy_gate=lambda: busy,
        say_cached=lambda s, t, p: said.append((s, t, p)),
        enqueue=enq.append,
        log=lambda *_a, **_k: None,
    )
    return ctl, said, enq


def test_right_ctx_gate_blocks_near_punct(monkeypatch):
    """候选标点后再收 <N 字：整条 interim 不喂（不开火、不烧目击/预算）。"""
    monkeypatch.delenv("BOK_INTERP_SPEC_RIGHT_CTX", raising=False)
    ctl, said, _enq = _ctl()

    async def scenario():
        for _ in range(4):  # 候选「我想请问一下，」后仅 1 字（<缺省 2）
            ctl.on_interim("我想请问一下，你")
            await asyncio.sleep(0.01)
        assert ctl.hold.src == "" and said == []
        # 补到 ≥2 字后：同候选第 2 次目击 → 开火
        ctl.on_interim("我想请问一下，你们")
        ctl.on_interim("我想请问一下，你们")
        for _ in range(50):
            if ctl.hold.src:
                break
            await asyncio.sleep(0.01)
        assert ctl.hold.src == "我想请问一下，"

    asyncio.run(scenario())


def test_right_ctx_gate_zero_disables(monkeypatch):
    """0=关：标点即尾也照旧线喂法开火（2 目击）。"""
    monkeypatch.setenv("BOK_INTERP_SPEC_RIGHT_CTX", "0")
    ctl, _said, _enq = _ctl()

    async def scenario():
        ctl.on_interim("我想请问一下，")
        ctl.on_interim("我想请问一下，")
        for _ in range(50):
            if ctl.hold.src:
                break
            await asyncio.sleep(0.01)
        assert ctl.hold.src == "我想请问一下，"

    asyncio.run(scenario())


# ---- 确认相似度（0.85 门）：HIT 直播 / MISS 回正常路径 ---------------------------


def test_confirm_hit_zero_synth_replay_and_rest_enqueue():
    ctl, said, enq = _ctl()
    ctl.hold.src = "我想请问一下"
    ctl.hold.text = "TR:HELD"
    ctl.hold.pcm = b"\x02" * 8
    played = ctl.on_final("我想请问一下你们那边")  # 归一前缀全等 sim=1.0 ≥ 0.85
    assert played is True
    assert said and said[0][1] == "TR:HELD" and said[0][2] == b"\x02" * 8  # held 零合成直播
    assert enq == ["你们那边"]  # 余段入正常管线


def test_confirm_miss_below_sim_gate():
    ctl, said, enq = _ctl()
    ctl.hold.src = "完全不同的投机"
    ctl.hold.text = "TR:X"
    ctl.hold.pcm = b"\x03"
    assert ctl.on_final("另一句最终转写文本") is False  # sim < 0.85 = MISS
    assert said == [] and enq == []  # 调用方走原路径（enqueue 由 pipeline 兜）


def test_confirm_final_shorter_than_span_misses():
    """final 归一不足 span 长度 → 余段恒空（len 门兜底）→ MISS。"""
    ctl, said, _enq = _ctl()
    ctl.hold.src = "我想请问一下"
    ctl.hold.text = "TR:HELD"
    ctl.hold.pcm = b"\x02" * 8
    assert ctl.on_final("我想请") is False
    assert said == []


# ---- busy 闸：FIFO 深度 / 真 MT 在途 / 死道 ------------------------------------


def _pipeline_spec(monkeypatch, mt, target="zh", **kw):
    monkeypatch.delenv("BOK_INTERP_SPEC_MT", raising=False)
    monkeypatch.delenv("BOK_INTERP_SPEC_RIGHT_CTX", raising=False)
    return InterpPipeline(
        FakeSession(), mt, "SYS", target_lang=target, voice_tags=True,
        lag=FakeLag(), first_ms={"ms": 0}, tts_provider=FakeTTS(), **kw,
    )


def test_busy_gate_fifo_depth_semantics(monkeypatch):
    """FIFO 深度 ≥2 才算忙（缺省档）；深度 1 闸开；1=旧「非空即封」档（旧线同键）。"""
    p = _pipeline_spec(monkeypatch, FakeMT([]))
    ctl = p._spec
    assert ctl is not None
    p.enqueue_raw("一句")  # FIFO 深度 1

    async def scenario():
        ctl.on_interim("我想请问一下，你们")
        ctl.on_interim("我想请问一下，你们")
        for _ in range(50):
            if ctl.hold.src:
                break
            await asyncio.sleep(0.01)
        assert ctl.hold.src == "我想请问一下，"  # 深度 1 不封

    asyncio.run(scenario())

    p2 = _pipeline_spec(monkeypatch, FakeMT([]))
    ctl2 = p2._spec
    p2.enqueue_raw("一")
    p2.enqueue_raw("二")  # 深度 2 = 忙
    ctl2.on_interim("我想请问一下，你们")
    ctl2.on_interim("我想请问一下，你们")
    assert ctl2.hold.src == ""  # 闸封零开火


def test_busy_gate_mt_inflight_and_lane_dead(monkeypatch):
    p = _pipeline_spec(monkeypatch, FakeMT([]))
    ctl = p._spec
    p.mt_busy["flag"] = True
    ctl.on_interim("我想请问一下，你们")
    ctl.on_interim("我想请问一下，你们")
    assert ctl.hold.src == ""  # 真 MT 在途=不开火
    p.mt_busy["flag"] = False
    p.lane_dead["reason"] = "deepseek mt http 402"
    ctl.on_interim("我想请问一下，你们")
    ctl.on_interim("我想请问一下，你们")
    assert ctl.hold.src == ""  # 死道=不开火（投机只会白烧快速失败）


# ---- pipeline 集成：final 确认路由 + kill-switch -------------------------------


def test_pipeline_final_hit_skips_queue(monkeypatch):
    """final 先过投机确认：HIT=held PCM 直播+余段入队，跳过整句正常路径。"""
    mt = FakeMT([iter(["好的，马上帮您处理，"])])
    p = _pipeline_spec(monkeypatch, mt)
    ctl = p._spec

    async def scenario():
        for _ in range(2):
            # 候选 10 字、右上下文「好的」=2 → 2 目击开火
            ctl.on_interim("好的，马上帮您处理，好的")
        for _ in range(100):
            if ctl.hold.pcm:
                break
            await asyncio.sleep(0.01)
        assert ctl.hold.text == "好的，马上帮您处理，"  # 投机 MT 已落地（held 文本=MT 译文）
        assert ctl.hold.pcm  # TTS 排干 PCM 在槽
        p.enqueue("好的，马上帮您处理，好的")  # final：span+余段
        # W8 乱序修复终版：HIT 走 enqueue_precomputed_text 入 FIFO（标记二元组），
        # 余段照旧入队——播放序由同类型 say(text) 保证
        assert p.q.qsize() == 2  # HIT 预计算 + 余段
        qitems = list(p.q._queue)
        assert isinstance(qitems[0], tuple) and qitems[0][0] == "__precomputed__"  # HIT 标记
        assert qitems[1] == "好的"  # 余段
        assert p.lag.dones == [0]  # 投机轮 mt_ms=0（旧线同口径）

    asyncio.run(scenario())


def test_pipeline_final_miss_goes_normal_path(monkeypatch):
    """MISS：final 照旧入队（确认门不吞句），无 audio 直播。"""
    p = _pipeline_spec(monkeypatch, FakeMT([]))
    assert p._spec is not None

    async def scenario():
        p.enqueue("完全对不上的最终句")
        assert list(p.q._queue) == ["完全对不上的最终句"]
        assert p.session.audio_said == []

    asyncio.run(scenario())


def test_kill_switch_off_zero_assembly(monkeypatch):
    """BOK_INTERP_SPEC_MT=0：控制器不装配；final 直接入队（不过确认门）。"""
    monkeypatch.setenv("BOK_INTERP_SPEC_MT", "0")
    p = InterpPipeline(
        FakeSession(), FakeMT([]), "SYS", target_lang="zh", voice_tags=True,
        lag=FakeLag(), first_ms={"ms": 0}, tts_provider=FakeTTS(),
    )
    assert p._spec is None
    p.enqueue("hello")
    assert p.q.qsize() == 1
    p.shutdown()  # no-op 不炸


def test_text_only_direction_zero_assembly(monkeypatch):
    """text-only 方向（无 TTS）：spec/催尾全 None（无可预热音频，旧线同判）。"""
    monkeypatch.setenv("BOK_INTERP_SPEC_MT", "1")
    p = InterpPipeline(
        FakeSession(), FakeMT([]), "SYS", target_lang="zh", voice_tags=False,
        lag=FakeLag(), first_ms={"ms": 0}, tts_provider=None,
    )
    assert p._spec is None and p._tail_flush is None


def test_enqueue_raw_never_reenters_confirm(monkeypatch):
    """enqueue_raw 不过确认门（spec 余段/defer 兜底专用——防 on_final 递归）。"""
    p = _pipeline_spec(monkeypatch, FakeMT([]))
    assert p._spec is not None
    orig = p._spec.on_final

    def _boom(_t):
        raise AssertionError("enqueue_raw 不准进确认门")

    p._spec.on_final = _boom
    p.enqueue_raw("余段")
    assert p.q.qsize() == 1
    p._spec.on_final = orig


def test_shutdown_cancels_inflight_spec(monkeypatch):
    p = _pipeline_spec(monkeypatch, FakeMT([]))

    async def scenario():
        hang = asyncio.create_task(asyncio.sleep(30))
        p._spec.hold.task = hang
        await asyncio.sleep(0.01)
        p.shutdown()
        await asyncio.sleep(0.01)
        assert hang.cancelled() or hang.done()

    asyncio.run(scenario())


# ---- 轮尾 task_flush（BOK_INTERP_TAIL_FLUSH 缺省 1） ----------------------------


def test_tail_flush_channel_guard_semantics():
    from agent_runtime.interp_lite.providers.tts_minimax import tail_flush_channel

    sent: list[str] = []

    class _WS:
        async def send(self, msg):
            sent.append(msg)

    class _Sess:
        def __init__(self, ws, active_epoch=0):
            self._ws = ws
            self.active_epoch = active_epoch  # 认领纪元(call-4322e14d:催尾须打 owner 戳)
            self.flush_epoch = 0

        def _alive(self):
            return self._ws is not None

    class _TTS:
        def __init__(self, ws, active_epoch=0):
            self._s = _Sess(ws, active_epoch)

        def _bidi_session(self):
            return self._s

    stamp = _TTS(_WS(), active_epoch=3)
    asyncio.run(tail_flush_channel(stamp)())
    assert sent == ['{"event": "task_flush"}']  # 有认领任务=催
    assert stamp._s.flush_epoch == 3  # 发送前把 ack 归属钉在当前认领流上

    # 无认领任务(active_epoch==0)=无事可催:催空任务只会污染 flush 握手 → no-op。
    asyncio.run(tail_flush_channel(_TTS(_WS(), active_epoch=0))())
    assert len(sent) == 1

    asyncio.run(tail_flush_channel(_TTS(None, active_epoch=3))())  # 连接不在场=no-op
    assert len(sent) == 1

    class _Other:  # 非 MiniMax 装配（无 _bidi_session 面）=永久 no-op
        pass

    asyncio.run(tail_flush_channel(_Other())())
    assert len(sent) == 1

    class _BoomWS:
        async def send(self, msg):
            raise RuntimeError("dead ws")

    asyncio.run(tail_flush_channel(_TTS(_BoomWS(), active_epoch=3))())  # 发送异常静默吞
    assert len(sent) == 1


def test_tail_flush_fires_when_fifo_empty_after_drain(monkeypatch):
    """FIFO 空+当前 say 排干 → 催尾恰好一枚；有后句时段间不催。"""
    monkeypatch.delenv("BOK_INTERP_TAIL_FLUSH", raising=False)
    mt = FakeMT([iter(["你好。"]), iter(["世界。"])])
    p = _pipeline_spec(monkeypatch, mt)
    calls: list[int] = []

    async def _flush():
        calls.append(1)

    p._tail_flush = _flush

    async def scenario():
        p.enqueue("a")
        p.enqueue("b")
        task = asyncio.create_task(p.run())
        for _ in range(300):
            if len(calls) >= 1 and p.q.empty():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with _suppress_cancel():
            await task
        assert len(calls) == 1  # 第一句排干时 FIFO 还有 b=不催；b 排干后催一枚

    asyncio.run(scenario())


def test_tail_flush_gates_busy_and_interrupted(monkeypatch):
    p = _pipeline_spec(monkeypatch, FakeMT([]))
    calls: list[int] = []

    async def _flush():
        calls.append(1)

    p._tail_flush = _flush

    async def scenario():
        p.enqueue_raw("占位")  # FIFO 非空
        from agent_runtime.interp_lite.turn_stream import maybe_tail_flush as _mtf; await _mtf(p)
        assert calls == []
        p.q.get_nowait()
        p.mt_busy["flag"] = True  # 真 MT 在途
        from agent_runtime.interp_lite.turn_stream import maybe_tail_flush as _mtf; await _mtf(p)
        assert calls == []
        p.mt_busy["flag"] = False
        p._last_say = _InterruptedHandle()  # 当前 say 被取消
        from agent_runtime.interp_lite.turn_stream import maybe_tail_flush as _mtf; await _mtf(p)
        assert calls == []
        p._last_say = None
        from agent_runtime.interp_lite.turn_stream import maybe_tail_flush as _mtf; await _mtf(p)
        assert calls == [1]
        p.lane_dead["reason"] = "x"
        from agent_runtime.interp_lite.turn_stream import maybe_tail_flush as _mtf; await _mtf(p)
        assert calls == [1]

    asyncio.run(scenario())


def test_tail_flush_env_off(monkeypatch):
    monkeypatch.setenv("BOK_INTERP_TAIL_FLUSH", "0")
    p = _pipeline_spec(monkeypatch, FakeMT([]))
    assert p._tail_flush is None  # 总闸关=装配期 None（旧路径逐字节）


def test_tail_flush_non_minimax_tts_is_noop_channel(monkeypatch):
    """无 _bidi_session 面的 TTS：通道仍装配但恒 no-op（调用方免判型）。"""
    monkeypatch.delenv("BOK_INTERP_TAIL_FLUSH", raising=False)
    p = InterpPipeline(
        FakeSession(), FakeMT([]), "SYS", target_lang="zh", voice_tags=True,
        lag=FakeLag(), first_ms={"ms": 0}, tts_provider=object(),
    )
    assert p._tail_flush is not None

    async def scenario():
        await p._tail_flush()  # 不炸

    asyncio.run(scenario())


# ---- fakes（本文件专用；session 消费生成器 + audio= 直播断言） ---------------------


class _InterruptedHandle:
    interrupted = True


class _suppress_cancel:
    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        return et is not None and issubclass(et, asyncio.CancelledError)


class FakeSession:
    """真 session.say 会消费生成器——fake 同款拉起后台排干，产出收进 spoken[i]。"""

    def __init__(self):
        self.said: list = []
        self.audio_said: list = []
        self.spoken: list[list[str]] = []
        self._tasks: set = set()

    def say(self, x, audio=None):
        self.said.append(x)
        self.audio_said.append(audio)
        if hasattr(x, "__aiter__"):
            bucket: list[str] = []
            self.spoken.append(bucket)

            async def _drain():
                try:
                    async for piece in x:
                        bucket.append(piece)
                except Exception:  # noqa: BLE001 - 排干尽力而为
                    pass

            try:
                t = asyncio.get_running_loop().create_task(_drain())
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)
            except RuntimeError:
                pass
        else:
            self.spoken.append([str(x)])


class FakeLag:
    def __init__(self):
        self.notes, self.dones, self.drops = [], [], []

    def note_src(self, t):
        self.notes.append(t)

    def done_mt(self, ms):
        self.dones.append(ms)

    def drop_src(self):
        self.drops.append(1)


class FakeMT:
    """脚本化 MT：calls[i] = 增量迭代器或异常（消费序=调用序）。"""

    def __init__(self, calls):
        self._calls = list(calls)
        self.last_msgs: list | None = None

    async def stream(self, msgs):
        self.last_msgs = msgs
        item = self._calls.pop(0)
        if isinstance(item, Exception):
            raise item
        for d in item:
            yield d


class _FakeSynthEvent:
    def __init__(self, data: bytes):
        self.frame = type("_F", (), {"data": data})()


class _FakeSynthStream:
    def __init__(self, pcm: bytes):
        self._pcm = pcm
        self._done = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._done:
            raise StopAsyncIteration
        self._done = True
        return _FakeSynthEvent(self._pcm)


class FakeTTS:
    """投机合成 fake：synthesize 记录文本、吐一枚 PCM 事件（会话同源口径）。"""

    def __init__(self):
        self.sample_rate = 24000
        self.synth_calls: list[str] = []

    def synthesize(self, text):
        self.synth_calls.append(text)
        return _FakeSynthStream(b"\x07" * 32)


# ---- W8-B 播放解耦（pump 排干即推进：播放时间=准备时间） -------------------------
#
# 病灶（HEAD 基线）：run 等 gen 排干=等框架播完本句才推进 FIFO——上一段播报期间
# 下一单元 MT 结构性不起跑（实弹尾巴单元 first_ms 6-7.8s=上一段播报时长）。
# 前兵 stash 版改法三处致死（本组测试逐条钉死回归）：
# ① run `wait_for(pump_task)`：gen 收尾 cancel 泵时 CancelledError 抬进 run=
#    整条 FIFO 死（进行性断流根因）——cancel 必须终结在泵内（test_..._barge_in）。
# ② say 前串行闸（prev.wait_for_playout 90s）：提交时点回退=解耦白做。
# ③ 催尾挂 SpeechHandle 回调+闸读 `self._last_say`：竞态下提前 task_flush 毒化
#    bidi flushed 握手（sentences=0 族）——催尾安全时点=playout 落地，触发器=
#    本地句柄观察者+newest 护栏（test_..._watcher_*）。


class _LazySession:
    """gen 只登记不消费：say 未被框架拉取=「正在播放/排队」姿势（解耦靶形）。"""

    def __init__(self):
        self.gens: list = []
        self.texts: list[str] = []

    def say(self, x, audio=None):
        if hasattr(x, "__aiter__"):
            self.gens.append(x)
        else:
            self.texts.append(str(x))
        return x


class _FakeHandle:
    """真 SpeechHandle 最小面：wait_for_playout（事件放行）+ interrupted。"""

    def __init__(self):
        self._done = asyncio.Event()
        self.interrupted = False

    async def wait_for_playout(self):
        await self._done.wait()

    def release(self):
        self._done.set()


class _HandleSession:
    """say 返回 _FakeHandle；gen 不消费（排队/播放中由测试控制何时落地）。"""

    def __init__(self):
        self.handles: list[_FakeHandle] = []
        self.gens: list = []
        self.texts: list[str] = []

    def say(self, x, audio=None):
        h = _FakeHandle()
        self.handles.append(h)
        if hasattr(x, "__aiter__"):
            self.gens.append(x)
        else:
            self.texts.append(str(x))
        return h


class _SlowMT:
    """脚本化 MT：calls[i] = {"pieces": [...], "gap": s, "hold": s}。

    pieces 逐片吐（片间可 gap）；hold=吐完后挂死 N 秒（流悬挂靶形）。
    closed[i] 记录每条流终结（自然完/aclose 都算——挂死流只能经 cancel→
    finally→aclose 收场）。"""

    def __init__(self, calls):
        self._calls = list(calls)
        self.started: list[int] = []
        self.closed: list[int] = []
        self.last_metrics: dict = {}

    async def stream(self, msgs):
        self.started.append(len(self.started))
        idx = len(self.started) - 1
        item = self._calls.pop(0)
        try:
            for d in item["pieces"]:
                yield d
                if item.get("gap"):
                    await asyncio.sleep(item["gap"])
            if item.get("hold"):
                await asyncio.sleep(item["hold"])
        finally:
            self.closed.append(idx)


def test_w8b_run_advances_while_say_unpulled(monkeypatch):
    """解耦核心：上一句 gen 永不被拉取（播放中/排队姿势）时，后续单元 MT 照常
    起跑、排干、配对入账——run 不等播放（旧码 done-wait 此处结构性卡死）。"""
    monkeypatch.delenv("BOK_INTERP_SPEC_MT", raising=False)
    trans = ["你好世界今天。", "第二句译文好。", "第三句也完成。"]
    mt = FakeMT([iter([t]) for t in trans])
    lag = FakeLag()
    p = InterpPipeline(
        _LazySession(), mt, "SYS", target_lang="zh", voice_tags=True,
        lag=lag, first_ms={"ms": 0},
    )

    async def scenario():
        for u in ("第一句", "第二句", "第三句"):
            p.enqueue_raw(u)
        task = asyncio.create_task(p.run())
        for _ in range(300):
            if p.q.empty() and len(lag.dones) >= 3 and len(p.session.gens) >= 3:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with _suppress_cancel():
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=5.0))
    assert len(p.session.gens) == 3  # 三个流式 say 全部提交（播放次序由框架保序）
    assert p.session.texts == []  # ≥4 字全走流式路（零整句直念）
    assert [t for _s, t in p.pairs] == trans
    assert len(lag.dones) == 3 and lag.notes == ["第一句", "第二句", "第三句"]
    assert p.mt_busy["flag"] is False  # run 已空转（busy 旗复位）


def test_w8b_fifo_submission_order_uneven_mt(monkeypatch):
    """首句 MT 慢、后续快：run 串行推进，gen 提交序与配对序严格=FIFO 序
    （保序铁律—— uneven MT 不换序、不重排）。"""
    monkeypatch.delenv("BOK_INTERP_SPEC_MT", raising=False)

    class _StaggerMT:
        def __init__(self):
            self.calls = 0

        async def stream(self, msgs):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(0.15)  # 首句 MT 显著慢于后续
                yield "第一句译文够长。"
            else:
                yield f"第{self.calls}句译文够长。"

    lag = FakeLag()
    p = InterpPipeline(
        _LazySession(), _StaggerMT(), "SYS", target_lang="zh", voice_tags=True,
        lag=lag, first_ms={"ms": 0},
    )

    async def scenario():
        for u in ("a", "b", "c"):
            p.enqueue_raw(u)
        task = asyncio.create_task(p.run())
        for _ in range(300):
            if len(lag.dones) >= 3:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with _suppress_cancel():
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=5.0))
    assert [t for _s, t in p.pairs] == [
        "第一句译文够长。", "第2句译文够长。", "第3句译文够长。",
    ]
    assert len(p.session.gens) == 3  # 提交序=配对序=FIFO 序


def test_w8b_pairing_exactly_once_mixed_paths(monkeypatch):
    """正常/空译/异常重试/语言门违约四形状混跑：每句恰好一次 done|drop（RC-8
    配对纪律解耦后不破），无重复配对、无吞账。"""
    monkeypatch.delenv("BOK_INTERP_SPEC_MT", raising=False)
    mt = FakeMT([
        iter(["你好世界今天。"]),          # 正常流式
        iter([]),                          # 空译 → drop
        RuntimeError("mt boom"),           # 泵捕获 → error_pre → 整句重试
        iter(["好的马上帮您处理。"]),      # 异常句的重试流
        iter(["hello world english out"]),  # 语言门违约（默认开）→ gate → 重试
        iter(["这是回退中文译文。"]),      # gate 句的重试流
    ])
    lag = FakeLag()
    p = InterpPipeline(
        _LazySession(), mt, "SYS", target_lang="zh", voice_tags=True,
        lag=lag, first_ms={"ms": 0},
    )

    async def scenario():
        for u in ("正常句", "空译句", "异常句", "外语句"):
            p.enqueue_raw(u)
        task = asyncio.create_task(p.run())
        for _ in range(500):
            if p.q.empty() and len(lag.dones) + len(lag.drops) >= 4:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with _suppress_cancel():
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=8.0))
    assert len(lag.notes) == 4  # 每句恰好一次 note_src
    assert len(lag.dones) == 3 and len(lag.drops) == 1  # 恰好一次 done|drop
    assert [t for _s, t in p.pairs] == [
        "你好世界今天。", "好的马上帮您处理。", "这是回退中文译文。",
    ]


def test_w8b_barge_in_gen_cancel_does_not_kill_run(monkeypatch):
    """断流复现回归（前兵 stash 版死法）：播放端 gen 被 cancel（框架 barge-in
    姿势）连带 cancel 在途泵——cancel 必须终结在泵内（哨兵/事件由 finally 兜），
    run 循环继续消化后续单元。stash 版 run `await pump_task`，泵 cancel 的
    CancelledError 抬进 run=整条翻译链死亡（进行性断流根因）。"""
    monkeypatch.delenv("BOK_INTERP_SPEC_MT", raising=False)
    mt = _SlowMT([
        {"pieces": ["你好世界", "今天很好"], "gap": 0.05},  # 慢流：拉一片后被打断
        {"pieces": ["第二句译文完整。"]},
        {"pieces": ["第三句译文完整。"]},
    ])
    lag = FakeLag()

    class _BargeInSession:
        """首句 gen 吐第一片后 cancel 拉取任务（框架打断姿势），其余只登记。"""

        def __init__(self):
            self.gens: list = []
            self.pulled: list[list[str]] = []
            self.first_task = None

        def say(self, x, audio=None):
            self.gens.append(x)
            if len(self.gens) == 1 and hasattr(x, "__aiter__"):
                bucket: list[str] = []
                self.pulled.append(bucket)

                async def _pull():
                    async for piece in x:
                        bucket.append(piece)

                self.first_task = asyncio.get_running_loop().create_task(_pull())
            return x

    sess = _BargeInSession()
    p = InterpPipeline(
        sess, mt, "SYS", target_lang="zh", voice_tags=True,
        lag=lag, first_ms={"ms": 0},
    )

    async def scenario():
        for u in ("一", "二", "三"):
            p.enqueue_raw(u)
        task = asyncio.create_task(p.run())
        for _ in range(200):  # 等首句 gen 吐出第一片（此刻泵仍在飞）
            if sess.pulled and sess.pulled[0]:
                break
            await asyncio.sleep(0.01)
        assert sess.pulled and sess.pulled[0][:1] == ["你好世界"]
        sess.first_task.cancel()  # 打断：gen 收尾 → 泵被连带 cancel
        with _suppress_cancel():
            await sess.first_task
        for _ in range(500):  # run 必须存活并消化完后续两单元
            if len(lag.dones) >= 3:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with _suppress_cancel():
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=8.0))
    assert len(lag.notes) == 3
    assert len(lag.dones) == 3  # run 存活：三句全部配对（stash 版此处=0，run 已死）
    assert [t for _s, t in p.pairs][1:] == ["第二句译文完整。", "第三句译文完整。"]


def test_w8b_stalled_mt_stream_deadline_recovers(monkeypatch):
    """MT 流挂死（吐一片后永不再增量）：run 超时兜底 cancel 泵 → 整句回退 →
    后续单元照常——FIFO 永不因单句流挂死停摆；挂死流被 aclose（无僵尸解码）。"""
    import agent_runtime.interp_lite.pipeline as pipeline_mod

    monkeypatch.delenv("BOK_INTERP_SPEC_MT", raising=False)
    monkeypatch.setattr(pipeline_mod, "_SENT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(pipeline_mod, "_SENT_TIMEOUT_GRACE_S", 0.05)
    mt = _SlowMT([
        {"pieces": ["你"], "hold": 30.0},        # 一片（<4 字=零 yield）后挂死
        {"pieces": ["好的这是兜底回退译文。"]},  # 挂死句的整句重试流
        {"pieces": ["下一句译文完整过关。"]},
    ])
    lag = FakeLag()
    p = InterpPipeline(
        _LazySession(), mt, "SYS", target_lang="zh", voice_tags=True,
        lag=lag, first_ms={"ms": 0},
    )

    async def scenario():
        p.enqueue_raw("挂死句")
        p.enqueue_raw("后续句")
        task = asyncio.create_task(p.run())
        for _ in range(500):
            if len(lag.dones) >= 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with _suppress_cancel():
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=8.0))
    assert len(lag.dones) == 2  # 挂死句走重试完成+后续句照常
    assert 0 in mt.closed  # 挂死流被 cancel→finally→aclose 收场（无僵尸）
    assert [t for _s, t in p.pairs] == ["好的这是兜底回退译文。", "下一句译文完整过关。"]


def test_w8b_shutdown_cancels_pump_and_watchers(monkeypatch):
    """shutdown 收线：在途泵+催尾观察者全部 cancel（不悬挂、不外抛）。"""
    monkeypatch.delenv("BOK_INTERP_SPEC_MT", raising=False)
    monkeypatch.delenv("BOK_INTERP_TAIL_FLUSH", raising=False)
    mt = _SlowMT([{"pieces": ["第一句译文够长。"], "gap": 0.05, "hold": 30.0}])
    sess = _HandleSession()
    p = InterpPipeline(
        sess, mt, "SYS", target_lang="zh", voice_tags=True,
        lag=FakeLag(), first_ms={"ms": 0},
    )
    calls: list[int] = []

    async def _flush():
        calls.append(1)

    p._tail_flush = _flush

    async def scenario():
        p.enqueue_raw("第一句")
        task = asyncio.create_task(p.run())
        for _ in range(300):
            if sess.handles and not p.mt_busy["flag"]:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert calls == [] and p._playout_watchers["n"] == 1  # 泵在途：观察者武装
        task.cancel()  # 生产序：run 先收线（worker _shutdown 同序），再 pipeline.shutdown
        with _suppress_cancel():
            await task
        p.shutdown()
        await asyncio.sleep(0.05)
        assert p._playout_watchers["n"] == 0  # 观察者被收线 cancel，计数回落
        assert calls == []  # 未落地零催尾

    asyncio.run(asyncio.wait_for(scenario(), timeout=5.0))


def test_w8b_tail_flush_watcher_fires_at_playout_done(monkeypatch):
    """真 SpeechHandle 形状（2026-10-09 话轮聚合配套契约更新）：泵排干+FIFO 空
    +MT 闲即催——不再等 playout（bidi 侧 stream_ended 门禁已把中途催尾 ack
    无害化，提前催=更快出尾声）；playout 落地后观察者 belt 至多再催一枚。"""
    monkeypatch.delenv("BOK_INTERP_SPEC_MT", raising=False)
    monkeypatch.delenv("BOK_INTERP_TAIL_FLUSH", raising=False)
    mt = FakeMT([iter(["第一句译文够长。"])])
    sess = _HandleSession()
    p = InterpPipeline(
        sess, mt, "SYS", target_lang="zh", voice_tags=True,
        lag=FakeLag(), first_ms={"ms": 0},
    )
    calls: list[int] = []

    async def _flush():
        calls.append(1)

    p._tail_flush = _flush

    async def scenario():
        p.enqueue_raw("第一句")
        task = asyncio.create_task(p.run())
        for _ in range(300):
            if sess.gens and p.q.empty() and not p.mt_busy["flag"]:
                break
            await asyncio.sleep(0.01)
        for _ in range(100):  # run 位：泵排干即催（无需 playout 落地）
            if calls:
                break
            await asyncio.sleep(0.01)
        assert calls == [1]
        sess.handles[0].release()  # playout 落地：belt 至多再一枚
        await asyncio.sleep(0.05)
        assert 1 <= len(calls) <= 2
        task.cancel()
        with _suppress_cancel():
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=5.0))


def test_w8b_tail_flush_newest_guard_and_interrupted(monkeypatch):
    """旧句 playout 落地时已被新句接棒=让位不催；最新句被掐=不催（belt 观察者
    语义不变）；run 位催尾只在 FIFO 空时发——有后句在队期间零催。"""
    monkeypatch.delenv("BOK_INTERP_SPEC_MT", raising=False)
    monkeypatch.delenv("BOK_INTERP_TAIL_FLUSH", raising=False)
    mt = FakeMT([iter(["第一句译文够长。"]), iter(["第二句译文够长。"])])
    sess = _HandleSession()
    p = InterpPipeline(
        sess, mt, "SYS", target_lang="zh", voice_tags=True,
        lag=FakeLag(), first_ms={"ms": 0},
    )
    calls: list[int] = []

    async def _flush():
        calls.append(1)

    p._tail_flush = _flush

    async def scenario():
        p.enqueue_raw("第一句")
        p.enqueue_raw("第二句")
        task = asyncio.create_task(p.run())
        for _ in range(300):
            if len(sess.gens) >= 2 and p.q.empty() and not p.mt_busy["flag"]:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert calls == [1]  # 两句在队期间零催；第二句排干后 run 位恰一枚
        sess.handles[0].release()  # 旧句落地：已被新句接棒 → belt 让位
        await asyncio.sleep(0.05)
        sess.handles[1].interrupted = True  # 最新句被掐：belt 不催
        sess.handles[1].release()
        await asyncio.sleep(0.05)
        assert calls == [1]
        assert p._playout_watchers["n"] == 0  # 两观察者都已退场
        task.cancel()
        with _suppress_cancel():
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=5.0))


def test_w8b_tail_flush_zero_yield_text_say_takes_over(monkeypatch):
    """零 yield 整句直念接棒：text 句柄成为 newest——gen 句柄先落地让位、text
    句柄落地 belt 至多再一枚（run 位已催过；重试/整句路同样接棒观察者）。"""
    monkeypatch.delenv("BOK_INTERP_SPEC_MT", raising=False)
    monkeypatch.delenv("BOK_INTERP_TAIL_FLUSH", raising=False)
    mt = FakeMT([iter(["好。"])])  # 2 字 < 软证据窗 → 零 yield → 整句直念
    sess = _HandleSession()
    p = InterpPipeline(
        sess, mt, "SYS", target_lang="zh", voice_tags=True,
        lag=FakeLag(), first_ms={"ms": 0},
    )
    calls: list[int] = []

    async def _flush():
        calls.append(1)

    p._tail_flush = _flush

    async def scenario():
        p.enqueue_raw("短句")
        task = asyncio.create_task(p.run())
        for _ in range(300):
            if len(sess.handles) >= 2 and p.q.empty() and not p.mt_busy["flag"]:
                break
            await asyncio.sleep(0.01)
        for _ in range(100):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert calls == [1] and p._playout_watchers["n"] == 2  # gen+text 双观察者
        sess.handles[0].release()  # gen 句柄落地：已被 text 接棒 → belt 让位
        await asyncio.sleep(0.05)
        assert calls == [1]
        sess.handles[1].release()  # text 句柄落地：belt 至多再一枚
        await asyncio.sleep(0.05)
        assert 1 <= len(calls) <= 2
        task.cancel()
        with _suppress_cancel():
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=5.0))
