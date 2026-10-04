from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import bok  # noqa: E402
from _bokpatch import patch_bok  # noqa: E402


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


# ---- I1（2026-10-03）：9B 前门闸 :1238 —— 消费口 URL 与代理拉起 ----


def test_settle_gate_url_follows_proxy_switch(monkeypatch) -> None:
    """queue 拓扑开=:1238 前门;关=裸 :1237（旧形状逐字节）。"""
    monkeypatch.setenv("BOK_LLM_QUEUE_PROXY", "1")
    assert bok._settle_gate_url() == "http://127.0.0.1:1238/v1"
    monkeypatch.setenv("BOK_LLM_QUEUE_PROXY", "0")
    assert bok._settle_gate_url() == "http://127.0.0.1:1237/v1"


def test_start_settle_proxy_lifecycle(monkeypatch, tmp_path) -> None:
    """拉起/幂等/开关三态：upstream=:1237、port=1238、pid/log 命名齐。"""
    spawned: list[tuple] = []
    monkeypatch.setenv("BOK_LLM_QUEUE_PROXY", "1")
    patch_bok(monkeypatch, "repo_python", lambda: "/usr/bin/python3")
    patch_bok(monkeypatch, "healthy", lambda p: False)
    patch_bok(
        monkeypatch, "_start_proc",
        lambda argv, pid, log, env=None: spawned.append((argv, pid, log, env)),
    )
    assert bok.servers._start_settle_proxy(tmp_path, tmp_path) is True
    assert len(spawned) == 1
    argv, pid, log, env = spawned[0]
    assert "queue_proxy.py" in " ".join(str(x) for x in argv)
    assert str(pid).endswith("settle-proxy.pid")
    assert str(log).endswith("settle-proxy.log")
    assert env["BOK_LLM_QUEUE_PORT"] == "1238"
    assert env["BOK_LLM_QUEUE_UPSTREAM"] == "http://127.0.0.1:1237"
    # 已健康=幂等跳过（healthy 早退路径同款）
    spawned.clear()
    patch_bok(monkeypatch, "healthy", lambda p: True)
    assert bok.servers._start_settle_proxy(tmp_path, tmp_path) is True
    assert spawned == []
    # queue 关=不起
    monkeypatch.setenv("BOK_LLM_QUEUE_PROXY", "0")
    patch_bok(monkeypatch, "healthy", lambda p: False)
    assert bok.servers._start_settle_proxy(tmp_path, tmp_path) is False
    assert spawned == []


def test_worker_env_bakes_ssl_cert_file(tmp_path, monkeypatch) -> None:
    """worker env builder（agent 生产档 + CP + serve 同源）自动注入 SSL_CERT_FILE，
    仅当 env 未设且 cacert.pem 在盘——干净 shell 起 worker 唔再炸 MiniMax TLS。"""
    py = _fake_venv_with_certifi(tmp_path)
    patch_bok(monkeypatch, "repo_python", lambda: py)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    expected = str(tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "certifi" / "cacert.pem")
    assert bok._agent_prod_env()["SSL_CERT_FILE"] == expected
    assert bok._control_plane_env("/tmp/bok_voice.db")["SSL_CERT_FILE"] == expected


def test_worker_env_ssl_cert_file_user_override_respected(monkeypatch, tmp_path) -> None:
    """显式设置的 SSL_CERT_FILE 永远优先，bake 唔覆盖。"""
    monkeypatch.setenv("SSL_CERT_FILE", "/custom/cacert.pem")
    patch_bok(monkeypatch, "repo_python", lambda: _fake_venv_with_certifi(tmp_path))
    assert bok._agent_prod_env()["SSL_CERT_FILE"] == "/custom/cacert.pem"


def test_worker_env_no_certifi_left_unset(tmp_path, monkeypatch) -> None:
    """certifi 找唔到（假 venv 空 + 当前解释器无 certifi）→ 唔注入，env 保持原样。"""
    patch_bok(monkeypatch, "repo_python", lambda: tmp_path / "venv" / "bin" / "python")
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    monkeypatch.setitem(sys.modules, "certifi", None)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    env = bok._agent_prod_env()
    assert "SSL_CERT_FILE" not in env


# ---- 2026-10-02 编排审计第二波 · CP env 面收编（prod 封闭白名单显式透传）


