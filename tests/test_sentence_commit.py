"""P2 句级提交单测（无网络，fake sidecar）：turn_detection="stt" 的 STT 侧事件源。

_while-customer-still-speaking 按句提交：partial 稳定出现强句标点（。！？!?）→
FINAL_TRANSCRIPT(句子) + END_OF_SPEECH（官方 stt 模式契约 audio_recognition.py:
1292-1327：EOS 置 committed + _run_eou_detection(trigger="stt")，endpointing
min_delay 仍适用）→ 框架按句建轮，LLM+TTS 与客户说话重叠。

门（缺一不可，见 livekit_plugins._sentence_boundary）：
- 句段（自上个提交边界起）≥6 字；
- 无 ≥2 连续 ASCII 字母/数字 run（单号/WhatsApp 高危 → 留给停嘴整句兜底）；
- 跨窗稳定（上一窗同坐标已是同一句段，防滑窗跳变 flicker）；
- 1.5s 限速（连珠短句排队并入下一边界或停嘴兜底）。

kill-switch：QWEN3_ASR_SENTENCE_COMMIT=0（B 线 interpret 即此档）或
TURN_DETECTION≠stt → 不发任何句级事件，VAD 停嘴整段 FINAL 行为同旧。
"""

from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import bok  # noqa: E402
from livekit.agents.utils.aio.channel import ChanClosed, ChanEmpty  # noqa: E402

from agent_runtime.agent import (  # noqa: E402
    _endpointing_delays_from_env,
    _turn_detection_mode_from_env,
)
from agent_runtime.providers import livekit_plugins as lp  # noqa: E402
from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    LanguageState,
    Qwen3ASRSTT,
    _Qwen3ASRLiveStream,
    _has_latin_or_digit_run,
    sentence_commit_enabled,
)


class _FakeResp:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _FakeClient:
    """sidecar fake：/api/start 回固定 session_id；/api/chunk 按序回预设窗。"""

    windows: list[dict] = []

    def __init__(self, *a, **kw):
        self._i = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url: str, *a, **kw):
        if "finish" in url or "start" in url:
            if "start" in url:
                return _FakeResp({"session_id": "sid"})
            return _FakeResp(getattr(_FakeClient, "finish_body", {"text": "", "language": ""}))
        win = _FakeClient.windows[min(self._i, len(_FakeClient.windows) - 1)]
        self._i += 1
        return _FakeResp(win)


def _make_stream(anchor: str = "cantonese") -> _Qwen3ASRLiveStream:
    stt_inner = Qwen3ASRSTT(base_url="http://127.0.0.1:8787", language_state=LanguageState(lang=anchor))
    return _Qwen3ASRLiveStream(stt_inner, vad=object(), conn_options=lp.APIConnectOptions())


async def _drive_window(stream: _Qwen3ASRLiveStream, body: dict) -> list[str]:
    """喂一窗 partial 并返回此刻 channel 里的事件类型序列（INTERIM/FINAL/EOS…）。"""
    events = await _drive_window_events(stream, body)
    return [name for (name, _text) in events]


async def _drive_window_events(stream: _Qwen3ASRLiveStream, body: dict) -> list[tuple[str, str]]:
    """同 _drive_window，但返回 (类型名, 文本) 序列（断言 FINAL/INTERIM 装咩文本）。"""
    stream._session_id = "sid"
    stream._last_post = 0.0
    stream._pending = bytearray(16000 * 2)  # ≥0.6s PCM，过门槛
    _FakeClient.windows = [body]
    await stream._maybe_partial()
    got: list[tuple[str, str]] = []
    while True:
        try:
            ev = stream._event_ch.recv_nowait()
            got.append((ev.type.name, ev.alternatives[0].text if ev.alternatives else ""))
        except ChanEmpty:
            return got
        except ChanClosed:  # pragma: no cover - channel 提前关了
            return got


async def _close(stream: _Qwen3ASRLiveStream) -> None:
    stream._event_ch.close()
    try:
        await asyncio.wait_for(stream._metrics_task, 1)
    except Exception:  # noqa: BLE001 - 监视任务收尾失败不影响断言
        pass


