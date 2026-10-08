"""豆包 SAUC STT provider 单测（零网络：fake websockets + fake VAD）。

覆盖：协议帧 round-trip / 请求配置（enable_lid+热词去重）/ 双鉴权头 / SSRF 护栏 /
总闸 / _transcribe_once 单发 / 全流事件序（START→INTERIM→EOS→FINAL）/ interim
去重 / 收线窗抑制 / live 失败整段重试。真端点冒烟另见 scripts/probes/probe_cloud_asr.py
与 reports/cloud-asr/（2026-10-03 三语全量定案表）。

驱动姿势备忘（livekit 1.7 基类语义）：RecognizeStream.__init__ 自启 _main_task→
_run，且 _event_ch 被 tee 二分（主分支=__anext__，另一支=metrics）——测试不得再
自建 _run 任务，事件只能经 stream.__anext__ 读。
"""

from __future__ import annotations

import asyncio
import gzip
import json
import struct
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.providers import doubao_asr as da  # noqa: E402
from agent_runtime.providers.doubao_asr import (  # noqa: E402
    MSG_AUDIO_ONLY_REQ,
    MSG_FULL_CLIENT_REQ,
    DoubaoSTT,
    _DoubaoLiveStream,
    _ws_host_ok,
    doubao_asr_enabled,
    frame_audio,
    frame_full_client_request,
    parse_server_frame,
)


# ---- 造帧工具 ----
def _srv_frame(payload: dict | None, *, last: bool = False, seq: int = 1) -> bytes:
    body = gzip.compress(json.dumps(payload or {}, ensure_ascii=False).encode())
    flags = 0b0010 if last else 0b0001  # last→无 seq；否则带正 seq
    buf = bytes([(1 << 4) | 1, (0b1001 << 4) | flags, (1 << 4) | 1, 0])
    if not last:
        buf += struct.pack(">i", seq)
    buf += struct.pack(">I", len(body)) + body
    return buf


def _text_payload(text: str, *, definite: bool = False) -> dict:
    return {"result": {"text": text, "utterances": [{"definite": definite, "text": text}]}}


class _FakeWS:
    """fake WS：send 记录；正 seq 音频包→推 on_audio 下一帧；末包（负 seq）→推 on_last。"""

    def __init__(self, on_audio: list[bytes] | None = None, on_last: list[bytes] | None = None):
        self.sent: list[bytes] = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self._on_audio = list(on_audio or [])
        self._on_last = list(on_last or [])
        self.closed = False

    async def send(self, data: bytes) -> None:
        data = bytes(data)
        self.sent.append(data)
        fr = parse_server_frame(data)
        if fr["type"] != MSG_AUDIO_ONLY_REQ:
            return
        seq = fr["seq"] or 0
        if seq < 0:
            for frame in self._on_last:
                self._queue.put_nowait(frame)
        elif self._on_audio:
            self._queue.put_nowait(self._on_audio.pop(0))

    async def recv(self) -> bytes:
        return await self._queue.get()

    async def close(self) -> None:
        self.closed = True


class _FakeVadStream:
    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()
        self.pushed: list = []

    def push_frame(self, frame) -> None:
        self.pushed.append(frame)

    def flush(self) -> None:
        pass

    def end_input(self) -> None:
        self.q.put_nowait(None)

    async def __aiter__(self):
        while True:
            ev = await self.q.get()
            if ev is None:
                return
            yield ev


class _FakeVad:
    def __init__(self):
        self.streams: list[_FakeVadStream] = []

    def stream(self) -> _FakeVadStream:
        s = _FakeVadStream()
        self.streams.append(s)
        return s


def _ev(type_, *, frames=None, silence: float = 0.3, inference: float = 0.0):
    return types.SimpleNamespace(
        type=type_, frames=frames or [], silence_duration=silence, inference_duration=inference
    )


def _fake_merge(monkeypatch) -> None:
    monkeypatch.setattr(
        da,
        "utils",
        types.SimpleNamespace(
            merge_frames=lambda frames: types.SimpleNamespace(
                data=b"".join(getattr(f, "data", b"") for f in frames)
            )
        ),
    )


