"""3.4 意图引擎模型层:判据宽容解析 / 严格校验 / judge_hit 裁决 / 候选预筛。

契约(plan 2026-09-18 §1):JSON `intents[].judge = {"prompt": str}` 1..400 字;
parse 宽容(坏形状丢字段不清退意图,Phase 2 行为);validate 严格(CP 保存拒绝);
`pick_graph_action(..., judge_hit=)` judge 命中与关键词命中**同权同守卫**。

无判据数据="零变化"的结构性保证由 `eligible_judge_intents` 恒空钉死——这是运行时
不调度判据任务的唯一依据(agent `_intent_judge_candidates` 的唯一数据闸)。
"""
from __future__ import annotations

import json

import pytest

from bok_voice_core.flow_graph import (
    JUDGE_PROMPT_MAX_CHARS,
    eligible_judge_intents,
    parse_flow_graph,
    pick_graph_action,
    validate_flow_graph,
)

_ID_A = "int_1a2b3c4d"
_ID_B = "int_2b3c4d5e"
_BND_A = "bnd_7e8f9a0b"
_BND_B = "bnd_c1d2e3f4"


def _graph(
    *,
    intents: list[dict],
    bindings: list[dict] | None = None,
) -> str:
    return json.dumps({"version": 1, "intents": intents, "bindings": bindings or []})


def _intent(intent_id: str = _ID_A, *, judge: object = None, **over: object) -> dict:
    item = {"id": intent_id, "label": "投诉", "keywords": ["投诉"], "enabled": True}
    if judge is not None:
        item["judge"] = judge
    item.update(over)
    return item


def _binding(
    binding_id: str = _BND_A,
    *,
    intent: str = _ID_A,
    action: str = "jump_step",
    **over: object,
) -> dict:
    item: dict = {"id": binding_id, "intent": intent, "action": action, "step": 4, "enabled": True}
    if action == "play_qa":
        item.pop("step", None)
        item["qa_id"] = "qa-1"
    item.update(over)
    return item


# ---- parse 宽容:坏形状丢字段、意图本体保留 ----


@pytest.mark.parametrize(
    "judge",
    [
        None,  # 键缺席
        5,  # 非 dict
        "客户说要投诉",  # 非 dict(str,运营手写错)
        [],  # 非 dict(list)
        {},  # dict 但无 prompt
        {"prompt": ""},  # 空串
        {"prompt": None},
        {"prompt": 123},  # 非 str
        {"note": "写错键"},  # 只有别的键
    ],
)
def test_parse_bad_judge_shapes_drop_field_keep_intent(judge):
    doc = parse_flow_graph(_graph(intents=[_intent(judge=judge)]))
    assert [i.id for i in doc.intents] == [_ID_A]  # 意图本体唔清退
    assert doc.intents[0].judge_prompt == ""
    assert doc.intents[0].keywords == ["投诉"]  # 关键词路照旧可用


def test_parse_judge_prompt_happy_path():
    doc = parse_flow_graph(_graph(intents=[_intent(judge={"prompt": "客户要求转人工或投诉"})]))
    assert doc.intents[0].judge_prompt == "客户要求转人工或投诉"


def test_parse_judge_prompt_no_silent_truncation():
    """超长判据运行时**不静默截断**——截断=静默改语义,长度是 CP 严格校验的事。"""
    long_prompt = "判" * (JUDGE_PROMPT_MAX_CHARS + 50)
    doc = parse_flow_graph(_graph(intents=[_intent(judge={"prompt": long_prompt})]))
    assert doc.intents[0].judge_prompt == long_prompt


def test_parse_judge_ignores_extra_keys():
    doc = parse_flow_graph(_graph(intents=[_intent(judge={"prompt": "判据", "examples": ["x"]})]))
    assert doc.intents[0].judge_prompt == "判据"


# ---- validate 严格 ----


def test_validate_judge_boundaries_pass():
    # 绑定边在场(P2.2 孤儿门):本测试的靶子是判据长度窗,不是绑定的有无。
    for prompt in ("判", "判" * JUDGE_PROMPT_MAX_CHARS):
        raw = _graph(intents=[_intent(judge={"prompt": prompt})], bindings=[_binding()])
        assert validate_flow_graph(raw) == []


@pytest.mark.parametrize(
    "judge",
    [
        5,
        "客户说要投诉",
        {},
        {"prompt": ""},
        {"prompt": 42},
        {"prompt": None},
        {"prompt": "判" * (JUDGE_PROMPT_MAX_CHARS + 1)},
        {"note": "缺 prompt 键"},
    ],
)
def test_validate_judge_bad_shapes_rejected(judge):
    raw = _graph(intents=[_intent(judge=judge)])
    errors = validate_flow_graph(raw)
    assert "intents[0].judge.prompt must be a 1-400 char string" in errors