def _default_gates(monkeypatch) -> None:
    """A 线默认门：句级提交开 + TURN_DETECTION=stt（unset → 默认）。"""
    monkeypatch.delenv("QWEN3_ASR_SENTENCE_COMMIT", raising=False)
    monkeypatch.delenv("TURN_DETECTION", raising=False)
    monkeypatch.setattr(lp, "httpx", types.SimpleNamespace(AsyncClient=_FakeClient))


def test_strong_sentence_commits_once(monkeypatch):
    """强标点句（≥6 字、无数字 run、跨窗稳定）→ FINAL+EOS 各一次；重复窗不再发。

    事件序（官方 stt 契约要求 FINAL 在前、EOS 在后，EOS 才有非空 _audio_transcript
    可 commit）：FINAL_TRANSCRIPT(句子) → END_OF_SPEECH → INTERIM(未提交剩余)。
    """
    _default_gates(monkeypatch)

    async def scenario():
        stream = _make_stream()
        try:
            ev1 = await _drive_window(stream, {"text": "唔該幫我查下張單", "language": "cantonese"})
            # 边界首现（上一窗冇呢个边界）→ 稳定性门拦住，唔提交
            ev2 = await _drive_window(
                stream, {"text": "唔該幫我查下張單。而家件貨喺邊度", "language": "cantonese"}
            )
            # 下一窗边界仍在同坐标 → 提交
            ev3 = await _drive_window(
                stream, {"text": "唔該幫我查下張單。而家件貨喺邊度呀", "language": "cantonese"}
            )
            # 同一窗文本再来（滑窗重绘同文）→ 去重，边界只发一次
            ev4 = await _drive_window(
                stream, {"text": "唔該幫我查下張單。而家件貨喺邊度呀", "language": "cantonese"}
            )
            return ev1, ev2, ev3, ev4, stream._committed_text, stream._commit_idx
        finally:
            await _close(stream)

    ev1, ev2, ev3, ev4, committed, idx = asyncio.run(scenario())
    assert ev1 == ["INTERIM_TRANSCRIPT"]
    # 边界首现唔提交，但稳定前缀（句号前）照旧发 PREFLIGHT（旧抢跑行为不变）
    assert ev2 == ["INTERIM_TRANSCRIPT", "PREFLIGHT_TRANSCRIPT"], f"边界首现唔可以提交(稳定性门): {ev2}"
    # 提交窗：FINAL+EOS 先发，PREFLIGHT 被跳过（文本已权威提交，省投机预算）
    assert ev3 == ["FINAL_TRANSCRIPT", "END_OF_SPEECH", "INTERIM_TRANSCRIPT"], ev3
    assert ev4 == [], f"同窗文本去重,边界只发一次: {ev4}"
    assert committed == "唔該幫我查下張單。"
    assert idx == len("唔該幫我查下張單。")


def test_sentence_final_carries_sentence_and_interim_carries_remainder(monkeypatch):
    """FINAL 只装句子；提交后 INTERIM/PREFLIGHT 换「未提交剩余」坐标系。"""
    _default_gates(monkeypatch)

    async def scenario():
        stream = _make_stream()
        events: list[tuple[str, str]] = []
        try:
            events += await _drive_window_events(
                stream, {"text": "唔該幫我查下張單。而家到咗邊度呀", "language": "cantonese"}
            )
            events += await _drive_window_events(
                stream, {"text": "唔該幫我查下張單。而家到咗邊度呀。仲有一件事", "language": "cantonese"}
            )
            return events
        finally:
            await _close(stream)

    events = asyncio.run(scenario())
    finals = [t for (name, t) in events if name == "FINAL_TRANSCRIPT"]
    interims = [t for (name, t) in events if name == "INTERIM_TRANSCRIPT"]
    assert finals == ["唔該幫我查下張單。"], f"FINAL 只装提交句: {finals}"
    # 提交后 INTERIM 只带剩余（唔再带已提交前缀——框架 _audio_transcript 已有）
    assert interims[-1] == "而家到咗邊度呀。仲有一件事", f"INTERIM 剩余坐标系: {interims}"