# ---- 协议 / 配置 / 护栏（纯函数） ----
def test_frame_roundtrip():
    cfg = {"request": {"model_name": "bigmodel"}}
    raw = frame_full_client_request(cfg)
    assert raw[1] >> 4 == MSG_FULL_CLIENT_REQ
    assert struct.unpack(">i", raw[4:8])[0] == 1
    size = struct.unpack(">I", raw[8:12])[0]
    assert json.loads(gzip.decompress(raw[12:12 + size])) == cfg

    pos = parse_server_frame(frame_audio(5, b"\x01\x02", last=False))
    assert pos["type"] == MSG_AUDIO_ONLY_REQ and pos["seq"] == 5
    neg = parse_server_frame(frame_audio(6, b"\x03\x04", last=True))
    assert neg["seq"] == -6


def test_server_frame_parse_last():
    fr = parse_server_frame(_srv_frame(_text_payload("你好"), last=True))
    assert fr["is_last"] is True
    assert json.loads(fr["payload"])["result"]["text"] == "你好"


def test_config_enable_lid_and_hotwords():
    stt = DoubaoSTT(api_key="k", hotword_terms=["拼多多", "单号", "拼多多", "  "])
    cfg = stt._config()
    req = cfg["request"]
    assert req["enable_lid"] is True
    assert req["enable_itn"] is True
    terms = json.loads(req["corpus"]["context"])["hotwords"]
    assert [t["word"] for t in terms] == ["拼多多", "单号"]  # 保序去重

    no_hw = DoubaoSTT(api_key="k")._config()
    assert "corpus" not in no_hw["request"]


def test_headers_new_and_old_auth():
    new = DoubaoSTT(api_key="sk-x")._headers()
    assert new["X-Api-Key"] == "sk-x" and "X-Api-App-Key" not in new
    assert new["X-Api-Resource-Id"] == da.DOUBAO_RESOURCE_DEFAULT
    assert new["X-Api-Connect-Id"] and new["X-Api-Request-Id"]

    old = DoubaoSTT(app_id="app", access_token="tok")._headers()
    assert old["X-Api-App-Key"] == "app" and old["X-Api-Access-Key"] == "tok"
    assert "X-Api-Key" not in old


def test_ws_host_guard():
    assert _ws_host_ok("wss://openspeech.bytedance.com/api/v3/sauc/bigmodel")
    assert not _ws_host_ok("ws://openspeech.bytedance.com/x")   # 明文拒
    assert not _ws_host_ok("wss://127.0.0.1/x")
    assert not _ws_host_ok("wss://localhost/x")
    assert not _ws_host_ok("wss://10.1.2.3/x")
    assert not _ws_host_ok("wss://192.168.1.5/x")
    assert not _ws_host_ok("wss://foo.internal/x")
    assert not _ws_host_ok("http://example.com/x")


def test_kill_switch(monkeypatch):
    monkeypatch.delenv("BOK_DOUBAO_ASR", raising=False)
    assert doubao_asr_enabled() is True
    monkeypatch.setenv("BOK_DOUBAO_ASR", "0")
    assert doubao_asr_enabled() is False


# ---- 单发（_transcribe_once） ----
async def _acoro(value):
    return value


def test_transcribe_once(monkeypatch):
    script = [_srv_frame(_text_payload("你系咪"), seq=1),
              _srv_frame(_text_payload("你系咪诈骗集团嚟？", definite=True), last=True)]
    fake = _FakeWS(on_last=script)
    monkeypatch.setattr("websockets.connect", lambda *a, **kw: _acoro(fake))

    stt = DoubaoSTT(api_key="k")
    text = asyncio.run(stt._transcribe_once(b"\x01\x02" * 3200))
    assert text == "你系咪诈骗集团嚟？"
    # 首帧=配置帧（seq=1）、末帧=负 seq 音频帧
    assert parse_server_frame(fake.sent[0])["seq"] == 1
    last = parse_server_frame(fake.sent[-1])
    assert last["type"] == MSG_AUDIO_ONLY_REQ and last["seq"] < 0


# ---- 全流（基类自启 _run；事件经 __anext__ 读） ----
def _make_connect(monkeypatch, *, connect_fail: bool, full_text: str, mid_text: str = "你好"):
    calls = {"n": 0}

    def _connect(*a, **kw):
        calls["n"] += 1
        if connect_fail and calls["n"] == 1:
            async def _boom():
                raise OSError("connect refused")
            return _boom()
        fake = _FakeWS(
            on_audio=[_srv_frame(_text_payload(mid_text), seq=1)],
            on_last=[_srv_frame(_text_payload(full_text, definite=True), last=True)],
        )
        calls["ws"] = fake
        return _acoro(fake)

    monkeypatch.setattr("websockets.connect", _connect)
    return calls