# 收编键 → 代表值（每组至少一键；键面完整清单见 tools/bok.py::_control_plane_env
# 的「CP env 面收编」注释块）。prod（launchd/schtasks）封闭 env 面收不到这些键
# = CP 读到的全是缺省/空；本地 dev 靠 _start_proc merge 掩盖。
_CP_ENV_COERCED_KEYS: tuple[tuple[str, str], ...] = (
    # ① 认证三键（auth.py / main 启动闸）
    ("BOK_AUTH_REQUIRED", "1"),
    ("BOK_JWT_SECRET", "test-jwt-secret-dummy"),
    ("BOK_CP_TOKEN", "test-cp-token"),
    # ② ops（日志/CORS/root 种子/SIP/公开地址）
    ("BOK_LOG_LEVEL", "DEBUG"),
    ("BOK_CORS_ORIGINS", "http://localhost:3000,https://cp.example.com"),
    ("BOK_ROOT_USERNAME", "opsroot"),
    ("BOK_ROOT_PASSWORD", "ops-root-pw"),
    ("BOK_SIP_MODE", "real"),
    ("BOK_CP_PUBLIC_URL", "https://cp.example.com"),
    # ③ node/static（节点制品/日志 TTL/静态 UI/app-data）
    ("BOK_NODE_ARTIFACTS_DIR", "/srv/bok/downloads"),
    ("BOK_NODE_LOG_TTL_DAYS", "7"),
    ("BOK_WEB_STATIC_DIR", "/srv/bok/web-out"),
    ("BOK_APP_DATA", "/srv/bok/data"),
    # ④ settle 闲时轮（poll/wait 两窗）
    ("BOK_SETTLE_IDLE_POLL_S", "5"),
    ("BOK_SETTLE_IDLE_WAIT_S", "120"),
    # ⑤ pregen/qa/embed
    ("BOK_PERSONA_AUTO_PREGEN", "0"),
    ("BOK_TTS_CACHE_DIR", "/srv/bok/tts-cache"),
    ("BOK_QA_CLUSTER_MODEL", "/models/qwen3-4b"),
    ("BOK_QA_DIGEST_INTERVAL_S", "300"),
    ("BOK_EMBED_BASE_URL", "http://127.0.0.1:8789"),
    # ⑥ MiniMax（CP 侧 TTS 设置回落 + 克隆端点域）
    ("MINIMAX_API_KEY", "sk-test-minimax"),
    ("MINIMAX_BASE_URL", "https://api.minimax.io/v1"),
    ("MINIMAX_REGION", "intl"),
    ("MINIMAX_MODEL", "speech-2.8-hd"),
    # ⑦ ops 端点覆盖（非默认拓扑 ASR/TTS/laya/csc）
    ("BOK_LAYA_URL", "http://127.0.0.1:8791"),
    ("BOK_CSC_URL", "http://127.0.0.1:8792"),
    ("QWEN3_ASR_BASE_URL", "http://127.0.0.1:8787"),
    ("QWEN3_TTS_BASE_URL", "http://127.0.0.1:8788"),
)


def test_control_plane_env_carries_audited_keys(monkeypatch) -> None:
    """显式设了的收编键必须原样下发（prod 单元 env 白名单）。"""
    for key, value in _CP_ENV_COERCED_KEYS:
        monkeypatch.setenv(key, value)
    env = bok._control_plane_env("/tmp/bok_voice.db")
    missing = [key for key, _v in _CP_ENV_COERCED_KEYS if env.get(key) is None]
    assert not missing, f"CP env 未收编: {missing}"
    for key, value in _CP_ENV_COERCED_KEYS:
        assert env[key] == value, key


def test_control_plane_env_no_empty_string_injection(monkeypatch, tmp_path) -> None:
    """未设/空串/纯空白一律不注入（显式设了才透传的既有先例；空串注入会覆盖
    CP 侧缺省档语义）。"""
    for key, _value in _CP_ENV_COERCED_KEYS:
        monkeypatch.delenv(key, raising=False)
    env = bok._control_plane_env(tmp_path / "db.sqlite")
    leaked = [key for key, _v in _CP_ENV_COERCED_KEYS if key in env]
    assert not leaked, f"未设键被注入: {leaked}"
    # 纯空白同样不注入
    for key, _value in _CP_ENV_COERCED_KEYS:
        monkeypatch.setenv(key, "   ")
    env2 = bok._control_plane_env(tmp_path / "db.sqlite")
    leaked2 = [key for key, _v in _CP_ENV_COERCED_KEYS if key in env2]
    assert not leaked2, f"空白值被注入: {leaked2}"
