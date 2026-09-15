"""2026-09-16 深测 P2-8：agent 端 ControlPlaneClient 从未携带 BOK_CP_TOKEN，
auth-on 全栈 turns/QA/设置上报全 401（文档契约 aspirational，代码 0 处引用）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))


def test_cp_client_carries_machine_token(monkeypatch):
    monkeypatch.setenv("BOK_CP_TOKEN", "mach-token-123")
    from agent_runtime.control_plane import ControlPlaneClient

    c = ControlPlaneClient("http://127.0.0.1:8000", call_id="call-x")
    assert c._client.headers.get("authorization") == "Bearer mach-token-123"


def test_cp_client_without_token_unchanged(monkeypatch):
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    from agent_runtime.control_plane import ControlPlaneClient

    c = ControlPlaneClient("http://127.0.0.1:8000")
    assert not c._client.headers.get("authorization")