def test_short_sentences_queue_not_commit(monkeypatch):
    """短句（<6 字）唔提交：排队累积，并入下一个够长的边界。"""
    _default_gates(monkeypatch)

    async def scenario():
        stream = _make_stream()
        try:
            ev1 = await _drive_window(stream, {"text": "好。", "language": "cantonese"})
            ev2 = await _drive_window(stream, {"text": "好。唔該。", "language": "cantonese"})
            ev3 = await _drive_window(
                stream, {"text": "好。唔該。唔好意思啊", "language": "cantonese"}
            )
            # 累积到 ≥6 字且稳定 → 一次提交整段（「好。唔該。唔好意思啊。」）
            ev4 = await _drive_window(
                stream, {"text": "好。唔該。唔好意思啊。", "language": "cantonese"}
            )
            ev5 = await _drive_window(
                stream, {"text": "好。唔該。唔好意思啊。唔該晒", "language": "cantonese"}
            )
            return ev1, ev2, ev3, ev4, ev5, stream._committed_text
        finally:
            await _close(stream)

    ev1, ev2, ev3, ev4, ev5, committed = asyncio.run(scenario())
    assert ev1 == ["INTERIM_TRANSCRIPT"]
    assert ev2 == ["INTERIM_TRANSCRIPT"]
    assert ev3 == ["INTERIM_TRANSCRIPT"]
    assert ev4 == ["INTERIM_TRANSCRIPT", "PREFLIGHT_TRANSCRIPT"], f"上一窗冇同边界(唔稳定): {ev4}"
    assert ev5 == ["FINAL_TRANSCRIPT", "END_OF_SPEECH", "INTERIM_TRANSCRIPT"], ev5
    assert committed == "好。唔該。唔好意思啊。"


def test_digit_run_suppressed_until_vad_stop(monkeypatch):
    """单号/数字 run（≥2 连写）句永唔句级提交 → 留给停嘴整句兜底（WhatsApp 铁律）。"""
    _default_gates(monkeypatch)

    async def scenario():
        stream = _make_stream()
        try:
            ev1 = await _drive_window(stream, {"text": "我張單號係七八九零", "language": "cantonese"})
            ev2 = await _drive_window(
                stream, {"text": "我張單號係7890123。請幫我查下", "language": "cantonese"}
            )
            ev3 = await _drive_window(
                stream, {"text": "我張單號係7890123。請幫我查下。", "language": "cantonese"}
            )
            return ev1, ev2, ev3, stream._committed_text
        finally:
            await _close(stream)

    ev1, ev2, ev3, committed = asyncio.run(scenario())
    assert ev1 == ["INTERIM_TRANSCRIPT"]
    assert ev2 == ["INTERIM_TRANSCRIPT"], f"数字 run 句唔可以句级提交: {ev2}"
    # 数字 run 句稳定都唔提交；剩余文本照旧 INTERIM+PREFLIGHT（ speculation 照旧）
    assert ev3 == ["INTERIM_TRANSCRIPT", "PREFLIGHT_TRANSCRIPT"], f"数字 run 句稳定都唔提交: {ev3}"
    assert committed == "", "数字句唔准入已提交前缀"

    # 停嘴兜底：VAD-stop FINAL 走 _uncommitted——数字句随「未提交尾巴」一起发
    async def tail_scenario():
        stream = _make_stream()
        try:
            stream._committed_text = "唔該幫我查下張單。"
            stream._last_sentence = "唔該幫我查下張單。"
            tail = stream._uncommitted("唔該幫我查下張單。我張單號係7890123。")
            assert tail == "我張單號係7890123。", f"停嘴尾巴要带数字句: {tail!r}"
            # 全部已提交 → 尾巴空 → VAD-stop 分支 if payload: 直接跳过 FINAL（零重复）
            assert stream._uncommitted("唔該幫我查下張單。") == ""
            # 句级门关（B 线档）→ 原样返回 → 整段 FINAL 行为同旧
            monkeypatch.setenv("QWEN3_ASR_SENTENCE_COMMIT", "0")
            stream2 = _make_stream()
            try:
                assert stream2._uncommitted("唔該幫我查下張單。") == "唔該幫我查下張單。"
            finally:
                await _close(stream2)
        finally:
            await _close(stream)

    asyncio.run(tail_scenario())


