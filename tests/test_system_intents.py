"""系统意图目录(spec §48 P2.3 六层意图收敛):具名归类 + last_rule_intent。

只验证收敛层(纯归类+命名+观测),不重复测行为(行为零变化由
tests/test_flow_controller.py 的既有断言保证)。铁律:正则对象与 flow 内既有
RE 是**同一对象**(is),防复制漂移。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime import flow as flow_mod  # noqa: E402
from agent_runtime.flow import (  # noqa: E402
    CONFIRM,
    DEFER,
    FAREWELL,
    FlowController,
    OBJECTION,
    OFFTOPIC,
    QUESTION,
    REFUSE,
    REPEAT,
    SYSTEM_INTENTS,
    UNCLEAR,
    classify_verdict_intent,
    classify_wa_intent,
    decide_advance,
    detect_whatsapp_signal,
)

WA_GOAL = "引導辦理:問客戶有冇用開WhatsApp、加專員"
WA_REF = "你加工作人員嘅WhatsApp帳號…你直接俾你個WhatsApp號碼我"

_KNOWN_VERDICTS = {CONFIRM, OBJECTION, QUESTION, REFUSE, FAREWELL, REPEAT, DEFER, UNCLEAR, OFFTOPIC}


# ---- 目录完整性 ----

def test_catalog_ids_unique_and_named():
    for iid, intent in SYSTEM_INTENTS.items():
        assert intent.id == iid, f"键 {iid} 与 intent.id {intent.id} 不一致"
        assert iid and intent.name, f"{iid} 缺 id/name"


def test_catalog_regexes_are_same_objects_as_existing_res():
    """每条目的族正则必须是 flow 既有 RE 对象的引用(同一对象),防复制漂移。"""
    module_res = [v for v in vars(flow_mod).values() if isinstance(v, re.Pattern)]
    assert module_res, "flow 模块应至少有一个编译正则"
    for iid, intent in SYSTEM_INTENTS.items():
        assert intent.regexes, f"{iid} 必须至少引用一条族正则"
        for rx in intent.regexes:
            assert any(rx is m for m in module_res), (
                f"{iid} 的正则 {rx.pattern!r} 不是 flow 既有 RE 对象(疑似复制漂移)"
            )


def test_catalog_verdicts_are_known():
    for iid, intent in SYSTEM_INTENTS.items():
        if intent.verdict:
            assert intent.verdict in _KNOWN_VERDICTS, f"{iid} 的 verdict {intent.verdict!r} 非已知判定值"


def test_catalog_covers_core_families():
    """核心词族都在册(REFUSE/FAREWELL/CONFIRM/WA/平台)。"""
    ids = set(SYSTEM_INTENTS)
    for expected in (
        "sys_refuse",
        "sys_farewell",
        "sys_confirm",
        "sys_whatsapp_capture",
        "sys_whatsapp_offer",
        "sys_whatsapp_caller_bound",
        "sys_platform",
    ):
        assert expected in ids, f"目录缺 {expected}"


def test_classify_verdict_intent_mapping():
    assert classify_verdict_intent(REFUSE) == "sys_refuse"
    assert classify_verdict_intent(FAREWELL) == "sys_farewell"
    assert classify_verdict_intent(CONFIRM) == "sys_confirm"
    assert classify_verdict_intent(OBJECTION) == "sys_objection"
    assert classify_verdict_intent(QUESTION) == "sys_question"
    assert classify_verdict_intent(REPEAT) == "sys_repeat"
    assert classify_verdict_intent(DEFER) == "sys_defer"
    assert classify_verdict_intent(UNCLEAR) == ""
    assert classify_verdict_intent("") == ""
    assert classify_verdict_intent("nonsense") == ""


def test_classify_wa_intent_mapping():
    assert classify_wa_intent(("captured", "6868123456")) == "sys_whatsapp_capture"
    assert classify_wa_intent(("captured_implicit", "")) == "sys_whatsapp_caller_bound"
    assert classify_wa_intent(("offered", "")) == "sys_whatsapp_offer"
    assert classify_wa_intent(None) == ""
    assert classify_wa_intent(("unknown", "")) == ""


# ---- last_rule_intent 归类正确 ----

def test_last_rule_intent_refuse():
    fc = FlowController()
    assert fc.rule_verdict("唔需要啦,唔好再打") == REFUSE
    assert fc.last_rule_intent == "sys_refuse"


def test_last_rule_intent_farewell():
    fc = FlowController()
    assert fc.rule_verdict("好嘅,拜拜") == FAREWELL
    assert fc.last_rule_intent == "sys_farewell"


def test_last_rule_intent_confirm():
    fc = FlowController()
    assert fc.rule_verdict("係我") == CONFIRM
    assert fc.last_rule_intent == "sys_confirm"


def test_last_rule_intent_default_and_clears_on_miss():
    fc = FlowController()
    assert fc.last_rule_intent == ""
    # 普通话闲聊 → UNCLEAR → 未命中清空
    assert fc.rule_verdict("今天天气不错") == UNCLEAR
    assert fc.last_rule_intent == ""
    # 命中一次 → 记上
    assert fc.rule_verdict("唔需要啦") == REFUSE
    assert fc.last_rule_intent == "sys_refuse"
    # 再来一个未命中轮 → 清空式重写(不保留旧命中)
    fc.rule_verdict("今天天气不错")
    assert fc.last_rule_intent == ""


def test_last_rule_intent_whatsapp_capture():
    fc = FlowController()
    sig = detect_whatsapp_signal(
        "我WhatsApp係 六八六八一二三四五六", step_goal=WA_GOAL, step_ref=WA_REF
    )
    assert sig is not None and sig[0] == "captured"
    fc.note_wa_signal(sig)
    assert fc.last_rule_intent == "sys_whatsapp_capture"


def test_last_rule_intent_whatsapp_offer():
    fc = FlowController()
    sig = detect_whatsapp_signal("好呀", step_goal=WA_GOAL, step_ref=WA_REF)
    assert sig is not None and sig[0] == "offered"
    fc.note_wa_signal(sig)
    assert fc.last_rule_intent == "sys_whatsapp_offer"


def test_last_rule_intent_whatsapp_caller_bound():
    fc = FlowController()
    sig = detect_whatsapp_signal(
        "WhatsApp就係綁定呢個來電", step_goal=WA_GOAL, step_ref=WA_REF
    )
    assert sig is not None and sig[0] == "captured_implicit"
    fc.note_wa_signal(sig)
    assert fc.last_rule_intent == "sys_whatsapp_caller_bound"


def test_note_wa_signal_none_clears():
    fc = FlowController()
    fc.note_wa_signal(("captured", "12345678"))
    assert fc.last_rule_intent == "sys_whatsapp_capture"
    fc.note_wa_signal(None)
    assert fc.last_rule_intent == ""


# ---- 行为零变化抽查(归类是纯观测) ----

def test_rule_verdict_is_observational_no_state_change():
    """rule_verdict 只归类、不推进;last_rule_intent 不影响推进语义。"""
    fc = FlowController()
    before = fc.current
    assert fc.rule_verdict("係我") == CONFIRM
    assert fc.current == before  # 推进只由 on_user_turn/advance 负责
    assert fc.closing is False


def test_rule_verdict_return_matches_decide_advance():
    """同一输入下 FlowController.rule_verdict 与模块 decide_advance 返回值一致。"""
    for text in ("係我", "唔需要啦", "点解啊？", "拜拜", "今天天气不错", "听唔清"):
        fc = FlowController()
        assert fc.rule_verdict(text) == decide_advance(
            text, facts=fc.vars_map, short_ack_confirms=fc._current_step_is_question()
        )
