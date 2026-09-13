"""罐头音频四修复回归（2026-09-11 晚四症状）。

覆盖：
1. W1 垫话「机器人声」根因=BackgroundAudioPlayer 内部固定 48k 混音器无重采样,
   24k 垫话帧直进=2 倍速升调——播放前必须重采样到 48k(资产层+缓存层两层)。
2. W2 zh/粤 1.2 语速:MINIMAX_SPEED 从全档 env 旋钮改为语言感知默认(zh/cantonese
   1.2,en 1.0;env 显式设置=全语言 kill-switch 覆盖);cache_key 条件含 speed
   (speed≠1.0 才进 key——旧 1.0 条目键不变零失效,zh/粤 1.2 新键自然重物化)。
3. W3 turns 账本 gen=filler 接线:垫话开火即上报(账本可见性)。
4. W4 tts-pregen _cp_get 重试:RemoteDisconnected 瞬断(实测 4 连崩)不再一把打死。
"""

from __future__ import annotations

import array
import asyncio
import json
import sys
import wave
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.fillers import (  # noqa: E402
    BACKGROUND_PLAYER_RATE,
    FillerDirector,
    resample_pcm,
)
from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    LanguageState,
    MiniMaxTTS,
    minimax_speed_for,
)
from agent_runtime.tts_cache import TtsAudioCache, cache_key  # noqa: E402


# ---- W1: 重采样 ----


def test_player_rate_is_48k():
    """SDK BackgroundAudioPlayer 内部 AudioSource/AudioMixer 固定 48k(1.8.0 源码)。"""
    assert BACKGROUND_PLAYER_RATE == 48000


def test_resample_24k_to_48k_doubles_samples():
    pcm = array.array("h", [1000, 2000, 3000, 4000]).tobytes()
    out = resample_pcm(pcm, 24000, 48000)
    assert len(out) == 16  # 8 样本 → 16 样本(s16le)
    samples = array.array("h")
    samples.frombytes(out)
    assert samples[0] == 1000 and samples[2] == 2000  # 偶数位=原样本(2× 上采样)
    assert samples[1] == 1500  # 奇数位=线性插值


def test_resample_identity_when_rates_equal():
    pcm = array.array("h", [5, -5, 7]).tobytes()
    assert resample_pcm(pcm, 24000, 24000) == pcm
    assert resample_pcm(b"", 24000, 48000) == b""


def test_resample_downsample_preserves_duration():
    """任意比率按时长守恒(样本数≈n*to/from)。"""
    pcm = array.array("h", [100] * 4800).tobytes()  # 0.2s @24k
    out = resample_pcm(pcm, 24000, 16000)
    samples = array.array("h")
    samples.frombytes(out)
    assert abs(len(samples) - 3200) <= 2  # 0.2s @16k


# ---- W2: 语言感知语速 + 缓存键 ----


def test_minimax_speed_language_aware(monkeypatch):
    monkeypatch.delenv("MINIMAX_SPEED", raising=False)
    assert minimax_speed_for("zh") == 1.2
    assert minimax_speed_for("cantonese") == 1.2
    assert minimax_speed_for("en") == 1.0
    assert minimax_speed_for("") == 1.0
    # env 显式设置=全语言 kill-switch 覆盖(旧部署回退通道保留)
    monkeypatch.setenv("MINIMAX_SPEED", "1.0")
    assert minimax_speed_for("zh") == 1.0


def test_minimax_voice_setting_speed_by_language(monkeypatch):
    monkeypatch.delenv("MINIMAX_SPEED", raising=False)
    zh = MiniMaxTTS(voice="v", language_state=LanguageState(lang="zh"))
    en = MiniMaxTTS(voice="v", language_state=LanguageState(lang="en"))
    assert zh._ws_voice_setting("v")["speed"] == 1.2
    assert en._ws_voice_setting("v")["speed"] == 1.0
    assert zh.resolved_speed() == 1.2


def test_cache_key_speed_suffix_backward_compatible():
    base = dict(voice_id="voice-a", model="speech-2.8-hd", sample_rate=24000)
    # speed=1.0 与旧键完全一致:存量 1.0 条目(en/旧 zh)零失效继续命中
    assert cache_key("你好", speed=1.0, **base) == cache_key("你好", **base)
    # speed=1.2 产生新键:zh/粤旧 1.0 音频不会被错配,自然触发重物化
    assert cache_key("你好", speed=1.2, **base) != cache_key("你好", **base)


def test_cache_lookup_and_store_carry_speed(tmp_path):
    cache = TtsAudioCache(tmp_path, sample_rate=24000)
    assert cache.lookup("你好", voice="v", model="m", speed=1.2) is None
    key = cache.key_for("你好", voice="v", model="m", speed=1.2)
    assert cache.store(key, b"\x01\x00" * 100, text="你好", voice="v", model="m", speed=1.2)
    assert cache.lookup("你好", voice="v", model="m", speed=1.2) is not None
    # 同文本 1.0 键查不到 1.2 条目
    assert cache.lookup("你好", voice="v", model="m", speed=1.0) is None
    meta = json.loads((tmp_path / f"{key}.json").read_text(encoding="utf-8"))
    assert meta.get("speed") == 1.2


# ---- W3: 垫话 turns 账本上报 ----