def test_consecutive_sentences_rate_limited(monkeypatch):
    """1.5s 限速：连珠句第 2 句被压住（排队），窗口推进后照常提交。"""
    _default_gates(monkeypatch)

    async def scenario():
        stream = _make_stream()
        try:
            await _drive_window(stream, {"text": "唔該幫我查下張單。而家到咗邊度呀", "language": "cantonese"})
            ev2 = await _drive_window(
                stream, {"text": "唔該幫我查下張單。而家到咗邊度呀。仲有一件事", "language": "cantonese"}
            )
            # 第 2 个边界已稳定，但距上次提交 <1.5s → 只发 INTERIM
            ev3 = await _drive_window(
                stream, {"text": "唔該幫我查下張單。而家到咗邊度呀。仲有一件事。唔該晒", "language": "cantonese"}
            )
            stream._last_sentence_commit_at = 0.0  # 快进限速窗
            ev4 = await _drive_window(
                stream, {"text": "唔該幫我查下張單。而家到咗邊度呀。仲有一件事。唔該晒呀", "language": "cantonese"}
            )
            return ev2, ev3, ev4, stream._committed_text
        finally:
            await _close(stream)

    ev2, ev3, ev4, committed = asyncio.run(scenario())
    assert ev2 == ["FINAL_TRANSCRIPT", "END_OF_SPEECH", "INTERIM_TRANSCRIPT"], ev2
    # 限速窗内：第 2 句被压住；剩余文本增长照旧发 INTERIM+PREFLIGHT（_stable 已重置）
    assert ev3 == ["INTERIM_TRANSCRIPT", "PREFLIGHT_TRANSCRIPT"], f"限速窗内第 2 句唔可以连发: {ev3}"
    assert ev4 == ["FINAL_TRANSCRIPT", "END_OF_SPEECH", "INTERIM_TRANSCRIPT"], ev4
    assert committed == "唔該幫我查下張單。而家到咗邊度呀。"


def test_decimal_point_not_boundary(monkeypatch):
    """ASCII 句点夹数字（5.20）唔算边界；小数点唔劈句。"""
    _default_gates(monkeypatch)

    async def scenario():
        stream = _make_stream()
        try:
            prev = "寄出日期係5.20。請查收"
            # 若把「5.」当边界，句子会劈成「寄出日期係5.」+「20…」——点规则挡住；
            # 整段又带数字 run（20）→ 数字门拦 → None（留给停嘴整句兜底）
            assert stream._sentence_boundary(prev, prev) is None
            # 无数字 run 的正常句：小数点完整保留喺句内，边界喺句尾 。
            sent, idx = stream._sentence_boundary("而家寄3.5公斤。唔該晒", "而家寄3.5公斤。唔該晒")
            # 窗口末尾恰好断喺「寄3.」(点后无字符可判)：点前一字符係 ascii 数字
            # → 同样视为小数点唔系边界，等下个窗口到齐再定——免得半截数字句提交。
            assert stream._sentence_boundary("而家寄3.", "而家寄3.") is None
            return sent, idx
        finally:
            await _close(stream)

    sent, idx = asyncio.run(scenario())
    assert sent == "而家寄3.5公斤。" and idx == len(sent)


def test_env_off_no_sentence_events(monkeypatch):
    """QWEN3_ASR_SENTENCE_COMMIT=0（B 线档）→ 无任何句级事件，行为同旧。"""
    monkeypatch.setenv("QWEN3_ASR_SENTENCE_COMMIT", "0")
    monkeypatch.delenv("TURN_DETECTION", raising=False)
    monkeypatch.setattr(lp, "httpx", types.SimpleNamespace(AsyncClient=_FakeClient))

    async def scenario():
        stream = _make_stream()
        try:
            ev1 = await _drive_window(stream, {"text": "唔該幫我查下張單", "language": "cantonese"})
            ev2 = await _drive_window(
                stream, {"text": "唔該幫我查下張單。而家件貨喺邊度呀", "language": "cantonese"}
            )
            ev3 = await _drive_window(
                stream, {"text": "唔該幫我查下張單。而家件貨喺邊度呀。唔該", "language": "cantonese"}
            )
            return ev1, ev2, ev3, stream._committed_text
        finally:
            await _close(stream)

    ev1, ev2, ev3, committed = asyncio.run(scenario())
    assert ev1 == ["INTERIM_TRANSCRIPT"]
    assert ev2 == ["INTERIM_TRANSCRIPT", "PREFLIGHT_TRANSCRIPT"]
    assert ev3 == ["INTERIM_TRANSCRIPT", "PREFLIGHT_TRANSCRIPT"], f"门关时不得发 FINAL/EOS: {ev3}"
    assert committed == ""


