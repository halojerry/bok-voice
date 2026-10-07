"""W5 声纹门·本地线（_Qwen3ASRLiveStream）接线单测（2026-10-07 接线波）。

真 ``_run`` 端到端（fake VAD + fake sidecar，镜像 test_late_final_guard 骨架）：
- 登记后异嗓音段：判定窗处停喂 sidecar、不出 partial、EOS/FINAL 整段抑制、
  ``QWEN3_ASR_SPEAKER_LOCK_SUPPRESS`` 打点；
- 首段（未登记=放行）finish 出 FINAL 后凭确证文本登记（``SPEAKER_LOCK_ENROLL``）；
- 总闸关（缺省）：同一把锁传入，异嗓音段照旧全链路出 FINAL（字节零漂移）。

合成素材与断言阈值复用 test_speaker_lock 标定（A=男声域/B=女声域，同人/异人
相似度分布钉在那边）。豆包线对偶件见 test_doubao_asr.py 尾部三测。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.providers import livekit_plugins as lp  # noqa: E402
from agent_runtime.providers.livekit_plugins import LanguageState, Qwen3ASRSTT  # noqa: E402
from agent_runtime.speaker_lock import SpeakerLock  # noqa: E402
from livekit.agents.utils.aio.channel import ChanClosed, ChanEmpty  # noqa: E402
from test_speaker_lock import _A_SEGS, _B_SEGS  # noqa: E402

# ---- fake sidecar（httpx 面最小桩；镜像 test_late_final_guard._FakeClient）----

class _FakeResp:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _FakeClient:
    finish_body: dict = {"text": "", "language": ""}
    posts: list[str] = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url: str, *a, **kw):
        _FakeClient.posts.append(url)
        if "start" in url:
            return _FakeResp({"session_id": "sid"})
        if "finish" in url:
            return _FakeResp(dict(_FakeClient.finish_body))
        return _FakeResp({"text": "", "language": ""})


def _fresh_gates(monkeypatch) -> None:
    for key in ("BOK_SPEAKER_LOCK", "QWEN3_ASR_SENTENCE_COMMIT", "TURN_DETECTION"):
        monkeypatch.delenv(key, raising=False)
    _FakeClient.posts.clear()
    monkeypatch.setattr(lp, "httpx", types.SimpleNamespace(AsyncClient=_FakeClient))
    monkeypatch.setattr(
        lp,
        "utils",
        types.SimpleNamespace(
            merge_frames=lambda frames: types.SimpleNamespace(
                data=b"".join(getattr(f, "data", b"") for f in frames)
            )
        ),
    )


def _make_stream(lock) -> lp._Qwen3ASRLiveStream:
    stt_inner = Qwen3ASRSTT(
        base_url="http://127.0.0.1:8787", language_state=LanguageState(lang="zh")
    )
    return lp._Qwen3ASRLiveStream(
        stt_inner, vad=object(), conn_options=lp.APIConnectOptions(), speaker_lock=lock
    )


async def _close(stream) -> None:
    stream._event_ch.close()
    await asyncio.gather(stream._metrics_task, return_exceptions=True)


async def _drive(stream, pcm: bytes) -> list[str]:
    """START(preroll 0.5s)→INFERENCE_DONE(0.2s 窗逐块)→END 跑真 _run；返回事件名。"""

    class _FakeVADStream:
        def __init__(self, ref):
            self._ref = ref
            self._n = 0
            self._queue: asyncio.Queue = asyncio.Queue()

        def flush(self):
            pass

        def end_input(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            self._n += 1
            if self._n == 1:
                preroll = int(0.5 * 16000) * 2
                return types.SimpleNamespace(
                    type=lp.vad.VADEventType.START_OF_SPEECH,
                    speech_duration=0.0,
                    silence_duration=0.0,
                    inference_duration=0.0,
                    probability=1.0,
                    speaking=True,
                    frames=[types.SimpleNamespace(data=pcm[:preroll])],
                )
            if self._n == 2:
                self._schedule_windows(pcm[int(0.5 * 16000) * 2 :])
                return await self._queue.get()
            ev = await self._queue.get()
            if ev is None:
                self._ref._input_ch.close()
                raise StopAsyncIteration
            return ev

        def _schedule_windows(self, rest: bytes) -> None:
            async def _pump() -> None:
                for i in range(0, len(rest), 6400):
                    await asyncio.sleep(0.005)
                    self._queue.put_nowait(
                        types.SimpleNamespace(
                            type=lp.vad.VADEventType.INFERENCE_DONE,
                            frames=[types.SimpleNamespace(data=rest[i : i + 6400])],
                        )
                    )
                await asyncio.sleep(0.005)
                self._queue.put_nowait(
                    types.SimpleNamespace(
                        type=lp.vad.VADEventType.END_OF_SPEECH,
                        speech_duration=2.0,
                        silence_duration=0.3,
                        inference_duration=0.05,
                        probability=0.0,
                        speaking=False,
                        frames=[],
                    )
                )
                self._queue.put_nowait(None)

            asyncio.get_running_loop().create_task(_pump())

    class _FakeVAD:
        def __init__(self, ref):
            self._ref = ref

        def stream(self):
            return _FakeVADStream(self._ref)

    stream._metrics_task.cancel()
    stream._vad = _FakeVAD(stream)
    try:
        await asyncio.wait_for(stream._task, 5)
    finally:
        names: list[str] = []
        while True:
            try:
                names.append(stream._event_ch.recv_nowait().type.name)
            except (ChanEmpty, ChanClosed):
                break
    return names


def test_local_lane_gate_drops_cross_voice_segment(monkeypatch, capsys):
    """登记 A 后 B 段：EOS/FINAL 整段抑制、窗后不再喂 sidecar、打点两行齐。"""
    _fresh_gates(monkeypatch)
    monkeypatch.setenv("BOK_SPEAKER_LOCK", "1")

    async def scenario():
        lock = SpeakerLock()
        assert lock.enroll(_A_SEGS[0])
        stream = _make_stream(lock)
        try:
            return await _drive(stream, _B_SEGS[1])
        finally:
            await _close(stream)

    names = asyncio.run(scenario())
    assert names == ["START_OF_SPEECH"], names
    out = capsys.readouterr().out
    assert "SPEAKER_LOCK_DROP" in out
    assert "QWEN3_ASR_SPEAKER_LOCK_SUPPRESS src=segment_eos" in out
    # 判定窗（1.5s≈preroll 0.5s+5 窗）之后不再喂 sidecar：chunk 帖 ≤ 窗内量
    chunk_posts = sum(1 for u in _FakeClient.posts if "chunk" in u)
    assert chunk_posts <= 6, chunk_posts


def test_local_lane_enroll_on_first_confirmed_text(monkeypatch, capsys):
    """首段（未登记=放行）finish 出 FINAL 后凭确证文本登记。"""
    _fresh_gates(monkeypatch)
    monkeypatch.setenv("BOK_SPEAKER_LOCK", "1")
    _FakeClient.finish_body = {"text": "你好我是陈大文啊", "language": "zh"}

    async def scenario():
        lock = SpeakerLock()
        assert lock.enrolled is False
        stream = _make_stream(lock)
        try:
            return await _drive(stream, _A_SEGS[0]), lock
        finally:
            await _close(stream)

    names, lock = asyncio.run(scenario())
    assert names[-1] == "FINAL_TRANSCRIPT", names
    assert lock.enrolled is True
    assert "SPEAKER_LOCK_ENROLL" in capsys.readouterr().out


def test_local_lane_env_off_byte_identical(monkeypatch, capsys):
    """总闸关（缺省）：同一把锁传入，异嗓音段照旧全链路出 FINAL=零行为。"""
    _fresh_gates(monkeypatch)
    _FakeClient.finish_body = {"text": "正常出稿", "language": "zh"}

    async def scenario():
        lock = SpeakerLock()
        lock.enroll(_A_SEGS[0])
        stream = _make_stream(lock)
        try:
            return await _drive(stream, _B_SEGS[0])
        finally:
            await _close(stream)

    names = asyncio.run(scenario())
    assert names[-1] == "FINAL_TRANSCRIPT", names
    out = capsys.readouterr().out
    assert "SPEAKER_LOCK_DROP" not in out
    assert "SPEAKER_LOCK_ENROLL" not in out
    assert "SPEAKER_LOCK_SUPPRESS" not in out


def test_grey_recheck_veto_wiring_source_pins():
    """源级 pin（2026-10-07 灰区段末复核）：两车道共三处消费 segment_end 的
    FINAL 否决权（豆包 END 1 处+本地线停嘴/hold-flush 2 处）——灰区复核判丢
    时 FINAL 必须被吞（幻听轮不成）。"""
    lp_src = (ROOT / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py").read_text(
        encoding="utf-8"
    )
    da_src = (ROOT / "apps" / "agent" / "agent_runtime" / "providers" / "doubao_asr.py").read_text(
        encoding="utf-8"
    )
    assert lp_src.count("if not self._gate.segment_end(text):") == 2, "本地线停嘴+hold-flush 两处否决"
    assert da_src.count("if not self._gate.segment_end(text):") == 1, "豆包 END 一处否决"
