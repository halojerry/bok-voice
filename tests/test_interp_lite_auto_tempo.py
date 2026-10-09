"""interp_lite 播放背压 auto_tempo（W8-B，jinxi 水位设计移植）单测。

四面：
- TempoController 水位升降档/一次一档/退出滞回（exitMs=进入阈值×0.7）/保持期；
- OLA 变速（单源 ``bok_voice_core.audio_tempo``）：时长≈原时长/speed ±10%、
  speed=1.0 逐字节恒等、流式 ChunkStretcher 与批式等长、采样率窗宽缩放；
- kill-switch ``BOK_INTERP_AUTO_TEMPO`` 关=装配全 None（旧路径逐字节）；
- 接入面：pipeline tempo tick（FIFO 深度×句均 EMA）、MiniMax bidi 流 emitter
  push 变速（fake WS，无网络）、frame_transform 缺省 None 零影响。
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.interp_lite import auto_tempo  # noqa: E402
from agent_runtime.interp_lite.auto_tempo import (  # noqa: E402
    ChunkStretcher,
    TempoController,
    build_tempo_controller,
    parse_speeds,
    speedup_frames,
)
from agent_runtime.interp_lite.pipeline import InterpPipeline  # noqa: E402

SR = 24000


# ---- 帮手 ----------------------------------------------------------------------


def _sine_pcm(sr: int = SR, secs: float = 1.0, freq: int = 440) -> bytes:
    buf = bytearray()
    n = int(sr * secs)
    for i in range(n):
        v = int(12000 * math.sin(2 * math.pi * freq * i / sr))
        buf += v.to_bytes(2, "little", signed=True)
    return bytes(buf)


# ---- 水位升降档 / 状态命名 -------------------------------------------------------


def test_levels_normal_below_t1():
    tc = TempoController()
    snap = tc.resolve(0.0, 0, 100.0)
    assert snap["level"] == 0 and snap["state"] == "NORMAL" and snap["speed"] == 1.0
    assert snap["severe"] is False


def test_upgrade_one_level_per_resolve():
    """一次一档：积压瞬跳 6000（≥T2）也不越级——下次 resolve 才到 CONSTRAINED。"""
    tc = TempoController()
    s1 = tc.resolve(6000.0, 2, 10.0)
    assert s1["level"] == 1 and s1["state"] == "GUARDED" and s1["speed"] == 1.25
    s2 = tc.resolve(6000.0, 2, 10.1)
    assert s2["level"] == 2 and s2["state"] == "CONSTRAINED" and s2["speed"] == 1.35
    assert s2["severe"] is True  # ≥T3 深积压旗亮（缺省三速=T3 只作旗）


def test_enter_at_exact_threshold():
    tc = TempoController()
    assert tc.resolve(1199.9, 1, 1.0)["level"] == 0
    assert tc.resolve(1200.0, 1, 1.1)["level"] == 1  # ≥T1 即进


def test_downgrade_needs_hold_and_exit_hysteresis():
    """降级双闸：保持期未满不动；满了但积压未低于进入阈值×0.7 也不动。"""
    tc = TempoController()
    tc.resolve(6000.0, 2, 10.0)
    tc.resolve(6000.0, 2, 10.1)  # level=2，changed_at=10.1
    # 保持期 1s < 3s：积压已很低也不降
    assert tc.resolve(500.0, 0, 11.0)["level"] == 2
    # 保持期满了，但 2500 ≥ T2×0.7=2100（CONSTRAINED 退出滞回）：不降
    assert tc.resolve(2500.0, 1, 14.0)["level"] == 2
    # 1000 < 2100 且保持满 → 降到 GUARDED
    assert tc.resolve(1000.0, 0, 14.2)["level"] == 1
    # GUARDED 退出滞回：840 = T1×0.7；900 未低于 → 不降
    assert tc.resolve(900.0, 0, 18.0)["level"] == 1
    assert tc.resolve(500.0, 0, 18.5)["level"] == 0  # <840 且保持满 → NORMAL


def test_upgrade_resets_hold_clock():
    """刚升上来的档不许马上降（保持时钟从进档时刻起算）。"""
    tc = TempoController()
    tc.resolve(1300.0, 1, 10.0)  # →GUARDED（changed_at=10）
    assert tc.resolve(100.0, 0, 10.5)["level"] == 1  # 才 0.5s<3s：不降
    assert tc.resolve(100.0, 0, 13.1)["level"] == 0  # 满 3.1s+低于 840 → 降


def test_hold_timer_resets_on_change():
    tc = TempoController()
    tc.resolve(6000.0, 2, 10.0)   # →1（changed_at=10）
    tc.resolve(6000.0, 2, 10.1)   # →2（changed_at=10.1，时钟刷新）
    assert tc.resolve(100.0, 0, 13.05)["level"] == 2  # 距刷新仅 2.95s<3s：不降
    assert tc.resolve(100.0, 0, 13.2)["level"] == 1   # 满 3.1s：降一档


def test_four_speed_env_t3_becomes_own_tier():
    """配 4 个速度档时 T3（≥5000）自成第四档（落后极端档）。"""
    tc = TempoController(speeds=(1.0, 1.25, 1.35, 1.5))
    assert tc.resolve(6000.0, 1, 1.0)["level"] == 1
    assert tc.resolve(6000.0, 1, 1.1)["level"] == 2
    s3 = tc.resolve(6000.0, 1, 1.2)
    assert s3["level"] == 3 and s3["speed"] == 1.5


# ---- env 装配 / kill-switch ------------------------------------------------------


def test_kill_switch_off_returns_none(monkeypatch):
    monkeypatch.setenv("BOK_INTERP_AUTO_TEMPO", "0")
    assert auto_tempo.auto_tempo_enabled() is False
    assert build_tempo_controller() is None


def test_kill_switch_default_on(monkeypatch):
    monkeypatch.delenv("BOK_INTERP_AUTO_TEMPO", raising=False)
    assert auto_tempo.auto_tempo_enabled() is True
    tc = build_tempo_controller()
    assert tc is not None and tc.thresholds_ms == (1200.0, 3000.0, 5000.0)
    assert tc.speeds == (1.0, 1.25, 1.35) and tc.hold_s == 3.0


def test_env_overrides_and_bad_values(monkeypatch):
    monkeypatch.delenv("BOK_INTERP_AUTO_TEMPO", raising=False)
    monkeypatch.setenv("BOK_INTERP_TEMPO_T1_MS", "900")
    monkeypatch.setenv("BOK_INTERP_TEMPO_T2_MS", "2500")
    monkeypatch.setenv("BOK_INTERP_TEMPO_T3_MS", "4000")
    monkeypatch.setenv("BOK_INTERP_TEMPO_HOLD_S", "2")
    monkeypatch.setenv("BOK_INTERP_TEMPO_SPEEDS", "1.0,1.2,1.4,1.6")
    tc = build_tempo_controller()
    assert tc.thresholds_ms == (900.0, 2500.0, 4000.0)
    assert tc.hold_s == 2.0 and tc.speeds == (1.0, 1.2, 1.4, 1.6)

    monkeypatch.setenv("BOK_INTERP_TEMPO_HOLD_S", "abc")  # 坏值回缺省
    monkeypatch.setenv("BOK_INTERP_TEMPO_T1_MS", "-5")  # 负值回缺省
    monkeypatch.setenv("BOK_INTERP_TEMPO_T2_MS", "x")
    monkeypatch.setenv("BOK_INTERP_TEMPO_T3_MS", "")
    tc2 = build_tempo_controller()
    assert tc2.thresholds_ms == (1200.0, 3000.0, 5000.0)  # 坏键逐键回缺省
    assert tc2.hold_s == 3.0


def test_env_unordered_thresholds_fall_back_to_defaults(monkeypatch):
    """乱序水位（T3<T1）整组回缺省——滞回语义防错乱。"""
    monkeypatch.delenv("BOK_INTERP_AUTO_TEMPO", raising=False)
    monkeypatch.setenv("BOK_INTERP_TEMPO_T1_MS", "5000")
    monkeypatch.setenv("BOK_INTERP_TEMPO_T2_MS", "3000")
    monkeypatch.setenv("BOK_INTERP_TEMPO_T3_MS", "1200")
    tc = build_tempo_controller()
    assert tc.thresholds_ms == (1200.0, 3000.0, 5000.0)


def test_parse_speeds_sanitizes():
    assert parse_speeds("1.0,1.25,1.35") == (1.0, 1.25, 1.35)
    assert parse_speeds("1.35, 1.0 ,x,0.9,1.25") == (1.0, 1.25, 1.35)  # 乱序化+剔非法/<1
    assert parse_speeds("") == (1.0, 1.25, 1.35)  # 空=缺省
    assert parse_speeds(None) == (1.0, 1.25, 1.35)
    assert parse_speeds("2.0") == (2.0,)


# ---- 积压估计（句均时长 EMA） ----------------------------------------------------


def test_backlog_estimate_uses_ema():
    tc = TempoController()
    assert tc.sent_ms == {"ema": 2500.0}  # 初值 2.5s
    assert tc.backlog_estimate_ms(2) == 5000.0
    tc.observe_audio_ms(1000.0)  # α=0.3
    assert abs(tc.sent_ms["ema"] - (0.3 * 1000 + 0.7 * 2500)) < 1e-6
    tc.observe_audio_ms(999999.0)  # 钳 60s 上限
    assert tc.sent_ms["ema"] < 60000.0


# ---- OLA 变速（单源 core；长度/采样率/恒等/流式等价） ----------------------------


def test_speedup_frames_length_and_identity():
    pcm = _sine_pcm(secs=1.0)
    assert speedup_frames([pcm], 1.0, SR) == pcm  # speed 1.0 逐字节恒等
    out = speedup_frames([pcm], 1.25, SR)
    want = len(pcm) / 1.25
    assert abs(len(out) - want) / want < 0.10  # 时长≈原时长/speed ±10%
    out135 = speedup_frames([pcm], 1.35, SR)
    want135 = len(pcm) / 1.35
    assert abs(len(out135) - want135) / want135 < 0.10


def test_speedup_frames_multi_chunk_and_sample_rate():
    pcm = _sine_pcm(secs=0.8)
    parts = [pcm[i:i + 1777] for i in range(0, len(pcm), 1777)]  # 任意奇块切分
    out = speedup_frames(iter(parts), 1.25, SR)
    want = len(pcm) / 1.25
    assert abs(len(out) - want) / want < 0.10
    # 采样率参数只影响窗宽，不改变时长关系（16k 窗=960 与探针历史常量一致）
    out16 = speedup_frames([_sine_pcm(16000, 1.0)], 1.25, 16000)
    assert abs(len(out16) - len(_sine_pcm(16000, 1.0)) / 1.25) / (
        len(_sine_pcm(16000, 1.0)) / 1.25
    ) < 0.10


def test_chunk_stretcher_streaming_matches_batch():
    """逐 200ms 块 push+finalize ≈ 批式一次变速（跨 chunk 窗口持续，无接缝丢窗）。"""
    pcm = _sine_pcm(secs=1.0)
    batch = speedup_frames([pcm], 1.25, SR)
    st = ChunkStretcher(SR, 1.25)
    outs = [st.push(pcm[i:i + 4800]) for i in range(0, len(pcm), 4800)]  # 200ms 块
    outs.append(st.finalize())
    stream = b"".join(outs)
    want = len(pcm) / 1.25
    assert abs(len(batch) - want) / want < 0.10
    assert abs(len(stream) - len(batch)) / len(batch) < 0.05  # 流式≈批式（窗级差异）
    assert abs(len(stream) - want) / want < 0.10


def test_chunk_stretcher_reports_input_ms_once():
    seen: list[float] = []
    st = ChunkStretcher(SR, 1.25, done_cb=seen.append)
    st.push(_sine_pcm(secs=0.5)[:3000])
    assert seen == []  # finalize 前不报
    st.finalize()
    assert len(seen) == 1 and abs(seen[0] - (1500 / SR * 1000)) < 1.0
    assert st.finalize() == b""  # 重复收尾=空（不重复观测）


def test_chunk_stretcher_sentence_divisor_normalizes_to_per_sentence():
    """R4（V3 审计）：话轮聚合档一条流吃 N 句——done_cb 回报须除以句数归一回
    句均，防 EMA 漂成话均→backlog 虚高常态顶格 1.35。除数缺省 1=旧语义。"""
    seen: list[float] = []
    st = ChunkStretcher(SR, 1.0, done_cb=seen.append)
    st.sentence_divisor = 4.0  # bidi finalize 注入该流服务端切句数
    st.push(_sine_pcm(secs=1.0)[: SR * 2])  # 1s 输入
    st.finalize()
    assert abs(seen[0] - 250.0) < 2.0  # 1000ms ÷ 4 = 250ms 句均
    # 缺省除数=1：逐句档逐字节旧口径
    seen2: list[float] = []
    st2 = ChunkStretcher(SR, 1.0, done_cb=seen2.append)
    st2.push(_sine_pcm(secs=1.0)[: SR * 2])
    st2.finalize()
    assert abs(seen2[0] - 1000.0) < 2.0


def test_chunk_stretcher_short_input_and_finalize_only():
    st = ChunkStretcher(SR, 1.35)
    out = st.push(b"\x01\x02")  # 不足一窗：中段不吐
    tail = st.finalize()
    assert len(tail) > 0 and (len(out) + len(tail)) > 0


def test_speedup_frames_empty_and_bad_speed():
    assert speedup_frames([], 1.25, SR) == b""
    pcm = _sine_pcm(secs=0.1)
    assert speedup_frames([pcm], 0.8, SR) == pcm  # 不减速=原样


# ---- frame_transform 工厂 --------------------------------------------------------


def test_factory_none_at_speed_1_and_stretcher_otherwise():
    tc = TempoController()
    factory = tc.make_frame_transform(SR)
    assert factory() is None  # 当前档 NORMAL(1.0)=零包装
    tc.resolve(6000.0, 2, 1.0)
    tc.resolve(6000.0, 2, 1.1)  # →CONSTRAINED 1.35
    st = factory()
    assert st is not None
    pcm = _sine_pcm(secs=0.6)
    out = st.push(pcm) + st.finalize()
    want = len(pcm) / 1.35
    assert abs(len(out) - want) / want < 0.10
    assert tc.sent_ms["ema"] < 2500.0  # 实测时长已回灌 EMA


# ---- pipeline 接入：tempo tick（FIFO 深度×句均 EMA） -----------------------------


class RecordingTempo:
    def __init__(self):
        self.calls: list[tuple[float, int]] = []
        self.current: dict = {"speed": 1.0, "state": "NORMAL", "level": 0}

    def resolve(self, backlog_ms: float, depth: int, now_s: float) -> dict:
        self.calls.append((backlog_ms, depth))
        self.current = {
            "speed": 1.0, "state": "NORMAL", "level": 0,
            "backlog_ms": backlog_ms, "queue_depth": depth, "severe": False,
        }
        return self.current

    def backlog_estimate_ms(self, depth: int) -> float:
        return depth * 2500.0


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
    def __init__(self, calls):
        self._calls = list(calls)

    async def stream(self, msgs):
        for d in self._calls.pop(0):
            yield d


class FakeSession:
    def __init__(self):
        self.gens: list = []

    def say(self, x, audio=None):
        self.gens.append(x)
        return x


class _SuppressCancel:
    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        return et is not None and issubclass(et, asyncio.CancelledError)


def test_pipeline_tempo_tick_per_unit(monkeypatch):
    """每单元出队后一次 tick：积压估计=当前 FIFO 深度×句均 EMA（RecordingTempo 记录）。"""
    monkeypatch.setenv("BOK_INTERP_SPEC_MT", "0")  # 投机关=纯管线面
    mt = FakeMT([iter(["第一句译文够长。"]), iter(["第二句译文够长。"])])
    tempo = RecordingTempo()
    p = InterpPipeline(
        FakeSession(), mt, "SYS", target_lang="zh", voice_tags=True,
        lag=FakeLag(), first_ms={"ms": 0}, tempo=tempo,
    )

    async def scenario():
        p.enqueue_raw("第一句")
        p.enqueue_raw("第二句")
        task = asyncio.create_task(p.run())
        for _ in range(300):
            if p.q.empty() and len(tempo.calls) >= 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with _SuppressCancel():
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=5.0))
    assert tempo.calls == [(2500.0, 1), (0.0, 0)]  # 首句出队时深度 1、末句 0


def test_pipeline_tempo_none_is_noop(monkeypatch):
    """tempo 缺省 None：管线行为与旧路径逐字节一致（tick 零开销直返）。"""
    monkeypatch.setenv("BOK_INTERP_SPEC_MT", "0")
    mt = FakeMT([iter(["第一句译文够长。"])])
    p = InterpPipeline(
        FakeSession(), mt, "SYS", target_lang="zh", voice_tags=True,
        lag=FakeLag(), first_ms={"ms": 0},
    )
    assert p._tempo is None
    p._tempo_tick()  # 不炸、无副作用

    async def scenario():
        p.enqueue_raw("第一句")
        task = asyncio.create_task(p.run())
        for _ in range(300):
            if p.q.empty() and len(p.pairs) >= 1:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with _SuppressCancel():
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=5.0))
    assert [t for _s, t in p.pairs] == ["第一句译文够长。"]


# ---- MiniMax bidi 流接入（fake WS，无网络） --------------------------------------

_FROM_LP = "agent_runtime.providers.livekit_plugins"


def _make_tts(frame_transform=None):
    from agent_runtime.providers.livekit_plugins import MiniMaxTTS

    return MiniMaxTTS(
        voice={"zh": "male-qn-qingse"},
        sample_rate=SR,
        api_key="test" + "-key",
        frame_transform=frame_transform,
    )


_CONNECTED = '{"event": "connected_success"}'
_STARTED = '{"event": "task_started"}'
_FLUSHED = '{"event": "task_flushed"}'


def _audio_msg(pcm: bytes) -> str:
    return json.dumps({"data": {"audio": pcm.hex()}, "is_final": True})


class _FakeWS:
    def __init__(self, script: list[str]):
        self._script = list(script)
        self.sent: list[dict] = []
        self.pings = 0
        self.closed = False

    async def recv(self):
        if self._script:
            return self._script.pop(0)
        await asyncio.Event().wait()

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def ping(self):
        self.pings += 1

    async def close(self):
        self.closed = True


class _FakeConnect:
    def __init__(self, ws):
        self._ws = ws

    async def __call__(self, *a, **kw):
        return self._ws


async def _wait_for(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


def _drain_bidi(tts, ws, monkeypatch):
    """跑一条 bidi 流到收尾，返回 emitter 实际产出的全部 PCM 字节。"""
    monkeypatch.setattr("websockets.connect", _FakeConnect(ws))

    async def run():
        s = tts.stream()
        s.push_text("你好")
        s.end_input()
        assert await _wait_for(lambda: s._task.done(), timeout=10)
        audio = bytearray()
        async for ev in s:
            audio += bytes(ev.frame.data)
        return bytes(audio)

    return asyncio.run(asyncio.wait_for(run(), timeout=15))


def test_bidi_no_transform_output_byte_identical(monkeypatch):
    """frame_transform 缺省 None：音频逐字节原样透传（A 线/旧行为不变）。

    框架 AudioEmitter 对尾帧补零成整帧（content 前缀恒等+至多一帧尾巴）。"""
    monkeypatch.setenv("MINIMAX_WS_MODE", "bidi")
    pcm = _sine_pcm(secs=0.5)
    ws = _FakeWS([_CONNECTED, _STARTED, _audio_msg(pcm), _FLUSHED])
    tts = _make_tts()
    assert tts._frame_transform is None
    out = _drain_bidi(tts, ws, monkeypatch)
    assert out[:len(pcm)] == pcm  # 逐字节恒等（含长度以内的每一字节）
    assert len(out) - len(pcm) <= 2 * (SR // 100)  # 至多一帧补零尾巴（240 样本=480 字节）


def test_bidi_factory_none_passthrough(monkeypatch):
    """工厂在位但当前档 1.0 返回 None：同样逐字节原样（NORMAL 档零包装）。"""
    monkeypatch.setenv("MINIMAX_WS_MODE", "bidi")
    pcm = _sine_pcm(secs=0.5)
    ws = _FakeWS([_CONNECTED, _STARTED, _audio_msg(pcm), _FLUSHED])
    tts = _make_tts(frame_transform=TempoController().make_frame_transform(SR))
    out = _drain_bidi(tts, ws, monkeypatch)
    assert out[:len(pcm)] == pcm
    assert len(out) - len(pcm) <= 2 * (SR // 100)


def test_bidi_stretched_at_guarded_speed(monkeypatch):
    """GUARDED 档（1.25）：本流音频时长≈原时长/1.25（±10%），且实测时长回灌 EMA。"""
    monkeypatch.setenv("MINIMAX_WS_MODE", "bidi")
    pcm = _sine_pcm(secs=0.5)
    ws = _FakeWS([_CONNECTED, _STARTED, _audio_msg(pcm), _FLUSHED])
    tc = TempoController()
    tc.resolve(6000.0, 2, 1.0)  # →GUARDED 1.25（current 供工厂读取）
    assert tc.current["speed"] == 1.25
    tts = _make_tts(frame_transform=tc.make_frame_transform(SR))
    out = _drain_bidi(tts, ws, monkeypatch)
    want = len(pcm) / 1.25
    assert abs(len(out) - want) / want < 0.10
    assert tc.sent_ms["ema"] < 2500.0  # finalize 的输入时长观测已回灌（0.5s 实测）


def test_bidi_factory_raises_falls_back_passthrough(monkeypatch):
    """工厂抛错=原速直通（变速纯增益，绝不成为新故障源）。"""
    monkeypatch.setenv("MINIMAX_WS_MODE", "bidi")
    pcm = _sine_pcm(secs=0.5)
    ws = _FakeWS([_CONNECTED, _STARTED, _audio_msg(pcm), _FLUSHED])

    def _boom():
        raise RuntimeError("factory boom")

    tts = _make_tts(frame_transform=_boom)
    out = _drain_bidi(tts, ws, monkeypatch)
    assert out[:len(pcm)] == pcm


# ---- tts_minimax.build 装配面 ----------------------------------------------------


def test_build_passes_frame_transform(monkeypatch):
    from agent_runtime.interp_lite.providers import tts_minimax

    monkeypatch.delenv("BOK_INTERP_AUTO_TEMPO", raising=False)
    marker = lambda: None  # noqa: E731  任意对象即可（装配面只透传）
    tts = tts_minimax.build({"api_key": "k", "sample_rate": SR}, "en", None, frame_transform=marker)
    assert tts._frame_transform is marker
    tts2 = tts_minimax.build({"api_key": "k", "sample_rate": SR}, "en")
    assert tts2._frame_transform is None  # 缺省 None=A 线零影响