def test_turn_detection_gate_and_agent_defaults(monkeypatch):
    """总门双 env 同源：SENTENCE_COMMIT=1 且 TURN_DETECTION=stt（双方默认同值）。

    TURN_DETECTION kill-switch（空串/vad）必须连句级 FINAL 一起关——非 stt 模式
    框架忽略 STT EOS，中途句子 FINAL 会叠进停嘴整段 → 重复转写。
    """
    monkeypatch.delenv("QWEN3_ASR_SENTENCE_COMMIT", raising=False)
    monkeypatch.delenv("TURN_DETECTION", raising=False)
    assert sentence_commit_enabled() is True
    assert _turn_detection_mode_from_env() == "stt"  # agent.py 同一默认

    monkeypatch.setenv("TURN_DETECTION", "")
    assert sentence_commit_enabled() is False
    assert _turn_detection_mode_from_env() == ""  # kill-switch → EOT 旧默认

    monkeypatch.setenv("TURN_DETECTION", "vad")
    assert sentence_commit_enabled() is False

    monkeypatch.setenv("TURN_DETECTION", "stt")
    monkeypatch.setenv("QWEN3_ASR_SENTENCE_COMMIT", "0")
    assert sentence_commit_enabled() is False

    # endpointing 配对默认（官方最快档）+ env 逐项可回退
    monkeypatch.delenv("ENDPOINT_MIN_DELAY", raising=False)
    monkeypatch.delenv("ENDPOINT_MAX_DELAY", raising=False)
    assert _endpointing_delays_from_env() == (0.25, 0.6)
    monkeypatch.setenv("ENDPOINT_MIN_DELAY", "0.35")
    monkeypatch.setenv("ENDPOINT_MAX_DELAY", "1.2")
    assert _endpointing_delays_from_env() == (0.35, 1.2)


def test_turn_detection_kill_switch_disables_emission(monkeypatch):
    """TURN_DETECTION=（空串，EOT kill-switch）→ 句边界窗零 FINAL/EOS。"""
    monkeypatch.setenv("QWEN3_ASR_SENTENCE_COMMIT", "1")
    monkeypatch.setenv("TURN_DETECTION", "")
    monkeypatch.setattr(lp, "httpx", types.SimpleNamespace(AsyncClient=_FakeClient))

    async def scenario():
        stream = _make_stream()
        try:
            await _drive_window(stream, {"text": "唔該幫我查下張單。而家到咗邊度呀", "language": "cantonese"})
            ev2 = await _drive_window(
                stream, {"text": "唔該幫我查下張單。而家到咗邊度呀。仲有一件事", "language": "cantonese"}
            )
            return ev2, stream._committed_text
        finally:
            await _close(stream)

    ev2, committed = asyncio.run(scenario())
    assert ev2 == ["INTERIM_TRANSCRIPT", "PREFLIGHT_TRANSCRIPT"], f"kill-switch 下不得句级提交: {ev2}"
    assert committed == ""


def test_interp_env_sentence_commit_off():
    """B 线 interpret worker env 显式关句级提交（本轮 B 线不切 stt）。"""
    env = bok._interp_env({"PASSTHROUGH": "1"})
    assert env["QWEN3_ASR_SENTENCE_COMMIT"] == "0"
    assert env["PASSTHROUGH"] == "1"  # agent_env 透传不受影响


