"""MiniMax 云 ASR provider 单测 + B 线四语车道路由（零网络：fake httpx）。

覆盖：请求/响应 round-trip（multipart 形状 + Bearer/language 头 + SSE delta 串接）/
噪声行跳过 / 流早断重试 / HTTP 错误空串兜底 / SSRF 护栏 / 总闸 / capabilities
offline 形状 / 48k→16k 重采样 / interpret 装配点四语判定矩阵。真端点冒烟另见
scripts/probes/probe_cloud_asr.py --engine minimax 与 reports/cloud-asr/
（2026-10-03 三语定案表 + 2026-10-06 W2c 四语实测）。
"""

from __future__ import annotations

import asyncio
import io
import sys
import types
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.interpret import _minimax_asr_lane  # noqa: E402
from agent_runtime.providers import minimax_asr as mm  # noqa: E402
from agent_runtime.providers.minimax_asr import (  # noqa: E402
    LANG_TAGS,
    MINIMAX_ASR_4LANG,
    MINIMAX_STT_URL_DEFAULT,
    MiniMaxSTT,
    _https_url_ok,
    _pcm_to_wav,
    _wrap_wav,
    minimax_asr_enabled,
)


class _LangState:
    def __init__(self, lang: str = "de"):
        self.lang = lang


# ---- fake httpx（provider 模块内 httpx 符号整体替换） -----------------------
class _AsyncCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeResp:
    def __init__(self, lines: list[str], *, status_error: Exception | None = None):
        self._lines = lines
        self._status_error = status_error

    def raise_for_status(self) -> None:
        if self._status_error is not None:
            raise self._status_error

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


def _install_fake_httpx(monkeypatch, responses: list[_FakeResp]) -> list[dict]:
    """替换 provider 模块可见的 httpx.AsyncClient；返回逐次调用记录。"""
    calls: list[dict] = []

    class _FakeClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, headers=None, data=None, files=None):
            calls.append(
                {"method": method, "url": url, "headers": headers, "data": data, "files": files}
            )
            resp = responses.pop(0) if responses else _FakeResp([])
            return _AsyncCtx(resp)

    monkeypatch.setattr(mm, "httpx", types.SimpleNamespace(AsyncClient=_FakeClient))
    return calls


def _sse(*chunks: str, finish: bool = True) -> _FakeResp:
    lines = [f'data:{{"delta":"{c}"}}' for c in chunks]
    if finish:
        lines.append('data:{"finish":true,"duration":1.2}')
    return _FakeResp(lines)


def _wav_bytes_from_files(files: dict) -> bytes:
    return files["file"][1]


def _wav_info(wav_bytes: bytes) -> dict:
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        return {
            "rate": w.getframerate(),
            "width": w.getsampwidth(),
            "channels": w.getnchannels(),
            "frames": w.getnframes(),
        }


# ---- 护栏 / 总闸 / 纯函数 ----------------------------------------------------
def test_https_url_guard():
    assert _https_url_ok("https://api.minimax.cn/v1/speech_to_text")
    assert not _https_url_ok("http://api.minimax.cn/x")      # 明文拒
    assert not _https_url_ok("https://127.0.0.1/x")
    assert not _https_url_ok("https://localhost/x")
    assert not _https_url_ok("https://10.1.2.3/x")
    assert not _https_url_ok("https://192.168.1.5/x")
    assert not _https_url_ok("https://foo.internal/x")
    assert not _https_url_ok("wss://api.minimax.cn/x")        # 本 provider 只发 https
    assert not _https_url_ok("")


def test_kill_switch(monkeypatch):
    monkeypatch.delenv("BOK_MINIMAX_ASR", raising=False)
    assert minimax_asr_enabled() is True
    monkeypatch.setenv("BOK_MINIMAX_ASR", "0")
    assert minimax_asr_enabled() is False


def test_four_lang_lane_tables():
    assert MINIMAX_ASR_4LANG == ("de", "fr", "ja", "pt")
    assert set(LANG_TAGS) == set(MINIMAX_ASR_4LANG)
    assert LANG_TAGS["ja"] == "ja" and LANG_TAGS["pt"] == "pt"


def test_capabilities_offline_shape():
    stt = MiniMaxSTT(api_key="k", lang_tag="de")
    caps = stt._capabilities
    assert caps.streaming is False
    assert caps.interim_results is False
    assert caps.offline_recognize is True
    assert stt.model == "minimax-asr" and stt.provider == "minimax"


