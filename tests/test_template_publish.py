"""W2-T1 模板发布两态:发布=冻结九键快照,保存=草稿。

契约(plan §5 T1,接口冻结):
- published_json TEXT DEFAULT ''——「已发布」≡非空;「有未发布改动」≡ live 与
  冻结九键不一致(CP 派生布尔 published/has_changes,不落列)。
- PUT 永不触碰 published_json;POST /api/templates/{id}/publish 是唯一写入口,
  闸链逐字与 PUT 同族(gate_page+deny_cross_account+deny_foreign_owner(edit))。
- GET 详情机器通道(state.machine)且已发布 → 冻结九键 overlay 后返回(agent
  建单装配恒吃发布版);人类通道恒 live;列表端点不 overlay。
- 未发布模板机器通道回退 live(零行为变化);存量回填逻辑在 deps(SQL 面,
  InMemory 测试替身不覆盖,回填判据见 deps.py 注释)。

测试夹具口令 PW 为测试常量,非真实凭据。
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-template-publish-0000")

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import hash_password

PW = "Passw0rd!x"

_MACHINE_TOKEN = "machine-token-t1"

_LIVE = {
    "account_id": "acc-001",
    "name": "三步话术",
    "language": "zh",
    "steps_json": json.dumps(
        [{"goal": "确认身份", "ref": "你好，请问是{name}吗？"},
         {"goal": "办理", "ref": "帮您登记。"}],
        ensure_ascii=False,
    ),
    "opening": "你好，请问是{name}吗？",
    "core": "核对地址后安排派送。",
    "hotwords": "快递,派送",
}

# 冻结九键(与 main.py _TEMPLATE_PUBLISH_KEYS 同源;测试独立枚举防两端一起漂)
_KEYS = (
    "steps_json", "graph_json", "hotwords", "tone_override",
    "opening", "core", "objection", "closing", "language",
)


def _client_and_repo(monkeypatch):
    """auth-off 形态:进 startup(audit tap 落 repo);鉴权 env 默认清空。"""
    from fastapi.testclient import TestClient

    from control_plane.main import app

    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    client = TestClient(app).__enter__()
    return client, repo


def _machine_env(monkeypatch):
    """auth-on + CP token:机器通道(Bearer=machine token)才打 state.machine 标。"""
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_CP_TOKEN", _MACHINE_TOKEN)


def _admin_headers(client, repo):
    repo.create_user(username="boss", password_hash=hash_password(PW), role="admin",
                     account_id="acc-001")
    r = client.post("/api/auth/login", json={"username": "boss", "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _machine_headers():
    return {"Authorization": f"Bearer {_MACHINE_TOKEN}"}


def _make_template(client, **overrides):
    body = {**_LIVE, **overrides}
    r = client.post("/api/templates", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ---- ① 新建+publish:冻结=当时 live、published=True、has_changes=False ----


def test_publish_freezes_live(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    tpl = _make_template(client)

    # 未发布:published=False、has_changes=False,published_json 空
    row = client.get(f"/api/templates/{tpl['id']}").json()
    assert row["published"] is False and row["has_changes"] is False
    assert repo.get_template(tpl["id"])["published_json"] == ""

    pub = client.post(f"/api/templates/{tpl['id']}/publish")
    assert pub.status_code == 200, pub.text
    body = pub.json()
    assert body["published"] is True and body["has_changes"] is False

    # 冻结串=当时 live 九键原样
    frozen = json.loads(repo.get_template(tpl["id"])["published_json"])
    assert set(frozen.keys()) == set(_KEYS)
    for key in _KEYS:
        assert frozen[key] == tpl[key], key

    # 详情读回 published=True/has_changes=False
    row = client.get(f"/api/templates/{tpl['id']}").json()
    assert row["published"] is True and row["has_changes"] is False
    # 列表行带同款派生字段
    listed = {t["id"]: t for t in client.get("/api/templates").json()}
    assert listed[tpl["id"]]["published"] is True and listed[tpl["id"]]["has_changes"] is False
    # 审计落账
    assert any(e.get("action") == "template.publish" and e.get("subject_id") == tpl["id"]
               for e in repo.audit_events)


# ---- ② publish 后 PUT 改 live:has_changes=True 且冻结串不变 ----


def test_put_after_publish_flags_changes_not_snapshot(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    tpl = _make_template(client)
    client.post(f"/api/templates/{tpl['id']}/publish")
    frozen_before = repo.get_template(tpl["id"])["published_json"]

    # PUT 改 live(PUT 无 published_json 键——schema 层就摸不到快照)
    r = client.put(f"/api/templates/{tpl['id']}", json={"steps_json": json.dumps(
        [{"goal": "新草稿步", "ref": "改稿内容"}], ensure_ascii=False)})
    assert r.status_code == 200, r.text

    # 冻结串原封不动;live 已变 → has_changes=True
    assert repo.get_template(tpl["id"])["published_json"] == frozen_before
    row = client.get(f"/api/templates/{tpl['id']}").json()
    assert row["published"] is True and row["has_changes"] is True
    listed = {t["id"]: t for t in client.get("/api/templates").json()}
    assert listed[tpl["id"]]["has_changes"] is True

    # 二次 publish 重冻结 → has_changes 归零
    client.post(f"/api/templates/{tpl['id']}/publish")
    row = client.get(f"/api/templates/{tpl['id']}").json()
    assert row["has_changes"] is False
    assert json.loads(repo.get_template(tpl["id"])["published_json"])["steps_json"] == json.dumps(
        [{"goal": "新草稿步", "ref": "改稿内容"}], ensure_ascii=False)


# ---- ③ 机器通道详情=冻结 overlay;人类通道=live;列表不 overlay ----


def test_machine_overlay_vs_human_live(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    tpl = _make_template(client)
    client.post(f"/api/templates/{tpl['id']}/publish")
    human = _admin_headers(client, repo)

    # 切 auth-on:机器/人类两通道分别读
    _machine_env(monkeypatch)
    live_steps = json.dumps([{"goal": "新草稿步", "ref": "改稿内容"}], ensure_ascii=False)
    client.put(f"/api/templates/{tpl['id']}", headers=human, json={"steps_json": live_steps})

    # 机器通道(agent 装配):steps_json=冻结版旧稿
    mrow = client.get(f"/api/templates/{tpl['id']}", headers=_machine_headers()).json()
    assert mrow["steps_json"] == tpl["steps_json"]
    assert mrow["published"] is True
    # 人类通道(编辑器):steps_json=live 新稿
    hrow = client.get(f"/api/templates/{tpl['id']}", headers=human).json()
    assert hrow["steps_json"] == live_steps
    # 列表端点不 overlay:机器通道也读 live
    mlist = {t["id"]: t for t in client.get("/api/templates", headers=_machine_headers()).json()}
    assert mlist[tpl["id"]]["steps_json"] == live_steps


# ---- ⑥ 未发布模板:机器通道回退 live ----


def test_machine_channel_unpublished_falls_back_to_live(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    tpl = _make_template(client)
    _machine_env(monkeypatch)
    mrow = client.get(f"/api/templates/{tpl['id']}", headers=_machine_headers()).json()
    assert mrow["steps_json"] == tpl["steps_json"]
    assert mrow["published"] is False and mrow["has_changes"] is False


# ---- ④ publish 闸:共享模板 user 403 / 跨账号 404 / 本人可发布 ----


def test_publish_gates(monkeypatch):
    from control_plane.auth import hash_password as _hp

    client, repo = _client_and_repo(monkeypatch)
    # 共享话术(owner_user_id=''=账号共享,由 admin 建)
    repo.create_user(username="boss", password_hash=_hp(PW), role="admin", account_id="acc-001")
    admin = {"Authorization": f"Bearer {client.post('/api/auth/login', json={'username': 'boss', 'password': PW}).json()['token']}"}
    shared = client.post("/api/templates", headers=admin,
                         json={**_LIVE, "name": "共享话术"}).json()
    # 他账号话术
    other = _make_template(client, account_id="acc-002", name="别家话术")
    # op1 个人话术(user 建话术 CP 强制盖章本人,body 指定归属无效)
    repo.create_user(username="op1", password_hash=_hp(PW), role="user", account_id="acc-001")
    op1 = {"Authorization": f"Bearer {client.post('/api/auth/login', json={'username': 'op1', 'password': PW}).json()['token']}"}
    mine = client.post("/api/templates", headers=op1,
                       json={**_LIVE, "name": "OP1私"}).json()

    # user 改共享基线 → 403(deny_foreign_owner(edit=True),与 PUT 同闸)
    assert client.post(f"/api/templates/{shared['id']}/publish", headers=op1).status_code == 403
    # 跨账号 → 404(不泄露存在性)
    assert client.post(f"/api/templates/{other['id']}/publish", headers=op1).status_code == 404
    # 本人个人话术可发布;admin 可发布共享
    assert client.post(f"/api/templates/{mine['id']}/publish", headers=op1).status_code == 200
    assert client.post(f"/api/templates/{shared['id']}/publish", headers=admin).status_code == 200
    # 不存在的模板 → 404
    assert client.post("/api/templates/nope-nope/publish", headers=admin).status_code == 404


# ---- ⑤ parse_steps scene 宽容三态 ----


def test_parse_steps_scene_tolerant():
    from agent_runtime.flow import FlowStep, parse_steps

    steps = parse_steps(json.dumps([
        {"goal": "g1", "ref": "r1", "scene": "开场"},
        {"goal": "g2", "ref": "r2", "scene": 123},      # 非 str → ""
        {"goal": "g3", "ref": "r3", "scene": None},     # None → ""
        {"goal": "g4", "ref": "r4"},                    # 缺失 → ""
    ], ensure_ascii=False))
    assert [s.scene for s in steps] == ["开场", "", "", ""]
    # 引擎语义零变化:goal/ref/say/emotion 照旧
    assert [s.goal for s in steps] == ["g1", "g2", "g3", "g4"]
    # 兼容旧行为:字符串步/空串输入/非 list
    assert all(s.scene == "" for s in parse_steps(json.dumps(["纯文本步"])))
    assert parse_steps("") == []
    assert parse_steps("{not json") == []
    assert FlowStep().scene == ""
