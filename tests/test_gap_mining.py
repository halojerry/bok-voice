"""LLM 漏网轮挖掘 + 快路覆盖率(L-①,2026-09-20)单测。

面:gap_mining 纯函数(归一/排除/快路判定/覆盖率/聚合)+ 查询面(内存仓造数)+
CP 端点(GET /api/stats/llm-gaps 形状与参数 / POST adopt 盖章幂等审计)。
造数姿势与 test_stats_dashboard.py 同款:DATABASE_URL="" 强制内存仓 +
monkeypatch control_plane.main._repo;turns 直接 create_turn(TurnEvent),
created_at 用固定宽度单调计数器(字典序=时间序,turns 无 seq 列同仓假设)。
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-gap-mining-0123456789")

ROOT = Path(__file__).resolve().parents[1]
for _p in ("packages/core", "packages/business-db"):
    _path = str(ROOT / _p)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pytest  # noqa: E402

from bok_voice_business_db.repository import InMemoryBusinessRepository  # noqa: E402
from control_plane import gap_mining as gm  # noqa: E402

_SEQ = iter(range(1, 100_000))
PW = "Passw0rd!"


# ---- 纯函数:归一化与排除 ----


def test_candidate_norm_unifies_punct_and_width():
    assert gm.candidate_norm("你哋幾時送到？") == gm.candidate_norm("你哋幾時送到?")
    assert gm.candidate_norm("幾時 送到。") == gm.candidate_norm("幾時送到")
    assert gm.candidate_norm("  幾時送到 ") == "幾時送到"


def test_candidate_norm_excludes_short_and_acks():
    # 过短(<3 归一字)与单字/双字应承
    assert gm.candidate_norm("嗯") == ""
    assert gm.candidate_norm("好") == ""
    assert gm.candidate_norm("係") == ""
    assert gm.candidate_norm("好的") == ""
    assert gm.candidate_norm("") == ""
    # ≥3 字应承语族(长度门拦不住的补拦)
    assert gm.candidate_norm("好好好") == ""
    assert gm.candidate_norm("okay") == ""
    assert gm.candidate_norm("yes") == ""
    # 正常三字问法过闸
    assert gm.candidate_norm("幾時送") == "幾時送"


def test_candidate_norm_excludes_number_dominated():
    assert gm.candidate_norm("我的單號係12345678") == ""  # ≥4 位数字 run
    assert gm.candidate_norm("電話 6123 4567") == ""  # 剥空白后成串
    assert gm.candidate_norm("單號係三七七八九零") == ""  # 中文数字归一后成 run
    assert gm.candidate_norm("1234") == ""
    # 夹零星数字的正常问法不误杀(数字占比 < 一半、无长 run)
    assert gm.candidate_norm("3天內送到嗎") == "3天內送到嗎"


def test_fastpath_and_llm_gen_sets():
    # 快路=gen ∈ {script, qa_fastpath};graph-play 复用 qa_fastpath(agent.py:4642)
    assert gm.is_fastpath_gen("script")
    assert gm.is_fastpath_gen("qa_fastpath")
    assert not gm.is_fastpath_gen("llm")
    assert not gm.is_fastpath_gen("")
    assert gm.is_llm_gen("llm")
    assert gm.is_reply_gen("")  # 旧数据保守留分母
    assert not gm.is_reply_gen("filler")
    assert not gm.is_reply_gen("interrupted")


def test_coverage_buckets_and_zero_denominator():
    rows = [
        {"gen": "script", "provider": "flow-say"},
        {"gen": "qa_fastpath", "provider": "qa-fastpath"},
        {"gen": "qa_fastpath", "provider": "graph-play"},  # 图 play 复用 qa_fastpath
        {"gen": "llm", "provider": ""},  # 纯 LLM 轮
        {"gen": "llm", "provider": "graph-jump"},  # 跳步副作用轮,回复仍是 LLM
        {"gen": "llm", "provider": "branch-jump"},
        {"gen": "filler", "provider": ""},  # 垫话账本:不进分母
        {"gen": "interrupted", "provider": ""},  # 打断残行:不进分母
        {"gen": "", "provider": ""},  # 旧数据:留分母、不归两边
    ]
    cov = gm.compute_coverage(rows)
    assert cov["turns"] == 7  # 9 行 - filler - interrupted
    assert cov["fastpath"] == 3 and cov["llm"] == 3
    assert cov["fastpath_ratio"] == round(3 / 7, 4)
    assert cov["by_gen"] == {"script": 1, "qa_fastpath": 2, "llm": 3, "": 1}
    assert cov["by_provider"] == {
        "flow-say": 1, "qa-fastpath": 1, "graph-play": 1,
        "graph-jump": 1, "branch-jump": 1,
    }
    # 分母为 0 → 0.0(不 ZeroDivisionError)
    empty = gm.compute_coverage([])
    assert empty["turns"] == 0 and empty["fastpath_ratio"] == 0.0
    only_ledger = gm.compute_coverage([{"gen": "filler", "provider": ""}])
    assert only_ledger["turns"] == 0 and only_ledger["fastpath_ratio"] == 0.0


# ---- 查询面:内存仓造数 ----


def _ts(n: int) -> str:
    """固定宽度单调时间戳(字典序=时间序,跨天不回绕——测试数据当日生成)。"""
    return f"2026-09-20T11:00:00.{n:06d}"


def _turn(cid, role, text, *, speaker="", gen="", provider="", line="a", step=0,
          lang="zh", created_at=""):
    from bok_voice_core.types import TurnEvent

    n = next(_SEQ)
    return TurnEvent(
        trace_id=cid,
        call_id=cid,
        turn_id=f"t{n}",
        role=role,
        transcript=text,
        provider=provider,
        language=lang,
        created_at=created_at or _ts(n),
        line=line,
        speaker=speaker,
        gen=gen,
        template_step=step,
    )


def _seed_call(repo, *, obj_name="张三", template_id="tpl-1", account_id="acc-001"):
    from bok_voice_core.types import CallMode, SessionManifest

    cid = f"call-gap-{next(_SEQ)}"
    obj_id = ""
    if obj_name is not None:
        obj_id = repo.create_object(account_id, {"display_name": obj_name, "phone": "+85200000001"})["id"]
    repo.create_call(
        SessionManifest(
            session_id=cid,
            account_id=account_id,
            object_id=obj_id,
            persona_id="",
            mode=CallMode.LIVE,
            direction="outbound",
            language="zh",
            providers={},
        )
    )
    if template_id:
        repo.update_call(cid, template_id=template_id)
    return cid


def _llm_exchange(cid, question, answer, *, step=3, lang="zh"):
    """客户问 → AI LLM 轮一组(created_at 单调递增)。"""
    n = next(_SEQ)
    return [
        _turn(cid, "user", question, speaker="customer", lang=lang, created_at=_ts(n)),
        _turn(cid, "assistant", answer, speaker="agent_ai", gen="llm", step=step,
              lang=lang, created_at=_ts(n + 1)),
    ]


def _report(repo, **kw):
    kw.setdefault("account_id", "acc-001")
    return gm.build_llm_gap_report(repo, **kw)


def test_report_aggregates_counts_sorts_and_threshold():
    repo = InMemoryBusinessRepository()
    for _ in range(3):
        cid = _seed_call(repo)
        for t in _llm_exchange(cid, "你哋幾時送到？", "兩到三日到。"):
            repo.create_turn(t)
        for t in _llm_exchange(cid, "可以退貨嗎", "七日內可以退。"):
            repo.create_turn(t)
    cid = _seed_call(repo)
    for t in _llm_exchange(cid, "你哋幾時送到", "兩到三日到。"):  # 同问法第 4 轮(归一同键,标点/尾差异剥掉)
        repo.create_turn(t)
    out = _report(repo, min_calls=4)
    assert out["coverage"]["turns"] == 7 and out["coverage"]["llm"] == 7
    assert out["coverage"]["fastpath_ratio"] == 0.0
    assert [g["customer_text"] for g in out["gaps"]] == ["你哋幾時送到？"]
    top = out["gaps"][0]
    assert top["count"] == 4 and top["calls"] == 4
    assert top["template_id"] == "tpl-1" and top["step"] == 3
    assert top["lang"] == "zh"
    # 「可以退貨嗎」只有 3 轮,过不了 min_calls=4
    assert all(g["count"] >= 4 for g in out["gaps"])


def test_report_template_grouping_and_empty_template_merges():
    repo = InMemoryBusinessRepository()
    for tpl in ("tpl-a", "tpl-b"):
        cid = _seed_call(repo, template_id=tpl)
        for t in _llm_exchange(cid, "运费几多钱", "首公斤二十。"):
            repo.create_turn(t)
    # 两通无模板通话:同问法合并成一组、calls 累计
    for _ in range(2):
        cid = _seed_call(repo, template_id="")
        for t in _llm_exchange(cid, "運費幾多錢", "首公斤二十元。"):
            repo.create_turn(t)
    out = _report(repo, min_calls=1)
    rows = [g for g in out["gaps"] if "費" in g["customer_text"] or "费" in g["customer_text"]]
    by_tpl = {g["template_id"]: g for g in rows}
    assert set(by_tpl) == {"tpl-a", "tpl-b", ""}
    assert by_tpl["tpl-a"]["count"] == 1 and by_tpl["tpl-b"]["count"] == 1
    assert by_tpl[""]["count"] == 2 and by_tpl[""]["calls"] == 2


def test_report_excludes_test_objects_and_objectless_calls():
    repo = InMemoryBusinessRepository()
    for obj_name in ("E2E-echo", "soak1-客户", "张三", None):
        cid = _seed_call(repo, obj_name=obj_name)
        for t in _llm_exchange(cid, "可以上门收件吗", "可以预约上门。"):
            repo.create_turn(t)
    # 只剩真实对象「张三」一通:count=1 过不了 min_calls=2,分母也只有 1 轮
    out = _report(repo, min_calls=2)
    assert out["coverage"]["turns"] == 1
    assert out["gaps"] == []
    # 真实对象第二通 → 候选过门槛
    cid = _seed_call(repo, obj_name="李四")
    for t in _llm_exchange(cid, "可以上门收件吗", "可以预约上门。"):
        repo.create_turn(t)
    out2 = _report(repo, min_calls=2)
    assert out2["coverage"]["turns"] == 2
    assert [g["count"] for g in out2["gaps"]] == [2]


def test_report_sample_answer_mode_and_fastpath_pairing():
    repo = InMemoryBusinessRepository()
    for ans in ("兩到三日到。", "兩到三日到。", "大概三日。"):
        cid = _seed_call(repo)
        for t in _llm_exchange(cid, "你哋幾時送到？", ans, step=2):
            repo.create_turn(t)
    # 快路轮夹在中间:接住客户话(不留给后面的 LLM 轮)、进快路分母
    cid = _seed_call(repo)
    seq = [
        _turn(cid, "user", "你哋幾時送到？", speaker="customer", created_at=_ts(9000)),
        _turn(cid, "assistant", "而家幫你查查。", speaker="agent_ai", gen="qa_fastpath",
              provider="qa-fastpath", step=2, created_at=_ts(9001)),
        _turn(cid, "assistant", "查到啦,要再等多陣。", speaker="agent_ai", gen="llm",
              step=2, created_at=_ts(9002)),
    ]
    for t in seq:
        repo.create_turn(t)
    out = _report(repo, min_calls=3)
    assert out["coverage"]["fastpath"] == 1 and out["coverage"]["llm"] == 4
    top = out["gaps"][0]
    assert top["count"] == 3 and top["calls"] == 3, "快路已接住的客户话不再配给 LLM 轮"
    assert top["sample_answer"] == "兩到三日到。"  # 众数
    assert top["sample_call_id"].startswith("call-gap-")
    assert top["step"] == 2 and top["lang"] == "zh"


def test_report_b_line_legacy_rows_and_template_filter():
    repo = InMemoryBusinessRepository()
    cid = _seed_call(repo, template_id="tpl-keep")
    rows = [
        # B 线轮次:不进 A 线口径
        _turn(cid, "user", "hello there friend", speaker="other", line="b",
              created_at=_ts(8001)),
        _turn(cid, "assistant", "hello, translating", speaker="agent_ai", gen="llm",
              line="b", created_at=_ts(8002)),
        # 旧数据(speaker 空):不计覆盖、不配对
        _turn(cid, "user", "你哋邊個呀", created_at=_ts(8003)),
        _turn(cid, "assistant", "我係集運專員。", gen="llm", created_at=_ts(8004)),
    ]
    for t in rows:
        repo.create_turn(t)
    for t in _llm_exchange(cid, "可以退貨嗎", "七日內可以退。"):
        repo.create_turn(t)
    out = _report(repo, min_calls=1)
    assert out["coverage"]["turns"] == 1 and out["coverage"]["llm"] == 1
    assert [g["customer_text"] for g in out["gaps"]] == ["可以退貨嗎"]
    # template_id 过滤:匹配保留、不匹配全空
    out_keep = _report(repo, min_calls=1, template_id="tpl-keep")
    assert out_keep["coverage"]["turns"] == 1
    out_miss = _report(repo, min_calls=1, template_id="tpl-other")
    assert out_miss["coverage"]["turns"] == 0 and out_miss["gaps"] == []


# ---- CP 端点 ----


@pytest.fixture()
def client_with_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane import main as cp_main

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    client = TestClient(cp_main.app)
    return SimpleNamespace(client=client, repo=repo)


def test_llm_gaps_endpoint_shape(client_with_repo):
    repo = client_with_repo.repo
    cid = _seed_call(repo)
    for t in _llm_exchange(cid, "你哋幾時送到？", "兩到三日到。"):
        repo.create_turn(t)
    r = client_with_repo.client.get("/api/stats/llm-gaps?account_id=acc-001&min_calls=1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"coverage", "gaps", "generated_at"}
    cov = body["coverage"]
    assert cov["turns"] == 1 and cov["llm"] == 1 and cov["fastpath"] == 0
    assert cov["fastpath_ratio"] == 0.0
    assert isinstance(cov["by_gen"], dict) and isinstance(cov["by_provider"], dict)
    assert body["gaps"][0]["customer_text"] == "你哋幾時送到？"
    assert "norm" not in body["gaps"][0], "内部聚合键不出仓"
    assert isinstance(body["generated_at"], int)


def test_adopt_creates_then_idempotent(client_with_repo, monkeypatch):
    from control_plane import main as cp_main

    events: list[tuple] = []
    monkeypatch.setattr(cp_main, "_audit", lambda action, **kw: events.append((action, kw)) or {})
    payload = {
        "account_id": "acc-001",
        "question_text": "你哋幾時送到？",
        "answer_text": "兩到三日到。",
        "lang": "zh",
        "template_id": "tpl-1",
        "step": 3,
    }
    r1 = client_with_repo.client.post("/api/stats/llm-gaps/adopt", json=payload)
    assert r1.status_code == 201, r1.text
    assert r1.json()["created"] is True
    rows = client_with_repo.repo.list_qa_entries("acc-001", owner_scope=None)
    assert len(rows) == 1
    row = rows[0]
    assert row["question_text"] == "你哋幾時送到？"
    assert row["answer_text"] == "兩到三日到。"
    assert row["source"] == "gap-adopt" and row["enabled"] is True
    assert row["scope"] == "global" and row["template_id"] == "tpl-1"
    assert row["owner_user_id"] == ""
    actions = [a for a, _kw in events if a == "qa_entry.create"]
    assert len(actions) == 1
    detail = [kw for a, kw in events if a == "qa_entry.create"][0]["detail"]
    assert detail["source"] == "gap-adopt" and detail["step"] == 3
    # 幂等:同问法(忽略标点差异)二次采集 → 200 created=false,不重复建
    payload["question_text"] = "你哋幾時送到"
    r2 = client_with_repo.client.post("/api/stats/llm-gaps/adopt", json=payload)
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"id": row["id"], "created": False}
    assert len(client_with_repo.repo.list_qa_entries("acc-001", owner_scope=None)) == 1
    assert len([a for a, _kw in events if a == "qa_entry.create"]) == 1


def test_adopt_lang_scoped_idempotency(client_with_repo):
    # 同问法不同语言(zh vs cantonese)是两条词条——运行时快路按 lang 过滤
    for lang in ("zh", "cantonese"):
        r = client_with_repo.client.post(
            "/api/stats/llm-gaps/adopt",
            json={"account_id": "acc-001", "question_text": "幾時送到",
                  "answer_text": "兩到三日。", "lang": lang},
        )
        assert r.status_code == 201 and r.json()["created"] is True
    rows = client_with_repo.repo.list_qa_entries("acc-001", owner_scope=None)
    assert {row["lang"] for row in rows} == {"zh", "cantonese"}


def test_adopt_user_stamps_owner_self(client_with_repo):
    from control_plane.auth import hash_password

    repo = client_with_repo.repo
    repo.create_user(username="peon", password_hash=hash_password(PW),
                     role="user", org_id="org-t", account_id="acc-001")
    login = client_with_repo.client.post(
        "/api/auth/login", json={"username": "peon", "password": PW})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    # body 试图改账号 → user 身份强制本账号+owner 本人(B3 盖章同 create_qa_entry)
    r = client_with_repo.client.post(
        "/api/stats/llm-gaps/adopt",
        json={"account_id": "acc-999", "question_text": "幾時送到",
              "answer_text": "兩到三日。", "lang": "zh"},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    rows = repo.list_qa_entries("acc-001", owner_scope=None)
    assert len(rows) == 1
    peon_id = next(u["id"] for u in repo.list_users() if u["username"] == "peon")
    assert rows[0]["owner_user_id"] == peon_id
    assert rows[0]["account_id"] == "acc-001"


def test_adopt_validates_empty_texts(client_with_repo):
    r = client_with_repo.client.post(
        "/api/stats/llm-gaps/adopt",
        json={"account_id": "acc-001", "question_text": "  ", "answer_text": "x"},
    )
    assert r.status_code == 400
    r2 = client_with_repo.client.post(
        "/api/stats/llm-gaps/adopt",
        json={"account_id": "acc-001", "question_text": "q", "answer_text": ""},
    )
    assert r2.status_code == 400


def test_gap_group_mode_tie_breaks_deterministic():
    """aggregate_gap_groups 众数并列取字典序(确定性,不依赖插入序)。"""
    groups = {
        ("幾時送到", ""): {
            "norm": "幾時送到",
            "template_id": "",
            "count": 3,
            "call_ids": {"c1", "c2", "c3"},
            "raws": Counter({"幾時送到": 3}),
            "answers": Counter({"乙答案": 1, "甲答案": 2}),
            "answer_calls": {"甲答案": "c1", "乙答案": "c2"},
            "steps": Counter({3: 2, 4: 1}),
            "langs": Counter({"zh": 3}),
        }
    }
    rows = gm.aggregate_gap_groups(groups)
    assert rows[0]["sample_answer"] == "甲答案"
    assert rows[0]["sample_call_id"] == "c1"
    assert rows[0]["step"] == 3 and rows[0]["lang"] == "zh"
