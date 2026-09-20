"""问答词条漂移提案「改答案/删词条」(L-③,2026-09-20)单测。

面:qa_drift 纯函数(数字判定/归一键/键派生/四类提案判定与互斥/人话文案)
+ 轮次状态机(_walk_turns 的 occurrences/fired/repeats/llm_answer)
+ 查询面(内存仓造数)+ CP 端点(GET 形状与 reports 闸 / POST adopt 改答案·删词条·
补录音触发·键校验·归属闸·幂等)。

造数姿势与 test_gap_mining.py 同款:DATABASE_URL="" 强制内存仓 +
monkeypatch control_plane.main._repo;turns 直接 create_turn(TurnEvent),
created_at 用固定宽度单调计数器(字典序=时间序,turns 无 seq 列同仓假设)。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
# 占位值刻意用重复串（熵 3.23 < gitleaks 阈值 3.5）：早期写法
# "test-secret-for-qa-drift-0123456789" 尾部的递增数字串把熵抬到 4.18，
# 触发密钥门禁 generic-api-key 误报（.gitleaks.toml 里逐条登记过）。
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-qa-drift-test-secret-qa-drift")

ROOT = Path(__file__).resolve().parents[1]
for _p in ("packages/core", "packages/business-db"):
    _path = str(ROOT / _p)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pytest  # noqa: E402

from bok_voice_business_db.repository import InMemoryBusinessRepository  # noqa: E402
from control_plane import qa_drift as qd  # noqa: E402

_SEQ = iter(range(1, 100_000))
PW = "Passw0rd!"

_Q_LATE = "幾時送到"


# ---- 纯函数:数字判定与归一键 ----


def test_has_digit_run_ascii_and_cjk():
    assert qd.has_digit_run("單號 1234")
    assert qd.has_digit_run("三七七八九零")  # 中文数字归一后成 run
    assert qd.has_digit_run("1234")
    assert not qd.has_digit_run("幾時送到")
    assert not qd.has_digit_run("3天內到嗎")  # 单数字不成 run(与运行时闸同口径)
    assert not qd.has_digit_run("")
    assert not qd.has_digit_run(None)


def test_qa_norm_strips_punct_and_width():
    assert qd.qa_norm({"question_text": "幾時送到？"}) == qd.qa_norm({"question_text": "幾時送到?"})
    assert qd.qa_norm({"question_text": " 幾時 送到 "}) == "幾時送到"
    assert qd.qa_norm({}) == ""
    assert qd.qa_norm(None) == ""
    # 体检口径用 normalize_question(不是 candidate_norm)——带数字的问法照样归一,
    # 才能被看见并按 digits_bypass 报出来。
    assert qd.qa_norm({"question_text": "單號 1234"}) == "單號1234"


def test_proposal_key_shape():
    assert qd.proposal_key(qd.KIND_REANSWER, "qa:abc") == "reanswer|qa:abc"
    assert qd.proposal_key(qd.KIND_RETIRE, "qa:abc") == "retire|qa:abc"


# ---- 纯函数:四类判定 + 互斥 + 健康不出提案 ----


def _counters(*, occurrences=None, fired=None, repeats=None, llm_answer=None):
    c = qd.drift_counters()
    c["occurrences"].update(occurrences or {})
    c["fired"].update(fired or {})
    c["repeats"].update(repeats or {})
    c["llm_answer"].update(llm_answer or {})
    return c


def _entry(q="幾時送到", a="兩到三日。", **kw):
    row = {"id": "qa:1", "question_text": q, "answer_text": a, "lang": "zh"}
    row.update(kw)
    return row


def test_never_asked_is_retire():
    p = qd.build_drift_proposal(_entry(), _counters(), window_calls=50)
    assert p["kind"] == qd.KIND_RETIRE and p["reason"] == qd.RS_NEVER_ASKED
    assert p["occurrences"] == 0 and p["suggested_answer"] == ""
    assert "50" in p["headline"]  # 口径透明:最近 50 通没人这么问过


def test_digits_bypass_is_retire_even_when_asked():
    # 问法带 ≥4 位数字 → 运行时数字闸旁路,词条永远命中不了 → 删
    p = qd.build_drift_proposal(
        _entry(q="單號 1234"),
        _counters(occurrences={"單號1234": 3}, fired={"單號1234": 0}),
        window_calls=10,
    )
    assert p["kind"] == qd.KIND_RETIRE and p["reason"] == qd.RS_DIGITS_BYPASS
    assert p["occurrences"] == 3


def test_repeat_after_play_is_reanswer_with_llm_suggestion():
    p = qd.build_drift_proposal(
        _entry(a="兩到三日。"),
        _counters(occurrences={_Q_LATE: 3}, fired={_Q_LATE: 3}, repeats={_Q_LATE: 2},
                  llm_answer={_Q_LATE: "大概三日內會到。"}),
        window_calls=10,
    )
    assert p["kind"] == qd.KIND_REANSWER and p["reason"] == qd.RS_REPEAT_AFTER_PLAY
    assert p["suggested_answer"] == "大概三日內會到。"
    assert p["current_answer"] == "兩到三日。"
    assert "2" in p["headline"] and "3" in p["headline"]  # 次数进人话


def test_never_fired_is_reanswer_and_names_both_causes():
    p = qd.build_drift_proposal(
        _entry(),
        _counters(occurrences={_Q_LATE: 4}, fired={_Q_LATE: 0},
                  llm_answer={_Q_LATE: "三日左右。"}),
        window_calls=10,
    )
    assert p["kind"] == qd.KIND_REANSWER and p["reason"] == qd.RS_NEVER_FIRED
    # 判据分不清缺料/被抢出场 → 两个可能都写进 detail(不假装能分)
    assert "录音" in p["detail"] and "抢" in p["detail"]


def test_never_fired_needs_min_occurrences():
    """只出现一两次就断言「快答没用上」是噪声(真栈 150 通实弹:occ=1 的两条正是如此)。"""
    for occ in (1, 2):
        assert qd.build_drift_proposal(
            _entry(),
            _counters(occurrences={_Q_LATE: occ}, fired={_Q_LATE: 0}),
            window_calls=10,
        ) is None, f"occ={occ} 不该出提案"
    now = qd.build_drift_proposal(
        _entry(),
        _counters(occurrences={_Q_LATE: qd.NEVER_FIRED_MIN_OCCURRENCES}, fired={_Q_LATE: 0}),
        window_calls=10,
    )
    assert now is not None and now["reason"] == qd.RS_NEVER_FIRED


def test_cross_language_answer_is_not_suggested():
    """建议答案必须与词条同语言——跨语言建议罐头化后会播出错语言的录音。"""
    counters = _counters(occurrences={_Q_LATE: 3}, fired={_Q_LATE: 3},
                         repeats={_Q_LATE: 2}, llm_answer={_Q_LATE: "我哋會跟進。"})
    counters["llm_answer_lang"][_Q_LATE] = "cantonese"  # 词条是 zh
    p = qd.build_drift_proposal(_entry(), counters, window_calls=10)
    # repeat 是铁证 → 提案照出,但不给错语言的建议,提示运营自己写
    assert p is not None and p["reason"] == qd.RS_REPEAT_AFTER_PLAY
    assert p["suggested_answer"] == ""
    assert "自己写" in p["detail"]


def test_same_language_answer_is_suggested():
    counters = _counters(occurrences={_Q_LATE: 3}, fired={_Q_LATE: 3},
                         repeats={_Q_LATE: 2}, llm_answer={_Q_LATE: "三日內到。"})
    counters["llm_answer_lang"][_Q_LATE] = "zh"
    p = qd.build_drift_proposal(_entry(), counters, window_calls=10)
    assert p["suggested_answer"] == "三日內到。"


def test_never_fired_without_usable_suggestion_still_warns_to_pregen():
    """never_fired 本就不是「改答案」而是「这条没生效」→ 没建议也照出,提示补录音。"""
    counters = _counters(occurrences={_Q_LATE: 5}, fired={_Q_LATE: 0})
    counters["llm_answer"][_Q_LATE] = "唔好意思。"
    counters["llm_answer_lang"][_Q_LATE] = "cantonese"
    p = qd.build_drift_proposal(_entry(), counters, window_calls=10)
    assert p is not None and p["reason"] == qd.RS_NEVER_FIRED
    assert p["suggested_answer"] == ""
    assert "补录音" in p["detail"]


def test_healthy_entry_yields_no_proposal():
    # 出现过且每次都播了快答 → 不打扰
    assert qd.build_drift_proposal(
        _entry(),
        _counters(occurrences={_Q_LATE: 5}, fired={_Q_LATE: 5}),
        window_calls=10,
    ) is None


def test_suggestion_equal_to_current_yields_no_proposal():
    # LLM 说的就是这条答案本身 → 没有可填内容,出提案只会让运营白点一次
    assert qd.build_drift_proposal(
        _entry(a="兩到三日。"),
        _counters(occurrences={_Q_LATE: 2}, fired={_Q_LATE: 1}, repeats={_Q_LATE: 1},
                  llm_answer={_Q_LATE: "兩到三日。"}),
        window_calls=10,
    ) is None


def test_entry_without_id_or_question_skipped():
    assert qd.build_drift_proposal({"id": "", "question_text": _Q_LATE}, _counters(), window_calls=1) is None
    assert qd.build_drift_proposal({"id": "qa:1", "question_text": ""}, _counters(), window_calls=1) is None


def test_priority_never_asked_beats_digits_beats_never_fired():
    # 同一词条同时满足多条 → 按序取首个(确定性,不随 dict 序变)
    p = qd.build_drift_proposal(_entry(q="單號 1234"), _counters(), window_calls=3)
    assert p["reason"] == qd.RS_NEVER_ASKED
    p2 = qd.build_drift_proposal(
        _entry(q="單號 1234"),
        _counters(occurrences={"單號1234": 2}, repeats={"單號1234": 2}),
        window_calls=3,
    )
    assert p2["reason"] == qd.RS_DIGITS_BYPASS


# ---- 轮次状态机 ----


def _ts(n: int) -> str:
    return f"2026-09-20T11:00:00.{n:06d}"


def _turn(cid, role, text, *, speaker="", gen="", provider="", line="a", lang="zh", created_at=""):
    from bok_voice_core.types import TurnEvent

    n = next(_SEQ)
    return TurnEvent(
        trace_id=cid, call_id=cid, turn_id=f"t{n}", role=role, transcript=text,
        provider=provider, language=lang, created_at=created_at or _ts(n),
        line=line, speaker=speaker, gen=gen,
    )


def _seed_call(repo, *, obj_name="张三", account_id="acc-001"):
    from bok_voice_core.types import CallMode, SessionManifest

    cid = f"call-drift-{next(_SEQ)}"
    obj_id = ""
    if obj_name is not None:
        obj_id = repo.create_object(account_id, {"display_name": obj_name, "phone": "+85200000001"})["id"]
    repo.create_call(SessionManifest(
        session_id=cid, account_id=account_id, object_id=obj_id, persona_id="",
        mode=CallMode.LIVE, direction="outbound", language="zh", providers={},
    ))
    return cid


def _canned(cid, q, a):
    """客户问 → AI 播罐头快答 一组。"""
    n = next(_SEQ)
    return [
        _turn(cid, "user", q, speaker="customer", created_at=_ts(n)),
        _turn(cid, "assistant", a, speaker="agent_ai", gen="qa_fastpath",
              provider="qa-fastpath", created_at=_ts(n + 1)),
    ]


def _llm(cid, q, a):
    n = next(_SEQ)
    return [
        _turn(cid, "user", q, speaker="customer", created_at=_ts(n)),
        _turn(cid, "assistant", a, speaker="agent_ai", gen="llm", created_at=_ts(n + 1)),
    ]


def _walk(repo, cid):
    c = qd.drift_counters()
    qd._walk_turns(repo.get_turns(cid), c)
    return c


def test_walk_counts_fired_repeats_and_llm_answer():
    repo = InMemoryBusinessRepository()
    cid = _seed_call(repo)
    for t in _canned(cid, _Q_LATE, "兩到三日。"):
        repo.create_turn(t)
    for t in _llm(cid, _Q_LATE, "大概三日內到。"):  # 播了罐头客户又问一遍 → 复问
        repo.create_turn(t)
    c = _walk(repo, cid)
    n = qd.qa_norm(_entry())
    assert c["occurrences"][n] == 2
    assert c["fired"][n] == 1
    assert c["repeats"][n] == 1
    assert c["llm_answer"][n] == "大概三日內到。"


def test_walk_ignores_non_reply_ledger_rows_for_pairing():
    """垫话/打断账本行不是「对这条的回复」——不记 fired、也不消费掉待配对客户轮。"""
    repo = InMemoryBusinessRepository()
    cid = _seed_call(repo)
    n = next(_SEQ)
    from bok_voice_core.types import TurnEvent

    rows = [
        _turn(cid, "user", _Q_LATE, speaker="customer", created_at=_ts(n)),
        TurnEvent(trace_id=cid, call_id=cid, turn_id="tf", role="assistant",
                  transcript="我先查一下", speaker="agent_ai", gen="filler",
                  created_at=_ts(n + 1), line="a", language="zh"),
        _turn(cid, "assistant", "兩到三日。", speaker="agent_ai", gen="qa_fastpath",
              created_at=_ts(n + 2)),
    ]
    for t in rows:
        repo.create_turn(t)
    c = _walk(repo, cid)
    key = qd.qa_norm(_entry())
    assert c["occurrences"][key] == 1
    assert c["fired"][key] == 1, "垫话不该吃掉待配对的客户轮(真快答仍应记 fired)"
    assert c["repeats"][key] == 0


def test_walk_script_reply_is_not_a_qa_fire():
    """脚本直念(开场白/心跳/直念步)不是这条快答的出口 → 不记 fired。"""
    repo = InMemoryBusinessRepository()
    cid = _seed_call(repo)
    n = next(_SEQ)
    rows = [
        _turn(cid, "user", _Q_LATE, speaker="customer", created_at=_ts(n)),
        _turn(cid, "assistant", "你好", speaker="agent_ai", gen="script",
              provider="flow-say", created_at=_ts(n + 1)),
    ]
    for t in rows:
        repo.create_turn(t)
    c = _walk(repo, cid)
    key = qd.qa_norm(_entry())
    assert c["occurrences"][key] == 1 and c["fired"][key] == 0


def test_walk_ignores_b_line_and_customer_side_rows():
    repo = InMemoryBusinessRepository()
    cid = _seed_call(repo)
    for t in [
        _turn(cid, "user", _Q_LATE, speaker="customer", line="b"),
        _turn(cid, "assistant", "x", speaker="agent_ai", gen="qa_fastpath", line="b"),
        _turn(cid, "assistant", "y", speaker="customer", gen="script", line="a"),
    ]:
        repo.create_turn(t)
    c = _walk(repo, cid)
    assert dict(c["occurrences"]) == {} and dict(c["fired"]) == {}


# ---- 查询面 ----


def _report(repo, **kw):
    kw.setdefault("account_id", "acc-001")
    return qd.build_qa_drift_report(repo, **kw)


def _mk_entry(repo, q, a, **kw):
    data = {"account_id": "acc-001", "question_text": q, "answer_text": a, "lang": "zh"}
    data.update(kw)
    return repo.create_qa_entry(data)


def test_report_classifies_entries_and_reports_window():
    repo = InMemoryBusinessRepository()
    e_late = _mk_entry(repo, _Q_LATE, "兩到三日。")
    e_dead = _mk_entry(repo, "可以退貨嗎", "七日內可以退。")
    e_ok = _mk_entry(repo, "送貨上門嗎", "送上門的。")
    for _ in range(2):
        cid = _seed_call(repo)
        for t in _canned(cid, _Q_LATE, "兩到三日。"):
            repo.create_turn(t)
        for t in _llm(cid, _Q_LATE, "大概三日內到。"):
            repo.create_turn(t)
        for t in _canned(cid, "送貨上門嗎", "送上門的。"):
            repo.create_turn(t)
    out = _report(repo)
    assert out["window_calls"] == 2
    assert out["entries_scanned"] == 3
    by_id = {p["qa_id"]: p for p in out["proposals"]}
    assert by_id[e_late["id"]]["reason"] == qd.RS_REPEAT_AFTER_PLAY
    assert by_id[e_dead["id"]]["reason"] == qd.RS_NEVER_ASKED
    assert e_ok["id"] not in by_id, "健康的词条不该出提案"
    assert out["counts"] == {qd.KIND_REANSWER: 1, qd.KIND_RETIRE: 1}


def test_report_sorts_reanswer_first_then_by_occurrences():
    repo = InMemoryBusinessRepository()
    a = _mk_entry(repo, "幾時送到", "A。")
    b = _mk_entry(repo, "點解咁慢", "B。")
    c = _mk_entry(repo, "冇人問過嘅問題", "C。")
    cid = _seed_call(repo)
    for _ in range(4):
        for t in _llm(cid, "幾時送到", "三日。"):
            repo.create_turn(t)
    for _ in range(3):
        for t in _llm(cid, "點解咁慢", "唔好意思。"):
            repo.create_turn(t)
    out = _report(repo)
    ids = [p["qa_id"] for p in out["proposals"]]
    assert ids[0] == a["id"], "出现次数多的改答案提案排最前"
    assert ids[1] == b["id"]
    assert ids[2] == c["id"], "删词条排最后(改答案更紧急)"
    assert out["proposals"][0]["kind"] == qd.KIND_REANSWER
    assert [p["occurrences"] for p in out["proposals"][:2]] == [4, 3]


def test_report_skips_disabled_entries():
    """停用词条是运营的明确意图,不去打扰。"""
    repo = InMemoryBusinessRepository()
    _mk_entry(repo, "冇人問過嘅問題", "C。", enabled=False)
    out = _report(repo)
    assert out["entries_scanned"] == 0 and out["proposals"] == []


def test_report_excludes_test_objects():
    """测试对象通话不进分析面(与 gap_mining 同口径,两张报表才可比)。"""
    repo = InMemoryBusinessRepository()
    _mk_entry(repo, _Q_LATE, "兩到三日。")
    cid = _seed_call(repo, obj_name="E2E-测试对象")
    for t in _llm(cid, _Q_LATE, "三日。"):
        repo.create_turn(t)
    out = _report(repo)
    assert out["window_calls"] == 0
    assert out["proposals"][0]["reason"] == qd.RS_NEVER_ASKED


def test_report_max_calls_bounds_window():
    repo = InMemoryBusinessRepository()
    _mk_entry(repo, _Q_LATE, "兩到三日。")
    # 内存仓 create_call 不盖 created_at(None)→ 窗口排序无时间依据,显式盖戳让
    # 「最近 N 通」这件事可测(生产 SQL 侧 created_at 由列默认值盖)。
    older = _seed_call(repo)
    for t in _canned(older, _Q_LATE, "兩到三日。"):
        repo.create_turn(t)
    newer = []
    for _ in range(2):
        cid = _seed_call(repo)
        newer.append(cid)
        for t in _llm(cid, "送貨上門嗎", "送上門。"):
            repo.create_turn(t)
    repo.calls[older]["created_at"] = "2026-09-20T10:00:00"
    for i, cid in enumerate(newer):
        repo.calls[cid]["created_at"] = f"2026-09-20T1{i + 1}:00:00"
    wide = _report(repo, max_calls=3)
    assert wide["window_calls"] == 3 and wide["proposals"] == [], "窗口含最早那通 → 词条健康"
    narrow = _report(repo, max_calls=2)
    assert narrow["window_calls"] == 2
    assert narrow["proposals"][0]["reason"] == qd.RS_NEVER_ASKED, "窗口缩到最近 2 通 → 没人问过"
    # 排序确定性:created_at 并列为空时,id 兜底让「最近 N」可复现(不会因稳定排序
    # 按原序截断而误留最旧 N 通)。
    for cid in [older, *newer]:
        repo.calls[cid]["created_at"] = ""
    again = _report(repo, max_calls=2)
    assert [p["key"] for p in again["proposals"]] == [p["key"] for p in _report(repo, max_calls=2)["proposals"]]


def test_report_is_deterministic():
    repo = InMemoryBusinessRepository()
    for q in ("幾時送到", "點解咁慢", "可以退貨嗎"):
        _mk_entry(repo, q, "答案。")
    first = _report(repo)
    second = _report(repo)
    assert [p["key"] for p in first["proposals"]] == [p["key"] for p in second["proposals"]]
    assert first["counts"] == second["counts"]


# ---- CP 端点 ----


@pytest.fixture()
def client_with_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane import main as cp_main

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    client = TestClient(cp_main.app)
    return SimpleNamespace(client=client, repo=repo)


def test_drift_endpoint_shape(client_with_repo):
    repo = client_with_repo.repo
    _mk_entry(repo, _Q_LATE, "兩到三日。")
    cid = _seed_call(repo)
    for _ in range(3):  # never_fired 需 occ≥3(保守闸)
        for t in _llm(cid, _Q_LATE, "大概三日內到。"):
            repo.create_turn(t)
    r = client_with_repo.client.get("/api/stats/qa-drift?account_id=acc-001")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"window_calls", "entries_scanned", "counts", "proposals", "generated_at"}
    assert body["window_calls"] == 1 and body["entries_scanned"] == 1
    p = body["proposals"][0]
    assert set(p) == {
        "key", "kind", "reason", "qa_id", "question_text", "current_answer",
        "suggested_answer", "lang", "scope", "occurrences", "fired", "repeats",
        "hits", "headline", "detail",
    }
    assert p["kind"] == qd.KIND_REANSWER
    assert p["key"] == f"reanswer|{p['qa_id']}"
    assert isinstance(body["generated_at"], int)


def test_drift_endpoint_reports_key_required_for_user(client_with_repo):
    """只读面挂 reports(默认关):user 身份缺键 403,给了才放行。"""
    from control_plane.auth import hash_password

    repo = client_with_repo.repo
    user = repo.create_user(username="peon", password_hash=hash_password(PW),
                            role="user", org_id="org-t", account_id="acc-001")
    login = client_with_repo.client.post(
        "/api/auth/login", json={"username": "peon", "password": PW})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    r = client_with_repo.client.get("/api/stats/qa-drift?account_id=acc-001", headers=headers)
    assert r.status_code == 403, r.text
    repo.update_user(user["id"], permissions_json='["qa","reports"]')
    r2 = client_with_repo.client.get("/api/stats/qa-drift?account_id=acc-001", headers=headers)
    assert r2.status_code == 200, r2.text


def test_adopt_reanswer_updates_answer_audits_and_pregens(client_with_repo, monkeypatch):
    from control_plane import main as cp_main
    from control_plane import pregen as pregen_mod

    events: list[tuple] = []
    monkeypatch.setattr(cp_main, "_audit", lambda action, **kw: events.append((action, kw)) or {})
    spawned: list[list[str]] = []

    def _fake_pregen(base, ids):
        spawned.append(list(ids))
        return {"status": "queued", "count": len(ids)}

    monkeypatch.setattr(pregen_mod, "qa_pregen_spawn", _fake_pregen)
    repo = client_with_repo.repo
    entry = _mk_entry(repo, _Q_LATE, "兩到三日。")
    key = qd.proposal_key(qd.KIND_REANSWER, entry["id"])
    r = client_with_repo.client.post(
        "/api/stats/qa-drift/adopt",
        json={"items": [{"key": key, "kind": "reanswer", "qa_id": entry["id"],
                         "text": "大概三日內會到。"}]},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["adopted"] == 1
    res = body["results"][0]
    assert res["created"] is True and res["needs_pregen"] is False
    assert res["pregen"]["status"] == "queued"
    assert spawned == [[entry["id"]]], "改答案后必须触发补录音,否则新答案没有罐头音频"
    # 答案真的写进去了
    row = repo.get_qa_entry(entry["id"])
    assert row["answer_text"] == "大概三日內會到。"
    # 审计:沿用 qa_entry.update + detail.source=qa-drift
    acts = [a for a, _ in events if a == "qa_entry.update"]
    assert acts == ["qa_entry.update"]
    detail = [kw for a, kw in events if a == "qa_entry.update"][0]["detail"]
    assert detail["source"] == "qa-drift"


def test_adopt_reanswer_idempotent_when_same_text(client_with_repo, monkeypatch):
    from control_plane import main as cp_main
    from control_plane import pregen as pregen_mod

    events: list[tuple] = []
    monkeypatch.setattr(cp_main, "_audit", lambda action, **kw: events.append((action, kw)) or {})
    monkeypatch.setattr(pregen_mod, "qa_pregen_spawn", lambda base, ids: {"status": "queued"})
    repo = client_with_repo.repo
    entry = _mk_entry(repo, _Q_LATE, "兩到三日。")
    key = qd.proposal_key(qd.KIND_REANSWER, entry["id"])
    payload = {"items": [{"key": key, "kind": "reanswer", "qa_id": entry["id"],
                          "text": "兩到三日。"}]}  # 与现状逐字相同
    r = client_with_repo.client.post("/api/stats/qa-drift/adopt", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["adopted"] == 0
    assert r.json()["results"][0]["created"] is False
    assert events == [], "幂等 no-op 不审计(同 L-①②纪律)"


def test_adopt_retire_deletes_entry(client_with_repo, monkeypatch):
    from control_plane import main as cp_main

    events: list[tuple] = []
    monkeypatch.setattr(cp_main, "_audit", lambda action, **kw: events.append((action, kw)) or {})
    repo = client_with_repo.repo
    entry = _mk_entry(repo, "冇人問過嘅問題", "答案。")
    key = qd.proposal_key(qd.KIND_RETIRE, entry["id"])
    r = client_with_repo.client.post(
        "/api/stats/qa-drift/adopt",
        json={"items": [{"key": key, "kind": "retire", "qa_id": entry["id"], "text": ""}]},
    )
    assert r.status_code == 201, r.text
    assert r.json()["results"][0]["created"] is True
    assert repo.get_qa_entry(entry["id"]) is None
    assert [a for a, _ in events] == ["qa_entry.delete"]
    assert [kw for a, kw in events if a == "qa_entry.delete"][0]["detail"]["source"] == "qa-drift"


def test_adopt_user_identity_defers_pregen_to_admin(client_with_repo, monkeypatch):
    """非 admin 改答案:答案照样写入,但不能绕配额闸烧云 → needs_pregen 交管理员。"""
    from control_plane import pregen as pregen_mod
    from control_plane.auth import hash_password

    spawned: list[list[str]] = []
    monkeypatch.setattr(pregen_mod, "qa_pregen_spawn",
                        lambda base, ids: spawned.append(list(ids)) or {"status": "queued"})
    repo = client_with_repo.repo
    user = repo.create_user(username="peon", password_hash=hash_password(PW),
                            role="user", org_id="org-t", account_id="acc-001")
    login = client_with_repo.client.post(
        "/api/auth/login", json={"username": "peon", "password": PW})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    # 词条归本人所有(共享词条 edit 只归 admin/root,那条闸另有测试覆盖)
    entry = _mk_entry(repo, _Q_LATE, "兩到三日。", owner_user_id=user["id"])
    key = qd.proposal_key(qd.KIND_REANSWER, entry["id"])
    r = client_with_repo.client.post(
        "/api/stats/qa-drift/adopt",
        json={"items": [{"key": key, "kind": "reanswer", "qa_id": entry["id"],
                         "text": "三日內到。"}]},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    res = r.json()["results"][0]
    assert res["created"] is True and res["needs_pregen"] is True and res["pregen"] is None
    assert spawned == [], "非 admin 不得触发烧云配额的物化"
    assert repo.get_qa_entry(entry["id"])["answer_text"] == "三日內到。"


def test_adopt_rejects_key_mismatch_and_unknown_entry(client_with_repo):
    repo = client_with_repo.repo
    entry = _mk_entry(repo, _Q_LATE, "兩到三日。")
    # 键与 qa_id/kind 对不上(伪造键/拿了别人的键)
    r = client_with_repo.client.post(
        "/api/stats/qa-drift/adopt",
        json={"items": [{"key": "reanswer|qa:bogus", "kind": "reanswer",
                         "qa_id": entry["id"], "text": "x"}]},
    )
    assert r.status_code == 400, r.text
    assert "键不一致" in r.text
    # 条目不存在
    r2 = client_with_repo.client.post(
        "/api/stats/qa-drift/adopt",
        json={"items": [{"key": qd.proposal_key(qd.KIND_RETIRE, "qa:nope"),
                         "kind": "retire", "qa_id": "qa:nope"}]},
    )
    assert r2.status_code == 404, r2.text


def test_adopt_validates_batch_and_kind(client_with_repo):
    repo = client_with_repo.repo
    entry = _mk_entry(repo, _Q_LATE, "兩到三日。")
    # 空批
    assert client_with_repo.client.post("/api/stats/qa-drift/adopt", json={"items": []}).status_code == 400
    # 未知类型
    r = client_with_repo.client.post(
        "/api/stats/qa-drift/adopt",
        json={"items": [{"key": "x", "kind": "explode", "qa_id": entry["id"]}]},
    )
    assert r.status_code == 400 and "未知提案类型" in r.text
    # 改答案但文本空
    key = qd.proposal_key(qd.KIND_REANSWER, entry["id"])
    r2 = client_with_repo.client.post(
        "/api/stats/qa-drift/adopt",
        json={"items": [{"key": key, "kind": "reanswer", "qa_id": entry["id"], "text": "  "}]},
    )
    assert r2.status_code == 400 and "新答案不能为空" in r2.text
    # 超批量上限
    r3 = client_with_repo.client.post(
        "/api/stats/qa-drift/adopt",
        json={"items": [{"key": key, "kind": "reanswer", "qa_id": entry["id"], "text": "x"}] * (qd.ADOPT_MAX_ITEMS + 1)},
    )
    assert r3.status_code == 400 and "最多采纳" in r3.text
    # 答案没被改动(整批拒 = 零副作用)
    assert repo.get_qa_entry(entry["id"])["answer_text"] == "兩到三日。"


def test_adopt_batch_is_atomic_on_invalid_item(client_with_repo, monkeypatch):
    """一批里有一条非法 → 整批拒,前一条也不落库(绝不出半批写入)。"""
    from control_plane import pregen as pregen_mod

    monkeypatch.setattr(pregen_mod, "qa_pregen_spawn", lambda base, ids: {"status": "queued"})
    repo = client_with_repo.repo
    ok = _mk_entry(repo, _Q_LATE, "兩到三日。")
    bad = _mk_entry(repo, "點解咁慢", "慢慢來。")
    r = client_with_repo.client.post(
        "/api/stats/qa-drift/adopt",
        json={"items": [
            {"key": qd.proposal_key(qd.KIND_REANSWER, ok["id"]), "kind": "reanswer",
             "qa_id": ok["id"], "text": "三日內到。"},
            {"key": "reanswer|qa:forged", "kind": "reanswer", "qa_id": bad["id"], "text": "x"},
        ]},
    )
    assert r.status_code == 400, r.text
    assert repo.get_qa_entry(ok["id"])["answer_text"] == "兩到三日。", "整批拒后前一条不得落库"


def test_adopt_delete_skips_reports_gate_but_needs_qa(client_with_repo):
    """写入面挂 qa(不是 reports):给了 qa 的 user 能采纳,只给 reports 的不能。"""
    from control_plane.auth import hash_password

    repo = client_with_repo.repo
    user = repo.create_user(username="peon", password_hash=hash_password(PW),
                            role="user", org_id="org-t", account_id="acc-001")
    login = client_with_repo.client.post(
        "/api/auth/login", json={"username": "peon", "password": PW})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    entry = _mk_entry(repo, "冇人問過嘅問題", "答案。")
    key = qd.proposal_key(qd.KIND_RETIRE, entry["id"])
    payload = {"items": [{"key": key, "kind": "retire", "qa_id": entry["id"]}]}
    # 默认集含 qa → 放行(共享词条 owner='' 但 edit 只归 admin/root → 403,故先给本人 owner)
    repo.update_qa_entry(entry["id"], {"owner_user_id": user["id"]})
    r = client_with_repo.client.post("/api/stats/qa-drift/adopt", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    # 撤掉 qa 键 → 403
    repo.update_user(user["id"], permissions_json='["reports"]')
    entry2 = _mk_entry(repo, "另一條冇人問", "答案。")
    key2 = qd.proposal_key(qd.KIND_RETIRE, entry2["id"])
    r2 = client_with_repo.client.post(
        "/api/stats/qa-drift/adopt",
        json={"items": [{"key": key2, "kind": "retire", "qa_id": entry2["id"]}]},
        headers=headers,
    )
    assert r2.status_code == 403, r2.text
