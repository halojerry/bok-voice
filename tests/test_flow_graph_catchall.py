"""P2.2 兜底意图(catch-all,bolna 式):模型层双轨 + 孤儿门 + 求值 + 命名软校验。

契约(plan 2026-09-21 §48 P2.2 / §46.3 bolna 调研):
- 保留意图 id `"*"`:keywords 必须为空、无 judge、其绑定 once 必须 false;
- validate 严格(CP 保存 400)、parse 宽容(坏行丢弃,宁空毋炸=绝不炸通话);
- 求值:常规意图(关键词+判据)**全未命中之后**才轮到兜底(`pick_catchall_action`);
  无 `"*"` 意图 → 落 LLM,逐字节同旧;
- 孤儿意图门:intents 非空时每个常规意图至少要有一条 enabled 绑定(空图豁免);
- 命名规范=软校验(`graph_warnings` 只出提示,绝不 400);意图 id 硬规则放宽到
  snake_case(镜像 scripts/probe_intent_mine.py 的 `_ID_RE`,旧 `int_<8 hex>` 是其
  子集 ⇒ 存量零影响),否则 P2.1 挖掘产物永远存不进图。

离线面直测纯函数 + FlowController;agent.py graph 块的三件接线(兜底 fallback 位置/
provider 标记/`FLOW_GRAPH catchall` 日志)在 tests/test_flow_graph_catchall_wiring.py
里源级钉住——真栈入口在 entrypoint 闭包内,离线起不了真栈。
"""
from __future__ import annotations

import json

from agent_runtime.flow import FlowController
from bok_voice_core.flow_graph import (
    CATCHALL_INTENT_ID,
    GraphBinding,
    eligible_judge_intents,
    graph_warnings,
    parse_flow_graph,
    pick_catchall_action,
    pick_graph_action,
    validate_flow_graph,
)

_ID_A = "int_1a2b3c4d"
_BND_A = "bnd_7e8f9a0b"
_BND_C = "bnd_c1d2e3f4"

_STEPS = json.dumps(
    [{"goal": f"第{i}步", "ref": f"第{i}步说法"} for i in range(1, 7)], ensure_ascii=False
)


def _graph(*, intents: list[dict], bindings: list[dict] | None = None) -> str:
    return json.dumps({"version": 1, "intents": intents, "bindings": bindings or []})


def _catchall_intent(**over: object) -> dict:
    item: dict = {"id": "*", "label": "兜底", "keywords": []}
    item.update(over)
    return item


def _binding(
    binding_id: str = _BND_C,
    *,
    intent: str = CATCHALL_INTENT_ID,
    action: str = "jump_step",
    step: int = 6,
    **over: object,
) -> dict:
    item: dict = {"id": binding_id, "intent": intent, "action": action, "step": step}
    if action == "play_qa":
        item.pop("step", None)
        item["qa_id"] = "qa-1"
    item.update(over)
    return item


# ---------------------------------------------------------------------------
# parse 宽容:"*" 形状坏 = 丢该意图/丢该绑定,常规意图零影响
# ---------------------------------------------------------------------------


def test_parse_catchall_happy_path():
    doc = parse_flow_graph(_graph(intents=[_catchall_intent()], bindings=[_binding()]))
    assert [i.id for i in doc.intents] == [CATCHALL_INTENT_ID]
    assert doc.intents[0].keywords == []
    assert doc.intents[0].judge_prompt == ""
    assert doc.intents[0].enabled is True
    assert [(b.id, b.intent, b.action) for b in doc.bindings] == [
        (_BND_C, CATCHALL_INTENT_ID, "jump_step")
    ]


def test_parse_catchall_steps_scope_and_empty_steps():
    """`steps` 与常规意图同语义(空=全程);窄化兜底只在指定步生效。"""
    doc_empty = parse_flow_graph(_graph(intents=[_catchall_intent()], bindings=[]))
    assert doc_empty.intents[0].steps == []
    doc = parse_flow_graph(_graph(intents=[_catchall_intent(steps=[3])], bindings=[]))
    assert doc.intents[0].steps == [3]


