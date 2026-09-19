"""意向规则引擎 W4-T1（CP/DB 面，2026-09-19）：core 纯函数 / repo 双后端 / 端点闸链 / 接线回归。

契约（plan 2026-09-19-ai-studio §7 T1）：
- 共享契约 ``bok_voice_core.intent_rules`` 只消费不修改（INTENT_FACTS/eval/validate）；
- intent_rules 两级作用域：account_id ''=全局行（admin/root 写）∪ 账号行（话务员写），
  读=两级行合并（``account_id IN ('', acct)``，owner_scope IN 先例）；
- call_sessions 新列 assist_status（''|notified|done，done 不降级）/intent_code；
- WhatsApp capture 顺手置 notified（仅当空）、takeover 顺手置 done、
  supervisor_end 可选 intent_code（非空落列+审计 detail）；
- disposition 语义零回归：自定义 disposition（interested）在 dashboard=接通、
  campaign item=done 不重拨（天然安全，测试钉住）。
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "")  # 端点测试强制内存仓（test_stats_dashboard 同款）
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")

import pytest
from fastapi.testclient import TestClient

from bok_voice_business_db.repository import InMemoryBusinessRepository
from bok_voice_core.intent_rules import (
    eval_intent_rules,
    parse_rule_row,
    validate_conditions,
)

PW = "Passw0rd!x"
_SEQ = iter(range(1, 10_000))


# ---- core 纯函数矩阵（契约只消费） ----


def test_core_eval_matrix():
    rules = [
        {"id": "r1", "priority": 20, "intent_code": "late",
         "conditions": [{"fact": "duration_s", "op": "gte", "value": 60}]},
        {"id": "r2", "priority": 10, "intent_code": "interested", "disposition": "interested",
         "label": "有意向",
         "conditions": [{"fact": "confirm_count", "op": "gte", "value": 2},
                        {"fact": "refuse_count", "op": "lte", "value": 0}]},
        {"id": "r3", "priority": 5, "intent_code": "wa_done",
         "conditions": [{"fact": "wa_captured", "op": "eq", "value": True}]},
    ]
    # gte 命中（多条规则按 priority 升序取首条：r2=10 先于 r1=20）
    hit = eval_intent_rules({"confirm_count": 3, "refuse_count": 0, "duration_s": 120}, rules)
    assert hit == {"intent_code": "interested", "label": "有意向", "disposition": "interested"}
    # lte 不命中（refuse_count 超限 → r2 失败；r3 缺键也不命中）
    assert eval_intent_rules({"confirm_count": 3, "refuse_count": 1}, rules) is None
    # eq 布尔
    assert eval_intent_rules({"wa_captured": True}, rules)["intent_code"] == "wa_done"
    assert eval_intent_rules({"wa_captured": False}, rules) is None
    # eq 数值
    single = [{"id": "a", "intent_code": "x",
               "conditions": [{"fact": "nudge_fired", "op": "eq", "value": 2}]}]
    assert eval_intent_rules({"nudge_fired": 2}, single)["intent_code"] == "x"
    assert eval_intent_rules({"nudge_fired": 3}, single) is None
    # facts 缺键=保守不命中
    assert eval_intent_rules({"confirm_count": 3}, rules) is None
    # disabled 跳过
    disabled = [dict(single[0], enabled=False)]
    assert eval_intent_rules({"nudge_fired": 2}, disabled) is None
    # 均缺 intent_code 跳过（bad row 永不命中）
    assert eval_intent_rules({"nudge_fired": 2},
                             [{"id": "a", "intent_code": "", "conditions": single[0]["conditions"]}]) is None


def test_core_parse_rule_row_tolerant():
    # 坏 conditions 串/空条件=None（fail-closed：空条件曾是 all([]) 恒真的
    # catch-all，命中每通挂断——2026-09-19 审计 P1-3 收口）；缺字段逐项兜底。
    assert parse_rule_row({"id": "x", "conditions": "{oops", "priority": "7", "enabled": None}) is None
    assert parse_rule_row({}) is None
    ok = parse_rule_row({"id": "x", "conditions": [{"fact": "duration_s", "op": "gte", "value": 60}], "priority": "7"})
    assert ok["priority"] == 7 and ok["enabled"] is True


def test_core_empty_conditions_never_match():
    # eval 侧双保险：空条件/坏行规则整体跳过，绝不命中。
    assert eval_intent_rules({"duration_s": 999}, [{"id": "r1", "intent_code": "INTERESTED", "conditions": []}]) is None
    assert eval_intent_rules({"duration_s": 999}, [{"id": "r1", "intent_code": "INTERESTED", "conditions": "{bad"}]) is None
    assert eval_intent_rules({"duration_s": 999}, [{"id": "r1", "intent_code": "INTERESTED", "conditions": [{"fact": "duration_s", "op": "gte", "value": 60}]}]) is not None


def test_core_validate_conditions_errors():
    ok = [{"fact": "duration_s", "op": "gte", "value": 60}]
    assert validate_conditions(ok) == []
    assert validate_conditions(ok + [{"fact": "wa_captured", "op": "eq", "value": False}]) == []
    assert validate_conditions("nope") == ["conditions must be an array"]
    # 空数组=catch-all（eval 侧 all([]) 恒真命中所有通话）——保存期即拒，不再放行。
    assert validate_conditions([]) == ["conditions must have at least 1 item"]
    errs = validate_conditions([{"fact": "bogus", "op": "gt", "value": "s"}])
    assert len(errs) == 3
    assert any(".fact" in e for e in errs) and any(".op" in e for e in errs) and any(".value" in e for e in errs)
    assert validate_conditions([ok[0]] * 13) == ["conditions must have at most 12 items"]


# ---- repo 双后端：CRUD + 两级行合并 ----


def test_repo_inmemory_two_level_merge_and_crud():
    os.environ["DATABASE_URL"] = ""
    repo = InMemoryBusinessRepository()
    global_row = repo.create_intent_rule({"name": "g", "intent_code": "i1", "account_id": ""})
    acct_row = repo.create_intent_rule({"name": "a", "intent_code": "i2", "account_id": "acc-001"})
    repo.create_intent_rule({"name": "o", "intent_code": "i3", "account_id": "acc-002"})
    # 两级行合并：''+本账号行都回、他账号行不回
    rows = repo.list_intent_rules("acc-001")
    assert {r["id"] for r in rows} == {global_row["id"], acct_row["id"]}
    assert repo.list_intent_rules("acc-002")[0]["id"] != acct_row["id"]
    # get / update 白名单 / delete
    assert repo.get_intent_rule(global_row["id"])["intent_code"] == "i1"
    updated = repo.update_intent_rule(
        acct_row["id"], {"label": "L", "priority": 0, "enabled": False, "account_id": "acc-009"})
    assert updated["label"] == "L" and updated["priority"] == 0 and updated["enabled"] is False
    assert updated["account_id"] == "acc-001"  # 白名单外键（作用域）忽略
    assert updated["conditions_json"] == "[]"  # 未传不修改
    assert repo.delete_intent_rule(acct_row["id"]) is True
    assert repo.get_intent_rule(acct_row["id"]) is None
    assert repo.delete_intent_rule(acct_row["id"]) is False


def test_repo_sql_backend_and_migration(tmp_path, monkeypatch):
    """tmp sqlite + build_engine 迁移（test_qa_cluster_field 同款）：两新列 + intent_rules 表。"""
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from bok_voice_business_db.repository import SqlAlchemyBusinessRepository
    from control_plane import deps

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/intent.db")
    engine = deps.build_engine()
    assert engine is not None
    assert deps.build_engine() is not None  # 二跑不炸=幂等
    repo = SqlAlchemyBusinessRepository(sessionmaker(bind=engine, expire_on_commit=False)())
    with engine.connect() as conn:
        cols = {c["name"] for c in sa.inspect(conn).get_columns("call_sessions")}
    assert {"assist_status", "intent_code"} <= cols
    # 双后端同语义：两级行合并 + CRUD 回程
    global_row = repo.create_intent_rule({"name": "g", "intent_code": "i1", "account_id": ""})
    acct_row = repo.create_intent_rule({"name": "a", "intent_code": "i2", "account_id": "acc-001",
                                        "conditions_json": json.dumps(
                                            [{"fact": "confirm_count", "op": "gte", "value": 2}])})
    repo.create_intent_rule({"name": "o", "intent_code": "i3", "account_id": "acc-002"})
    rows = repo.list_intent_rules("acc-001")
    assert {r["id"] for r in rows} == {global_row["id"], acct_row["id"]}
    assert repo.get_intent_rule(acct_row["id"])["conditions_json"].startswith("[")
    patched = repo.update_intent_rule(acct_row["id"], {"intent_code": "i2b"})
    assert patched["intent_code"] == "i2b"
    assert repo.delete_intent_rule(acct_row["id"]) is True


# ---- 端点：闸链 + RBAC 三态 + 校验 ----


@pytest.fixture()
def client_with_repo(monkeypatch):
    from control_plane import main as cp_main

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr(cp_main, "_repo", lambda: repo)
    return SimpleNamespace(client=TestClient(cp_main.app), repo=repo)


def _make_user(repo, username, role, account_id="acc-001"):
    from control_plane.auth import hash_password

    return repo.create_user(username=username, password_hash=hash_password(PW), role=role,
                            org_id="" if role == "root" else "org-t", account_id=account_id)


def _login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


_GOOD_CONDS = [{"fact": "confirm_count", "op": "gte", "value": 2}]


def test_intent_rules_rbac_three_states(client_with_repo):
    client, repo = client_with_repo.client, client_with_repo.repo
    _make_user(repo, "peon", "user")
    _make_user(repo, "rooty", "root")
    uh = _login(client, "peon")
    rh = _login(client, "rooty")

    with client:  # startup 挂 audit tap（JSONL → repo），/api/audit 才查得到
        # user POST：body 塞他账号也强制本账号行
        r = client.post("/api/intent-rules", json={
            "name": "n1", "intent_code": "interested", "conditions": _GOOD_CONDS,
            "account_id": "acc-002"}, headers=uh)
        assert r.status_code == 200 and r.json()["account_id"] == "acc-001"
        # root POST account_id='' → 全局行
        r = client.post("/api/intent-rules", json={
            "name": "g1", "intent_code": "help", "conditions": _GOOD_CONDS,
            "account_id": ""}, headers=rh)
        assert r.status_code == 200 and r.json()["account_id"] == ""
        gid = r.json()["id"]
        # user PATCH 全局行 → 403（全局行仅 admin/root）
        assert client.patch(f"/api/intent-rules/{gid}", json={"label": "x"}, headers=uh).status_code == 403
        # admin PATCH 全局行 → 200；user PATCH 自己账号行 → 200
        _make_user(repo, "adm", "admin")
        ah = _login(client, "adm")
        assert client.patch(f"/api/intent-rules/{gid}", json={"label": "g"}, headers=ah).status_code == 200
        aid = [r for r in repo.intent_rules.values() if r["account_id"] == "acc-001"][0]["id"]
        assert client.patch(f"/api/intent-rules/{aid}", json={"priority": 3}, headers=uh).json()["priority"] == 3
        # 两级行合并读取（user 视角：''+本账号）
        rows = client.get("/api/intent-rules", headers=uh).json()
        assert {row["account_id"] for row in rows} == {"", "acc-001"}
        # 出仓形状：conditions 对象数组（conditions_json 不外泄）
        assert all("conditions_json" not in row and row["conditions"] == _GOOD_CONDS for row in rows)
        # 审计 intent.rule.create
        created = client.get("/api/audit", params={"action": "intent.rule.create"}).json()
    assert {row["account_id"] for row in created} == {"", "acc-001"}


def test_intent_rule_post_invalid_conditions_400(client_with_repo):
    client, repo = client_with_repo.client, client_with_repo.repo
    _make_user(repo, "rooty", "root")
    rh = _login(client, "rooty")
    r = client.post("/api/intent-rules", json={
        "name": "n", "intent_code": "x",
        "conditions": [{"fact": "bogus", "op": "gt", "value": "s"}]}, headers=rh)
    assert r.status_code == 400
    assert "fact" in r.json()["detail"] and "op" in r.json()["detail"]
    assert repo.intent_rules == {}  # 校验失败不落库


def test_intent_rule_patch_invalid_conditions_400(client_with_repo):
    client, repo = client_with_repo.client, client_with_repo.repo
    _make_user(repo, "rooty", "root")
    rh = _login(client, "rooty")
    row = repo.create_intent_rule({"name": "n", "intent_code": "x", "account_id": "acc-001"})
    # 形状合法(数组)但内容非法 → validate_conditions 400;非数组由 pydantic 422 拦。
    r = client.patch(f"/api/intent-rules/{row['id']}",
                     json={"conditions": [{"fact": "bogus", "op": "gt", "value": "s"}]}, headers=rh)
    assert r.status_code == 400
    assert repo.get_intent_rule(row["id"])["conditions_json"] == "[]"  # 原值未动


def test_intent_rule_cross_account_404_and_delete(client_with_repo):
    client, repo = client_with_repo.client, client_with_repo.repo
    _make_user(repo, "peon", "user")
    uh = _login(client, "peon")
    foreign = repo.create_intent_rule({"name": "f", "intent_code": "x", "account_id": "acc-002"})
    # 越权 by-ID：404 不泄露存在性
    assert client.patch(f"/api/intent-rules/{foreign['id']}", json={"label": "x"}, headers=uh).status_code == 404
    assert client.delete(f"/api/intent-rules/{foreign['id']}", headers=uh).status_code == 404
    # 自己账号行可删
    mine = repo.create_intent_rule({"name": "m", "intent_code": "x", "account_id": "acc-001"})
    assert client.delete(f"/api/intent-rules/{mine['id']}", headers=uh).json()["deleted"] is True


# ---- assist 端点：幂等 + 枚举 ----


def _seed_call(repo, **fields) -> str:
    from bok_voice_core.types import CallMode, SessionManifest

    cid = f"call-ir-{next(_SEQ)}"
    repo.create_call(SessionManifest(
        session_id=cid, account_id="acc-001", object_id="obj-1", persona_id="",
        mode=CallMode.LIVE, direction="outbound", language="zh", providers={}))
    if fields:
        repo.update_call(cid, **fields)
    return cid


def test_assist_idempotent_done_not_downgraded(client_with_repo):
    client, repo = client_with_repo.client, client_with_repo.repo
    cid = _seed_call(repo)
    assert client.post(f"/api/calls/{cid}/assist",
                       json={"status": "notified", "source": "intent"}).json()["assist_status"] == "notified"
    assert client.post(f"/api/calls/{cid}/assist",
                       json={"status": "done", "source": "whatsapp"}).json()["assist_status"] == "done"
    # 幂等：done 不降级 notified
    assert client.post(f"/api/calls/{cid}/assist", json={"status": "notified"}).json()["assist_status"] == "done"
    # 枚举外 400
    assert client.post(f"/api/calls/{cid}/assist", json={"status": "bogus"}).status_code == 400
    assert repo.get_call(cid)["assist_status"] == "done"


# ---- 接线：whatsapp 顺手置 / takeover 置 done / end 落列+审计 ----


def test_whatsapp_capture_sets_assist_when_empty(client_with_repo):
    client, repo = client_with_repo.client, client_with_repo.repo
    cid = _seed_call(repo)
    r = client.post(f"/api/calls/{cid}/whatsapp", json={"number": "", "channel": "whatsapp"})
    assert r.status_code == 200
    assert r.json()["whatsapp_status"] == "offered" and r.json()["assist_status"] == "notified"
    # 已有 done 不动
    cid2 = _seed_call(repo)
    repo.update_call(cid2, assist_status="done")
    r = client.post(f"/api/calls/{cid2}/whatsapp", json={"number": ""})
    assert r.json()["assist_status"] == "done"


def test_takeover_sets_assist_done(client_with_repo):
    client, repo = client_with_repo.client, client_with_repo.repo
    _make_user(repo, "adm", "admin")
    ah = _login(client, "adm")
    cid = _seed_call(repo, assist_status="notified")
    assert client.post(f"/api/supervisor/{cid}/takeover", headers=ah).status_code == 200
    assert repo.get_call(cid)["assist_status"] == "done"
    # 空 assist 也顺手置 done
    cid2 = _seed_call(repo)
    client.post(f"/api/supervisor/{cid2}/takeover", headers=ah)
    assert repo.get_call(cid2)["assist_status"] == "done"


def test_supervisor_end_records_intent_code_and_audit(client_with_repo):
    client, repo = client_with_repo.client, client_with_repo.repo
    _make_user(repo, "rooty", "root")
    rh = _login(client, "rooty")
    cid = _seed_call(repo)
    with client:
        r = client.post(f"/api/supervisor/{cid}/end?disposition=declined&intent_code=interested",
                        headers=rh)
        assert r.status_code == 200
        call = repo.get_call(cid)
        assert call["status"] == "ended" and call["intent_code"] == "interested"
        rows = client.get("/api/audit", params={"action": "supervisor.end"}).json()
    assert rows and rows[0]["detail"].get("intent_code") == "interested"
    # 空码不落列（零写入）
    cid2 = _seed_call(repo)
    with client:
        assert client.post(f"/api/supervisor/{cid2}/end", headers=rh).status_code == 200
    assert repo.get_call(cid2)["intent_code"] == ""
    # 意向码截 32
    cid3 = _seed_call(repo)
    with client:
        client.post(f"/api/supervisor/{cid3}/end?intent_code={'x' * 40}", headers=rh)
    assert repo.get_call(cid3)["intent_code"] == "x" * 32


# ---- disposition 语义零回归：自定义意向 disposition 天然安全 ----


def test_dashboard_interested_counts_answered_and_tags(client_with_repo):
    client, repo = client_with_repo.client, client_with_repo.repo
    _seed_call(repo, status="ended", disposition="interested", duration_s=45)
    _seed_call(repo, status="ended", disposition="no_answer")
    data = client.get("/api/stats/dashboard").json()
    assert data["calls"]["answered"] == 1
    assert data["calls"]["answer_rate"] == 0.5
    assert data["tags"]["disposition"] == {"interested": 1, "no_answer": 1}


def test_campaign_item_status_interested_done():
    from control_plane.campaign import item_status_for_call

    assert item_status_for_call({"status": "ended", "disposition": "interested"}) == "done"
    assert item_status_for_call({"status": "ended", "disposition": "completed"}) == "done"
    assert item_status_for_call({"status": "ended", "disposition": "no_answer"}) == "no_answer"
    assert item_status_for_call({"status": "failed", "disposition": "interested"}) == "failed"
