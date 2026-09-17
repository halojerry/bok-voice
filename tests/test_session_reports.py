"""P1-A/P1-B session-report 收编测试（2026-09-17 全量 debug）。

契约：
- P1-B 写入主体收紧：机器通道（BOK_CP_TOKEN）与 admin/root 可写，role=user 403，
  真 auth-off（双关）零变化；
- P1-A per-worker 报告：worker=""（旧 A 线）语义逐字节保留（首写进主列、
  ended+已有 → 409 幽灵守卫）；worker 非空走 session_reports_json per-worker
  历史（同 worker 重发=替换、异 worker=追加、ended 后合并 200 不 409）；
- usage 读点跨「主列+历史列」聚合且镜像不双算（_iter_call_reports 去重）。

测试口令走模块常量 PW（测试夹具，非真实凭据）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-session-reports")
PW = "Passw0rd!x"  # 测试夹具口令(与 test_auth.py 同源),非真实凭据

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import hash_password


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app
    from control_plane.nodes_store import NodeStore

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    # 熔断窒息点中间件对带 Bearer 的 POST /api/calls 解 node_token——app 是模块级
    # 单例、startup 未必在本会话跑过，钉内存 NodeStore（NodeStore(None) 双模）。
    monkeypatch.setattr(app.state, "node_store", NodeStore(None), raising=False)
    return TestClient(app), repo


def _mk_user(repo, username, role="user", account="acc-001", password=PW):
    return repo.create_user(
        username=username, password_hash=hash_password(password),
        role=role, org_id="org-t", account_id=account,
    )


def _login(client, username, password=PW):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _report(total_in: int = 100, total_out: int = 10) -> dict:
    return {
        "usage": [{"type": "llm_usage", "input_tokens": total_in, "output_tokens": total_out}],
        "llm_usage": {"total_tokens": total_in + total_out},
        "chat_history": {"items": []},
    }


def _entries(call: dict) -> list[dict]:
    return json.loads(call.get("session_reports_json") or "[]")


# ---- P1-B: 写入主体收紧 ----


def test_user_identity_cannot_write_session_report(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_CP_TOKEN", "machine-token")  # 建单走机器通道
    call = client.post(
        "/api/calls", json={"account_id": "acc-001"},
        headers={"Authorization": "Bearer machine-token"},
    ).json()
    _mk_user(repo, "peon")
    tok = _login(client, "peon")
    r = client.post(
        f"/api/calls/{call['id']}/session-report", headers=_auth(tok), json=_report(),
    )
    assert r.status_code == 403
    assert not (repo.get_call(call["id"]).get("session_report") or "")


def test_machine_channel_can_write_session_report(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_CP_TOKEN", "machine-token")
    call = client.post(
        "/api/calls", json={"account_id": "acc-001"},
        headers={"Authorization": "Bearer machine-token"},
    ).json()
    r = client.post(
        f"/api/calls/{call['id']}/session-report",
        headers={"Authorization": "Bearer machine-token"}, json=_report(),
    )
    assert r.status_code == 200, r.text
    assert json.loads(repo.get_call(call["id"])["session_report"])["usage"]


def test_admin_can_write_session_report(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_CP_TOKEN", "machine-token")  # 建单走机器通道
    call = client.post(
        "/api/calls", json={"account_id": "acc-001"},
        headers={"Authorization": "Bearer machine-token"},
    ).json()
    _mk_user(repo, "boss", role="admin")
    tok = _login(client, "boss")
    r = client.post(
        f"/api/calls/{call['id']}/session-report", headers=_auth(tok), json=_report(),
    )
    assert r.status_code == 200, r.text


def test_dual_off_write_unchanged(monkeypatch):
    """真 auth-off（双关、无身份无 token）零变化。"""
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    call = client.post("/api/calls", json={"account_id": "acc-001"}).json()
    r = client.post(f"/api/calls/{call['id']}/session-report", json=_report())
    assert r.status_code == 200, r.text


# ---- P1-A: worker="" 旧语义逐字节保留 ----


def test_legacy_first_write_wins_and_ghost_409(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    call = client.post("/api/calls", json={"account_id": "acc-001"}).json()
    r1 = client.post(f"/api/calls/{call['id']}/session-report", json=_report(11, 1))
    assert r1.status_code == 200 and r1.json() == {"call_id": call["id"], "stored": True}
    repo.update_call(call["id"], status="ended")
    r2 = client.post(f"/api/calls/{call['id']}/session-report", json=_report(22, 2))
    assert r2.status_code == 409
    # 主列首写不动、无历史条目
    assert json.loads(repo.get_call(call["id"])["session_report"])["llm_usage"]["total_tokens"] == 12
    assert _entries(repo.get_call(call["id"])) == []


# ---- P1-A: per-worker upsert / append / ended 合并 ----


def test_worker_retry_replaces_own_entry(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    call = client.post("/api/calls", json={"account_id": "acc-001"}).json()
    cid = call["id"]
    r1 = client.post(f"/api/calls/{cid}/session-report",
                     json={"worker": "bok-interp-fwd", **_report(100, 10)})
    assert r1.status_code == 200 and r1.json()["merged"] is True
    r2 = client.post(f"/api/calls/{cid}/session-report",
                     json={"worker": "bok-interp-fwd", **_report(111, 11)})
    assert r2.status_code == 200 and r2.json()["replaced"] is True
    rows = _entries(repo.get_call(cid))
    assert len(rows) == 1  # 重试替换，不累积
    assert rows[0]["worker"] == "bok-interp-fwd"
    assert rows[0]["report"]["llm_usage"]["total_tokens"] == 122
    assert rows[0]["ts"]  # iso8601 时间戳在场
    # 主列=首份镜像（旧读点向后兼容）
    assert json.loads(repo.get_call(cid)["session_report"])["llm_usage"]["total_tokens"] == 110


def test_second_worker_appends(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    call = client.post("/api/calls", json={"account_id": "acc-001"}).json()
    cid = call["id"]
    client.post(f"/api/calls/{cid}/session-report",
                json={"worker": "bok-interp-fwd", **_report(100, 10)})
    r = client.post(f"/api/calls/{cid}/session-report",
                    json={"worker": "bok-interp-rev", **_report(50, 5)})
    assert r.status_code == 200
    rows = _entries(repo.get_call(cid))
    assert [e["worker"] for e in rows] == ["bok-interp-fwd", "bok-interp-rev"]


def test_ended_call_worker_report_merges_not_409(monkeypatch):
    """P1-A 核心：ended 后异 worker 第二份照收（合并 200）——B 线双 worker 竞态修复。"""
    client, repo = _client_and_repo(monkeypatch)
    call = client.post("/api/calls", json={"account_id": "acc-001"}).json()
    cid = call["id"]
    client.post(f"/api/calls/{cid}/session-report",
                json={"worker": "bok-interp-fwd", **_report(100, 10)})
    repo.update_call(cid, status="ended")
    r = client.post(f"/api/calls/{cid}/session-report",
                    json={"worker": "bok-interp-rev", **_report(50, 5)})
    assert r.status_code == 200, r.text
    assert r.json()["merged"] is True
    rows = _entries(repo.get_call(cid))
    assert len(rows) == 2
    # 主列已有首份镜像 → rev 不覆盖主列（镜像仅当主列为空）
    assert json.loads(repo.get_call(cid)["session_report"])["llm_usage"]["total_tokens"] == 110


def test_worker_report_mirrors_into_empty_main_column(monkeypatch):
    """无旧报告的通话（B 线直连形态）worker 报告镜像进主列——backfill/旧读点兼容。"""
    client, repo = _client_and_repo(monkeypatch)
    cid = client.post("/api/calls", json={"account_id": "acc-001"}).json()["id"]
    client.post(f"/api/calls/{cid}/session-report",
                json={"worker": "bok-interp-fwd", **_report(7, 3)})
    assert json.loads(repo.get_call(cid)["session_report"])["llm_usage"]["total_tokens"] == 10


# ---- usage 读点聚合 ----


def test_usage_aggregates_worker_reports_without_double_count(monkeypatch):
    """/api/reports/usage 跨 worker 报告累加；主列镜像与历史同文时不得双算。"""
    client, repo = _client_and_repo(monkeypatch)
    call = client.post("/api/calls", json={"account_id": "acc-001"}).json()
    cid = call["id"]
    client.post(f"/api/calls/{cid}/session-report",
                json={"worker": "bok-interp-fwd", **_report(100, 10)})
    client.post(f"/api/calls/{cid}/session-report",
                json={"worker": "bok-interp-rev", **_report(50, 5)})
    out = client.get("/api/reports/usage?account_id=acc-001").json()
    assert out["llm_tokens"] == 165  # 110 + 55，镜像不双算
    assert out["llm_tokens_estimated_calls"] == 0


def test_usage_estimation_fallback_unchanged(monkeypatch):
    """无报告通话回退轮数估算（legacy 口径零变化）。"""
    client, repo = _client_and_repo(monkeypatch)
    from bok_voice_core.types import TurnEvent

    call = client.post("/api/calls", json={"account_id": "acc-001"}).json()
    repo.create_turn(TurnEvent(trace_id=call["id"], call_id=call["id"], turn_id="t1",
                               role="user", transcript="你好"))
    repo.create_turn(TurnEvent(trace_id=call["id"], call_id=call["id"], turn_id="t2",
                               role="assistant", transcript="您好"))
    out = client.get("/api/reports/usage?account_id=acc-001").json()
    assert out["llm_tokens_estimated_calls"] == 1
    assert out["llm_tokens"] == 2  # 无报告回退=轮数计数（端点原口径，非 ×300）


def test_iter_call_reports_helper_dedupes_mirror():
    """_iter_call_reports：主列+历史合并视图，同文镜像去重、坏 JSON 跳过。"""
    import control_plane.main as cp_main

    report = _report(1, 2)
    call = {
        "session_report": json.dumps(report),
        "session_reports_json": json.dumps([
            {"worker": "w1", "report": report, "ts": "2026-09-17T00:00:00"},
            {"worker": "w2", "report": _report(3, 4), "ts": "2026-09-17T00:00:01"},
        ]),
    }
    reports = cp_main._iter_call_reports(call)
    assert len(reports) == 2  # 同文镜像去重：主列与 w1 同文只算一份
    assert reports[0]["llm_usage"]["total_tokens"] == 3   # w1（=主列内容）
    assert reports[1]["llm_usage"]["total_tokens"] == 7   # w2
    # 顺序契约:主列（未镜像场合）在前
    call2 = {"session_report": json.dumps(_report(9, 9)), "session_reports_json": ""}
    assert cp_main._iter_call_reports(call2)[0]["llm_usage"]["total_tokens"] == 18
    # 坏数据不炸
    assert cp_main._iter_call_reports({"session_report": "{bad", "session_reports_json": "[bad"}) == []
