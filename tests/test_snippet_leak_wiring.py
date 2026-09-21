"""E1 snippet 后置轨 + E2 热词泄漏清洗的接线钉死(2026-09-21)。

被接线的是两个纯函数模块(packages/core/bok_voice_core/snippets.py /
hotword_leak.py,本身已有离线单测)——本文件只钉**接线语义**:

- 函数级:`_asr_postprocess` 的两轨顺序(先 E2 后 E1)、两枚 kill-switch 独立
  回退、空词表零变化、纯 dump 丢弃、命中替换 + applied 记账、数字逐字不动;
- 结构级:落点必须插在所有下游消费者(含 turns 落库面)之前,且 hook 与
  `_on_conversation_item` 用同一条链复算(听 A 记 B 禁令);
- 立法面:两枚 env 键必须在 `bok._FORWARD_ENV`(test_forward_env 同源契约)。

参照 test_intent_judge_wiring.py 的源级锚姿势(闭包接线离线起不了真栈)。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import bok  # noqa: E402

from agent_runtime import agent as ag  # noqa: E402

_SRC = (
    Path(__file__).resolve().parents[1] / "apps" / "agent" / "agent_runtime" / "agent.py"
).read_text(encoding="utf-8")


# ---------------------------------------------------------------- 函数级语义


def test_kill_switches_default_on_both_tracks(monkeypatch):
    """默认档(env 未设):E2 剥 dump、E1 替换都生效。"""
    monkeypatch.delenv("BOK_SNIPPETS", raising=False)
    monkeypatch.delenv("BOK_HOTWORD_LEAK_SANITIZE", raising=False)
    rules = [{"trigger": "单后", "replacement": "单号"}]
    out, leak, applied = ag._asr_postprocess(
        "Vocabulary: 拼多多 京东", snippet_rules=rules, hotword_terms=["拼多多", "京东"]
    )
    assert (out, leak) == ("", "dropped")
    assert applied == []


def test_e2_kill_switch_off_keeps_dump(monkeypatch):
    """BOK_HOTWORD_LEAK_SANITIZE=0 → E2 整轨回退(原文保留),E1 照常。"""
    monkeypatch.setenv("BOK_HOTWORD_LEAK_SANITIZE", "0")
    monkeypatch.delenv("BOK_SNIPPETS", raising=False)
    rules = [{"trigger": "单后", "replacement": "单号"}]
    out, leak, applied = ag._asr_postprocess(
        "Vocabulary: 拼多多 京东",
        snippet_rules=rules,
        hotword_terms=["拼多多", "京东"],
    )
    assert leak == "" and out == "Vocabulary: 拼多多 京东"
    assert applied == []


def test_e1_kill_switch_off_keeps_text(monkeypatch):
    """BOK_SNIPPETS=0 → E1 回退零替换;E2 照常(snippet 不影响 E2 判定)。"""
    monkeypatch.setenv("BOK_SNIPPETS", "0")
    monkeypatch.delenv("BOK_HOTWORD_LEAK_SANITIZE", raising=False)
    rules = [{"trigger": "单后", "replacement": "单号"}]
    out, leak, applied = ag._asr_postprocess(
        "请问单后", snippet_rules=rules, hotword_terms=["拼多多", "京东"]
    )
    assert (out, leak, applied) == ("请问单后", "", [])


def test_empty_hotlist_and_rules_zero_change(capsys):
    """空词表 + 空规则 → 零变化;pure helper 零日志(日志在 hook 侧)。"""
    out, leak, applied = ag._asr_postprocess(
        "请问几时到", snippet_rules=[], hotword_terms=[]
    )
    assert (out, leak, applied) == ("请问几时到", "", [])
    captured = capsys.readouterr()
    assert "SNIPPET" not in captured.out and "ASR_LEAK" not in captured.out


def test_leak_dump_dropped_and_trimmed(monkeypatch):
    """纯 dump → dropped(空串);真话头+dump 尾 → trimmed(保留真话头)。"""
    monkeypatch.delenv("BOK_HOTWORD_LEAK_SANITIZE", raising=False)
    terms = ["拼多多", "京东"]
    dropped, st, _ = ag._asr_postprocess(
        "Vocabulary: 拼多多, 京东", snippet_rules=[], hotword_terms=terms
    )
    assert (dropped, st) == ("", "dropped")
    # 词表前缀 dump + 真内容尾(≥2 连续热词判泄漏)→ 丢 dump、留真内容(trimmed)
    trimmed, st2, _ = ag._asr_postprocess(
        "拼多多 京东 我想查下啦", snippet_rules=[], hotword_terms=terms
    )
    assert st2 == "trimmed" and "拼多多" not in trimmed and "我想查下" in trimmed


def test_snippet_hit_applied_and_recorded(monkeypatch):
    """命中:E1 替换落文本 + applied 记账(顺序在 E2 之后)。"""
    monkeypatch.delenv("BOK_SNIPPETS", raising=False)
    merged = ag.compile_snippet_rules([{"trigger": "单后", "replacement": "单号"}])
    assert [r.trigger for r in merged] == ["单后"]
    out, leak, applied = ag._asr_postprocess(
        "我想查下我嘅单后", snippet_rules=merged, hotword_terms=[]
    )
    assert out == "我想查下我嘅单号"
    assert applied == [("单后", "单号")]


def test_compile_snippet_rules_logs_skipped(capsys):
    """装配期编译:数字 trigger 被守卫拦下 → skipped 审计日志一条。"""
    ag.compile_snippet_rules([{"trigger": "单号123", "replacement": "单号"}])
    assert "SNIPPET skipped=1 reasons=digits" in capsys.readouterr().out


def test_digits_pass_chain_byte_identical(monkeypatch):
    """数字铁律:含数字文本过整链逐字不变(空词表 + 命中规则两档)。"""
    monkeypatch.delenv("BOK_SNIPPETS", raising=False)
    monkeypatch.delenv("BOK_HOTWORD_LEAK_SANITIZE", raising=False)
    merged = ag.compile_snippet_rules([{"trigger": "单后", "replacement": "单号"}])
    text = "我的单号係 8817 6655 4433，WhatsApp 係 5704 1234"
    # 词表在场(单号是热词),但句子不以热词打头 → 不该被当成泄漏剥掉
    out, leak, applied = ag._asr_postprocess(
        text, snippet_rules=merged, hotword_terms=["单号", "WhatsApp"]
    )
    assert out == text and leak == "" and applied == []
    # 热词打头 + 单词命中但 fallback 为空 → 单词命中不算泄漏(模块偏差②契约)
    out2, leak2, _ = ag._asr_postprocess(
        "单号 881766554433", snippet_rules=[], hotword_terms=["单号"]
    )
    assert out2 == "单号 881766554433" and leak2 == ""


# ---------------------------------------------------------------- 结构级锚


def test_hook_postprocess_precedes_downstream_and_last_user_text():
    """落点:净化必须插在 flow_ctrl.last_user_text 赋值(下游口径起点)之前。"""
    call = _SRC.index("_asr_postprocess(\n                user_text,")
    last_user = _SRC.index("flow_ctrl.last_user_text = user_text", call)
    branch = _SRC.index("if _leak_state == \"dropped\":", call)
    assert call < branch < last_user  # 净化 → 丢弃判定 → 才是下游口径
    # 丢弃走既有静音分支(StopResponse),不新造语义
    seg = _SRC[call:last_user]
    assert "_cancel_response_watchdog()" in seg and "raise StopResponse()" in seg


def test_conversation_item_recomputes_same_chain():
    """落库面同链复算:钩子净化后文本才是下游用的,落库必须同文本。"""
    item_def = _SRC.index("def _on_conversation_item(ev):")
    item_end = _SRC.index("_spawn_report(_report_assistant_turn", item_def)
    seg = _SRC[item_def:item_end]
    assert "_asr_postprocess(" in seg
    assert "ASR_LEAK_SANITIZE_TURN_HIDDEN" in seg  # 纯 dump 不落库
    # 落库调用用的是复算后的 text
    assert "_clean_transcript(text)" in seg


def test_forward_env_registers_both_kill_switches():
    """立法动作:两枚 env 键进 _FORWARD_ENV(prod 封闭面 kill-switch 可达)。"""
    assert "BOK_SNIPPETS" in bok._FORWARD_ENV
    assert "BOK_HOTWORD_LEAK_SANITIZE" in bok._FORWARD_ENV
