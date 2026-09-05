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


# ---- P1.5 FIX 1: SSL_CERT_FILE bake（.venv312 OpenSSL 无默认 CA 束 → MiniMax WSS 炸）


def _fake_venv_with_certifi(tmp_path: Path) -> Path:
    """搭一个假 venv 目录结构：<venv>/bin/python + site-packages/certifi/cacert.pem。"""
    pem = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "certifi" / "cacert.pem"
    pem.parent.mkdir(parents=True)
    pem.write_text("-----BEGIN CERTIFICATE-----\nFAKE\n")
    (tmp_path / "venv" / "bin").mkdir(parents=True, exist_ok=True)
    (tmp_path / "venv" / "bin" / "python").write_text("")
    return tmp_path / "venv" / "bin" / "python"


def test_certifi_bundle_path_probe(tmp_path) -> None:
    """路径探测：目标解释器 site-packages 里的 cacert.pem 被找到。"""
    py = _fake_venv_with_certifi(tmp_path)
    assert bok._certifi_bundle(py) == str(
        tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "certifi" / "cacert.pem"
    )


def test_certifi_bundle_missing_returns_empty(tmp_path, monkeypatch) -> None:
    """目标 venv 无 certifi + 当前解释器也无 certifi（sys.modules 塞 None）→ 返回 ""。"""
    empty_py = tmp_path / "venv" / "bin" / "python"
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    monkeypatch.setitem(sys.modules, "certifi", None)  # import certifi → ImportError
    assert bok._certifi_bundle(empty_py) == ""


def test_worker_env_bakes_ssl_cert_file(tmp_path, monkeypatch) -> None:
    """worker env builder（agent 生产档 + CP + serve 同源）自动注入 SSL_CERT_FILE，
    仅当 env 未设且 cacert.pem 在盘——干净 shell 起 worker 唔再炸 MiniMax TLS。"""
    py = _fake_venv_with_certifi(tmp_path)
    monkeypatch.setattr(bok, "repo_python", lambda: py)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    expected = str(tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "certifi" / "cacert.pem")
    assert bok._agent_prod_env()["SSL_CERT_FILE"] == expected
    assert bok._control_plane_env("/tmp/bok_voice.db")["SSL_CERT_FILE"] == expected


def test_worker_env_ssl_cert_file_user_override_respected(monkeypatch, tmp_path) -> None:
    """显式设置的 SSL_CERT_FILE 永远优先，bake 唔覆盖。"""
    monkeypatch.setenv("SSL_CERT_FILE", "/custom/cacert.pem")
    monkeypatch.setattr(bok, "repo_python", lambda: _fake_venv_with_certifi(tmp_path))
    assert bok._agent_prod_env()["SSL_CERT_FILE"] == "/custom/cacert.pem"


def test_worker_env_no_certifi_left_unset(tmp_path, monkeypatch) -> None:
    """certifi 找唔到（假 venv 空 + 当前解释器无 certifi）→ 唔注入，env 保持原样。"""
    monkeypatch.setattr(bok, "repo_python", lambda: tmp_path / "venv" / "bin" / "python")
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    monkeypatch.setitem(sys.modules, "certifi", None)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    env = bok._agent_prod_env()
    assert "SSL_CERT_FILE" not in env
