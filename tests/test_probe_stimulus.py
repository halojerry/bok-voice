"""探针客户话音单点开关（scripts/probe_stimulus.py）离线测试(2026-09-21)。

全离线、零网络：local 档用假 httpx.Client 捕获「本该发出的请求」，
钉死 URL/payload/sample_rate 与历史逐字节一致；cloud 档替换 _mm_pcm，
断言委托参数。另附一条结构测试：已改脚本不再各自内联 8788 的 POST。
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import probe_stimulus  # noqa: E402


# --------------------------------------------------------------------------- #
# 1) 选择器真值表
# --------------------------------------------------------------------------- #

def test_backend_default_is_local():
    assert probe_stimulus.resolve_stimulus_backend({}) == "local"


def test_backend_cloud():
    assert probe_stimulus.resolve_stimulus_backend({probe_stimulus.ENV_SWITCH: "cloud"}) == "cloud"


@pytest.mark.parametrize(
    "value",
    ["", "   ", "local", "LOCAL", "1", "yes", "true", "cloudy", "gpu", "云", "nonsense"],
)
def test_backend_unknown_falls_back_to_local(value):
    assert probe_stimulus.resolve_stimulus_backend({probe_stimulus.ENV_SWITCH: value}) == "local"


def test_backend_missing_key_is_local():
    # 键不存在（而非空串）也是 local。
    assert probe_stimulus.resolve_stimulus_backend({"OTHER": "cloud"}) == "local"


@pytest.mark.parametrize("value", ["cloud", "Cloud", "CLOUD", "  cloud  "])
def test_backend_cloud_is_case_and_space_insensitive(value):
    assert probe_stimulus.resolve_stimulus_backend({probe_stimulus.ENV_SWITCH: value}) == "cloud"


# --------------------------------------------------------------------------- #
# 2) local 档：假 httpx.Client 捕获请求，钉死历史 payload
# --------------------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.raised = False

    def raise_for_status(self) -> None:
        self.raised = True


class _FakeClient:
    """记录最近一次 post 的 url/json/timeout，返回固定 PCM。"""

    last: dict = {}

    def __init__(self, timeout=None) -> None:
        self.timeout = timeout

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def post(self, url, json=None):
        _FakeClient.last = {"url": url, "json": json, "timeout": self.timeout}
        return _FakeResponse(b"FAKE-PCM")


@pytest.fixture()
def fake_local(monkeypatch):
    _FakeClient.last = {}
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    # 保证环境不残留 cloud 开关，走 local 默认。
    monkeypatch.delenv(probe_stimulus.ENV_SWITCH, raising=False)
    monkeypatch.delenv("TTS_URL", raising=False)
    return _FakeClient


def test_local_default_request_is_byte_identical(fake_local):
    out = probe_stimulus.stimulus_pcm("我件貨爛咗", "cantonese")
    assert out == b"FAKE-PCM"
    sent = fake_local.last
    # 历史各脚本一字不差：同一 URL、同一 payload 键序、同一默认音色、16k、timeout=60
    assert sent["url"] == "http://127.0.0.1:8788/v1/audio/speech"
    assert sent["json"] == {
        "input": "我件貨爛咗",
        "language": "cantonese",
        "voice": "Vivian",
        "sample_rate": 16000,
    }
    assert list(sent["json"].keys()) == ["input", "language", "voice", "sample_rate"]
    assert sent["timeout"] == 60


def test_local_honours_tts_url_env(fake_local, monkeypatch):
    monkeypatch.setenv("TTS_URL", "http://127.0.0.1:9999")
    probe_stimulus.stimulus_pcm("hi", "en")
    assert fake_local.last["url"] == "http://127.0.0.1:9999/v1/audio/speech"


def test_local_explicit_tts_url_wins_over_env(fake_local, monkeypatch):
    monkeypatch.setenv("TTS_URL", "http://127.0.0.1:9999")
    probe_stimulus.stimulus_pcm("hi", "en", tts_url="http://127.0.0.1:8788")
    assert fake_local.last["url"] == "http://127.0.0.1:8788/v1/audio/speech"


def test_local_voice_and_sample_rate_passthrough(fake_local):
    # mock_callee / probe_vad_head_syllable 姿势：透传脚本级音色与采样率。
    probe_stimulus.stimulus_pcm("你好", "zh", voice="Chinese_wenrounvxing", sample_rate=8000)
    sent = fake_local.last
    assert sent["json"]["voice"] == "Chinese_wenrounvxing"
    assert sent["json"]["sample_rate"] == 8000


def test_local_timeout_passthrough(fake_local):
    probe_stimulus.stimulus_pcm("你好", "zh", timeout=120.0)
    assert fake_local.last["timeout"] == 120.0


# --------------------------------------------------------------------------- #
# 3) cloud 档：委托 _mm_pcm，同参；不发本地请求
# --------------------------------------------------------------------------- #

def test_cloud_delegates_to_mm_pcm(monkeypatch, fake_local):
    calls: list[tuple] = []

    def fake_mm(text, lang):
        calls.append((text, lang))
        return b"CLOUD-PCM"

    monkeypatch.setattr(probe_stimulus, "_mm_pcm", fake_mm)
    monkeypatch.setenv(probe_stimulus.ENV_SWITCH, "cloud")
    out = probe_stimulus.stimulus_pcm("拼多多", "cantonese")
    assert out == b"CLOUD-PCM"
    assert calls == [("拼多多", "cantonese")]
    # cloud 档不碰本地 sidecar。
    assert fake_local.last == {}


# --------------------------------------------------------------------------- #
# 4) 结构测试：转换后的脚本不再各自内联 8788 的 POST
# --------------------------------------------------------------------------- #

CONVERTED = [
    "e2e_real_customer.py",
    "e2e_barge_in.py",
    "e2e_edge_cases.py",
    "e2e_trilingual_livekit.py",
    "mock_callee.py",
    "probe_brand_words.py",
    "probe_hotword_ab.py",
    "probe_interp_continuous.py",
    "probe_vad_head_syllable.py",
]


@pytest.mark.parametrize("name", CONVERTED)
def test_converted_scripts_have_no_inline_sidecar_post(name):
    src = (ROOT / "scripts" / name).read_text(encoding="utf-8")
    assert "/v1/audio/speech" not in src, f"{name} still inlines the sidecar POST"
    assert "stimulus_pcm" in src, f"{name} does not delegate to stimulus_pcm"


@pytest.mark.parametrize("name", ["measure_latency.py", "smoke_sidecars.py"])
def test_deliberately_unconverted_scripts_keep_inline_post(name):
    # 这两个脚本走 24k/streaming 是 sidecar 自身测量/自测，刻意保留内联路径。
    src = (ROOT / "scripts" / name).read_text(encoding="utf-8")
    assert "/v1/audio/speech" in src
