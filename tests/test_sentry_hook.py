"""Sentry 接线单点(R3,2026-10-04)——no-op 铁律与双面 env 转发钉。

四件:
1. DSN 缺席=完整 no-op(init False/capture 静默),**绝不 raise**;
2. DSN 在场=init 参数形状(traces 0.2/无 PII/component 标签),capture 带 tag;
3. 初始化失败/SDK 缺席=告警不炸启动;
4. env 转发双面:SENTRY_DSN/SENTRY_ENVIRONMENT 进 _FORWARD_ENV(worker 面)
   与 _control_plane_env(CP 面)——缺一面=prod 封闭 env 死门。
"""
from __future__ import annotations

import sys
from pathlib import Path

from _bok_src import bok_source
import pytest
from bok_voice_obs import sentry_hook

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(sentry_hook, "_initialized", False)
    yield


# ---- ① DSN 缺席 = no-op ----

def test_no_dsn_is_full_noop(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    # SDK 真在场也不得被触碰。
    calls: list[str] = []
    import sentry_sdk

    monkeypatch.setattr(sentry_sdk, "init", lambda *a, **k: calls.append("init"))
    assert sentry_hook.init_sentry("agent-worker") is False
    assert calls == []
    sentry_hook.capture(ValueError("x"))  # no-op:不 raise 即过


# ---- ② DSN 在场 = 形状正确 ----

def test_init_shape_traces_no_pii_component_tag(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", "https://k@example.invalid/1")
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "pilot-hk")
    monkeypatch.delenv("SENTRY_SEND_PII", raising=False)  # 缺省=保守档
    import sentry_sdk

    inits: list[dict] = []
    tags: list[tuple] = []

    class _Scope:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def set_tag(self, k, v):
            tags.append((k, v))

    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: inits.append(kw))
    monkeypatch.setattr(sentry_sdk, "set_tag", lambda k, v: tags.append(("global", k, v)))
    monkeypatch.setattr(sentry_sdk, "push_scope", lambda: _Scope())
    captured: list = []
    monkeypatch.setattr(sentry_sdk, "capture_exception", captured.append)

    assert sentry_hook.init_sentry("control-plane") is True
    assert inits and inits[0]["dsn"] == "https://k@example.invalid/1"
    assert inits[0]["traces_sample_rate"] == 0.2
    assert inits[0]["send_default_pii"] is False  # 缺省保守档
    assert inits[0]["environment"] == "pilot-hk"
    assert ("global", "component", "control-plane") in tags

    exc = RuntimeError("judge bg died")
    sentry_hook.capture(exc, lane="judge-bg", step="3")
    assert captured == [exc]
    assert ("lane", "judge-bg") in tags and ("step", "3") in tags


def test_pii_env_switch_flips_send_default_pii(monkeypatch):
    """SENTRY_SEND_PII=1 → send_default_pii True(Ethan 2026-10-04 dev 档拍板开)。"""
    monkeypatch.setenv("SENTRY_DSN", "https://k@example.invalid/1")
    monkeypatch.setenv("SENTRY_SEND_PII", "1")
    import sentry_sdk

    inits: list[dict] = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: inits.append(kw))
    monkeypatch.setattr(sentry_sdk, "set_tag", lambda k, v: None)
    assert sentry_hook.init_sentry("control-plane") is True
    assert inits[0]["send_default_pii"] is True


# ---- ③ 初始化失败 = 告警不炸 ----

def test_init_failure_never_raises(monkeypatch, capsys):
    monkeypatch.setenv("SENTRY_DSN", "https://k@example.invalid/1")
    import sentry_sdk

    def _boom(**_kw):
        raise RuntimeError("relay down")

    monkeypatch.setattr(sentry_sdk, "init", _boom)
    assert sentry_hook.init_sentry("control-plane") is False
    assert "init failed" in capsys.readouterr().out


def test_sdk_missing_warns_not_raises(monkeypatch, capsys):
    monkeypatch.setenv("SENTRY_DSN", "https://k@example.invalid/1")
    real = sys.modules.pop("sentry_sdk", None)
    sys.modules["sentry_sdk"] = None  # import 语句命中 None → ImportError
    try:
        assert sentry_hook.init_sentry("agent-worker") is False
        assert "未安装" in capsys.readouterr().out
    finally:
        if real is not None:
            sys.modules["sentry_sdk"] = real
        else:
            sys.modules.pop("sentry_sdk", None)


# ---- ④ env 转发双面源级 pin ----

def test_sentry_env_keys_flow_to_both_faces():
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import bok
    finally:
        sys.path.remove(str(ROOT / "tools"))
    assert "SENTRY_DSN" in bok.env._FORWARD_ENV
    assert "SENTRY_ENVIRONMENT" in bok.env._FORWARD_ENV
    assert "SENTRY_SEND_PII" in bok.env._FORWARD_ENV
    bok_src = bok_source()
    cp_loop = bok_src[bok_src.index("for _k in (\"BOK_LOG_LEVEL\""):]
    cp_loop = cp_loop[: cp_loop.index(")") + 1]
    for _k in ("SENTRY_DSN", "SENTRY_ENVIRONMENT", "SENTRY_SEND_PII"):
        assert _k in cp_loop


def test_worker_and_cp_wire_sentry_init():
    agent_src = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    assert '_init_sentry("agent-worker")' in agent_src
    assert 'lane="watchdog-fire"' in agent_src
    assert 'lane="judge-bg"' in agent_src
    cp_src = (ROOT / "apps" / "control-plane" / "control_plane" / "main.py").read_text(encoding="utf-8")
    assert 'init_sentry("control-plane")' in cp_src
