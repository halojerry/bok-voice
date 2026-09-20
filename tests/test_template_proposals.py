"""话术分支/意图词提案(L-②,2026-09-20)单测。

面:gap_proposals 纯函数(提案生成/分支行语法 round-trip/冗余抑制/写助手)+
查询面(build_template_proposal_report,内存仓造数复用 gap_mining 聚合)+
CP 端点(GET /api/stats/template-proposals 形状 / POST adopt 幂等·键校验·
闸链·审计·published_json 不触碰)。造数姿势与 tests/test_gap_mining.py 同款。

分支行语法 round-trip 用 **agent 侧真 parse_step_ref**(sys.path 挂 apps/agent,
同 tests/test_flow_controller.py 先例)——CP 模块内的 `_BRANCH_COND_RE` 是
flow.py:102 的镜像(CP 不 import agent_runtime,云端镜像不含 apps/agent),
镜像与原件一致性由本文件钉住。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-template-proposals-0123456")

ROOT = Path(__file__).resolve().parents[1]
for _p in ("packages/core", "packages/business-db", "apps/agent"):
    _path = str(ROOT / _p)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pytest  # noqa: E402

from bok_voice_business_db.repository import InMemoryBusinessRepository  # noqa: E402
from bok_voice_core.flow_graph import validate_flow_graph  # noqa: E402
from control_plane import gap_mining as gm  # noqa: E402
from control_plane import gap_proposals as gp  # noqa: E402

_SEQ = iter(range(1, 100_000))
PW = "Passw0rd!"

# ---- 夹具:模板 steps/graph(与运行时/画布消费的真实形状一致) ----

STEPS_JSON = json.dumps(
    [
        {"goal": "开场", "ref": "你好，請問係林先生嗎？"},
        {"goal": "通知", "ref": "你有一個包裹到咗，唔好意思打擾。\n如果客户話唔係本人→我哋之後再聯絡。", "say": True},
        {"goal": "平台", "ref": "我哋係淘寶集運嘅客服。"},
        {"goal": "賠償", "ref": "照平台規則可以賠。\n如果客户嫌少→可以申請覆核。"},
    ],
    ensure_ascii=False,
)

GRAPH_JSON = json.dumps(
    {
        "version": 1,
        "intents": [
            {"id": "int_11111111", "label": "转人工", "keywords": ["投訴", "主管"], "steps": [], "enabled": True},
            {"id": "int_22222222", "label": "查询时效", "keywords": ["幾時到"], "steps": [3], "enabled": True},
        ],
        "bindings": [
            {"id": "bnd_11111111", "intent": "int_11111111", "action": "notify_human", "enabled": True},
            {"id": "bnd_22222222", "intent": "int_22222222", "action": "jump_step", "step": 3, "enabled": True},
        ],
    },
    ensure_ascii=False,
)


def _gap(customer_text="幾時可以送到呀", *, template_id="tpl-1", step=3, count=4, calls=3,
         lang="cantonese", sample_answer="一般兩到三日到。", call_id="call-1", norm=""):
    """aggregate_gap_groups 出仓形状的 gap 行(norm 可显式给,缺省按原话重算)。"""
    return {
        "norm": norm or gm.candidate_norm(customer_text),
        "customer_text": customer_text,
        "count": count,
        "calls": calls,
        "template_id": template_id,
        "step": step,
        "lang": lang,
        "sample_answer": sample_answer,
        "sample_call_id": call_id,
    }


def _build(gaps, *, steps_json=STEPS_JSON, graph_json=GRAPH_JSON, template_id="tpl-1",
           template_name="粤语集运", template_found=True):
    return gp.build_proposals_for_template(
        gaps, template_id=template_id, steps_json=steps_json, graph_json=graph_json,
        template_name=template_name, template_found=template_found,
    )


def _by_kind(proposals, kind):
    return next((p for p in proposals if p["kind"] == kind), None)


# ---- 纯函数:提案生成 ----


def test_branch_proposal_emitted_with_full_context():
    rows = _build([_gap()])
    branch = _by_kind(rows, gp.KIND_BRANCH)
    assert branch is not None
    assert branch["available"] is True and branch["blocked_reason"] == ""
    assert branch["template_id"] == "tpl-1" and branch["template_name"] == "粤语集运"
    assert branch["step"] == 3 and branch["step_goal"] == "平台"
    assert branch["branch_cond"] == "幾時可以送到呀"
    assert branch["branch_resp"] == "一般兩到三日到。"
    assert branch["branch_line"] == "如果客户幾時可以送到呀→一般兩到三日到。"
    assert branch["customer_text"] == "幾時可以送到呀" and branch["norm"] == "幾時可以送到呀"
    assert branch["count"] == 4 and branch["calls"] == 3 and branch["lang"] == "cantonese"
    assert branch["sample_call_id"] == "call-1"
    assert branch["key"] == gp.branch_proposal_key("tpl-1", 3, "幾時可以送到呀")


def test_proposals_deterministic():
    a = _build([_gap(), _gap("有冇其它賠償", step=4)])
    b = _build([_gap(), _gap("有冇其它賠償", step=4)])
    assert a == b, "同输入同输出(不依赖 dict/插入序)"


def test_norm_recomputed_from_customer_text_when_popped():
    """build_llm_gap_report 出仓前 pop norm——builder 用 candidate_norm(原话)同值重算。"""
    row = _gap("幾時可以送到呀。", norm="")  # 带标点原话
    row.pop("norm")
    branch = _by_kind(_build([row]), gp.KIND_BRANCH)
    assert branch["norm"] == "幾時可以送到呀"
    assert branch["key"] == gp.branch_proposal_key("tpl-1", 3, "幾時可以送到呀")


# ---- 纯函数:分支行语法 round-trip(agent 侧真 parse_step_ref) ----


def test_branch_line_round_trips_agent_parse_step_ref():
    from agent_runtime.flow import parse_step_ref  # 真·运行时解析器

    for text, answer in (
        ("幾時可以送到呀", "一般兩到三日到。"),
        ("點解要賠咁少", "最高可以賠200蚊。"),
        ("可唔可以退貨,我想退", "七日內可以退。"),  # 条件含标点
    ):
        proposal = _by_kind(_build([_gap(text, sample_answer=answer)]), gp.KIND_BRANCH)
        parts = parse_step_ref(proposal["branch_line"])
        assert parts.branches == [(text, answer)], f"行必须被运行时原样认账:{proposal['branch_line']}"


def test_append_branch_then_step_ref_parses_new_branch_and_keeps_old():
    from agent_runtime.flow import parse_step_ref

    new_steps, changed = gp.append_branch_to_steps(STEPS_JSON, 4, "點解要賠咁少", "最高賠200。")
    assert changed is True
    arr = json.loads(new_steps)
    assert len(arr) == 4
    assert arr[1]["say"] is True, "其余步骤原样(say 直念标记不丢)"
    assert arr[0]["ref"] == "你好，請問係林先生嗎？"
    parts = parse_step_ref(arr[3]["ref"])
    assert ("嫌少", "可以申請覆核。") in [(c, r) for c, r in parts.branches], "既有分支不丢"
    assert ("點解要賠咁少", "最高賠200。") in [(c, r) for c, r in parts.branches], "新分支运行时可解析"
    assert parts.script.startswith("照平台規則可以賠。")


def test_branch_cond_strips_edge_sentence_punctuation():
    """漏网原话是转写原文(带句号)——条件是给人看的,首尾句末标点要剥掉;
    内部逗号是运行时 bigram 条件匹配的信号,必须保留。"""
    from agent_runtime.flow import parse_step_ref

    proposal = _by_kind(_build([_gap("拼多多。", step=2)]), gp.KIND_BRANCH)
    assert proposal["branch_cond"] == "拼多多", "尾部句号不该进条件"
    assert proposal["branch_line"] == f"如果客户拼多多→{proposal['branch_resp']}"
    assert parse_step_ref(proposal["branch_line"]).branches == [
        ("拼多多", proposal["branch_resp"])
    ]
    # 内部逗号保留(运营原话的停顿词对条件命中是信号)
    inner = _by_kind(_build([_gap("啊，我不記得了。", step=2)]), gp.KIND_BRANCH)
    assert inner["branch_cond"] == "啊，我不記得了"
    # 首尾标点全剥:两侧都带标点
    both = _by_kind(_build([_gap("。點解咁慢？", step=2)]), gp.KIND_BRANCH)
    assert both["branch_cond"] == "點解咁慢"
    # 纯标点条件 → 提不出可用条件(不可采纳)
    blank = _by_kind(_build([_gap("。。。", step=2)]), gp.KIND_BRANCH)
    assert blank["available"] is False and blank["blocked_reason"] == gp.BR_EMPTY_TEXT
    # 写助手同样卫生(直接调 API 的调用方走同一条路)
    new_steps, changed = gp.append_branch_to_steps(
        json.dumps([{"goal": "g"}, {"goal": "h", "ref": "底稿。"}], ensure_ascii=False),
        2, "拼多多。", "好的。",
    )
    assert changed is True
    assert parse_step_ref(json.loads(new_steps)[1]["ref"]).branches == [("拼多多", "好的。")]


def test_branch_cond_sanitized_arrow_and_newlines():
    text = "我要\n投訴→你哋"
    proposal = _by_kind(_build([_gap(text, step=2)]), gp.KIND_BRANCH)
    assert "\n" not in proposal["branch_cond"] and "→" not in proposal["branch_cond"]
    from agent_runtime.flow import parse_step_ref

    parts = parse_step_ref(proposal["branch_line"])
    assert parts.branches == [("我要 投訴 你哋", proposal["branch_resp"])], \
        "清洗后整行只劈出一条合法分支,条件不含分隔符"


# ---- 纯函数:不可采纳(去重/失配)提案 ----


def test_no_template_blocks_both_kinds():
    rows = _build([_gap()], template_id="")
    assert len(rows) == 2
    for p in rows:
        assert p["available"] is False and p["blocked_reason"] == gp.BR_NO_TEMPLATE
        assert p["blocked_label"]
    rows_missing = _build([_gap()], template_found=False)
    assert all(p["blocked_reason"] == gp.BR_NO_TEMPLATE for p in rows_missing)


def test_branch_blocked_unknown_step_and_out_of_range():
    assert _by_kind(_build([_gap(step=0)]), gp.KIND_BRANCH)["blocked_reason"] == gp.BR_NO_STEP
    assert _by_kind(_build([_gap(step=9)]), gp.KIND_BRANCH)["blocked_reason"] == gp.BR_STEP_RANGE


def test_branch_blocked_when_equivalent_branch_exists():
    steps = json.dumps(
        [
            {"goal": "开场", "ref": "你好"},
            {"goal": "s", "ref": "x"},
            {"goal": "賠償", "ref": "照規則賠。\n如果客户幾時可以送到呀→已經答咗。"},
        ],
        ensure_ascii=False,
    )
    p = _by_kind(_build([_gap()], steps_json=steps), gp.KIND_BRANCH)
    assert p["available"] is False
    assert p["blocked_reason"] == gp.BR_BRANCH_EXISTS and "第 3 步" in p["blocked_label"]
    # 近似重复:既有条件包含提案条件(归一)同样抑制
    steps_near = json.dumps(
        [
            {"goal": "开场", "ref": "你好"},
            {"goal": "s", "ref": "x"},
            {"goal": "賠償", "ref": "照規則賠。\n如果客户幾時可以送到呀點算→已經答咗。"},
        ],
        ensure_ascii=False,
    )
    p2 = _by_kind(_build([_gap()], steps_json=steps_near), gp.KIND_BRANCH)
    assert p2["blocked_reason"] == gp.BR_BRANCH_EXISTS


def test_branch_blocked_when_no_answer_available():
    p = _by_kind(_build([_gap(sample_answer="")]), gp.KIND_BRANCH)
    assert p["blocked_reason"] == gp.BR_NO_ANSWER


def test_intent_proposal_matches_affinity_and_scope():
    p = _by_kind(_build([_gap("幾時可以送到呀")]), gp.KIND_INTENT_KEYWORD)
    assert p["available"] is True
    assert p["intent_id"] == "int_22222222" and p["intent_label"] == "查询时效"
    assert p["keyword"] == "幾時可以送到呀"
    assert p["key"] == gp.intent_proposal_key("tpl-1", "int_22222222", "幾時可以送到呀")


def test_intent_blocked_no_graph_and_no_active_intent():
    assert (
        _by_kind(_build([_gap()], graph_json=""), gp.KIND_INTENT_KEYWORD)["blocked_reason"]
        == gp.BR_NO_GRAPH
    )
    graph_no_binding = json.dumps(
        {"version": 1, "intents": [{"id": "int_11111111", "label": "转人工", "keywords": ["投訴"]}], "bindings": []},
        ensure_ascii=False,
    )
    assert (
        _by_kind(_build([_gap()], graph_json=graph_no_binding), gp.KIND_INTENT_KEYWORD)["blocked_reason"]
        == gp.BR_NO_ACTIVE_INTENT
    )
    # 有可触发意图但零词面关系 → 不猜
    assert (
        _by_kind(_build([_gap("完全無關嘅一句話")]), gp.KIND_INTENT_KEYWORD)["blocked_reason"]
        == gp.BR_NO_MATCHING_INTENT
    )


def test_intent_blocked_keyword_present_and_covered():
    # 提案词与既有关键词逐字相等(本地图:关键词恰为整句原话)
    graph_exact = json.dumps(
        {
            "version": 1,
            "intents": [{"id": "int_11111111", "label": "投诉", "keywords": ["投訴熱線"], "steps": []}],
            "bindings": [{"id": "bnd_11111111", "intent": "int_11111111", "action": "notify_human"}],
        },
        ensure_ascii=False,
    )
    p = _by_kind(_build([_gap("投訴熱線")], graph_json=graph_exact), gp.KIND_INTENT_KEYWORD)
    assert p["blocked_reason"] == gp.BR_KEYWORD_PRESENT
    # 提案词包含既有关键词(命中面是其子集,加了不改行为)
    p2 = _by_kind(_build([_gap("我要投訴你哋")]), gp.KIND_INTENT_KEYWORD)
    assert p2["blocked_reason"] == gp.BR_KEYWORD_COVERED
    # 既有关键词已能命中本次漏网原话
    p3 = _by_kind(_build([_gap("你哋投訴熱線係邊個")]), gp.KIND_INTENT_KEYWORD)
    assert p3["blocked_reason"] == gp.BR_KEYWORD_COVERED
    # 被抑制的提案仍带意图上下文与人话原因(驾驶舱「看得见为什么不行」)
    assert p3["intent_id"] == "int_11111111" and p3["blocked_label"]


def test_intent_blocked_keywords_full():
    many = json.dumps(
        {
            "version": 1,
            "intents": [
                # 32 条已满(含「查詢」保证词面亲和 >0);FULL 判定先于冗余判定
                {"id": "int_11111111", "label": "转人工",
                 "keywords": [f"詞{i:02d}" for i in range(31)] + ["查詢"], "steps": []}
            ],
            "bindings": [{"id": "bnd_11111111", "intent": "int_11111111", "action": "notify_human"}],
        },
        ensure_ascii=False,
    )
    p = _by_kind(_build([_gap("查詢進度點算")], graph_json=many), gp.KIND_INTENT_KEYWORD)
    assert p["blocked_reason"] == gp.BR_KEYWORDS_FULL


# ---- 写助手 ----


def test_append_branch_idempotent_and_preserves_others():
    new1, changed1 = gp.append_branch_to_steps(STEPS_JSON, 3, "點解要賠咁少", "最高賠200。")
    assert changed1 is True
    new2, changed2 = gp.append_branch_to_steps(new1, 3, "點解要賠咁少", "最高賠200。")
    assert changed2 is False and new2 == new1, "同分支二次采纳幂等"
    # 归一近似(加问号)也幂等
    new3, changed3 = gp.append_branch_to_steps(new1, 3, "點解要賠咁少？", "換個講法。")
    assert changed3 is False and new3 == new1, "既有等价分支时不重复追加"


def test_append_branch_errors():
    with pytest.raises(gp.ProposalError):
        gp.append_branch_to_steps("not-json", 1, "a", "b")
    with pytest.raises(gp.ProposalError):
        gp.append_branch_to_steps(STEPS_JSON, 9, "a", "b")
    with pytest.raises(gp.ProposalError):
        gp.append_branch_to_steps(STEPS_JSON, 1, "", "b")
    with pytest.raises(gp.ProposalError):
        gp.append_branch_to_steps(STEPS_JSON, 1, "a", "  ")


def test_append_keyword_valid_and_idempotent():
    new, kw, changed = gp.append_keyword_to_graph(GRAPH_JSON, "int_22222222", "幾時送到")
    assert changed is True and kw == "幾時送到"
    assert validate_flow_graph(new) == [], "写入前严格校验,产物必须过 CP 保存同款校验器"
    data = json.loads(new)
    target = next(i for i in data["intents"] if i["id"] == "int_22222222")
    assert "幾時送到" in target["keywords"]
    # 幂等:完全相同 / 提案词包含既有词(命中面子集)
    again, _kw2, changed2 = gp.append_keyword_to_graph(new, "int_22222222", "幾時送到")
    assert changed2 is False and again == new
    _a3, _k3, changed3 = gp.append_keyword_to_graph(new, "int_22222222", "幾時送到快啲")
    assert changed3 is False


def test_append_keyword_errors_and_validator_surface():
    with pytest.raises(gp.ProposalError):
        gp.append_keyword_to_graph("", "int_11111111", "x")
    with pytest.raises(gp.ProposalError):
        gp.append_keyword_to_graph(GRAPH_JSON, "int_99999999", "x")
    # 追加后超 MAX_KEYWORDS → ProposalError 携带校验器原文(不写坏数据)
    many = json.dumps(
        {
            "version": 1,
            "intents": [{"id": "int_11111111", "label": "转人工", "keywords": [f"詞{i:02d}" for i in range(32)]}],
            "bindings": [{"id": "bnd_11111111", "intent": "int_11111111", "action": "notify_human"}],
        },
        ensure_ascii=False,
    )
    with pytest.raises(gp.ProposalError) as exc:
        gp.append_keyword_to_graph(many, "int_11111111", "新詞")
    assert "keywords exceed 32" in str(exc.value)


def test_write_helpers_never_emit_published_json():
    """published_json 不在写助手面上(字符串进字符串出)——端点级由
    test_adopt_branch_writes_steps_leaves_published_json 钉。"""
    new_steps, _ = gp.append_branch_to_steps(STEPS_JSON, 1, "a", "b")
    new_graph, _, _ = gp.append_keyword_to_graph(GRAPH_JSON, "int_11111111", "x")
    assert "published" not in new_steps and "published" not in new_graph


# ---- 查询面(内存仓造数,复用 gap_mining 聚合) ----

# (造数助手与 tests/test_gap_mining.py 同款)


def _ts(n: int) -> str:
    return f"2026-09-20T12:00:00.{n:06d}"


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

    cid = f"call-tp-{next(_SEQ)}"
    obj_id = ""
    if obj_name is not None:
        obj_id = repo.create_object(account_id, {"display_name": obj_name, "phone": "+85200000002"})["id"]
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


def _llm_exchange(cid, question, answer, *, step=3, lang="cantonese"):
    n = next(_SEQ)
    return [
        _turn(cid, "user", question, speaker="customer", lang=lang, created_at=_ts(n)),
        _turn(cid, "assistant", answer, speaker="agent_ai", gen="llm", step=step,
              lang=lang, created_at=_ts(n + 1)),
    ]


def _seed_repo(*, template_rows=True):
    repo = InMemoryBusinessRepository()
    if template_rows:
        repo.create_template({
            "id": "tpl-1", "account_id": "acc-001", "name": "粤语集运",
            "language": "cantonese", "steps_json": STEPS_JSON, "graph_json": GRAPH_JSON,
        })
    for _ in range(4):
        cid = _seed_call(repo)
        for t in _llm_exchange(cid, "幾時可以送到呀", "一般兩到三日到。", step=3):
            repo.create_turn(t)
    return repo


def test_report_reuses_gap_aggregation_and_attaches_proposals():
    repo = _seed_repo()
    out = gp.build_template_proposal_report(repo, account_id="acc-001", min_calls=4)
    assert set(out) == {"coverage", "gaps", "proposals", "generated_at"}
    assert out["coverage"]["llm"] == 4
    assert len(out["gaps"]) == 1 and out["gaps"][0]["template_id"] == "tpl-1"
    kinds = {p["kind"] for p in out["proposals"]}
    assert kinds == {gp.KIND_BRANCH, gp.KIND_INTENT_KEYWORD}
    branch = _by_kind(out["proposals"], gp.KIND_BRANCH)
    assert branch["available"] is True and branch["step"] == 3
    intent = _by_kind(out["proposals"], gp.KIND_INTENT_KEYWORD)
    assert intent["intent_id"] == "int_22222222"
    # gap 行 norm 已被 L-① pop,提案 norm 与键仍成立
    assert branch["norm"] == "幾時可以送到呀"


def test_report_missing_template_and_cross_account_template_blocked():
    repo = _seed_repo(template_rows=False)
    # 无模板行 → 提案不可采纳 + 人话原因
    out = gp.build_template_proposal_report(repo, account_id="acc-001", min_calls=4)
    assert out["proposals"] and all(p["blocked_reason"] == gp.BR_NO_TEMPLATE for p in out["proposals"])
    # 跨账号模板:归属校验按「不存在」出提案,不泄露他账号模板内容
    repo.create_template({"id": "tpl-1", "account_id": "acc-002", "name": "别家的", "steps_json": STEPS_JSON})
    out2 = gp.build_template_proposal_report(repo, account_id="acc-001", min_calls=4)
    assert all(p["blocked_reason"] == gp.BR_NO_TEMPLATE for p in out2["proposals"])
    assert all(p["template_name"] == "" for p in out2["proposals"])


# ---- CP 端点 ----


@pytest.fixture()
def client_with_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane import main as cp_main

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    client = TestClient(cp_main.app)
    return SimpleNamespace(client=client, repo=repo)


def _publish(repo, tpl_id="tpl-1"):
    """模拟已发布模板:冻结九键进 published_json(发布两态)。"""
    row = repo.get_template(tpl_id)
    frozen = {k: str(row.get(k) or "") for k in (
        "steps_json", "graph_json", "hotwords", "tone_override", "opening",
        "core", "objection", "closing", "language")}
    repo.update_template(tpl_id, {"published_json": json.dumps(frozen, ensure_ascii=False)})
    return frozen


def test_proposals_endpoint_shape(client_with_repo):
    _seed_into(client_with_repo.repo)
    r = client_with_repo.client.get("/api/stats/template-proposals?account_id=acc-001&min_calls=4")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"coverage", "gaps", "proposals", "generated_at"}
    assert body["coverage"]["llm"] == 4
    assert {p["kind"] for p in body["proposals"]} == {"branch", "intent_keyword"}
    branch = next(p for p in body["proposals"] if p["kind"] == "branch")
    for key in ("key", "kind", "template_id", "template_name", "customer_text", "norm", "count",
                "calls", "lang", "sample_answer", "sample_call_id", "step", "step_goal",
                "branch_cond", "branch_resp", "branch_line", "intent_id", "intent_label",
                "keyword", "available", "blocked_reason", "blocked_label"):
        assert key in branch, f"提案字段缺 {key}"
    assert branch["available"] is True and body["gaps"][0]["customer_text"] == "幾時可以送到呀"


def _seed_into(repo):
    repo.create_template({
        "id": "tpl-1", "account_id": "acc-001", "name": "粤语集运",
        "language": "cantonese", "steps_json": STEPS_JSON, "graph_json": GRAPH_JSON,
    })
    for _ in range(4):
        cid = _seed_call(repo)
        for t in _llm_exchange(cid, "幾時可以送到呀", "一般兩到三日到。", step=3):
            repo.create_turn(t)


def _get_branch_proposal(client, params="?account_id=acc-001&min_calls=4"):
    r = client.get(f"/api/stats/template-proposals{params}")
    assert r.status_code == 200, r.text
    body = r.json()
    return body, next(p for p in body["proposals"] if p["kind"] == "branch" and p["available"])


def test_adopt_branch_writes_steps_leaves_published_json(client_with_repo, monkeypatch):
    from control_plane import main as cp_main

    events: list[tuple] = []
    monkeypatch.setattr(cp_main, "_audit", lambda action, **kw: events.append((action, kw)) or {})
    _seed_into(client_with_repo.repo)
    _publish(client_with_repo.repo)
    published_before = client_with_repo.repo.get_template("tpl-1")["published_json"]

    _body, branch = _get_branch_proposal(client_with_repo.client)
    edited_answer = "人工改過嘅回答。"
    r = client_with_repo.client.post(
        "/api/stats/template-proposals/adopt",
        json={"items": [{
            "key": branch["key"], "kind": "branch", "template_id": branch["template_id"],
            "norm": branch["norm"], "step": branch["step"],
            "cond": branch["branch_cond"], "text": edited_answer,
        }]},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["adopted"] == 1 and body["results"][0]["created"] is True
    assert body["results"][0]["id"] == "tpl-1"

    row = client_with_repo.repo.get_template("tpl-1")
    assert edited_answer in str(row["steps_json"])
    assert f"如果客户{branch['branch_cond']}→{edited_answer}" in str(row["steps_json"])
    assert row["published_json"] == published_before, "published_json 永不被采纳路径触碰"
    assert len(client_with_repo.repo.list_template_revisions("tpl-1")) == 1, "版本快照与 PUT 同款"
    actions = [(a, kw) for a, kw in events if a == "template.branch_adopt"]
    assert len(actions) == 1
    detail = actions[0][1]["detail"]
    assert detail["step"] == 3 and detail["source"] == "gap-proposal" and detail["revision"] == 1

    # 幂等:同一提案(同键同内容)二次采纳 → 200 created=false,不重复写/不重复审计
    r2 = client_with_repo.client.post(
        "/api/stats/template-proposals/adopt",
        json={"items": [{
            "key": branch["key"], "kind": "branch", "template_id": branch["template_id"],
            "norm": branch["norm"], "step": branch["step"],
            "cond": branch["branch_cond"], "text": edited_answer,
        }]},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["results"][0]["created"] is False
    assert len([a for a, _ in events if a == "template.branch_adopt"]) == 1
    assert len(client_with_repo.repo.list_template_revisions("tpl-1")) == 1


def test_adopt_intent_keyword_writes_graph(client_with_repo, monkeypatch):
    from control_plane import main as cp_main

    events: list[tuple] = []
    monkeypatch.setattr(cp_main, "_audit", lambda action, **kw: events.append((action, kw)) or {})
    _seed_into(client_with_repo.repo)
    r = client_with_repo.client.get("/api/stats/template-proposals?account_id=acc-001&min_calls=4")
    body = r.json()
    intent = next(p for p in body["proposals"] if p["kind"] == "intent_keyword" and p["available"])
    r2 = client_with_repo.client.post(
        "/api/stats/template-proposals/adopt",
        json={"items": [{
            "key": intent["key"], "kind": "intent_keyword", "template_id": intent["template_id"],
            "norm": intent["norm"], "intent_id": intent["intent_id"], "text": intent["keyword"],
        }]},
    )
    assert r2.status_code == 201, r2.text
    row = client_with_repo.repo.get_template("tpl-1")
    assert validate_flow_graph(str(row["graph_json"])) == []
    target = next(i for i in json.loads(row["graph_json"])["intents"] if i["id"] == intent["intent_id"])
    assert intent["keyword"] in target["keywords"]
    assert [a for a, _ in events if a == "template.intent_keyword_adopt"]
    # 幂等:同关键词再采 → 200 created=false
    r3 = client_with_repo.client.post(
        "/api/stats/template-proposals/adopt",
        json={"items": [{
            "key": intent["key"], "kind": "intent_keyword", "template_id": intent["template_id"],
            "norm": intent["norm"], "intent_id": intent["intent_id"], "text": intent["keyword"],
        }]},
    )
    assert r3.status_code == 200 and r3.json()["results"][0]["created"] is False


def test_adopt_rejects_bad_keys_and_stale_targets(client_with_repo):
    _seed_into(client_with_repo.repo)
    _body, branch = _get_branch_proposal(client_with_repo.client)

    def _item(**over):
        base = {
            "key": branch["key"], "kind": "branch", "template_id": branch["template_id"],
            "norm": branch["norm"], "step": branch["step"],
            "cond": branch["branch_cond"], "text": "回答。",
        }
        base.update(over)
        return base

    # 空批次
    r0 = client_with_repo.client.post("/api/stats/template-proposals/adopt", json={"items": []})
    assert r0.status_code == 400
    # 未知类型
    r1 = client_with_repo.client.post(
        "/api/stats/template-proposals/adopt", json={"items": [_item(kind="magic")]})
    assert r1.status_code == 400
    # 键与内容不一致(改了 cond 拿旧键提交/伪造键)
    r2 = client_with_repo.client.post(
        "/api/stats/template-proposals/adopt", json={"items": [_item(cond="改过的条件")]})
    assert r2.status_code == 400, r2.text
    # 模板不存在(过期提案)
    r3 = client_with_repo.client.post(
        "/api/stats/template-proposals/adopt",
        json={"items": [_item(template_id="tpl-gone",
                             key=gp.branch_proposal_key("tpl-gone", 3, branch["norm"]))]})
    assert r3.status_code == 404
    # 步号越界(模板已改小)→ proposal_conflict
    repo = client_with_repo.repo
    repo.update_template("tpl-1", {"steps_json": json.dumps([{"goal": "开场", "ref": "hi"}], ensure_ascii=False)})
    r4 = client_with_repo.client.post(
        "/api/stats/template-proposals/adopt", json={"items": [_item()]})
    assert r4.status_code == 400
    assert r4.json()["detail"]["error"] == "proposal_conflict"
    # 分支缺应答文本
    r5 = client_with_repo.client.post(
        "/api/stats/template-proposals/adopt", json={"items": [_item(text="  ")]})
    assert r5.status_code == 400


def test_adopt_user_gates_shared_and_foreign_templates(client_with_repo):
    from control_plane.auth import hash_password

    repo = client_with_repo.repo
    _seed_into(repo)
    repo.create_user(username="peon", password_hash=hash_password(PW),
                     role="user", org_id="org-t", account_id="acc-001")
    login = client_with_repo.client.post("/api/auth/login", json={"username": "peon", "password": PW})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    _body, branch = _get_branch_proposal(client_with_repo.client)
    item = {
        "key": branch["key"], "kind": "branch", "template_id": branch["template_id"],
        "norm": branch["norm"], "step": branch["step"],
        "cond": branch["branch_cond"], "text": "回答。",
    }
    # 共享模板(owner='')改动 → 403(与 PUT deny_foreign_owner(edit) 同口径)
    r_shared = client_with_repo.client.post(
        "/api/stats/template-proposals/adopt", json={"items": [item]}, headers=headers)
    assert r_shared.status_code == 403, r_shared.text
    # 别人(owner=他人)的模板 → 404 不泄露存在性
    repo.update_template("tpl-1", {"owner_user_id": "someone-else"})
    r_foreign = client_with_repo.client.post(
        "/api/stats/template-proposals/adopt", json={"items": [item]}, headers=headers)
    assert r_foreign.status_code == 404
    # 自己的模板 → 放行
    peon_id = next(u["id"] for u in repo.list_users() if u["username"] == "peon")
    repo.update_template("tpl-1", {"owner_user_id": peon_id})
    r_own = client_with_repo.client.post(
        "/api/stats/template-proposals/adopt", json={"items": [item]}, headers=headers)
    assert r_own.status_code == 201, r_own.text


def test_adopt_requires_templates_page_key(client_with_repo):
    """写模板的采纳闸键=templates(不是 reports):只给 reports 键的 user → 403。"""
    from control_plane.auth import hash_password

    repo = client_with_repo.repo
    _seed_into(repo)
    repo.create_user(username="viewer", password_hash=hash_password(PW),
                     role="user", org_id="org-t", account_id="acc-001",
                     permissions_json='["reports"]')
    login = client_with_repo.client.post("/api/auth/login", json={"username": "viewer", "password": PW})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    # GET(reports 键)放行
    r_get = client_with_repo.client.get(
        "/api/stats/template-proposals?account_id=acc-001&min_calls=4", headers=headers)
    assert r_get.status_code == 200, r_get.text
    _body, branch = _get_branch_proposal(client_with_repo.client)
    r_post = client_with_repo.client.post(
        "/api/stats/template-proposals/adopt",
        json={"items": [{
            "key": branch["key"], "kind": "branch", "template_id": branch["template_id"],
            "norm": branch["norm"], "step": branch["step"],
            "cond": branch["branch_cond"], "text": "回答。",
        }]},
        headers=headers,
    )
    assert r_post.status_code == 403, r_post.text