async def _drive_segment(stt: DoubaoSTT, vad: _FakeVad) -> list[tuple[str, str]]:
    """走完整段：START→INFERENCE→END→收尾；返回 (事件名, 文本) 序列。"""
    stream = _DoubaoLiveStream(stt, conn_options=da.APIConnectOptions())
    for _ in range(200):  # 等基类 _main_task 起 _run（vad.stream() 落账）
        await asyncio.sleep(0.005)
        if vad.streams:
            break
    vs = vad.streams[0]
    got: list[tuple[str, str]] = []

    async def _collect() -> None:
        try:
            while True:
                ev = await asyncio.wait_for(stream.__anext__(), timeout=0.5)
                text = ev.alternatives[0].text if getattr(ev, "alternatives", None) else ""
                got.append((ev.type.name, text))
        except (asyncio.TimeoutError, StopAsyncIteration):
            return

    collector = asyncio.create_task(_collect())
    vs.q.put_nowait(_ev(da.vad.VADEventType.START_OF_SPEECH,
                        frames=[types.SimpleNamespace(data=b"\x11\x11" * 800)]))
    await asyncio.sleep(0.05)
    vs.q.put_nowait(_ev(da.vad.VADEventType.INFERENCE_DONE,
                        frames=[types.SimpleNamespace(data=b"\x22\x22" * 3200)]))
    await asyncio.sleep(0.05)
    vs.q.put_nowait(_ev(da.vad.VADEventType.END_OF_SPEECH))
    await asyncio.sleep(0.4)  # 定稿链（末包→fake 响应→FINAL）毫秒级；固定窗等稳
    stream._input_ch.close()  # → _forward_input 收尾 → vad end_input → _recognize 收尾
    try:
        await asyncio.wait_for(collector, 3)
    except asyncio.TimeoutError:
        collector.cancel()
    await stream.aclose()
    return got


def test_live_flow_events(monkeypatch):
    _fake_merge(monkeypatch)
    calls = _make_connect(monkeypatch, connect_fail=False, full_text="你好，帮我查下单号。")
    vad = _FakeVad()
    stt = DoubaoSTT(api_key="k", vad_=vad)

    events = asyncio.run(_drive_segment(stt, vad))
    names = [n for n, _ in events]
    assert names[0] == "START_OF_SPEECH"
    assert names[-1] == "FINAL_TRANSCRIPT"
    assert "INTERIM_TRANSCRIPT" in names
    assert names.index("END_OF_SPEECH") < names.index("FINAL_TRANSCRIPT")
    assert events[-1][1] == "你好，帮我查下单号。"
    # 配置帧（seq=1）+ 音频帧都发出
    assert parse_server_frame(calls["ws"].sent[0])["seq"] == 1
    assert any(parse_server_frame(f)["type"] == MSG_AUDIO_ONLY_REQ for f in calls["ws"].sent[1:])
    # pre-reset 快照（EOS 时刻 partial 末稿）重贴给 FINAL 轮
    assert stt.last_partial_text() == "你好"


def test_interim_dedupe(monkeypatch):
    _fake_merge(monkeypatch)
    stt = DoubaoSTT(api_key="k", vad_=_FakeVad())

    async def scenario():
        stream = _DoubaoLiveStream(stt, conn_options=da.APIConnectOptions())
        stream._maybe_interim("a")
        stream._maybe_interim("a")   # 同文去重
        stream._maybe_interim("ab")
        got: list[str] = []
        try:
            while True:
                ev = await asyncio.wait_for(stream.__anext__(), timeout=0.2)
                got.append(ev.alternatives[0].text)
        except (asyncio.TimeoutError, StopAsyncIteration):
            pass
        await stream.aclose()
        return got

    assert asyncio.run(scenario()) == ["a", "ab"]