def test_validate_judge_error_index_and_other_errors_kept():
    """错误列表契约不变:judge 错误带下标,不遮蔽既有规则错误。"""
    raw = _graph(
        intents=[
            _intent(judge={"prompt": "ok"}),
            {"id": _ID_B, "label": "", "keywords": [], "judge": {"prompt": 7}},
        ]
    )
    errors = validate_flow_graph(raw)
    assert "intents[1].judge.prompt must be a 1-400 char string" in errors
    assert "intents[1].label must be 1-64 chars" in errors
    assert "intents[1].keywords must be a non-empty list" in errors


# ---- pick_graph_action:judge_hit 与关键词同权 ----


def _doc_with(judge: dict | None, *, keyword: str, steps: list[int] | None = None):
    return parse_flow_graph(
        _graph(
            intents=[
                _intent(
                    judge=judge,
                    keywords=[keyword],
                    steps=steps if steps is not None else [],
                )
            ],
            bindings=[_binding()],
        )
    )


def test_judge_hit_fires_when_keywords_miss():
    doc = _doc_with({"prompt": "客户表达强烈不满"}, keyword="投诉")
    text = "你们这样搞我真的受不了了"
    assert pick_graph_action(doc, text, step_1based=1, fired=set()) is None
    hit = pick_graph_action(doc, text, step_1based=1, fired=set(), judge_hit=_ID_A)
    assert hit is not None and hit.id == _BND_A


def test_judge_hit_unknown_id_is_noop():
    doc = _doc_with({"prompt": "判据"}, keyword="投诉")
    assert pick_graph_action(doc, "随便一句话", step_1based=1, fired=set(), judge_hit="int_deadbeef") is None


def test_judge_hit_disabled_intent_is_noop():
    doc = _doc_with({"prompt": "判据"}, keyword="投诉")
    doc.intents[0].enabled = False
    assert pick_graph_action(doc, "随便一句话", step_1based=1, fired=set(), judge_hit=_ID_A) is None


def test_judge_hit_step_scope_enforced():
    doc = _doc_with({"prompt": "判据"}, keyword="投诉", steps=[3])
    assert pick_graph_action(doc, "随便一句话", step_1based=1, fired=set(), judge_hit=_ID_A) is None
    assert pick_graph_action(doc, "随便一句话", step_1based=3, fired=set(), judge_hit=_ID_A) is not None


def test_judge_hit_binding_disabled_is_noop():
    doc = _doc_with({"prompt": "判据"}, keyword="投诉")
    doc.bindings[0].enabled = False
    assert pick_graph_action(doc, "随便一句话", step_1based=1, fired=set(), judge_hit=_ID_A) is None


def test_judge_hit_once_fired_binding_excluded():
    doc = _doc_with({"prompt": "判据"}, keyword="投诉")
    doc.bindings[0].once = True
    assert pick_graph_action(doc, "随便一句话", step_1based=1, fired=set(), judge_hit=_ID_A) is not None
    assert pick_graph_action(doc, "随便一句话", step_1based=1, fired={_BND_A}, judge_hit=_ID_A) is None


def test_judge_hit_empty_text_is_noop():
    """`user_text` 为空整体不裁决(判据语义依附于话语,见 pick docstring)。"""
    doc = _doc_with({"prompt": "判据"}, keyword="投诉")
    assert pick_graph_action(doc, "", step_1based=1, fired=set(), judge_hit=_ID_A) is None


