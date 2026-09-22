"""P2.4 意图喂下游（§48，2026-09-21）：当轮意图 → LLM 尾部 + 垫话类别提示。

三面：
- `ContextState.set_customer_intent` / 【客户意图】行（全量档渲染、slim 不含、
  kill-switch `BOK_INTENT_CONTEXT=0` 全关字节同旧）；
- `fillers.intent_category_hint` 纯函数映射 + `FillerDirector.hint_category`
  pending→consume（arm 消费）+ `_select` 采用合法提示、字面罐头命中优先级不变；
- agent 侧接线用**源级 pin**（照 tests/test_flow_graph_catchall_wiring.py 惯例）：
  `note_wa_signal` 在 detect 之后、`set_customer_intent` 在 graph/兜底派发之后且
  `_filler.arm()` 之前。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.agent import (  # noqa: E402
    _intent_context_enabled,
    _intent_display_name,
    _turn_intent_id,
)
from agent_runtime.fillers import (  # noqa: E402
    FillerDirector,
    intent_category_hint,
)
from agent_runtime.providers.livekit_plugins import ContextState  # noqa: E402

_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")

_INTENT_LABEL = "【客户意图】"


# ============================================================
# ① ContextState.set_customer_intent / 【客户意图】行
# ============================================================


def test_set_customer_intent_renders_line_in_full_tail():
    st = ContextState(account_id="t")
    st.set_flow_current("流程第 2/5 步\n这一步要达成:xxx")
    st.set_customer_intent("明确拒绝/收线")
    tail = st.render_context_tail()
    assert _INTENT_LABEL + "明确拒绝/收线" in tail


def test_empty_state_has_no_intent_row():
    st = ContextState(account_id="t")
    st.set_flow_current("流程第 1/5 步")
    assert _INTENT_LABEL not in st.render_context_tail()


def test_customer_intent_truncated_to_40_chars():
    st = ContextState(account_id="t")
    st.set_customer_intent("甲" * 60)
    assert st._customer_intent == "甲" * 40
    assert len(st._customer_intent) == 40
    assert _INTENT_LABEL + "甲" * 40 in st.render_context_tail()


def test_customer_intent_empty_clears():
    st = ContextState(account_id="t")
    st.set_customer_intent("X")
    assert _INTENT_LABEL in st.render_context_tail()
    st.set_customer_intent("")  # 每轮覆盖:空=清位
    assert st._customer_intent == ""
    assert _INTENT_LABEL not in st.render_context_tail()


def test_customer_intent_bumps_revision_only_on_change():
    st = ContextState(account_id="t")
    rev0 = st.revision
    st.set_customer_intent("A")
    assert st.revision == rev0 + 1  # 意图属实质变化 → 全量尾部才带得出
    st.set_customer_intent("A")
    assert st.revision == rev0 + 1  # 同值不虚增
    st.set_customer_intent("B")
    assert st.revision == rev0 + 2


def test_slim_tail_omits_intent_row():
    st = ContextState(account_id="t")
    st.set_flow_current("流程第 2/5 步\n这一步要达成:xxx")
    st.set_customer_intent("应承确认")
    st.record_applied_tail("u1", "u1\n\n" + st.render_context_tail())  # 冻结同 revision
    slim = st.render_context_tail()
    assert "·继续】" in slim and "状态无实质变化" in slim
    assert _INTENT_LABEL not in slim, "slim 语义=状态无实质变化;意图只在全量档渲染"


def test_intent_kill_switch_off_is_noop_and_bytes_identical(monkeypatch):
    monkeypatch.setenv("BOK_INTENT_CONTEXT", "0")
    assert _intent_context_enabled() is False
    base = ContextState(account_id="t")
    base.set_flow_current("流程第 2/5 步")
    base.set_last_reply("好的，我帮你查下。")
    with_intent = ContextState(account_id="t")
    with_intent.set_flow_current("流程第 2/5 步")
    with_intent.set_last_reply("好的，我帮你查下。")
    with_intent.set_customer_intent("明确拒绝/收线")  # kill-switch 下 no-op
    assert with_intent._customer_intent == ""
    assert with_intent.revision == base.revision  # 不虚增 revision
    assert with_intent.render_context_tail() == base.render_context_tail()
    assert with_intent.render_system_message() == base.render_system_message()


def test_intent_kill_switch_default_on(monkeypatch):
    monkeypatch.delenv("BOK_INTENT_CONTEXT", raising=False)
    assert _intent_context_enabled() is True


# ============================================================
# ② fillers:intent_category_hint 纯函数 + hint_category 消费 + _select
# ============================================================


def test_intent_category_hint_mapping():
    assert intent_category_hint("sys_objection") == "empathy"
    assert intent_category_hint("sys_confirm") == "ack"
    assert intent_category_hint("sys_question") == "check"
    assert intent_category_hint("sys_defer") == "ack"


def test_intent_category_hint_unknown_and_empty():
    # 其余族 → 不提示(交回字面分类器)
    for iid in ("sys_refuse", "sys_farewell", "sys_repeat", "sys_whatsapp_capture",
                "sys_platform", "sys_confirm_strong", "graph_intent", ""):
        assert intent_category_hint(iid) == ""


class _Session:
    agent_state = "thinking"


class _StubIndex:
    """FillerEntryIndex 同形替身:可控命中/未命中,记录 classifier_cat 透传。"""

    def __init__(self, hit: dict | None = None):
        self.hit = hit
        self.calls: list[tuple[str, str]] = []

    def match(self, user_text, *, lang="", classifier_cat="", used=None, threshold=None):
        self.calls.append((user_text, classifier_cat))
        return (dict(self.hit), 1.0) if self.hit else (None, 0.0)


def _director(cats: list[str], *, entries_index=None, user_text: str = ""):
    """池类别由 cats 决定(顺序即 pool 顺序);_pools 覆写绕开 manifest 文件。"""
    d = FillerDirector(
        _Session(),
        lang_resolver=lambda: "cantonese",
        player=None,
        guards=lambda: False,
        entries_index=entries_index,
        user_text_provider=lambda: user_text,
    )
    d._pools = lambda: {"cantonese": [  # type: ignore[method-assign]
        {"text": f"t{i}", "file": f"f{i}", "cat": c} for i, c in enumerate(cats)
    ]}
    return d


def test_hint_category_pending_consumed_at_arm():
    d = _director(["check", "minimal"])
    d.hint_category("check")
    assert d._hint_pending == "check" and d._hint_round == ""
    d.arm()  # player=None → 定时器不起,但 pending 已消费
    assert d._hint_round == "check"
    assert d._hint_pending == "", "消费即清,不泄漏下一轮"


def test_hint_category_cleared_when_next_arm_without_hint():
    d = _director(["check", "minimal"])
    d.hint_category("check")
    d.arm()
    d.arm()  # 下一轮未调 hint_category
    assert d._hint_round == "", "上一轮提示不得跨轮残留"


def test_hinted_category_legal_wins_illegal_falls_back():
    d = _director(["check", "minimal"])
    d._hint_round = "check"
    assert d._hinted_category("cantonese", "minimal") == "check"
    d._hint_round = "empathy"  # 池里没有该类 → 回退
    assert d._hinted_category("cantonese", "minimal") == "minimal"
    d._hint_round = ""
    assert d._hinted_category("cantonese", "minimal") == "minimal"


def test_select_hint_overrides_classifier_on_miss():
    idx = _StubIndex(hit=None)  # 字面未命中 → 走回退池
    d = _director(["check", "minimal"], entries_index=idx, user_text="好的呀")
    d._hint_round = "check"  # 分类器会归 minimal,hint 钉 check
    entry, cat = d._select("cantonese")
    assert cat == "check"
    assert entry is not None
    assert idx.calls[0][1] == "check", "提示类别须经 classifier_cat 透传给索引打分"


def test_select_no_hint_keeps_classifier():
    idx = _StubIndex(hit=None)
    d = _director(["check", "minimal"], entries_index=idx, user_text="好的呀")
    entry, cat = d._select("cantonese")
    assert cat == "minimal"  # 「好的呀」→ minimal,字面分类器不变


def test_select_literal_hit_beats_hint():
    """字面罐头命中优先级不变:命中即返,提示不参与抢条目。"""
    idx = _StubIndex(hit={"text": "收到,帮您睇下。", "id": "e1"})
    d = _director(["check", "minimal"], entries_index=idx, user_text="帮我查下啦")
    d._hint_round = "minimal"
    entry, cat = d._select("cantonese")
    assert entry == {"text": "收到,帮您睇下。", "file": None}
    assert cat == "minimal"


def test_select_no_index_uses_legal_hint():
    d = _director(["check", "minimal"])  # 无 entries_index → 纯资产池路径
    d._hint_round = "check"
    entry, cat = d._select("cantonese")
    assert cat == "check"
    assert entry is not None


def test_select_no_index_no_hint_empty_category():
    d = _director(["check", "minimal"])
    entry, cat = d._select("cantonese")
    assert cat == ""  # 无提示 → 旧行为(整池随机)
    assert entry is not None


# ============================================================
# ③ 纯函数 _intent_display_name / _turn_intent_id
# ============================================================


class _GraphFC:
    def __init__(self, doc, rule_intent: str = ""):
        self.graph = doc
        self.last_rule_intent = rule_intent


def _graph_doc():
    from bok_voice_core.flow_graph import FlowGraphDoc, FlowIntent

    return FlowGraphDoc(
        intents=[FlowIntent(id="whatsapp_contact", label="加 WhatsApp"),
                 FlowIntent(id="*", label="兜底")],
        bindings=[],
    )


def test_intent_display_name_graph_label_then_system_then_id():
    fc = _GraphFC(_graph_doc())
    assert _intent_display_name(fc, "whatsapp_contact") == "加 WhatsApp"
    assert _intent_display_name(fc, "*") == "兜底"
    # 系统意图(SYSTEM_INTENTS.name)
    assert _intent_display_name(fc, "sys_objection") == "否认/异议/质疑"
    # 都查不到 → 原样回 id;空 id → 空串
    assert _intent_display_name(fc, "no_such_intent") == "no_such_intent"
    assert _intent_display_name(fc, "") == ""


class _B:
    def __init__(self, intent):
        self.intent = intent


def test_turn_intent_id_priority():
    fc = _GraphFC(_graph_doc(), rule_intent="sys_confirm")
    assert _turn_intent_id(_B("graph_a"), _B("*"), fc) == "graph_a"  # 常规命中优先
    assert _turn_intent_id(None, _B("*"), fc) == "*"  # 兜底补位
    assert _turn_intent_id(None, None, fc) == "sys_confirm"  # 规则归类
    fc2 = _GraphFC(_graph_doc(), rule_intent="")
    assert _turn_intent_id(None, None, fc2) == ""  # 全空


# ============================================================
# ④ agent 侧接线源级 pin(离线起不了真栈,钉结构)
# ============================================================


def test_note_wa_signal_wired_after_detect():
    call = "flow_ctrl.note_wa_signal(_wa_signal)"
    assert call in _SRC
    detect_at = _SRC.rindex("detect_whatsapp_signal(")
    assert detect_at < _SRC.index(call), "note_wa_signal 必须在 WA detect 之后"


def test_set_customer_intent_after_graph_dispatch_before_filler_arm():
    call = "context_state.set_customer_intent("
    assert call in _SRC
    dispatch_at = _SRC.index("await _gdispatch(_gcatchall_stash, True)")
    arm_at = _SRC.rindex("_filler.arm()")  # 注释里也出现一次,取真调用(最后一次)
    at = _SRC.index(call)
    assert dispatch_at < at, "意图取序须在 graph/兜底派发之后(读到本轮命中)"
    assert at < arm_at, "set 必须在 LLM 路径(_filler.arm 前)"


def test_hint_category_before_filler_arm():
    call = "_filler.hint_category("
    assert call in _SRC
    arm_at = _SRC.rindex("_filler.arm()")
    assert _SRC.index(call) < arm_at, "hint 须先于 arm 落 pending,否则本轮垫话拿不到提示"


def test_intent_wiring_gated_by_kill_switch():
    assert '"BOK_INTENT_CONTEXT"' in (ROOT / "apps" / "agent" / "agent_runtime"
                                      / "providers" / "livekit_plugins.py").read_text(encoding="utf-8")
    # agent 接线在同一 `if _intent_context_enabled():` 块内(set + hint 都受闸)
    block = _SRC[_SRC.index("if _intent_context_enabled():") :]
    block = block[: block.index("_filler.arm()")]
    assert "context_state.set_customer_intent(" in block
    assert "_filler.hint_category(" in block