class _FakePlayHandle:
    def __init__(self):
        self.stopped = False
        self._done = asyncio.get_event_loop_policy().new_event_loop().create_future() if False else None

    def done(self):
        return True

    def stop(self):
        self.stopped = True

    async def wait_for_playout(self):
        return None


class _FakePlayer:
    def __init__(self):
        self.plays: list[object] = []

    def play(self, source):
        self.plays.append(source)
        return _FakePlayHandle()


class _FakeSession:
    agent_state = "thinking"


def _make_assets(tmp_path: Path) -> Path:
    assets = tmp_path / "fillers"
    assets.mkdir(parents=True, exist_ok=True)
    entries = []
    for i, text in enumerate(["好的，您稍等。", "马上帮您查一下。"], 1):
        name = f"zh-{i:02d}.wav"
        with wave.open(str(assets / name), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes((1000).to_bytes(2, "little", signed=True) * 480)
        entries.append({"text": text, "file": name, "dur_s": 1.0})
    (assets / "manifest.json").write_text(
        json.dumps({"zh": entries}, ensure_ascii=False), encoding="utf-8"
    )
    return assets


def test_filler_director_reports_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("BOK_FILLER_DELAY_MS", "1")
    reported: list[tuple[str, float]] = []

    def _report(line: str, dur: float) -> None:
        reported.append((line, dur))

    director = FillerDirector(
        _FakeSession(),
        lang_resolver=lambda: "zh",
        player=_FakePlayer(),
        guards=lambda: False,
        assets_dir=_make_assets(tmp_path),
        report=_report,
    )

    async def _go():
        director.arm()
        await asyncio.sleep(0.05)

    asyncio.run(asyncio.wait_for(_go(), 5.0))
    assert len(reported) == 1, reported
    line, dur = reported[0]
    assert line in ("好的，您稍等。", "马上帮您查一下。")
    # 账本时长=实际播放 PCM 时长(480 样本@24k=0.02s),唔係 manifest dur_s(1.0)
    # ——人设物化版时长≠资产层(1.11s→2.72s 实证同根因,call-55c6fb1f)。
    assert dur == 0.02


def test_filler_report_strips_pause_marks(tmp_path, monkeypatch):
    """<#0.3#> 停顿标记是合成指令:字幕/账本展示面必须剥掉,缓存键仍用原文。"""
    monkeypatch.setenv("BOK_FILLER_DELAY_MS", "1")
    reported: list[tuple[str, float]] = []
    captioned: list[str] = []

    assets = _make_assets(tmp_path)
    manifest = json.loads((assets / "manifest.json").read_text(encoding="utf-8"))
    # 两条都带标记:director 随机挑句,断言面恒非空
    manifest["zh"][0]["text"] = "嗯<#0.2#>您稍等，我看一下。"
    manifest["zh"][1]["text"] = "好<#0.3#>我帮您查一下啊。"
    (assets / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    director = FillerDirector(
        _FakeSession(),
        lang_resolver=lambda: "zh",
        player=_FakePlayer(),
        guards=lambda: False,
        assets_dir=assets,
        report=lambda line, dur: reported.append((line, dur)),
        caption=captioned.append,
    )

    async def _go():
        director.arm()
        await asyncio.sleep(0.05)

    asyncio.run(asyncio.wait_for(_go(), 5.0))
    assert len(reported) == 1 and len(captioned) == 1, (reported, captioned)
    for line, _dur in reported:
        assert "<#" not in line
    assert reported[0][0] in ("嗯您稍等，我看一下。", "好我帮您查一下啊。")
    for cap in captioned:
        assert "<#" not in cap


def test_filler_director_no_report_callback_is_noop(tmp_path, monkeypatch):
    """report 可选:不传=零行为变化(存量调用方零破坏)。"""
    monkeypatch.setenv("BOK_FILLER_DELAY_MS", "1")
    player = _FakePlayer()
    director = FillerDirector(
        _FakeSession(),
        lang_resolver=lambda: "zh",
        player=player,
        guards=lambda: False,
        assets_dir=_make_assets(tmp_path),
    )

    async def _go():
        director.arm()
        await asyncio.sleep(0.05)

    asyncio.run(asyncio.wait_for(_go(), 5.0))
    assert len(player.plays) == 1


# ---- W4: pregen _cp_get 重试 ----


def _load_pregen():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pregen_tts_under_test", ROOT / "scripts" / "pregen_tts.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_pregen_cp_get_retries_transient_disconnect(monkeypatch):
    mod = _load_pregen()
    calls = {"n": 0}

    def _flaky_open(req, timeout=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise ConnectionResetError("remote closed")
        return _Resp(b'{"ok": true}')

    monkeypatch.setattr(mod, "_CP_RETRY_DELAYS", (0.0, 0.0))
    out = mod._cp_get("http://x", "/api/settings", "", opener=_flaky_open)
    assert out == {"ok": True}
    assert calls["n"] == 3


def test_pregen_cp_get_raises_after_all_attempts_fail(monkeypatch):
    mod = _load_pregen()
    calls = {"n": 0}

    def _always_fail(req, timeout=None):
        calls["n"] += 1
        raise ConnectionResetError("remote closed")

    monkeypatch.setattr(mod, "_CP_RETRY_DELAYS", (0.0, 0.0))
    try:
        mod._cp_get("http://x", "/api/settings", "", opener=_always_fail)
    except ConnectionResetError:
        pass
    else:
        raise AssertionError("should raise after all attempts")
    assert calls["n"] == 3