def test_wrap_wav_roundtrip():
    pcm = b"\x01\x02" * 3200  # 3200 帧
    info = _wav_info(_wrap_wav(pcm, 16000, 1))
    assert info == {"rate": 16000, "width": 2, "channels": 1, "frames": 3200}


def test_pcm_to_wav_resamples_48k():
    """非 16k 输入（房间轨常见 48k）重采样到 16k mono——定案表 24/24 分数只在
    16k wav 形状上成立。时长守恒（±10% 容差）。"""
    in_samples = 48000  # 1s @48k
    wav_bytes = _pcm_to_wav(b"\x00\x01" * in_samples, 48000, 1)
    info = _wav_info(wav_bytes)
    assert info["rate"] == 16000 and info["channels"] == 1 and info["width"] == 2
    out_dur = info["frames"] / 16000
    assert abs(out_dur - 1.0) < 0.1


# ---- 整段单发（_transcribe_once） --------------------------------------------
def test_transcribe_once_request_and_response(monkeypatch, capsys):
    calls = _install_fake_httpx(monkeypatch, [_sse("Guten", " Tag")])
    stt = MiniMaxSTT(api_key="sk-mm", lang_tag="de", language_state=_LangState("de"))

    text = asyncio.run(stt._transcribe_once(b"\x01\x02" * 3200))
    assert text == "Guten Tag"
    call = calls[0]
    assert call["method"] == "POST" and call["url"] == MINIMAX_STT_URL_DEFAULT
    assert call["headers"]["Authorization"] == "Bearer sk-mm"
    assert call["headers"]["language"] == "de"
    assert call["data"] == {"model": "asr-1.0", "stream": "true"}
    name, payload, ctype = call["files"]["file"]
    assert name == "segment.wav" and ctype == "audio/wav"
    assert _wav_info(payload)["rate"] == 16000
    out = capsys.readouterr().out
    assert "MINIMAX_ASR_TEXT 'Guten Tag' de" in out and "(cloud)" in out
    # 凭据绝不外泄到任何打印面
    assert "sk-mm" not in out


def test_transcribe_once_skips_noise_lines(monkeypatch):
    resp = _FakeResp([
        ":keepalive",
        "event: delta",
        'data:{"delta":"Bonjour"}',
        "data:not-json",
        'data:{"delta":""}',
        'data:{"finish":true}',
    ])
    _install_fake_httpx(monkeypatch, [resp])
    stt = MiniMaxSTT(api_key="k", lang_tag="fr", language_state=_LangState("fr"))
    assert asyncio.run(stt._transcribe_once(b"\x01\x02" * 1600)) == "Bonjour"


def test_transcribe_once_stream_ended_early_retries_then_partial(monkeypatch, capsys):
    """流早断（未见 finish）：有预算就整段重试一次；仍早断则返回半截稿。"""
    calls = _install_fake_httpx(monkeypatch, [_sse("Hallo", finish=False), _sse("Hallo Welt")])
    stt = MiniMaxSTT(api_key="k", lang_tag="de", language_state=_LangState("de"))
    assert asyncio.run(stt._transcribe_once(b"\x01\x02" * 1600)) == "Hallo Welt"
    assert len(calls) == 2

    calls2 = _install_fake_httpx(
        monkeypatch, [_sse("Teil", finish=False), _sse("Teil", finish=False)]
    )
    text = asyncio.run(stt._transcribe_once(b"\x01\x02" * 1600))
    assert text == "Teil" and len(calls2) == 2
    out = capsys.readouterr().out
    assert "MINIMAX_ASR_STREAM_ENDED_EARLY" in out


def test_transcribe_once_http_error_empty_after_retry(monkeypatch, capsys):
    calls = _install_fake_httpx(
        monkeypatch,
        [
            _FakeResp([], status_error=RuntimeError("http 500")),
            _FakeResp([], status_error=RuntimeError("http 500")),
        ],
    )
    stt = MiniMaxSTT(api_key="k", lang_tag="ja", language_state=_LangState("ja"))
    assert asyncio.run(stt._transcribe_once(b"\x01\x02" * 1600)) == ""
    assert len(calls) == 2  # 异常单段重试一次，之后空串兜底不悬挂轮次
    out = capsys.readouterr().out
    assert "MINIMAX_ASR_ERROR" in out
    assert "k" != "" and "Bearer" not in out  # 打印面零凭据


def test_transcribe_once_url_rejected_no_call(monkeypatch, capsys):
    calls = _install_fake_httpx(monkeypatch, [_sse("x")])
    stt = MiniMaxSTT(api_key="k", url="http://127.0.0.1:9999/v1/speech_to_text")
    assert asyncio.run(stt._transcribe_once(b"\x01\x02" * 1600)) == ""
    assert calls == []
    assert "MINIMAX_ASR_URL_REJECTED" in capsys.readouterr().out