def test_judge_hit_shares_candidate_pool_and_sort_with_keywords():
    """judge 命中与关键词命中同轮=**同一候选池**,按 (priority, id) 统一排序。

    A 意图走关键词(priority 5=该赢),B 意图走 judge(priority 20=该输)→ 同轮两者
    都命中时仍是 A 胜:judge 命中只是「多了一个命中意图」,唔係特权通道(若 judge
    直接抢答/不参与排序,B 会错赢;若 judge 覆盖 hit_ids,A 的关键词命中会丢)。
    """
    doc = parse_flow_graph(
        _graph(
            intents=[
                _intent(_ID_A, judge={"prompt": "A 判据"}, keywords=["投诉"]),
                _intent(_ID_B, judge={"prompt": "B 判据"}, keywords=["绝不可能出现的词"]),
            ],
            bindings=[
                _binding(_BND_A, intent=_ID_A, action="jump_step", step=4, priority=5),
                _binding(_BND_B, intent=_ID_B, action="jump_step", step=6, priority=20),
            ],
        )
    )
    # 仅关键词:只有 A 命中
    only_kw = pick_graph_action(doc, "我要投诉", step_1based=1, fired=set())
    assert only_kw is not None and only_kw.id == _BND_A
    # 仅 judge:关键词未中,B 补位
    only_judge = pick_graph_action(doc, "你们这样搞我真的受不了了", step_1based=1, fired=set(), judge_hit=_ID_B)
    assert only_judge is not None and only_judge.id == _BND_B
    # 关键词 + judge 同轮:同一池 → priority 5 的 A 仍胜
    both = pick_graph_action(doc, "我要投诉", step_1based=1, fired=set(), judge_hit=_ID_B)
    assert both is not None and both.id == _BND_A


def test_judge_hit_absent_judge_prompt_still_allows_keyword_route():
    """judge_prompt 空(未写判据)不影响关键词确定性命中——judge 是补充唔係替代。"""
    doc = _doc_with(None, keyword="投诉")
    assert doc.intents[0].judge_prompt == ""
    hit = pick_graph_action(doc, "我想投诉", step_1based=1, fired=set())
    assert hit is not None and hit.id == _BND_A


# ---- eligible_judge_intents:候选预筛(零回归的结构性保证) ----


def test_eligible_empty_when_no_judge_data():
    """**零回归 pin**:纯关键词图(无 judge 字段)=候选恒空=调度门恒不过。"""
    doc = parse_flow_graph(_graph(intents=[_intent()], bindings=[_binding()]))
    assert eligible_judge_intents(doc, step_1based=1, fired=set()) == []


def test_eligible_picks_intent_with_judge_and_live_binding():
    doc = parse_flow_graph(
        _graph(intents=[_intent(judge={"prompt": "判据"})], bindings=[_binding()])
    )
    assert [i.id for i in eligible_judge_intents(doc, step_1based=1, fired=set())] == [_ID_A]


def test_eligible_excludes_disabled_intent_and_disabled_binding():
    doc = parse_flow_graph(
        _graph(intents=[_intent(judge={"prompt": "判据"})], bindings=[_binding()])
    )
    doc.intents[0].enabled = False
    assert eligible_judge_intents(doc, step_1based=1, fired=set()) == []
    doc.intents[0].enabled = True
    doc.bindings[0].enabled = False
    assert eligible_judge_intents(doc, step_1based=1, fired=set()) == []


def test_eligible_excludes_intent_without_live_binding():
    doc = parse_flow_graph(
        _graph(intents=[_intent(judge={"prompt": "判据"})], bindings=[])
    )
    assert eligible_judge_intents(doc, step_1based=1, fired=set()) == []
    # once 已烧且别无绑定 → 唔再判(判返都触发唔到,白烧一次 9B)
    doc = parse_flow_graph(
        _graph(
            intents=[_intent(judge={"prompt": "判据"})],
            bindings=[_binding(once=True)],
        )
    )
    assert eligible_judge_intents(doc, step_1based=1, fired={_BND_A}) == []
    assert [i.id for i in eligible_judge_intents(doc, step_1based=1, fired=set())] == [_ID_A]


def test_eligible_step_scope_and_keyword_only_excluded():
    doc = parse_flow_graph(
        _graph(
            intents=[_intent(_ID_A, judge={"prompt": "判据"}, steps=[3])],
            bindings=[_binding()],
        )
    )
    assert eligible_judge_intents(doc, step_1based=2, fired=set()) == []
    assert [i.id for i in eligible_judge_intents(doc, step_1based=3, fired=set())] == [_ID_A]
    # 无判据(仅关键词)的意图永不进判定候选
    doc2 = parse_flow_graph(_graph(intents=[_intent()], bindings=[_binding()]))
    assert eligible_judge_intents(doc2, step_1based=1, fired=set()) == []


def test_eligible_keeps_doc_order():
    doc = parse_flow_graph(
        _graph(
            intents=[
                _intent(_ID_A, judge={"prompt": "A"}),
                _intent(_ID_B, judge={"prompt": "B"}),
            ],
            bindings=[
                _binding(_BND_A, intent=_ID_A),
                _binding(_BND_B, intent=_ID_B, action="jump_step", step=5),
            ],
        )
    )
    assert [i.id for i in eligible_judge_intents(doc, step_1based=1, fired=set())] == [_ID_A, _ID_B]