def test_parse_catchall_with_real_keywords_dropped():
    """带实质关键词的 "*" = 运营配错(validate 400)→ 运行时丢该意图(宁空毋炸)。"""
    for keywords in (["投诉"], ["", "有效词"], 5, "x", {"a": 1}, True):
        doc = parse_flow_graph(
            _graph(intents=[_catchall_intent(keywords=keywords)], bindings=[_binding()])
        )
        assert doc.intents == [], keywords
        assert doc.bindings == [], keywords  # 意图不在 → 其绑定照旧悬空丢弃


def test_parse_catchall_blank_keywords_kept():
    """纯空白关键词 = 无实质词 → 保留(与 validate 同一判据);缺省/空列表同档。"""
    for keywords in (None, [], [""], ["  "], [None]):
        raw = {"id": "*", "label": "兜底"}
        if keywords is not None:
            raw["keywords"] = keywords
        doc = parse_flow_graph(_graph(intents=[raw], bindings=[]))
        assert [i.id for i in doc.intents] == [CATCHALL_INTENT_ID], keywords
        assert doc.intents[0].keywords == [], keywords


def test_parse_catchall_with_judge_prompt_dropped():
    """写了实质判据的 "*" = 配错(validate 400)→ 丢该意图;判据形状坏只丢字段档同常规。"""
    doc = parse_flow_graph(
        _graph(intents=[_catchall_intent(judge={"prompt": "客户说任何话"})], bindings=[_binding()])
    )
    assert doc.intents == [] and doc.bindings == []
    # 空判据/形状坏(5/"x"/{})不算判据 → 保留(与常规意图 _judge_prompt_of 同向)
    for judge in ({"prompt": ""}, 5, "x", {}, None):
        kept = parse_flow_graph(_graph(intents=[_catchall_intent(judge=judge)], bindings=[]))
        assert [i.id for i in kept.intents] == [CATCHALL_INTENT_ID], judge
        assert kept.intents[0].judge_prompt == ""


def test_parse_catchall_bad_steps_shape_dropped():
    for steps in (5, True, {"a": 1}):
        doc = parse_flow_graph(_graph(intents=[_catchall_intent(steps=steps)], bindings=[]))
        assert doc.intents == [], steps


def test_parse_catchall_binding_once_true_dropped_intent_kept():
    """兜底绑定 once 必须 false:坏绑定只丢该条,"*" 意图留着(=显式声明落 LLM)。"""
    doc = parse_flow_graph(
        _graph(
            intents=[_catchall_intent()],
            bindings=[
                _binding(_BND_C, action="notify_human", once=True),
                _binding("bnd_8f9a0b1c", action="notify_human"),
            ],
        )
    )
    assert [i.id for i in doc.intents] == [CATCHALL_INTENT_ID]
    assert [b.id for b in doc.bindings] == ["bnd_8f9a0b1c"]


def test_parse_catchall_does_not_touch_regular_intents():
    """零回归:同一份常规意图/绑定,有 "*" 与无 "*" 两种图解析结果逐字段相同。"""
    regular_intent = {
        "id": _ID_A, "label": "投诉", "keywords": ["投诉"], "steps": [2], "judge": {"prompt": "p"},
    }
    regular_binding = _binding(_BND_A, intent=_ID_A, action="jump_step", step=2, priority=3)
    a = parse_flow_graph(_graph(intents=[regular_intent], bindings=[regular_binding]))
    b = parse_flow_graph(
        _graph(
            intents=[regular_intent, _catchall_intent(steps=[2])],
            bindings=[regular_binding, _binding(_BND_C, action="notify_human")],
        )
    )
    assert [(i.id, i.label, i.keywords, i.steps, i.judge_prompt, i.enabled) for i in a.intents] == [
        (i.id, i.label, i.keywords, i.steps, i.judge_prompt, i.enabled) for i in b.intents
    ][:1]
    assert [(x.id, x.intent, x.action, x.step, x.priority, x.once, x.enabled) for x in a.bindings] == [
        (x.id, x.intent, x.action, x.step, x.priority, x.once, x.enabled) for x in b.bindings
    ][:1]


