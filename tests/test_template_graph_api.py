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


# ---------------------------------------------------------------------------
# P2.2:兜底意图 "*" / 孤儿意图门 / id 放宽——保存路径 400 原文透出(两条调用点)
# ---------------------------------------------------------------------------

_CATCHALL_OK = {
    "version": 1,
    "intents": [
        {"id": "int_1a2b3c4d", "label": "投诉", "keywords": ["投诉"], "steps": []},
        {"id": "*", "label": "兜底", "keywords": []},
    ],
    "bindings": [
        {"id": "bnd_7e8f9a0b", "intent": "int_1a2b3c4d", "action": "jump_step", "step": 4},
        {"id": "bnd_c1d2e3f4", "intent": "*", "action": "notify_human"},
    ],
}


def _detail_text(resp) -> str:
    return json.dumps(resp.json(), ensure_ascii=False)


def test_catchall_graph_saves_and_snake_case_id_allowed(monkeypatch):
    """兜底图合法保存;意图 id 放宽到 snake_case(P2.1 挖掘产物出口)。"""
    client, _repo = _client_and_repo(monkeypatch)
    ok = client.post("/api/templates", json={"name": "t", "graph_json": json.dumps(_CATCHALL_OK)})
    assert ok.status_code == 200, ok.text
    mined = {
        "version": 1,
        "intents": [{"id": "whatsapp_contact", "label": "加联系方式", "keywords": ["加你"]}],
        "bindings": [{"id": "bnd_8f9a0b1c", "intent": "whatsapp_contact", "action": "notify_human"}],
    }
    ok2 = client.post("/api/templates", json={"name": "t2", "graph_json": json.dumps(mined)})
    assert ok2.status_code == 200, ok2.text


def test_catchall_bad_shapes_rejected_with_new_detail(monkeypatch):
    """三条新错误各自 400 且原文透出(CP 保存校验路径照旧 detail 列表)。"""
    client, _repo = _client_and_repo(monkeypatch)

    def _post(graph: dict):
        return client.post("/api/templates", json={"name": "x", "graph_json": json.dumps(graph)})

    keywords = dict(_CATCHALL_OK)
    keywords["intents"] = [{"id": "*", "label": "兜底", "keywords": ["投诉"]}]
    keywords["bindings"] = []
    bad = _post(keywords)
    assert bad.status_code == 400 and "keywords must be empty" in _detail_text(bad), bad.text

    once = dict(_CATCHALL_OK)
    once["bindings"] = [{"id": "bnd_c1d2e3f4", "intent": "*", "action": "notify_human", "once": True}]
    bad2 = _post(once)
    assert bad2.status_code == 400 and "once must be false" in _detail_text(bad2), bad2.text

    judge = dict(_CATCHALL_OK)
    judge["intents"] = [
        {"id": "*", "label": "兜底", "keywords": [], "judge": {"prompt": "任何话"}},
    ]
    judge["bindings"] = [{"id": "bnd_c1d2e3f4", "intent": "*", "action": "notify_human"}]
    bad3 = _post(judge)
    assert bad3.status_code == 400 and "judge is not allowed" in _detail_text(bad3), bad3.text


def test_orphan_intent_rejected_but_empty_graph_exempt(monkeypatch):
    """孤儿意图 400(配了意图没绑动作=图不通);空图完全豁免(存量不得 breaking)。"""
    client, _repo = _client_and_repo(monkeypatch)
    orphan = {
        "version": 1,
        "intents": [{"id": "refund_request", "label": "退款", "keywords": ["退款"]}],
        "bindings": [],
    }
    bad = client.post("/api/templates", json={"name": "t", "graph_json": json.dumps(orphan)})
    assert bad.status_code == 400
    assert "has no enabled binding: refund_request" in _detail_text(bad), bad.text
    # 空图(无 intents)豁免 + 空串未启用照旧放行
    for graph in ({"version": 1, "intents": [], "bindings": []}, ""):
        ok = client.post(
            "/api/templates",
            json={"name": "t2", "graph_json": json.dumps(graph) if graph else ""},
        )
        assert ok.status_code == 200, ok.text


def test_put_path_carries_catchall_errors_and_keeps_old_value(monkeypatch):
    """PUT 是同族第二个调用点:新错误同样 400,且被拒保存不落库/不留版本行。"""
    client, repo = _client_and_repo(monkeypatch)
    tpl = client.post("/api/templates", json={"name": "t", "graph_json": json.dumps(_GOOD)}).json()
    before_rows = len(repo.list_template_revisions(tpl["id"]))
    orphan = {
        "version": 1,
        "intents": [{"id": "int_1a2b3c4d", "label": "投诉", "keywords": ["投诉"]}],
        "bindings": [],
    }
    bad = client.put(f"/api/templates/{tpl['id']}", json={"graph_json": json.dumps(orphan)})
    assert bad.status_code == 400
    assert "has no enabled binding" in _detail_text(bad), bad.text
    assert repo.get_template(tpl["id"])["graph_json"] == json.dumps(_GOOD)
    assert len(repo.list_template_revisions(tpl["id"])) == before_rows
    # 补位对照:兜底图 PUT 照过
    ok = client.put(f"/api/templates/{tpl['id']}", json={"graph_json": json.dumps(_CATCHALL_OK)})
    assert ok.status_code == 200, ok.text
