"""画布使用埋点（V17 阶段 A ①）：审计记录保存键面，判别画布 vs 表单编辑路径。

画布保存=单键部分更新（PUT /api/templates 只带 steps_json 或 graph_json 单键；
qa 画布拖线挂簇=PATCH 只带 cluster_head_id、挂步=scope+step_index），表单保存=
多键全量。此前审计不记 payload 键面无法区分，现 template.update 的
detail["keys"] 与 qa_entry.update 的 detail["fields"] 各记排序列表。

CP TestClient 姿势同 tests/test_model_routing.py：内存仓 monkeypatch _repo、
monkeypatch cp_main._audit 捕获事件（零落盘）。
"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("DATABASE_URL", "")  # 强制内存仓
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")

from bok_voice_business_db.repository import InMemoryBusinessRepository


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch):
    """auth-off 单机形态（匿名直通），防其它测试残留 env。"""
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane import main as cp_main

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr(cp_main, "_repo", lambda: repo)
    return TestClient(cp_main.app), repo


def _capture_audit(monkeypatch) -> list[tuple]:
    from control_plane import main as cp_main

    events: list[tuple] = []
    monkeypatch.setattr(cp_main, "_audit", lambda action, **kw: events.append((action, kw)))
    return events


def _mk_template(client) -> str:
    r = client.post("/api/templates", json={"name": "埋点模板", "account_id": "acc-001"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _mk_qa_entry(client) -> str:
    r = client.post("/api/qa-entries", json={
        "question_text": "几时到", "answer_text": "三个工作日内", "account_id": "acc-001"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _detail_of(events, action: str) -> dict:
    hits = [kw.get("detail") or {} for a, kw in events if a == action]
    assert hits, f"no audit event {action} in {[a for a, _ in events]}"
    return hits[-1]


# ---- template.update：detail["keys"] ----


def test_template_put_multi_key_records_sorted_keys(monkeypatch):
    """表单保存=多键全量 → keys 面为包含全部显式键的排序列表。"""
    client, _repo = _client_and_repo(monkeypatch)
    events = _capture_audit(monkeypatch)
    tid = _mk_template(client)

    r = client.put(f"/api/templates/{tid}", json={
        "name": "改名", "hotwords": "a,b", "language": "cantonese"})
    assert r.status_code == 200, r.text

    detail = _detail_of(events, "template.update")
    keys = detail["keys"]
    assert isinstance(keys, list) and keys == sorted(keys)
    assert keys == ["hotwords", "language", "name"]


def test_template_put_single_graph_key_canvas_shape(monkeypatch):
    """画布形态：PUT 只带 graph_json 单键 → keys == ["graph_json"]。"""
    client, _repo = _client_and_repo(monkeypatch)
    events = _capture_audit(monkeypatch)
    tid = _mk_template(client)

    graph = json.dumps({"version": 1, "intents": [], "bindings": []})
    r = client.put(f"/api/templates/{tid}", json={"graph_json": graph})
    assert r.status_code == 200, r.text

    detail = _detail_of(events, "template.update")
    assert detail["keys"] == ["graph_json"]
    # 既有键不受影响：graph_saved 照旧语义
    assert detail["graph_saved"] is True


def test_template_put_single_steps_key_canvas_shape(monkeypatch):
    """画布形态另一臂：updateTemplate({steps_json}) 单键部分更新。"""
    client, _repo = _client_and_repo(monkeypatch)
    events = _capture_audit(monkeypatch)
    tid = _mk_template(client)

    r = client.put(f"/api/templates/{tid}", json={"steps_json": "[]"})
    assert r.status_code == 200, r.text

    assert _detail_of(events, "template.update")["keys"] == ["steps_json"]


# ---- qa_entry.update：detail["fields"] ----


def test_qa_patch_cluster_head_only_canvas_shape(monkeypatch):
    """qa 画布拖线挂簇：PATCH 只带 cluster_head_id → fields == ["cluster_head_id"]。"""
    client, _repo = _client_and_repo(monkeypatch)
    events = _capture_audit(monkeypatch)
    qid = _mk_qa_entry(client)

    r = client.patch(f"/api/qa-entries/{qid}", json={"cluster_head_id": ""})
    assert r.status_code == 200, r.text

    assert _detail_of(events, "qa_entry.update")["fields"] == ["cluster_head_id"]


def test_qa_patch_form_multi_key_records_sorted_fields(monkeypatch):
    """表单编辑=多键 PATCH → fields 面为排序列表（与画布单键形态可判别）。"""
    client, _repo = _client_and_repo(monkeypatch)
    events = _capture_audit(monkeypatch)
    qid = _mk_qa_entry(client)

    r = client.patch(f"/api/qa-entries/{qid}", json={
        "question_text": "几时送到", "answer_text": "明日内", "enabled": False})
    assert r.status_code == 200, r.text

    fields = _detail_of(events, "qa_entry.update")["fields"]
    assert isinstance(fields, list) and fields == sorted(fields)
    assert fields == ["answer_text", "enabled", "question_text"]


def test_qa_patch_scope_step_index_canvas_shape(monkeypatch):
    """qa 画布挂步：PATCH 只带 scope+step_index → fields 恰为这两键。"""
    client, _repo = _client_and_repo(monkeypatch)
    events = _capture_audit(monkeypatch)
    qid = _mk_qa_entry(client)

    r = client.patch(f"/api/qa-entries/{qid}", json={"scope": "step", "step_index": 2})
    assert r.status_code == 200, r.text

    assert _detail_of(events, "qa_entry.update")["fields"] == ["scope", "step_index"]