# ---------------------------------------------------------------------------
# validate 严格(CP 保存 400)
# ---------------------------------------------------------------------------


def test_validate_catchall_legal_forms():
    legal = [
        _graph(intents=[_catchall_intent()], bindings=[]),  # 无绑定=显式声明落 LLM
        _graph(intents=[_catchall_intent()], bindings=[_binding(action="notify_human")]),
        _graph(intents=[_catchall_intent()], bindings=[_binding(action="jump_step", step=3)]),
        _graph(intents=[_catchall_intent()], bindings=[_binding(action="play_qa", then_jump=4)]),
        _graph(intents=[_catchall_intent(keywords=[""], steps=[3])], bindings=[_binding()]),
        # 兜底与常规意图共存
        _graph(
            intents=[{"id": _ID_A, "label": "投诉", "keywords": ["投诉"]}, _catchall_intent()],
            bindings=[_binding(_BND_A, intent=_ID_A, action="jump_step", step=2), _binding()],
        ),
    ]
    for raw in legal:
        assert validate_flow_graph(raw) == [], raw


def test_validate_catchall_duplicated():
    errs = validate_flow_graph(
        _graph(intents=[_catchall_intent(), _catchall_intent(steps=[2])], bindings=[])
    )
    assert any("duplicated: *" in e for e in errs), errs


def test_validate_catchall_keywords_must_be_empty():
    for keywords in (["投诉"], ["有效词"], 5, {"a": 1}, True):
        errs = validate_flow_graph(
            _graph(intents=[_catchall_intent(keywords=keywords)], bindings=[])
        )
        assert any("keywords must be empty" in e for e in errs), (keywords, errs)


def test_validate_catchall_judge_rejected():
    errs = validate_flow_graph(
        _graph(intents=[_catchall_intent(judge={"prompt": "客户说任何话"})], bindings=[])
    )
    assert any('judge is not allowed for intent "*"' in e for e in errs), errs


def test_validate_catchall_binding_once_must_be_false():
    errs = validate_flow_graph(_graph(intents=[_catchall_intent()], bindings=[_binding(once=True)]))
    assert any('once must be false for intent "*"' in e for e in errs), errs
    assert validate_flow_graph(
        _graph(intents=[_catchall_intent()], bindings=[_binding(once=False)])
    ) == []


def test_validate_catchall_binding_missing_intent_still_rejected():
    """绑定引用不存在的意图照旧 400(兜底不豁免引用检查)。"""
    errs = validate_flow_graph(_graph(intents=[], bindings=[_binding()]))
    assert any("references missing intent" in e for e in errs), errs


def test_validate_intent_id_shape_relaxed_to_snake_case():
    """P2.2 id 硬规则放宽:snake_case 语义名可保存(挖掘产物=P2.1 出口),坏形状照旧 400。"""
    for good in ("refund_request", "whatsapp_contact", _ID_A, "a1"):
        raw = _graph(
            intents=[{"id": good, "label": "x", "keywords": ["k"]}],
            bindings=[_binding(_BND_A, intent=good, action="jump_step", step=2)],
        )
        assert validate_flow_graph(raw) == [], good
        assert parse_flow_graph(raw).intents[0].id == good
    for bad in ("BAD ID", "询问赔偿", "1abc", "-x", "x y", ""):
        errs = validate_flow_graph(
            _graph(intents=[{"id": bad, "label": "x", "keywords": ["k"]}], bindings=[])
        )
        assert any("id malformed" in e for e in errs), bad


# ---------------------------------------------------------------------------
# 孤儿意图门(P2.2)
# ---------------------------------------------------------------------------


def test_validate_orphan_intent_rejected():
    errs = validate_flow_graph(
        _graph(intents=[{"id": _ID_A, "label": "投诉", "keywords": ["投诉"]}], bindings=[])
    )
    assert errs == [f"intents[0] has no enabled binding: {_ID_A}"], errs


def test_validate_orphan_gate_skips_disabled_binding():
    """绑定了但被停用(不是 enabled 绑定)同档 = 孤儿。"""
    errs = validate_flow_graph(
        _graph(
            intents=[{"id": _ID_A, "label": "投诉", "keywords": ["投诉"]}],
            bindings=[_binding(_BND_A, intent=_ID_A, enabled=False)],
        )
    )
    assert any("has no enabled binding" in e for e in errs), errs