def test_stable_prefix_listener_fed_common_prefix(monkeypatch):
    """A 线对偶件（2026-10-07）：interim 更新喂「与上一 interim 的公共前缀」给
    PrefillSpeculator 挂点——首个 interim 无前文不喂、后续喂公共头；回调炸
    不影响 interim 事件流。"""
    _fake_merge(monkeypatch)
    stt = DoubaoSTT(api_key="k", vad_=_FakeVad())
    assert stt.stable_prefix_listener is None  # 类级缺省=零行为

    fed: list[str] = []

    def _cb(text: str) -> None:
        fed.append(text)

    async def scenario(cb, boom: bool):
        stt.stable_prefix_listener = cb
        stream = _DoubaoLiveStream(stt, conn_options=da.APIConnectOptions())
        stream._maybe_interim("我个单号")
        stream._maybe_interim("我个单号系三七")     # 公共前缀=「我个单号」
        stream._maybe_interim("我个单号系三八七九")  # 公共前缀=「我个单号系三」
        stream._maybe_interim("我个单号系三八七九")  # 同文去重=不喂
        got: list[str] = []
        try:
            while True:
                ev = await asyncio.wait_for(stream.__anext__(), timeout=0.2)
                got.append(ev.alternatives[0].text)
        except (asyncio.TimeoutError, StopAsyncIteration):
            pass
        await stream.aclose()
        return got

    def _boom(text: str) -> None:
        raise RuntimeError("listener boom")

    # 正常臂：喂公共前缀，interim 事件照发
    assert asyncio.run(scenario(_cb, False)) == ["我个单号", "我个单号系三七", "我个单号系三八七九"]
    assert fed == ["我个单号", "我个单号系三"]
    # 炸弹臂：回调抛异常不吞 interim 事件流（interim 状态在流实例上，新流重放）
    fed.clear()
    assert len(asyncio.run(scenario(_boom, True))) == 3


def test_live_closing_say_suppresses(monkeypatch):
    _fake_merge(monkeypatch)
    _make_connect(monkeypatch, connect_fail=False, full_text="不该出现")
    vad = _FakeVad()
    stt = DoubaoSTT(api_key="k", vad_=vad)
    stt.set_closing_say(True)

    names = [n for n, _ in asyncio.run(_drive_segment(stt, vad))]
    assert "START_OF_SPEECH" in names
    assert "END_OF_SPEECH" not in names and "FINAL_TRANSCRIPT" not in names


def test_live_retry_on_connect_fail(monkeypatch):
    _fake_merge(monkeypatch)
    calls = _make_connect(monkeypatch, connect_fail=True, full_text="重试全文")
    vad = _FakeVad()
    stt = DoubaoSTT(api_key="k", vad_=vad)

    events = asyncio.run(_drive_segment(stt, vad))
    finals = [t for n, t in events if n == "FINAL_TRANSCRIPT"]
    assert finals == ["重试全文"]
    assert calls["n"] == 2  # live 连接失败 → 整段单发重试一次


# ---- W5 声纹门（BOK_SPEAKER_LOCK，默认关；长 PCM 段驱动器）--------------------

from agent_runtime.speaker_lock import SpeakerLock  # noqa: E402
from test_speaker_lock import _A_SEGS, _B_SEGS  # noqa: E402


async def _drive_segment_pcm(stt: DoubaoSTT, vad: _FakeVad, pcm: bytes) -> list[tuple[str, str]]:
    """与 _drive_segment 同骨架，但喂整段长 PCM（preroll 0.5s + 0.2s 窗逐块）——
    判定窗 1.5s 需要真长段才能跨越。"""
    stream = _DoubaoLiveStream(stt, conn_options=da.APIConnectOptions())
    for _ in range(200):
        await asyncio.sleep(0.005)
        if vad.streams:
            break
    vs = vad.streams[-1]  # 同一 FakeVad 多次驱动：取本流（基类每流新 vad.stream()）
    got: list[tuple[str, str]] = []

    async def _collect() -> None:
        try:
            while True:
                ev = await asyncio.wait_for(stream.__anext__(), timeout=0.5)
                text = ev.alternatives[0].text if getattr(ev, "alternatives", None) else ""
                got.append((ev.type.name, text))
        except (asyncio.TimeoutError, StopAsyncIteration):
            return

    collector = asyncio.create_task(_collect())
    preroll_n = int(0.5 * 16000) * 2
    vs.q.put_nowait(
        _ev(da.vad.VADEventType.START_OF_SPEECH, frames=[types.SimpleNamespace(data=pcm[:preroll_n])])
    )
    await asyncio.sleep(0.02)
    rest = pcm[preroll_n:]
    for i in range(0, len(rest), 6400):
        vs.q.put_nowait(
            _ev(da.vad.VADEventType.INFERENCE_DONE, frames=[types.SimpleNamespace(data=rest[i : i + 6400])])
        )
        await asyncio.sleep(0.01)
    vs.q.put_nowait(_ev(da.vad.VADEventType.END_OF_SPEECH))
    await asyncio.sleep(0.4)
    stream._input_ch.close()
    try:
        await asyncio.wait_for(collector, 3)
    except asyncio.TimeoutError:
        collector.cancel()
    await stream.aclose()
    return got


