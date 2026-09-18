"""模板 graph_json API:保存校验 400 / exclude_unset 部分更新 / 空串清空 / 审计布尔。"""
from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-canned")

_GOOD = {
    "version": 1,
    "intents": [{"id": "int_1a2b3c4d", "label": "投诉", "keywords": ["投诉"], "steps": [], "enabled": True}],
    "bindings": [{"id": "bnd_7e8f9a0b", "intent": "int_1a2b3c4d", "action": "jump_step", "step": 4}],
}


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    client = TestClient(app).__enter__()
    return client, repo


def test_create_with_graph_and_invalid_rejected(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    ok = client.post("/api/templates", json={"name": "t", "graph_json": json.dumps(_GOOD)})
    assert ok.status_code == 200, ok.text
    assert json.loads(ok.json()["graph_json"]) == _GOOD
    bad = client.post("/api/templates", json={"name": "t2", "graph_json": "{not json"})
    assert bad.status_code == 400
    assert bad.json()["detail"]["error"] == "invalid_graph_json"


def test_put_partial_update_semantics(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    tpl = client.post("/api/templates", json={"name": "t", "graph_json": json.dumps(_GOOD)}).json()
    # 只传 name——exclude_unset 不得抹掉 graph_json
    client.put(f"/api/templates/{tpl['id']}", json={"name": "t2"})
    assert repo.get_template(tpl["id"])["graph_json"] != ""
    # 显式空串=清空
    client.put(f"/api/templates/{tpl['id']}", json={"graph_json": ""})
    assert repo.get_template(tpl["id"])["graph_json"] == ""
    # PUT 非法图 → 400 且旧值保留
    client.put(f"/api/templates/{tpl['id']}", json={"graph_json": json.dumps(_GOOD)})
    bad = client.put(f"/api/templates/{tpl['id']}", json={"graph_json": '{"version":9}'})
    assert bad.status_code == 400
    assert repo.get_template(tpl["id"])["graph_json"] == json.dumps(_GOOD)


def test_invalid_put_writes_no_revision_row(monkeypatch):
    """被拒的保存连版本行都不准写:append_template_revision 自带 commit,
    校验若晚于它,每个非法图都会白吃一个版本号并留下与现状等同的历史行
    (autosave 编辑器可刷满历史)——review R1。"""
    client, repo = _client_and_repo(monkeypatch)
    tpl = client.post("/api/templates", json={"name": "t", "graph_json": json.dumps(_GOOD)}).json()
    before_rows = len(repo.list_template_revisions(tpl["id"]))
    bad = client.put(f"/api/templates/{tpl['id']}", json={"graph_json": '{"version":9}'})
    assert bad.status_code == 400
    assert len(repo.list_template_revisions(tpl["id"])) == before_rows
    assert repo.get_template(tpl["id"])["graph_json"] == json.dumps(_GOOD)
    # 计数灵敏度自证:合法保存照旧 +1(否则上面的相等断言恒真=空断言)。
    ok = client.put(f"/api/templates/{tpl['id']}", json={"name": "t2"})
    assert ok.status_code == 200
    assert len(repo.list_template_revisions(tpl["id"])) == before_rows + 1