def test_validate_orphan_gate_empty_graph_exempt():
    """空图(无 intents)完全豁免:存量空图模板的保存不得被 breaking。"""
    assert validate_flow_graph(_graph(intents=[], bindings=[])) == []
    assert validate_flow_graph(_graph(intents=[], bindings=[_binding()])) == [
        "bindings[0].intent references missing intent: '*'"
    ]
    assert validate_flow_graph("") == []  # 空串=未启用,照旧合法


def test_validate_orphan_gate_catchall_exempt():
    """"*" 允许无绑定(=显式声明落 LLM);孤儿门只盯常规意图。"""
    assert validate_flow_graph(_graph(intents=[_catchall_intent()], bindings=[])) == []


def test_validate_orphan_gate_bad_intent_id_only_reports_id_error():
    """id 形状坏已单报,不再叠孤儿错(一条坏数据只出一条人话)。"""
    errs = validate_flow_graph(
        _graph(intents=[{"id": "bad-id", "label": "x", "keywords": ["k"]}], bindings=[])
    )
    assert any("id malformed" in e for e in errs)
    assert not any("has no enabled binding" in e for e in errs), errs


def test_validate_orphan_gate_scope_per_intent():
    """两个常规意图:一个绑好、一个孤儿 → 只报孤儿那条。"""
    errs = validate_flow_graph(
        _graph(
            intents=[
                {"id": _ID_A, "label": "投诉", "keywords": ["投诉"]},
                {"id": "refund_request", "label": "退款", "keywords": ["退款"]},
            ],
            bindings=[_binding(_BND_A, intent="refund_request", action="jump_step", step=2)],
        )
    )
    assert errs == [f"intents[0] has no enabled binding: {_ID_A}"], errs


# ---------------------------------------------------------------------------
# 求值:pick_catchall_action / FlowController.pick_catchall_binding
# ---------------------------------------------------------------------------


def _doc_with_catchall(
    *,
    catchall_steps: list[int] | None = None,
    catchall_enabled: bool = True,
    bindings: list[dict] | None = None,
):
    intents = [
        {"id": _ID_A, "label": "投诉", "keywords": ["投诉"]},
        _catchall_intent(steps=catchall_steps or [], enabled=catchall_enabled),
    ]
    if bindings is None:
        bindings = [
            _binding(_BND_A, intent=_ID_A, action="jump_step", step=2),
            _binding(_BND_C, action="notify_human"),
        ]
    return parse_flow_graph(_graph(intents=intents, bindings=bindings))


def test_pick_catchall_only_after_regular_miss():
    doc = _doc_with_catchall()
    # 常规命中 → 常规绑定先赢(调用方不会问兜底)
    assert pick_graph_action(doc, "我要投诉", step_1based=1, fired=set()).id == _BND_A
    # 常规未命中 → 兜底出场(无需 user_text:判据依附话语,兜底是配置层出口)
    assert pick_graph_action(doc, "随便一句话", step_1based=1, fired=set()) is None
    assert pick_catchall_action(doc, step_1based=1, fired=set()).id == _BND_C


def test_pick_catchall_not_triggered_when_no_catchall_intent():
    doc = parse_flow_graph(
        _graph(
            intents=[{"id": _ID_A, "label": "投诉", "keywords": ["投诉"]}],
            bindings=[_binding(_BND_A, intent=_ID_A, action="jump_step", step=2)],
        )
    )
    assert pick_catchall_action(doc, step_1based=1, fired=set()) is None
    assert pick_catchall_action(parse_flow_graph(""), step_1based=1, fired=set()) is None


def test_pick_catchall_disabled_intent_or_binding_is_none():
    assert pick_catchall_action(_doc_with_catchall(catchall_enabled=False), step_1based=1, fired=set()) is None
    doc = _doc_with_catchall(bindings=[_binding(_BND_C, action="notify_human", enabled=False)])
    assert pick_catchall_action(doc, step_1based=1, fired=set()) is None