def _audio_frames_sent(ws) -> int:
    return sum(1 for f in ws.sent if parse_server_frame(f)["type"] == MSG_AUDIO_ONLY_REQ
               and (parse_server_frame(f)["seq"] or 0) > 0)


def test_speaker_lock_gate_drops_cross_voice_segment(monkeypatch, capsys):
    """登记 A 后 B 段：判定窗处停发音频、EOS/FINAL/interim 全抑制、关会话止损；
    紧接的 A 段照常出 FINAL（门不误杀通话对象）。"""
    _fake_merge(monkeypatch)
    monkeypatch.setenv("BOK_SPEAKER_LOCK", "1")
    calls = _make_connect(monkeypatch, connect_fail=False, full_text="键盘幻听不该成轮")
    vad = _FakeVad()
    lock = SpeakerLock()
    assert lock.enroll(_A_SEGS[0])
    stt = DoubaoSTT(api_key="k", vad_=vad, speaker_lock=lock)

    events = asyncio.run(_drive_segment_pcm(stt, vad, _B_SEGS[1]))
    names = [n for n, _ in events]
    # EOS/FINAL 全被吞；INTERIM 只允许判定窗前的泄漏（1.5s 前本就放行）
    assert names[0] == "START_OF_SPEECH", names
    assert "END_OF_SPEECH" not in names and "FINAL_TRANSCRIPT" not in names, names
    assert names.count("INTERIM_TRANSCRIPT") <= 1, names
    assert "SPEAKER_LOCK_DROP" in capsys.readouterr().out
    # 判定窗后停发：音频包 ≤ 窗内量（1.5s≈8 包 200ms），远小于整段 1.6s+preroll
    assert _audio_frames_sent(calls["ws"]) <= 8
    assert calls["ws"].closed is True  # 段末关会话止损

    # 同一把锁：A 段照常（hit，不误杀）
    events = asyncio.run(_drive_segment_pcm(stt, vad, _A_SEGS[2]))
    finals = [t for n, t in events if n == "FINAL_TRANSCRIPT"]
    assert finals == ["键盘幻听不该成轮"]  # fake 服务端文本（内容无关紧要，FINAL 要出）
    assert calls["ws"] is not None


def test_speaker_lock_enroll_on_first_confirmed_text(monkeypatch, capsys):
    """首段（未登记=放行）出 FINAL 后凭确证文本登记；随后异嗓音段被丢。"""
    _fake_merge(monkeypatch)
    monkeypatch.setenv("BOK_SPEAKER_LOCK", "1")
    _make_connect(monkeypatch, connect_fail=False, full_text="你好我系陈大文啊")
    vad = _FakeVad()
    lock = SpeakerLock()
    assert lock.enrolled is False
    stt = DoubaoSTT(api_key="k", vad_=vad, speaker_lock=lock)

    events = asyncio.run(_drive_segment_pcm(stt, vad, _A_SEGS[0]))
    assert [t for n, t in events if n == "FINAL_TRANSCRIPT"] == ["你好我系陈大文啊"]
    assert lock.enrolled is True
    assert "SPEAKER_LOCK_ENROLL" in capsys.readouterr().out

    events = asyncio.run(_drive_segment_pcm(stt, vad, _B_SEGS[2]))
    names = [n for n, _ in events]
    assert names[0] == "START_OF_SPEECH", names  # 已登记 → 异嗓音整段抑制
    assert "FINAL_TRANSCRIPT" not in names and "END_OF_SPEECH" not in names, names


