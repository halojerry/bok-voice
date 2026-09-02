from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import bok  # noqa: E402


def test_control_plane_env_includes_livekit_credentials() -> None:
    """打包/开发模式下 control-plane 必须拿到 LiveKit 凭据，
    否则 /api/token 会走 sha256 假 token，A 线 UI 永远接通失败。"""
    env = bok._control_plane_env("/tmp/bok_voice.db")
    assert env["LIVEKIT_URL"] == "ws://127.0.0.1:7880"
    assert env["LIVEKIT_API_KEY"] == "devkey"
    assert env["LIVEKIT_API_SECRET"] == "devsecret"
    assert env["DATABASE_URL"] == "sqlite:////tmp/bok_voice.db"
    assert env["VAULT_ROOT"]
    assert env["BOK_SERVICE"] == "control-plane"


def test_control_plane_env_honors_environment_override(monkeypatch) -> None:
    monkeypatch.setenv("LIVEKIT_URL", "ws://127.0.0.1:7881")
    monkeypatch.setenv("LIVEKIT_API_KEY", "custom-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "custom-secret")
    env = bok._control_plane_env("/tmp/bok_voice.db")
    assert env["LIVEKIT_URL"] == "ws://127.0.0.1:7881"
    assert env["LIVEKIT_API_KEY"] == "custom-key"
    assert env["LIVEKIT_API_SECRET"] == "custom-secret"
