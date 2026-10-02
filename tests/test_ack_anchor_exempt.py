"""ack/兜底直念行不进【你上一句】锚与摘要(2026-09-28 道歉毒性消散)。

深调实证:慢轮次的 fallback 道歉/watchdog-ack 落进重复锚+摘要记忆后,4B 会
模仿「唔好意思…」开头、回复收敛变短——慢轮次教会模型道歉敷衍。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.agent import (  # noqa: E402
    _ack_anchor_texts,
    _defer_ack_line,
    _followup_ack_line,
    _is_ack_anchor_text,
    _llm_fallback_line,
    _starve_ack_line,
    _storm_ack_line,
)


def test_all_ack_lines_across_langs_are_exempt():
    # EX-2（2026-09-28）补 _followup_ack_line：跟进建单确认语同属零内容承诺应承。
    for fn in (
        _llm_fallback_line,
        _starve_ack_line,
        _storm_ack_line,
        _defer_ack_line,
        _followup_ack_line,
    ):
        for lang in ("zh", "cantonese", "en"):
            assert _is_ack_anchor_text(fn(lang)), f"{fn.__name__}/{lang} 应豁免"


def test_real_replies_not_exempt():
    assert not _is_ack_anchor_text("好的，我帮您核对订单，您是在哪个平台买的？")
    assert not _is_ack_anchor_text("首先，我们会先核实您这件货品的订单金额。")
    assert not _is_ack_anchor_text("")
    # 非整行(真回复恰好以道歉开头但继续给了内容)不豁免——只挡「纯 ack 行」
    assert not _is_ack_anchor_text("不好意思让您久等了，赔偿流程是 3 到 5 个工作日。")


def test_exempt_set_shape():
    s = _ack_anchor_texts()
    assert len(s) == 6 * 3  # EX-2：6 族(含 followup-ack / garbled-reask) × 3 语言,无碰撞
    assert all(isinstance(x, str) and x for x in s)


def test_item_handler_wiring_pinned():
    """源级 pin:_on_item_for_context 豁免分支在场(跳过锚+跳过摘要 spawn);
    _report_assistant_turn 把流内兜底道歉行归位(退出「实答」判定面)。"""
    src = (Path(__file__).resolve().parents[1] / "apps/agent/agent_runtime/agent.py").read_text(
        encoding="utf-8"
    )
    assert "_assistant_ack = _is_ack_anchor_text(text)" in src
    assert "不进重复锚/摘要" in src
    assert "if not _assistant_ack:\n                _spawn_report(_async_update_context(" in src
    assert 'if gen == "llm" and _is_ack_anchor_text(text):\n            gen, provider = "script", "fallback-ack"' in src


def test_timeout_budgets_pinned():
    """源级 pin(2026-09-28 定时器普查):响应看门狗缺省 6s(=commit→首声 p95
    5.66s 之外才出手)、LLM read-gap 22s(盖冷 prefill p95 18.9s)——防手调
    常数回退到与延迟分布相撞的旧值。"""
    root = Path(__file__).resolve().parents[1]
    agent_src = (root / "apps/agent/agent_runtime/agent.py").read_text(encoding="utf-8")
    assert 'os.environ.get("BOK_RESPONSE_WATCHDOG_S", "6")' in agent_src
    plugins_src = (
        (root / "apps/agent/agent_runtime/providers/livekit_plugins.py").read_text(encoding="utf-8")
    )
    assert 'os.environ.get("LLM_REQUEST_TIMEOUT_S", "22")' in plugins_src