def test_speaker_lock_env_off_byte_identical(monkeypatch, capsys):
    """总闸关（缺省）：同一把锁传入，异嗓音段照旧走全链路出 FINAL=零行为。"""
    _fake_merge(monkeypatch)
    monkeypatch.delenv("BOK_SPEAKER_LOCK", raising=False)
    _make_connect(monkeypatch, connect_fail=False, full_text="正常出稿")
    vad = _FakeVad()
    lock = SpeakerLock()
    lock.enroll(_A_SEGS[0])
    stt = DoubaoSTT(api_key="k", vad_=vad, speaker_lock=lock)

    events = asyncio.run(_drive_segment_pcm(stt, vad, _B_SEGS[0]))
    assert [t for n, t in events if n == "FINAL_TRANSCRIPT"] == ["正常出稿"]
    out = capsys.readouterr().out
    assert "SPEAKER_LOCK_DROP" not in out and "SPEAKER_LOCK_ENROLL" not in out


# ---- 尾部续说观察窗（B 线 3b，2026-10-08） ----


def _make_connect_replies(monkeypatch, *, replies: list, final_text: str):
    """fake connect：on_audio 逐包吐 replies、末包吐 definite final。"""
    calls = {"n": 0}

    def _connect(*a, **kw):
        calls["n"] += 1
        fake = _FakeWS(
            on_audio=[_srv_frame(_text_payload(t), seq=1) for t in replies],
            on_last=[_srv_frame(_text_payload(final_text, definite=True), last=True)],
        )
        calls["ws"] = fake

        async def _ret():
            return fake

        return _ret()

    monkeypatch.setattr("websockets.connect", _connect)
    return calls


async def _drive_utt(stt: DoubaoSTT, vad: _FakeVad, *, resume: bool, settle_s: float):
    """utt 档驱动：START→INFERENCE→END→(resume: START→INFERENCE→END)→等定稿。"""
    stream = _DoubaoLiveStream(stt, conn_options=da.APIConnectOptions())
    for _ in range(200):
        await asyncio.sleep(0.005)
        if vad.streams:
            break
    vs = vad.streams[0]
    got: list[tuple[str, str]] = []

    async def _collect() -> None:
        try:
            while True:
                ev = await asyncio.wait_for(stream.__anext__(), timeout=0.8)
                text = ev.alternatives[0].text if getattr(ev, "alternatives", None) else ""
                got.append((ev.type.name, text))
        except (asyncio.TimeoutError, StopAsyncIteration):
            return

    collector = asyncio.create_task(_collect())

    def _fire(ev):
        vs.q.put_nowait(ev)

    _fire(_ev(da.vad.VADEventType.START_OF_SPEECH,
              frames=[types.SimpleNamespace(data=b"\x11\x11" * 800)]))
    await asyncio.sleep(0.03)
    _fire(_ev(da.vad.VADEventType.INFERENCE_DONE,
              frames=[types.SimpleNamespace(data=b"\x22\x22" * 3200)]))
    await asyncio.sleep(0.03)
    _fire(_ev(da.vad.VADEventType.END_OF_SPEECH))
    if resume:
        await asyncio.sleep(0.06)  # < utt_wait：尾窗内续讲=并段
        _fire(_ev(da.vad.VADEventType.START_OF_SPEECH,
                  frames=[types.SimpleNamespace(data=b"\x33\x33" * 800)]))
        await asyncio.sleep(0.03)
        _fire(_ev(da.vad.VADEventType.INFERENCE_DONE,
                  frames=[types.SimpleNamespace(data=b"\x44\x44" * 3200)]))
        await asyncio.sleep(0.03)
        _fire(_ev(da.vad.VADEventType.END_OF_SPEECH))
    await asyncio.sleep(settle_s)
    stream._input_ch.close()
    try:
        await asyncio.wait_for(collector, 3)
    except asyncio.TimeoutError:
        collector.cancel()
    await stream.aclose()
    return got