def test_transcribe_once_empty_pcm_no_call(monkeypatch):
    calls = _install_fake_httpx(monkeypatch, [_sse("x")])
    stt = MiniMaxSTT(api_key="k")
    assert asyncio.run(stt._transcribe_once(b"")) == ""
    assert calls == []


# ---- recognize（offline 接口面） --------------------------------------------
def test_recognize_impl_final_event(monkeypatch):
    _install_fake_httpx(monkeypatch, [_sse("Guten", " Morgen")])
    stt = MiniMaxSTT(api_key="k", lang_tag="de", language_state=_LangState("de"))
    buf = types.SimpleNamespace(data=b"\x01\x02" * 3200, sample_rate=16000, num_channels=1)
    ev = asyncio.run(stt._recognize_impl(buf))
    assert ev.type.name == "FINAL_TRANSCRIPT"
    assert len(ev.alternatives) == 1
    assert ev.alternatives[0].text == "Guten Morgen"
    assert ev.alternatives[0].language == "de"


def test_recognize_impl_empty_buffer_empty_alternatives(monkeypatch):
    _install_fake_httpx(monkeypatch, [_sse("x")])
    stt = MiniMaxSTT(api_key="k", lang_tag="de", language_state=_LangState("de"))
    buf = types.SimpleNamespace(data=b"", sample_rate=16000, num_channels=1)
    ev = asyncio.run(stt._recognize_impl(buf))
    assert ev.type.name == "FINAL_TRANSCRIPT" and ev.alternatives == []


def test_recognize_impl_48k_resampled(monkeypatch):
    """48k 段（StreamAdapter 不重采样直通）经 provider 重采样后请求形状仍 16k。"""
    calls = _install_fake_httpx(monkeypatch, [_sse("Ok")])
    stt = MiniMaxSTT(api_key="k", lang_tag="pt", language_state=_LangState("pt"))
    buf = types.SimpleNamespace(
        data=b"\x00\x01" * 9600, sample_rate=48000, num_channels=1  # 0.2s @48k
    )
    ev = asyncio.run(stt._recognize_impl(buf))
    assert ev.alternatives[0].text == "Ok"
    assert _wav_info(_wav_bytes_from_files(calls[0]["files"]))["rate"] == 16000


# ---- interpret 装配点判定矩阵 ------------------------------------------------
def test_lane_routing_matrix(monkeypatch):
    monkeypatch.delenv("BOK_MINIMAX_ASR", raising=False)
    # 四语 + 总闸开 + 凭据在场 → 上车
    for lang in MINIMAX_ASR_4LANG:
        assert _minimax_asr_lane(lang, "sk-mm") is True
    # 三语 → 既有装配链逐字节（不上车）
    for lang in ("zh", "cantonese", "en"):
        assert _minimax_asr_lane(lang, "sk-mm") is False
    # 总闸关 → 逐字节回旧装配链
    monkeypatch.setenv("BOK_MINIMAX_ASR", "0")
    assert _minimax_asr_lane("de", "sk-mm") is False
    # 无凭据 → 不上车（绝不哑门：缺 key 时豆包/本地链照旧可服务）
    monkeypatch.delenv("BOK_MINIMAX_ASR", raising=False)
    assert _minimax_asr_lane("de", "") is False
    assert _minimax_asr_lane("de", "   ") is False


# ---- 接线源级 pin（装配点/立法双表,同 test_interp_spec_mt 姿势） --------------
def test_wiring_source_pins():
    interp_src = (ROOT / "apps" / "agent" / "agent_runtime" / "interpret.py").read_text(
        encoding="utf-8"
    )
    env_src = (ROOT / "tools" / "bokctl" / "env.py").read_text(encoding="utf-8")
    # 装配点：官方 StreamAdapter 包 offline provider + 观测行 + 车道在豆包分支之前
    assert "stt_provider = lk_stt.StreamAdapter(" in interp_src
    assert "lang_tag=LANG_TAGS[source_lang]" in interp_src
    assert "[interp] asr=minimax (4lang lane) lang=" in interp_src
    assert interp_src.index("_minimax_asr_lane(source_lang, _minimax_key)") < interp_src.index(
        "_asr_provider_name in (\"doubao\", \"doubao_asr\")"
    )
    # 立法双面：_FORWARD_ENV 表 + _interp_env B 线透传白名单同键
    assert env_src.count('"BOK_MINIMAX_ASR"') >= 2