def test_pick_catchall_step_scope():
    doc = _doc_with_catchall(catchall_steps=[3])
    assert pick_catchall_action(doc, step_1based=1, fired=set()) is None
    assert pick_catchall_action(doc, step_1based=3, fired=set()).id == _BND_C
    # 空 steps = 全程
    assert pick_catchall_action(_doc_with_catchall(), step_1based=5, fired=set()).id == _BND_C


def test_pick_catchall_priority_order_and_once_fired_defense():
    """多绑定按 (priority,id) 升序取首;once 已烧的绑定跳过(手造状态=漂移防线)。"""
    doc = _doc_with_catchall(
        bindings=[
            _binding("bnd_8f9a0b1c", action="notify_human", priority=20),
            _binding(_BND_C, action="jump_step", step=4, priority=5),
        ]
    )
    assert pick_catchall_action(doc, step_1based=1, fired=set()).id == _BND_C
    doc.bindings[0].once = True
    doc.bindings[1].once = True
    assert pick_catchall_action(doc, step_1based=1, fired={_BND_C}).id == "bnd_8f9a0b1c"
    assert pick_catchall_action(doc, step_1based=1, fired={_BND_C, "bnd_8f9a0b1c"}) is None


def test_catchall_intent_never_enters_judge_candidates():
    """兜底无判据(形状约束)→ 永不进判据候选;手造带判据的坏数据也进不去(结构防线)。"""
    assert eligible_judge_intents(_doc_with_catchall(), step_1based=1, fired=set()) == []
    doc = parse_flow_graph(_graph(intents=[_catchall_intent()], bindings=[_binding()]))
    doc.intents[0].judge_prompt = "客户说任何话"
    assert eligible_judge_intents(doc, step_1based=1, fired=set()) == []


def test_catchall_intent_never_matches_keyword_path():
    """关键词路结构性跳开 "*"(跳过支路),手造带词的坏数据也不变命中语义。"""
    doc = parse_flow_graph(_graph(intents=[_catchall_intent()], bindings=[_binding()]))
    doc.intents[0].keywords = ["投诉"]
    assert pick_graph_action(doc, "我要投诉", step_1based=1, fired=set()) is None


# ---- FlowController 层:装配 + 步 scope + once 账本 + closing 冻结 ----


_TEMPLATE = {
    "steps_json": _STEPS,
    "graph_json": _graph(
        intents=[{"id": _ID_A, "label": "投诉", "keywords": ["投诉"]}, _catchall_intent()],
        bindings=[
            _binding(_BND_A, intent=_ID_A, action="jump_step", step=4),
            _binding(_BND_C, action="jump_step", step=6),
        ],
    ),
}


def test_flow_controller_parses_catchall_and_picks_it():
    fc = FlowController.from_template(_TEMPLATE, None)
    assert [i.id for i in fc.graph.intents] == [_ID_A, CATCHALL_INTENT_ID]
    assert fc.pick_catchall_binding().id == _BND_C          # current=0 → 第 1 步
    fc.jump_to(3)
    assert fc.pick_catchall_binding().id == _BND_C          # 步号随 current 走(空 steps=全程)
    # 两条路互不串门:常规话走常规绑定(关键词路),兜底只出 _BND_C
    assert pick_graph_action(fc.graph, "我要投诉", step_1based=4, fired=set()).id == _BND_A


def test_flow_controller_catchall_once_ledger_excludes_fired():
    """once 资格吃 `graph_fired` 账本(手造 once=True 状态钉住漂移)。"""
    fc = FlowController.from_template(_TEMPLATE, None)
    fc.graph.bindings[1].once = True
    assert fc.pick_catchall_binding().id == _BND_C
    fc.graph_fired.add(_BND_C)
    assert fc.pick_catchall_binding() is None


def test_flow_controller_catchall_step_scope_and_override():
    """step_1based 可显式覆盖(调用点传 int(current)+1,与常规路同源)。"""
    tpl = {
        "steps_json": _STEPS,
        "graph_json": _graph(
            intents=[_catchall_intent(steps=[3])],
            bindings=[_binding(_BND_C, action="notify_human")],
        ),
    }
    fc = FlowController.from_template(tpl, None)
    assert fc.pick_catchall_binding() is None               # current=0 → 第 1 步,出 scope
    assert fc.pick_catchall_binding(step_1based=3).id == _BND_C