def test_latin_digit_run_helper():
    """run ≥2 判定：号码/英文连写拦，单字母夹句唔拦。"""
    assert _has_latin_or_digit_run("單號7890123多謝") is True
    assert _has_latin_or_digit_run("check 個 status") is True
    assert _has_latin_or_digit_run("OK") is True
    assert _has_latin_or_digit_run("而家寄3.5公斤") is False
    assert _has_latin_or_digit_run("A 嘅貨") is False
    assert _has_latin_or_digit_run("純中文句子") is False


def test_vad_stop_tail_end_to_end(monkeypatch):
    """VAD 停嘴分支端到端（fake VAD + fake sidecar 跑真 _run）：

    - 已提交句子唔随整段重复，FINAL 只装未提交尾巴（数字句随尾巴兜底）；
    - 尾巴空（整段已按句提交）→ FINAL 唔发，EOS 照常收尾；
    - 无提交（旧路径）→ 整段 FINAL 行为同旧。
    """

    class _FakeVADStream:
        def __init__(self, stream_ref):
            self._ref = stream_ref
            self._n = 0

        def flush(self):
            pass

        def end_input(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            self._n += 1
            if self._n == 1:
                return types.SimpleNamespace(
                    type=lp.vad.VADEventType.START_OF_SPEECH,
                    speech_duration=0.0,
                    silence_duration=0.0,
                    inference_duration=0.0,
                    probability=1.0,
                    speaking=True,
                    frames=[],
                )
            if self._n == 2:
                return types.SimpleNamespace(
                    type=lp.vad.VADEventType.END_OF_SPEECH,
                    speech_duration=2.0,
                    silence_duration=0.2,
                    inference_duration=0.05,
                    probability=0.0,
                    speaking=False,
                    frames=[],
                )
            self._ref._input_ch.close()  # 收 _forward_input，让 gather 归位
            raise StopAsyncIteration

    class _FakeVAD:
        def __init__(self, stream_ref):
            self._ref = stream_ref

        def stream(self):
            return _FakeVADStream(self._ref)

    monkeypatch.delenv("QWEN3_ASR_SENTENCE_COMMIT", raising=False)
    monkeypatch.delenv("TURN_DETECTION", raising=False)
    monkeypatch.setattr(lp, "httpx", types.SimpleNamespace(AsyncClient=_FakeClient))

    async def scenario(committed_prefix: str, finish_text: str):
        _FakeClient.finish_body = {"text": finish_text, "language": "cantonese"}
        stream = _make_stream()
        # metrics 监视任务会跟测试抢 _event_ch（tee pull 型）——取消掉，事件全留
        # 给断言；_run 走完后手动收尾。
        stream._metrics_task.cancel()
        stream._vad = _FakeVAD(stream)
        if committed_prefix:
            # 模拟说话中途已有句级提交（prefix 已 FINAL+EOS 发过）
            stream._committed_text = committed_prefix
            stream._last_sentence = committed_prefix
            stream._commit_idx = len(committed_prefix)
        stream._pending = bytearray(b"\x00\x00")
        try:
            await asyncio.wait_for(stream._task, 2)
        finally:
            names: list[str] = []
            while True:
                try:
                    names.append(stream._event_ch.recv_nowait().type.name)
                except (ChanEmpty, ChanClosed):
                    break
            stream._event_ch.close()
            await asyncio.gather(stream._metrics_task, return_exceptions=True)
        return names

    # 尾巴非空：VAD-stop FINAL 只装未提交尾巴（数字句随尾巴兜底，零重复）
    names = asyncio.run(scenario("唔該幫我查下張單。", "唔該幫我查下張單。我張單號係7890123。"))
    assert names == ["START_OF_SPEECH", "END_OF_SPEECH", "FINAL_TRANSCRIPT"], names

    # 尾巴空（整段已按句提交）：FINAL 唔发（零重复），EOS 照常收尾
    names = asyncio.run(scenario("唔該幫我查下張單。", "唔該幫我查下張單。"))
    assert names == ["START_OF_SPEECH", "END_OF_SPEECH"], names

    # 无提交（句级门关/无边界）：整段 FINAL 行为同旧
    names = asyncio.run(scenario("", "我張單號係7890123。"))
    assert names == ["START_OF_SPEECH", "END_OF_SPEECH", "FINAL_TRANSCRIPT"], names
