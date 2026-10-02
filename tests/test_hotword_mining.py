"""ASR 热词沉淀（EX-H1，2026-09-28）单测。

面：hotword_mining 纯函数（三源抽取 / 数字与回声铁律 / near-miss 规范用语 /
gap n-gram 覆盖排除 / freq 排序与每语言上限 / LLM 判定解析）+ flow.py
`_REPEAT_EXPLICIT_RE` 短语族镜像（pathlib 读源钉住）+ 表 DDL/仓储（镜像
test_intent_rules 的 tmp sqlite + build_engine 幂等）+ CP 端点（GET
/api/asr/hotwords 两级合并/过滤/400；POST /api/qa/cluster hotwords 段/采纳/审计/
新鲜缓存守卫/非管理写不进全局行）。

造数姿势与 tests/test_qa_cluster_cp.py 同款（InMemoryBusinessRepository +
monkeypatch iter_call_conversations/_llm_chat/_discover_model，零网络）。CP 侧不
import agent_runtime；flow.py 只被**测试**经 pathlib 读取做镜像比对。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-hotword-mining-0123456")

ROOT = Path(__file__).resolve().parents[1]
for _p in ("packages/core", "packages/business-db", "apps/control-plane"):
    _path = str(ROOT / _p)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pytest  # noqa: E402

from bok_voice_core.qa_text import normalize_question  # noqa: E402
from control_plane import hotword_mining as hm  # noqa: E402
from control_plane import qa_cluster as qc  # noqa: E402

PW = "Passw0rd!"


@pytest.fixture(autouse=True)
def _reset_cluster_state():
    qc._plan_cache.clear()
    qc._RUNNING = False
    yield
    qc._plan_cache.clear()
    qc._RUNNING = False


# ---- 纯函数：三源抽取 ----


def _conv(text: str, lang: str = "zh", call_id: str = "call-1") -> list[dict]:
    return [
        {"role": "user", "text": text, "lang": lang, "call_id": call_id},
        {"role": "assistant", "text": "好", "lang": lang, "call_id": call_id},
    ]


def _kinds(out: list[dict]) -> set[tuple[str, str]]:
    return {(r["kind"], r["word"]) for r in out}


def test_polish_fix_candidate_from_raw_ne_polished():
    """raw「顺风」被确定性纠错吸附成「顺丰」→ 纠错 span 的词即候选。"""
    table = {"zh": {"顺丰": ["顺风"]}}
    out = hm.extract_hotword_candidates([_conv("我要顺风快递")], [], variant_table=table)
    assert ("polish_fix", "顺丰") in _kinds(out)
    cand = next(r for r in out if r["word"] == "顺丰")
    assert cand["lang"] == "zh"
    assert cand["evidence"][0]["raw"] == "我要顺风快递"
    assert cand["evidence"][0]["fixed"] == "顺丰"


def test_digit_run_candidate_dropped():
    """纠错后 span 含数字 → 数字铁律丢弃（数字绝不进热词）。"""
    table = {"zh": {"好产品1": ["好产品"]}}
    out = hm.extract_hotword_candidates([_conv("我好产品")], [], variant_table=table)
    assert all(not any(ch.isdigit() for ch in r["word"]) for r in out)
    assert hm.is_digit_dominant("123456") is True
    assert hm.is_digit_dominant("三通") is False  # 单个中文数词不误杀真词
    assert hm.is_digit_dominant("顺丰") is False


def test_echo_turn_skipped():
    """含热词词表的抄词/碎片轮（looks_garbled）整轮跳过，不产出 n-gram 候选。"""
    convs = [_conv("拼多多123", "zh", f"c{i}") for i in range(3)]
    # 不跳过时「拼多」「多多」会成 gap_ngram（freq=3）；跳过则整表空。
    assert hm.extract_hotword_candidates(convs, ["拼多多"]) == []


def test_near_miss_maps_to_canonical_phrase():
    """近似命中要求重复用语族（编辑距离 1）→ 候选=规范用语（非客户原话）。"""
    out = hm.extract_hotword_candidates([_conv("你说什幺")], [])
    near = [r for r in out if r["kind"] == "near_miss"]
    assert any(r["word"] == "你说什么" for r in near)
    cand = next(r for r in near if r["word"] == "你说什么")
    assert cand["evidence"][0]["fixed"] == "你说什么"


def test_gap_ngram_needs_three_and_qa_coverage_excludes():
    """gap n-gram：≥3 次才出；qa 词条问法覆盖（归一子串）则排除。"""
    two = [_conv("我要退货退款", "zh", f"c{i}") for i in range(2)]
    assert not any("退货" in r["word"] for r in hm.extract_hotword_candidates(two, []))

    three = [_conv("我要退货退款", "zh", f"c{i}") for i in range(3)]
    out = hm.extract_hotword_candidates(three, [])
    assert any(r["word"] == "退货" and r["kind"] == "gap_ngram" and r["freq"] == 3 for r in out)

    covered = hm.extract_hotword_candidates(
        three, [], existing_q_norms={normalize_question("我要退货退款")}
    )
    assert not any("退货" in r["word"] for r in covered)


def test_freq_ordering_and_per_lang_cap():
    """每语言按 freq 降序、截前 PER_LANG_CAP。"""
    words = [f"word{chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(25)]
    convs: list[list[dict]] = []
    for i, w in enumerate(words):
        for j in range(25 - i):
            convs.append(_conv(w, "en", f"e{i}-{j}"))
    out = hm.extract_hotword_candidates(convs, [])
    en = [r for r in out if r["lang"] == "en"]
    # 25 个词各出现 25..1 次；<3 次的（后 2 个）本就不出，cap 再把 top 收到 20。
    assert len(en) == hm.PER_LANG_CAP
    freqs = [r["freq"] for r in en]
    assert freqs == sorted(freqs, reverse=True)
    assert en[0]["word"] == "wordaa" and en[0]["freq"] == 25


# ---- 纯函数：LLM 判定解析 ----


def test_parse_hotword_plan_fenced_and_garbled():
    fenced = (
        '```json\n[{"i":0,"verdict":"adopt","reason":"品牌"},'
        '{"i":1,"verdict":"reject","reason":"语气词"}]\n```'
    )
    d = hm.parse_hotword_plan(fenced)
    assert d[0]["verdict"] == "adopt" and d[1]["verdict"] == "reject"
    assert hm.parse_hotword_plan("no json here") == {}
    assert hm.parse_hotword_plan('[{"i":0,"verdict":"maybe"}]') == {}
    assert hm.parse_hotword_plan('prose [{"i":true,"verdict":"adopt"}] tail') == {}


def test_build_messages_and_attach_verdicts_default_reject():
    cands = [{"word": "拼多多", "lang": "zh", "kind": "gap_ngram", "freq": 7, "evidence": []}]
    msg = hm.build_hotword_messages(cands)
    assert "拼多多" in msg and '"i": 0' in msg
    rows = hm.attach_verdicts(cands, {0: {"verdict": "adopt", "reason": "品牌"}})
    assert rows[0]["verdict"] == "adopt" and rows[0]["reason"] == "品牌"
    unjudged = hm.attach_verdicts(cands, {})
    assert unjudged[0]["verdict"] == "reject"


# ---- 镜像 pin：REPEAT_PHRASES == flow.py _REPEAT_EXPLICIT_RE 短语 ----


def test_repeat_phrases_mirror_flow_regex():
    """pathlib 读 flow.py 源，抽出 `_REPEAT_EXPLICIT_RE` 的 alternation → 逐项比对。

    CP 不 import agent_runtime（云端镜像不含 apps/agent），故短语族以字节镜像
    常量 `hm.REPEAT_PHRASES` 承载，本测试钉住镜像不漂移（改 flow.py 该正则须同步）。
    """
    src = (ROOT / "apps/agent/agent_runtime/flow.py").read_text(encoding="utf-8")
    m = re.search(
        r"_REPEAT_EXPLICIT_RE\s*=\s*re\.compile\(\s*(.*?)\s*,\s*re\.IGNORECASE",
        src,
        re.S,
    )
    assert m is not None, "flow.py _REPEAT_EXPLICIT_RE 未找到（改了形状须同步本测试）"
    literals = re.findall(r'r"((?:[^"\\]|\\.)*)"', m.group(1))
    pattern = "".join(literals)
    assert pattern.startswith("(") and pattern.endswith(")")
    flow_phrases = [t for t in pattern[1:-1].split("|") if t]
    assert list(hm.REPEAT_PHRASES) == flow_phrases


# ---- 表 DDL / 仓储（镜像 test_intent_rules） ----


def test_repo_sql_hotword_table_and_upsert(tmp_path, monkeypatch):
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from bok_voice_business_db.repository import SqlAlchemyBusinessRepository
    from control_plane import deps

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/hotword.db")
    engine = deps.build_engine()
    assert engine is not None
    assert deps.build_engine() is not None  # 二跑不炸=幂等
    with engine.connect() as conn:
        assert sa.inspect(conn).has_table("hotword_entries")
    repo = SqlAlchemyBusinessRepository(sessionmaker(bind=engine, expire_on_commit=False)())

    glob = repo.upsert_hotword_entry({"account_id": "", "lang": "zh", "word": "拼多多", "freq": 2})
    acct = repo.upsert_hotword_entry({"account_id": "acc-001", "lang": "zh", "word": "顺丰", "freq": 3})
    repo.upsert_hotword_entry({"account_id": "acc-002", "lang": "zh", "word": "圆通", "freq": 1})
    rows = repo.list_hotword_entries("acc-001")
    assert {r["id"] for r in rows} == {glob["id"], acct["id"]}  # 两级合并,不含别账号
    # 幂等:同 freq 再 upsert=created/changed 双 False;bump=changed True。
    again = repo.upsert_hotword_entry({"account_id": "", "lang": "zh", "word": "拼多多", "freq": 2})
    assert again["created"] is False and again["changed"] is False
    bumped = repo.upsert_hotword_entry({"account_id": "", "lang": "zh", "word": "拼多多", "freq": 5})
    assert bumped["changed"] is True and bumped["freq"] == 5


# ---- CP 端点 harness ----


_CONVOS = [_conv("我要退货退款", "zh", f"call-{i}") for i in range(3)]


def _make(monkeypatch, users=()):
    from fastapi.testclient import TestClient

    from bok_voice_business_db.repository import InMemoryBusinessRepository
    from control_plane import main as cp_main
    from control_plane.auth import hash_password

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr(cp_main, "_repo", lambda: repo)
    monkeypatch.setattr(
        repo, "iter_call_conversations",
        lambda account_id, exclude_test_objects=False: _CONVOS,
    )
    for u in users:
        repo.create_user(
            username=u["username"], password_hash=hash_password(PW),
            role=u.get("role", "user"), org_id="org-t", account_id=u.get("account", "acc-001"),
        )
    client = TestClient(cp_main.app).__enter__()
    return client, repo


def _fake_llm(monkeypatch):
    calls = {"chat": 0, "hotword": 0}

    def fake_chat(base_url, model, system, user, **kw):
        if "热词" in str(system or ""):
            calls["hotword"] += 1
            return "[]"  # 无判定 → 候选默认 reject（本文件不靠 verdict 决定可采纳）
        calls["chat"] += 1
        return "[]"

    monkeypatch.setattr(qc, "_llm_chat", fake_chat)
    monkeypatch.setattr(qc, "_discover_model", lambda base_url: "/models/fake-Qwen3-4B")
    return calls


def _login(client, username: str) -> dict:
    r = client.post("/api/auth/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ---- GET /api/asr/hotwords ----


def test_get_hotwords_two_tier_and_filters(monkeypatch):
    client, repo = _make(monkeypatch)
    repo.upsert_hotword_entry({"account_id": "", "lang": "zh", "word": "拼多多", "enabled": True, "freq": 5})
    repo.upsert_hotword_entry({"account_id": "acc-001", "lang": "zh", "word": "顺丰", "enabled": True, "freq": 3})
    repo.upsert_hotword_entry({"account_id": "acc-001", "lang": "zh", "word": "停用词", "enabled": False, "freq": 9})
    repo.upsert_hotword_entry({"account_id": "acc-001", "lang": "en", "word": "refund", "enabled": True, "freq": 2})
    repo.upsert_hotword_entry({"account_id": "acc-001", "lang": "zh", "word": "123456", "enabled": True, "freq": 1})

    r = client.get("/api/asr/hotwords?account_id=acc-001&lang=zh")
    assert r.status_code == 200, r.text
    words = r.json()["words"]
    assert "拼多多" in words and "顺丰" in words  # 全局行 ∪ 账号行
    assert "停用词" not in words  # enabled=False 不取
    assert "refund" not in words  # lang 过滤
    assert "123456" not in words  # 数字铁律服务端滤除


def test_get_hotwords_global_only_and_lang_required(monkeypatch):
    client, repo = _make(monkeypatch)
    repo.upsert_hotword_entry({"account_id": "", "lang": "zh", "word": "拼多多", "enabled": True, "freq": 1})
    repo.upsert_hotword_entry({"account_id": "acc-001", "lang": "zh", "word": "顺丰", "enabled": True, "freq": 1})
    r = client.get("/api/asr/hotwords?account_id=&lang=zh")
    assert r.status_code == 200 and r.json()["words"] == ["拼多多"]  # ''=仅全局行
    assert client.get("/api/asr/hotwords?account_id=acc-001").status_code == 400  # lang 必填
    assert client.get("/api/asr/hotwords?account_id=acc-001&lang=fr").status_code == 400


# ---- POST /api/qa/cluster：hotwords 段 / 采纳 / 审计 / 守卫 ----


def test_dry_plan_has_hotwords_section(monkeypatch):
    client, repo = _make(monkeypatch)
    _fake_llm(monkeypatch)
    r = client.post("/api/qa/cluster", json={})
    assert r.status_code == 200, r.text
    plan = r.json()
    assert "hotwords" in plan
    assert plan["hotwords"]["counts"]["candidates"] >= 1
    c = plan["hotwords"]["candidates"][0]
    assert {"word", "lang", "freq", "kind", "verdict", "reason", "evidence"} <= set(c)


def test_apply_hotwords_stamps_and_audits(monkeypatch):
    client, repo = _make(monkeypatch, users=[{"username": "agent1", "role": "user"}])
    _fake_llm(monkeypatch)
    plan = client.post("/api/qa/cluster", json={}).json()
    cands = plan["hotwords"]["candidates"]
    assert cands
    word, freq = cands[0]["word"], cands[0]["freq"]

    from control_plane import main as cp_main

    events: list[tuple] = []
    monkeypatch.setattr(cp_main, "_audit", lambda action, **kw: events.append((action, kw)) or {})

    headers = _login(client, "agent1")
    r = client.post(
        "/api/qa/cluster",
        json={"apply": True, "select": [], "hotword_select": [0]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["hotwords_created"] == 1
    rows = repo.list_hotword_entries("acc-001", enabled=True)
    assert any(x["word"] == word and x["source"] == "mined" for x in rows)
    hw_events = [kw for a, kw in events if a == "hotword.create"]
    assert len(hw_events) == 1
    assert hw_events[0]["detail"] == {"source": "qa-cluster", "freq": freq}


def test_apply_hotword_select_requires_fresh_plan_409(monkeypatch):
    client, repo = _make(monkeypatch)
    _fake_llm(monkeypatch)
    client.post("/api/qa/cluster", json={})  # dry → 缓存
    qc._plan_cache.clear()  # 模拟 TTL 过期
    r = client.post("/api/qa/cluster", json={"apply": True, "hotword_select": [0]})
    assert r.status_code == 409


def test_apply_hotwords_non_admin_scoped_no_global_row(monkeypatch):
    """user 带 account_id='' 也被 scoped 到本账号 → 采纳落本账号行，产不出全局行。"""
    client, repo = _make(monkeypatch, users=[{"username": "agent2", "role": "user"}])
    _fake_llm(monkeypatch)
    headers = _login(client, "agent2")
    client.post("/api/qa/cluster?account_id=", json={}, headers=headers)
    r = client.post(
        "/api/qa/cluster?account_id=",
        json={"apply": True, "select": [], "hotword_select": [0]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert repo.list_hotword_entries("", enabled=True) == []  # 无全局行
    assert repo.list_hotword_entries("acc-001", enabled=True)  # 落在本账号


def test_apply_hotwords_idempotent_no_audit(monkeypatch):
    """同计划同下标二次采纳=幂等 no-op（changed False）→ 不审计。"""
    from types import SimpleNamespace

    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    plan = {
        "hotwords": {
            "candidates": [
                {"word": "拼多多", "lang": "zh", "freq": 4, "kind": "gap_ngram",
                 "verdict": "adopt", "reason": "", "evidence": []}
            ]
        },
        "junk": [],
        "model": "",
    }
    events: list[tuple] = []
    audit = lambda action, **kw: events.append((action, kw)) or {}
    req = SimpleNamespace(state=SimpleNamespace(identity=None, machine=False), headers={})

    r1 = qc.apply_cluster(repo, req, "acc-001", plan, None, [0], audit=audit)
    assert r1["hotwords_created"] == 1
    n1 = len([a for a, _ in events if a == "hotword.create"])
    r2 = qc.apply_cluster(repo, req, "acc-001", plan, None, [0], audit=audit)
    assert r2["hotwords_created"] == 0
    n2 = len([a for a, _ in events if a == "hotword.create"])
    assert n2 == n1 == 1  # 二次 no-op 不追加审计