def test_flow_controller_catchall_frozen_after_closing():
    """closing 冻结(同 jump_to/apply_then_jump):告别轮图不再动作。"""
    fc = FlowController.from_template(_TEMPLATE, None)
    assert fc.pick_catchall_binding() is not None
    fc.enter_closing()
    assert fc.pick_catchall_binding() is None


def test_flow_controller_no_catchall_is_none():
    fc = FlowController.from_template(
        {"steps_json": _STEPS, "graph_json": _graph(intents=[], bindings=[])}, None
    )
    assert fc.pick_catchall_binding() is None
    # 坏 graph_json 同档(宽容 parse → 空图)
    fc2 = FlowController.from_template({"steps_json": _STEPS, "graph_json": "garbage"}, None)
    assert fc2.pick_catchall_binding() is None


# ---------------------------------------------------------------------------
# 命名规范软校验(graph_warnings):只提示,绝不 400
# ---------------------------------------------------------------------------


def test_graph_warnings_shape_rules():
    raw = _graph(
        intents=[
            {"id": "BAD ID", "label": "x", "keywords": ["k"]},
            {"id": "refund_request", "label": "x", "keywords": ["k"]},
            {"id": _ID_A, "label": "x", "keywords": ["k"]},
            _catchall_intent(),
        ],
        bindings=[],
    )
    warns = graph_warnings(raw)
    assert len(warns) == 2, warns
    assert "BAD ID" in warns[0] and "小写字母" in warns[0]
    assert _ID_A in warns[1] and "<主体>_<关系>" in warns[1]
    # "*" 兜底意图豁免命名规范;合法形状零警告
    assert graph_warnings(_graph(intents=[_catchall_intent()], bindings=[])) == []
    assert graph_warnings(
        _graph(intents=[{"id": "refund_request", "label": "x", "keywords": ["k"]}], bindings=[])
    ) == []


def test_graph_warnings_never_blocks_save():
    """软校验与严格档互不影响:机器占位 id 有提示但保存照过(存量图零 breaking)。"""
    raw = _graph(
        intents=[{"id": _ID_A, "label": "x", "keywords": ["k"]}],
        bindings=[_binding(_BND_A, intent=_ID_A, action="jump_step", step=2)],
    )
    assert graph_warnings(raw)                      # 有提示
    assert validate_flow_graph(raw) == []           # 但保存不阻断


def test_graph_warnings_tolerates_bad_input():
    for bad in ("", "   ", "not json", '{"version":2}', "[1,2]", None, b"\xff\xfe", 5):
        assert graph_warnings(bad) == [], bad


def test_graph_warnings_accepts_flow_graph_doc():
    raw = _graph(
        intents=[{"id": _ID_A, "label": "x", "keywords": ["k"]}],
        bindings=[_binding(_BND_A, intent=_ID_A, action="jump_step", step=2)],
    )
    assert graph_warnings(parse_flow_graph(raw)) == graph_warnings(raw)


def test_graph_warnings_raw_index_keeps_bad_row_visible():
    """宽容 parse 会丢坏 id 行 → 喂 doc 看不到,喂原 JSON 按下标报(审计面)。"""
    raw = _graph(intents=[{"id": "BAD ID", "label": "x", "keywords": ["k"]}], bindings=[])
    assert parse_flow_graph(raw).intents == []       # 运行时丢弃
    assert graph_warnings(parse_flow_graph(raw)) == []
    assert graph_warnings(raw)                       # 原文档态照报


def test_graph_binding_dataclass_contract():
    """GraphBinding 是 flow.py 求值接入的类型面(导入名/默认值漂移即红)。"""
    b = GraphBinding(id=_BND_C, intent=CATCHALL_INTENT_ID, action="notify_human")
    assert (b.intent, b.enabled, b.priority, b.once) == (CATCHALL_INTENT_ID, True, 10, False)
