"""QA 自学习聚类 CP 端点单测(2026-09-19 W3-T1)。

dry 形状/缓存命中不二次调 LLM/apply 盖章(user→owner 本人、admin→共享)/
select 子集/单飞 409/LLM 失败 503/审计行。假 LLM=monkeypatch runner 的
_llm_chat/_discover_model(零网络);假对话=monkeypatch repo.iter_call_conversations
(与 /api/reports/qa-pairs 同源口径)。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-qa-cluster")

import httpx  # noqa: E402
import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
for _p in ("packages/core", "packages/business-db"):
    _path = str(ROOT / _p)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from control_plane import qa_cluster as qc  # noqa: E402

PW = "Passw0rd!"


@pytest.fixture(autouse=True)
def _reset_cluster_state():
    """模块级单飞标志/计划缓存跨测试隔离(与 pregen TTL 缓存 fixture 同理)。"""
    qc._plan_cache.clear()
    qc._RUNNING = False
    yield
    qc._plan_cache.clear()
    qc._RUNNING = False


# ---- fixture 组装 ----

_TARGET = {"question_text": "你们是哪家公司", "answer_text": "我们是集运中转仓。", "lang": "zh"}


def _conversations():
    """两 zh 候选(各 6 通)+ 一 en 候选(en 无词条→fresh 旁路,不出 LLM 请求)。"""
    def convo(q: str, a: str, lang: str = "zh") -> list[dict]:
        return [
            {"role": "user", "text": q, "lang": lang},
            {"role": "assistant", "text": a, "lang": lang},
        ]

    out = []
    for _ in range(6):
        out.append(convo("请问你们是哪间公司的呀？", "我们是集运中转仓。"))
        out.append(convo("可以退换货吗", "七日内可以退换。"))
        out.append(convo("how long does shipping take", "three to five days.", lang="en"))
    return out


def _make(monkeypatch, users=()):
    from fastapi.testclient import TestClient

    from bok_voice_business_db.repository import InMemoryBusinessRepository
    from control_plane import main as cp_main
    from control_plane.auth import hash_password

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr(cp_main, "_repo", lambda: repo)
    target = repo.create_qa_entry({**_TARGET, "account_id": "acc-001"})
    monkeypatch.setattr(repo, "iter_call_conversations",
                        lambda account_id, exclude_test_objects=False: _conversations())
    for u in users:
        repo.create_user(username=u["username"], password_hash=hash_password(PW),
                         role=u.get("role", "user"), org_id="org-t",
                         account_id=u.get("account", "acc-001"))
    client = TestClient(cp_main.app).__enter__()
    return client, repo, target


def _fake_llm(monkeypatch, target_id: str, decisions: str | None = None):
    """假 LLM:记录 user 消息条数;返回固定决策。"""
    calls = {"chat": 0, "discover": 0, "messages": []}

    def fake_chat(base_url, model, system, user, **kw):
        calls["chat"] += 1
        calls["messages"].append(user)
        return decisions or (
            '[{"i":0,"decision":"new","target":"","note":"全新"},'
            '{"i":1,"decision":"variant","target":"%s","note":"同义"}]' % target_id
        )

    def fake_discover(base_url):
        calls["discover"] += 1
        return "/models/fake-Qwen3-4B"

    monkeypatch.setattr(qc, "_llm_chat", fake_chat)
    monkeypatch.setattr(qc, "_discover_model", fake_discover)
    return calls


def _login(client, username: str) -> dict:
    r = client.post("/api/auth/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ---- dry ----

def test_dry_plan_shape_and_variant_payload(monkeypatch):
    client, repo, target = _make(monkeypatch)
    calls = _fake_llm(monkeypatch, target["id"])
    r = client.post("/api/qa/cluster", json={})
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["model"] == "/models/fake-Qwen3-4B"
    assert plan["counts"]["variants"] == 1 and plan["counts"]["fresh"] == 2
    assert plan["counts"]["candidates"] == 3
    assert len(plan["junk"]) == 0
    v = plan["variants"][0]
    assert v["question_text"] == "请问你们是哪间公司的呀"  # 归一化候选原话=匹配面
    assert v["answer_text"] == "我们是集运中转仓。"  # 答案继承目标词条,防漂移
    assert v["cluster_head_id"] == target["id"]  # W3-T1 新增:接同义簇
    assert v["source"] == "mined" and v["enabled"] is True and v["scope"] == "global"
    # en 无词条 → 旁路 fresh,不出 LLM 请求(zh 一批;langs 排序 en<zh,fresh[0]=en 行)
    assert calls["chat"] == 1
    fresh_qs = [row["question"] for row in plan["fresh"]]
    assert fresh_qs[0] == "howlongdoesshippingtake", "归一化剥空白"
    assert "可以退换货吗" in fresh_qs


def test_dry_cache_hit_no_second_llm(monkeypatch):
    client, repo, target = _make(monkeypatch)
    calls = _fake_llm(monkeypatch, target["id"])
    r1 = client.post("/api/qa/cluster", json={})
    r2 = client.post("/api/qa/cluster", json={})
    assert r1.status_code == r2.status_code == 200
    assert calls["chat"] == 1, "TTL 内第二个 dry 请求吃缓存,不二次调 LLM"
    assert r1.json() == r2.json()


def test_limit_clamped_to_100(monkeypatch):
    client, repo, target = _make(monkeypatch)
    _fake_llm(monkeypatch, target["id"])
    plan = client.post("/api/qa/cluster", json={"limit": 500}).json()
    assert plan["limit"] == 100


def test_model_env_override(monkeypatch):
    client, repo, target = _make(monkeypatch)
    calls = _fake_llm(monkeypatch, target["id"])
    monkeypatch.setenv("BOK_QA_CLUSTER_MODEL", "/models/override-9B")
    plan = client.post("/api/qa/cluster", json={}).json()
    assert plan["model"] == "/models/override-9B"
    assert calls["discover"] == 0, "env 直覆盖不走 /v1/models 发现"


def test_llm_failure_maps_to_503(monkeypatch):
    client, repo, target = _make(monkeypatch)

    def boom(base_url, model, system, user, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(qc, "_llm_chat", boom)
    monkeypatch.setattr(qc, "_discover_model", lambda base_url: "/models/fake-4B")
    r = client.post("/api/qa/cluster", json={})
    assert r.status_code == 503
    assert "connection refused" in r.json()["detail"], "503 文案带原因"


# ---- 单飞 ----

def test_single_flight_conflict_409(monkeypatch):
    client, repo, target = _make(monkeypatch)
    calls = _fake_llm(monkeypatch, target["id"])
    monkeypatch.setattr(qc, "_RUNNING", True)
    r = client.post("/api/qa/cluster", json={})
    assert r.status_code == 409
    assert calls["chat"] == 0
    r2 = client.post("/api/qa/cluster", json={"apply": True})
    assert r2.status_code == 409


# ---- apply ----

def test_apply_user_stamps_owner_self(monkeypatch):
    client, repo, target = _make(monkeypatch, users=[{"username": "peon", "role": "user"}])
    _fake_llm(monkeypatch, target["id"])
    client.post("/api/qa/cluster", json={})  # dry 先行(勾选采纳守卫:select 需新鲜计划)
    headers = _login(client, "peon")
    r = client.post("/api/qa/cluster", json={"apply": True, "select": [{"kind": "variant", "i": 0}]},
                    headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 1
    peon_id = next(u["id"] for u in repo.list_users() if u["username"] == "peon")
    rows = repo.list_qa_entries("acc-001")
    created = [e for e in rows if e["id"] != target["id"]]
    assert len(created) == 1
    row = created[0]
    assert row["owner_user_id"] == peon_id, "user 采纳强制 owner 本人"
    assert row["account_id"] == "acc-001"
    assert row["cluster_head_id"] == target["id"]
    assert row["source"] == "mined" and row["enabled"] is True and row["scope"] == "global"
    assert row["priority"] == 10


def test_apply_admin_stamps_shared(monkeypatch):
    client, repo, target = _make(monkeypatch, users=[{"username": "boss", "role": "admin"}])
    _fake_llm(monkeypatch, target["id"])
    client.post("/api/qa/cluster", json={})  # dry 先行(勾选采纳守卫:select 需新鲜计划)
    headers = _login(client, "boss")
    r = client.post("/api/qa/cluster", json={"apply": True, "select": [{"kind": "variant", "i": 0}]},
                    headers=headers)
    assert r.status_code == 200, r.text
    rows = repo.list_qa_entries("acc-001")
    created = [e for e in rows if e["id"] != target["id"]]
    assert len(created) == 1
    assert created[0]["owner_user_id"] == "", "admin 采纳默认共享"


def test_apply_select_subset_creates_only_selected(monkeypatch):
    client, repo, target = _make(monkeypatch)
    _fake_llm(monkeypatch, target["id"])
    client.post("/api/qa/cluster", json={})  # dry 先行(勾选采纳守卫:select 需新鲜计划)
    # fresh 顺序:en 行(en 无词条旁路)在前,zh new 行在后 → zh new=fresh[1]
    r = client.post("/api/qa/cluster", json={"apply": True, "select": [{"kind": "fresh", "i": 1}]})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1
    rows = [e for e in repo.list_qa_entries("acc-001") if e["id"] != target["id"]]
    assert len(rows) == 1
    assert rows[0]["question_text"] == "可以退换货吗"  # 选中的 fresh,variant 未被选中不落库
    assert rows[0]["cluster_head_id"] == ""
    assert rows[0]["source"] == "mined"


def test_apply_all_by_default_and_invalid_select_ignored(monkeypatch):
    client, repo, target = _make(monkeypatch)
    _fake_llm(monkeypatch, target["id"])
    client.post("/api/qa/cluster", json={})  # dry 先行(勾选采纳守卫:select 需新鲜计划)
    r = client.post("/api/qa/cluster",
                    json={"apply": True, "select": [{"kind": "variant", "i": 0},
                                                    {"kind": "variant", "i": 99},
                                                    {"kind": "bogus", "i": 0}]})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1, "越界/未知 kind 静默忽略,只建选中的 variant"


def test_apply_invalidates_cache(monkeypatch):
    """采纳成功后 dry 缓存作废:下次 dry 重算(已入库问法即 dup-existing 不再 offered)。"""
    client, repo, target = _make(monkeypatch)
    calls = _fake_llm(monkeypatch, target["id"])
    client.post("/api/qa/cluster", json={})  # dry → 缓存
    assert calls["chat"] == 1
    client.post("/api/qa/cluster", json={"apply": True, "select": [{"kind": "variant", "i": 0}]})
    client.post("/api/qa/cluster", json={})  # 缓存已被 apply 作废 → 重算
    assert calls["chat"] == 2


# ---- 审计 ----

def test_apply_writes_audit_rows(monkeypatch):
    client, repo, target = _make(monkeypatch)
    _fake_llm(monkeypatch, target["id"])
    client.post("/api/qa/cluster", json={})  # dry 先行(勾选采纳守卫:select 需新鲜计划)
    from control_plane import main as cp_main

    events: list[tuple] = []
    monkeypatch.setattr(cp_main, "_audit", lambda action, **kw: events.append((action, kw)) or {})
    client.post("/api/qa/cluster", json={"apply": True, "select": [{"kind": "variant", "i": 0}]})
    actions = [a for a, _kw in events]
    assert actions.count("qa_entry.create") == 1, "每行一条 qa_entry.create"
    summary = [kw for a, kw in events if a == "qa.cluster"]
    assert len(summary) == 1
    assert summary[0]["detail"] == {"variants": 1, "fresh": 0, "junk": 0}


# ---- 勾选采纳守卫(主会话审计修复) ----

def test_apply_select_requires_fresh_plan_409(monkeypatch):
    """缓存过期/缺席时 select 采纳=409(防下标对到重算的新计划采错条目);采纳全部可安全重算。"""
    client, repo, target = _make(monkeypatch)
    calls = _fake_llm(monkeypatch, target["id"])
    client.post("/api/qa/cluster", json={})  # dry → 缓存
    qc._plan_cache.clear()  # 模拟 TTL 过期
    r = client.post("/api/qa/cluster", json={"apply": True, "select": [{"kind": "variant", "i": 0}]})
    assert r.status_code == 409
    assert "重新生成" in r.json()["detail"]
    # select=None 的「采纳全部」不受守卫:重算后整计划采纳
    r2 = client.post("/api/qa/cluster", json={"apply": True})
    assert r2.status_code == 200, r2.text
    assert r2.json()["created"] == 3  # variants 1 + fresh 2


def test_apply_select_param_mismatch_409(monkeypatch):
    """apply 参数与 dry 不符=缓存键不同=无新鲜计划 → 409(不同参数是另一份计划)。"""
    client, repo, target = _make(monkeypatch)
    _fake_llm(monkeypatch, target["id"])
    client.post("/api/qa/cluster", json={"limit": 30})  # dry limit=30
    r = client.post("/api/qa/cluster", json={"apply": True, "limit": 60,
                                             "select": [{"kind": "variant", "i": 0}]})
    assert r.status_code == 409


def test_cache_keyed_by_params(monkeypatch):
    """缓存键=(account,min_calls,limit):同参数吃缓存,换参数重算。"""
    client, repo, target = _make(monkeypatch)
    calls = _fake_llm(monkeypatch, target["id"])
    client.post("/api/qa/cluster", json={"limit": 5})
    client.post("/api/qa/cluster", json={"limit": 5})
    assert calls["chat"] == 1, "同参数第二次吃缓存"
    client.post("/api/qa/cluster", json={"limit": 6})
    assert calls["chat"] == 2, "换参数=另一份计划,重算"
