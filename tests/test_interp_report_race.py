"""B 线 interp session-report 竞态——审计关联收尾（2026-09-18）。

本会话原始修法（reporter 标记 + 主列 blob 幂等合并）与并行 PR #95 的 P1-A
同题同撞：P1-A 用 worker 字段 + per-worker 历史列（session_reports_json）
+ _iter_call_reports 聚合读点，覆盖面更完整且已自带 tests/test_session_reports.py
12 条——冲突收敛取 P1-A 为准，本文件不再重复钉竞态行为。

保留的本会话增量：**审计事件显式带 call_id**。_audit 文档口径「服务端已知
call_id 就显式传入，别依赖调用方带头」，但本端点三处审计事件此前只传
subject_id——按 call_id 过滤 /api/audit 查不到 session-report 任何留痕
（幽灵拒绝、双 worker 合并均不可按通话关联），排查竞态时账本断链。
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "")  # in-memory repo
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

from fastapi.testclient import TestClient  # noqa: E402

from control_plane.main import app  # noqa: E402


def _mk_call(client: TestClient) -> str:
    created = client.post(
        "/api/calls",
        json={"account_id": "acc-001", "object_id": "obj-1", "persona_id": "p-1",
              "mode": "simulation"},
    ).json()
    return str(created["id"])


def _audit_actions_with_call_id(client: TestClient, call_id: str) -> set[str]:
    """按 call_id 过滤出的审计动作集——只有显式带 call_id 的事件才可见。"""
    events = client.get("/api/audit", params={"call_id": call_id}).json()
    return {str(e.get("action") or "") for e in events}


def test_worker_report_audits_are_visible_by_call_id():
    """worker 报告（含 ended 合并）的审计事件按 call_id 可关联。"""
    with TestClient(app) as client:
        call_id = _mk_call(client)
        client.post(f"/api/supervisor/{call_id}/end")
        # B 线 fwd 先存、rev 后到：ended 后异 worker 照收（P1-A 合并语义）
        first = client.post(
            f"/api/calls/{call_id}/session-report",
            json={"worker": "bok-interp-fwd", "job_id": "AJ_1", "usage": []},
        )
        assert first.status_code == 200
        second = client.post(
            f"/api/calls/{call_id}/session-report",
            json={"worker": "bok-interp-rev", "job_id": "AJ_2", "usage": []},
        )
        assert second.status_code == 200 and second.json()["merged"] is True
        actions = _audit_actions_with_call_id(client, call_id)
        assert "call.session_report" in actions


def test_ghost_rejected_audit_is_visible_by_call_id():
    """幽灵覆盖 409 的拒绝留痕同样按 call_id 可关联（排查竞态的账本不断链）。"""
    with TestClient(app) as client:
        call_id = _mk_call(client)
        client.post(f"/api/supervisor/{call_id}/end")
        # 旧格式（无 worker=A 线语义）：首个照存，第二份 409
        assert client.post(
            f"/api/calls/{call_id}/session-report", json={"job_id": "AJ_main"}
        ).status_code == 200
        assert client.post(
            f"/api/calls/{call_id}/session-report", json={"job_id": "AJ_ghost"}
        ).status_code == 409
        actions = _audit_actions_with_call_id(client, call_id)
        assert "call.session_report" in actions
        assert "call.session_report_rejected" in actions
        # 真数据未被覆盖
        row = client.get(f"/api/calls/{call_id}").json()
        assert json.loads(row["session_report"])["job_id"] == "AJ_main"