def test_utt_merge_resume_merges_micro_pause(monkeypatch, capsys):
    """微停顿并段：尾窗内续讲→同会话续喂(连接数 1)、单条 FINAL=单调累积全文、
    DOUBAO_UTT_MERGE 观测行在场、DOUBAO_ASR_TEXT 带 utt_merges=1。"""
    _fake_merge(monkeypatch)
    calls = _make_connect_replies(
        monkeypatch, replies=["你好", "你好我想问下订单"], final_text="你好，我想问下订单。"
    )
    vad = _FakeVad()
    stt = DoubaoSTT(api_key="k", vad_=vad, utt_merge=True, utt_wait_s=0.25)

    events = asyncio.run(_drive_utt(stt, vad, resume=True, settle_s=0.5))
    finals = [t for n, t in events if n == "FINAL_TRANSCRIPT"]
    assert finals == ["你好，我想问下订单。"]  # 单条 FINAL=两段并一稿
    assert calls["n"] == 1  # 同一 WS 会话跨停顿存活（并段关键）
    out = capsys.readouterr().out
    assert "DOUBAO_UTT_MERGE resume" in out
    assert "utt_merges=1" in out


def test_utt_merge_timeout_finalizes(monkeypatch, capsys):
    """真停顿：尾窗睡满→负 seq 定稿单段 FINAL；无并段观测行。"""
    _fake_merge(monkeypatch)
    calls = _make_connect_replies(monkeypatch, replies=["你好"], final_text="你好，帮我查单。")
    vad = _FakeVad()
    stt = DoubaoSTT(api_key="k", vad_=vad, utt_merge=True, utt_wait_s=0.12)

    events = asyncio.run(_drive_utt(stt, vad, resume=False, settle_s=0.4))
    names = [n for n, _ in events]
    finals = [t for n, t in events if n == "FINAL_TRANSCRIPT"]
    assert finals == ["你好，帮我查单。"]
    assert names.index("END_OF_SPEECH") < names.index("FINAL_TRANSCRIPT")
    assert calls["n"] == 1
    out = capsys.readouterr().out
    assert "DOUBAO_UTT_MERGE" not in out and "utt_merges=0" in out


def test_utt_merge_default_off_is_legacy(monkeypatch):
    """缺省不传旗=A 线姿势逐字节旧路：utt 旗 False、END 立刻定稿（无尾窗延迟）。"""
    _fake_merge(monkeypatch)
    assert DoubaoSTT(api_key="k")._utt_merge is False
    assert DoubaoSTT(api_key="k", utt_merge=True)._utt_merge is True
    _make_connect_replies(monkeypatch, replies=["你好"], final_text="旧路定稿。")
    vad = _FakeVad()
    stt = DoubaoSTT(api_key="k", vad_=vad)
    events = asyncio.run(_drive_utt(stt, vad, resume=False, settle_s=0.4))
    finals = [t for n, t in events if n == "FINAL_TRANSCRIPT"]
    assert finals == ["旧路定稿。"]


def test_utt_merge_interpret_wiring_pins():
    """接线 pin：B 线装配传 utt_merge/utt_wait_s；A 线 agent.py 装配零触碰。"""
    import agent_runtime.interpret as interpret

    interp_src = (Path(__file__).resolve().parents[1] / "apps" / "agent" / "agent_runtime" / "interpret.py").read_text(encoding="utf-8")
    agent_src = (Path(__file__).resolve().parents[1] / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    assert "utt_merge=_interp_utt_merge_enabled()," in interp_src
    assert "utt_wait_s=_interp_utt_wait_s()," in interp_src
    assert "utt_merge" not in agent_src.split('stt_provider = DoubaoSTT(')[1].split(")")[0]
    # env 纯函数：缺省 0.2（W0 复核 0.45→0.2）/坏值回缺省/负钳 0/上限 3.0
    import os

    assert interpret._interp_utt_wait_s() == 0.2
    os.environ["BOK_INTERP_UTT_WAIT_S"] = "1.2"
    assert interpret._interp_utt_wait_s() == 1.2
    os.environ["BOK_INTERP_UTT_WAIT_S"] = "abc"
    assert interpret._interp_utt_wait_s() == 0.2
    os.environ["BOK_INTERP_UTT_WAIT_S"] = "-1"
    assert interpret._interp_utt_wait_s() == 0.0
    os.environ["BOK_INTERP_UTT_WAIT_S"] = "9"
    assert interpret._interp_utt_wait_s() == 3.0
    del os.environ["BOK_INTERP_UTT_WAIT_S"]
    assert interpret._interp_utt_merge_enabled() is True
    os.environ["BOK_INTERP_UTT_MERGE"] = "0"
    assert interpret._interp_utt_merge_enabled() is False
    del os.environ["BOK_INTERP_UTT_MERGE"]
