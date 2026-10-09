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
        assert p.q.qsize() == 1 and list(p.q._queue) == ["好的"]  # 余段入队
        assert p.session.audio_said[0] is not None  # HIT=audio 直播（零合成）
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
        def __init__(self, ws):
            self._ws = ws

        def _alive(self):
            return self._ws is not None

    class _TTS:
        def __init__(self, ws):
            self._s = _Sess(ws)

        def _bidi_session(self):
            return self._s

    asyncio.run(tail_flush_channel(_TTS(_WS()))())
    assert sent == ['{"event": "task_flush"}']

    asyncio.run(tail_flush_channel(_TTS(None))())  # 连接不在场=no-op
    assert len(sent) == 1

    class _Other:  # 非 MiniMax 装配（无 _bidi_session 面）=永久 no-op
        pass

    asyncio.run(tail_flush_channel(_Other())())
    assert len(sent) == 1

    class _BoomWS:
        async def send(self, msg):
            raise RuntimeError("dead ws")

    asyncio.run(tail_flush_channel(_TTS(_BoomWS()))())  # 发送异常静默吞
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
        await p._maybe_tail_flush()
        assert calls == []
        p.q.get_nowait()
        p.mt_busy["flag"] = True  # 真 MT 在途
        await p._maybe_tail_flush()
        assert calls == []
        p.mt_busy["flag"] = False
        p._last_say = _InterruptedHandle()  # 当前 say 被取消
        await p._maybe_tail_flush()
        assert calls == []
        p._last_say = None
        await p._maybe_tail_flush()
        assert calls == [1]
        p.lane_dead["reason"] = "x"
        await p._maybe_tail_flush()
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
