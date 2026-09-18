"""flow_graph 纯函数:解析宽容/校验严格/命中裁决(spec 2026-09-18 §3/§4)。"""
from __future__ import annotations

import json

from bok_voice_core.flow_graph import (
    GRAPH_MAX_BYTES,
    parse_flow_graph,
    pick_graph_action,
    validate_flow_graph,
)


def _good_doc() -> str:
    return json.dumps(
        {
            "version": 1,
            "intents": [
                {"id": "int_1a2b3c4d", "label": "投诉", "keywords": ["投诉", "举报"], "steps": [], "enabled": True},
                {"id": "int_2b3c4d5e", "label": "退款", "keywords": ["退款", "Refund"], "steps": [3], "enabled": True},
            ],
            "bindings": [
                {"id": "bnd_7e8f9a0b", "intent": "int_1a2b3c4d", "action": "jump_step", "step": 4, "priority": 10, "once": False, "enabled": True},
                {"id": "bnd_c1d2e3f4", "intent": "int_2b3c4d5e", "action": "play_qa", "qa_id": "qa-1", "priority": 5, "once": True, "enabled": True},
            ],
        },
        ensure_ascii=False,
    )


def test_parse_tolerant_empty_and_garbage():
    for raw in ("", "   ", "not json", "[1,2]", '{"version": 2}', '{"version": 1, "intents": "x"}'):
        doc = parse_flow_graph(raw)
        assert doc.intents == []
        assert doc.bindings == []


def test_parse_tolerant_skips_bad_items():
    raw = json.dumps(
        {
            "version": 1,
            "intents": [
                {"id": "bad-id", "label": "x", "keywords": ["k"], "steps": [], "enabled": True},  # id 格式坏→跳过
                {"id": "int_1a2b3c4d", "label": "ok", "keywords": ["退款"], "steps": [1, "z"], "enabled": True},  # 坏步号跳过、好步号保留
            ],
            "bindings": [{"id": "bnd_7e8f9a0b", "intent": "int_nope", "action": "jump_step", "step": 2}],
        }
    )
    doc = parse_flow_graph(raw)
    assert [i.id for i in doc.intents] == ["int_1a2b3c4d"]
    assert doc.intents[0].steps == [1]
    assert doc.bindings == []  # 引用不存在的意图→解析期丢弃（运行时宽容优先于数据保真）


def test_parse_tolerant_non_iterable_fields():
    """字段形状坏（数字/布尔/对象等非 list）→ 整项跳过，绝不 TypeError（never-raise 契约）。"""
    doc = parse_flow_graph(
        '{"version":1,"intents":['
        '{"id":"int_1a2b3c4d","keywords":5},'
        '{"id":"int_2b3c4d5e","label":"ok","keywords":["k"],"steps":5}'
        '],"bindings":[]}'
    )
    assert doc.intents == []  # 首项 keywords 非 list / 次项 steps 非 list → 两项各自整项跳过
    # 布尔与对象同档（bool 是 int 子类，单列一条）
    assert (
        parse_flow_graph(
            '{"version":1,"intents":[{"id":"int_3c4d5e6f","label":"b","keywords":true,"steps":[]}],"bindings":[]}'
        ).intents
        == []
    )
    assert (
        parse_flow_graph(
            '{"version":1,"intents":[{"id":"int_3c4d5e6f","label":"o","keywords":{"a":1},"steps":{"b":2}}],"bindings":[]}'
        ).intents
        == []
    )
    # 反向对照：字段缺省 / 为 null 属「空值」而非坏形状 → 保留该项，steps 空=全程生效
    kept = parse_flow_graph('{"version":1,"intents":[{"id":"int_4d5e6f70","label":"ok","keywords":["k"]}],"bindings":[]}')
    assert [i.id for i in kept.intents] == ["int_4d5e6f70"]
    assert kept.intents[0].steps == []


def test_parse_bytes_matches_str_form():
    raw = _good_doc()
    str_doc = parse_flow_graph(raw)
    bytes_doc = parse_flow_graph(raw.encode("utf-8"))
    assert bytes_doc == str_doc
    assert [i.id for i in bytes_doc.intents] == ["int_1a2b3c4d", "int_2b3c4d5e"]
    assert [b.id for b in bytes_doc.bindings] == ["bnd_7e8f9a0b", "bnd_c1d2e3f4"]
    # 坏编码仍走宽容路径（替换字符→非 JSON→空图），绝不抛错
    assert parse_flow_graph(b'\xff\xfe{"version":1}').intents == []


def test_validate_strict_good_and_errors():
    assert validate_flow_graph(_good_doc()) == []
    assert validate_flow_graph("") == []  # 空串=未启用,合法
    errs = validate_flow_graph("not json")
    assert errs and "json" in errs[0]
    # 超限
    assert validate_flow_graph(json.dumps({"version": 1, "intents": [{"id": f"int_{i:08x}", "label": "x", "keywords": ["k"]} for i in range(65)], "bindings": []}))
    # 引用缺失意图 / 坏 action / priority 越界
    errs = validate_flow_graph(
        json.dumps(
            {
                "version": 1,
                "intents": [{"id": "int_1a2b3c4d", "label": "x", "keywords": ["k"], "steps": [], "enabled": True}],
                "bindings": [
                    {"id": "bnd_7e8f9a0b", "intent": "int_nope", "action": "jump_step", "step": 2},
                    {"id": "bnd_c1d2e3f4", "intent": "int_1a2b3c4d", "action": "shout", "step": 2},
                    {"id": "bnd_d2e3f4a5", "intent": "int_1a2b3c4d", "action": "jump_step", "step": 2, "priority": 9999},
                ],
            }
        )
    )
    joined = "\n".join(errs)
    assert "int_nope" in joined and "shout" in joined and "priority" in joined


def test_validate_size_cap():
    assert any("bytes" in e for e in validate_flow_graph("x" * (GRAPH_MAX_BYTES + 1)))


def test_pick_priority_scope_casefold_once():
    doc = parse_flow_graph(_good_doc())
    # step=3:两意图均命中(step 空=全程 / steps=[3]);priority 5 先于 10
    hit = pick_graph_action(doc, "我要投诉，我要退款", step_1based=3, fired=set())
    assert hit is not None and hit.action == "play_qa"
    # casefold:关键词 "Refund" 命中 "REFUND my order"
    assert pick_graph_action(doc, "please REFUND my order", step_1based=3, fired=set()) is not None
    # step=2:退款意图(steps=[3])不命中 → 只剩投诉绑定
    hit2 = pick_graph_action(doc, "我要投诉", step_1based=2, fired=set())
    assert hit2 is not None and hit2.action == "jump_step"
    # once 已消耗 → 跳过
    assert pick_graph_action(doc, "我想退款", step_1based=3, fired={"bnd_c1d2e3f4"}) is None
    # 空文本/空图 → None
    assert pick_graph_action(doc, "", step_1based=1, fired=set()) is None
    assert pick_graph_action(parse_flow_graph(""), "退款", step_1based=1, fired=set()) is None
