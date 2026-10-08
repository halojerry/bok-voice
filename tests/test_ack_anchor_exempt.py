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
    # W2 刀2(2026-10-08)起 7 族(+repeat-ack)× 3 语言,无碰撞
    assert len(s) == 7 * 3
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
    agent_src = (root / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    assert 'os.environ.get("BOK_RESPONSE_WATCHDOG_S", "6")' in agent_src
    plugins_src = (
        (root / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py").read_text(encoding="utf-8")
    )
    assert 'os.environ.get("LLM_REQUEST_TIMEOUT_S", "22")' in plugins_src


def test_interrupted_item_corpus_skip_pinned():
    """2026-10-07 风暴误杀票:被打断的 LLM 半截 item 复用豁免通道——不进上句
    锚/账本/摘要。真因链:碎片进锚 → 风暴后重生成同答案首句 SequenceMatcher
    ≥0.9 → 头冻结 → 6s 看门狗 force-interrupt → 33 字死在 REPEAT_GUARD_CANCEL_DROP。
    打断后重述=有意识修复,碎片不配当复读比对语料;chat ctx 真历史不受影响。"""
    root = Path(__file__).resolve().parents[1]
    src = (root / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    assert '_item_interrupted = bool(getattr(item, "interrupted", False))' in src
    assert "interrupted-item corpus skip" in src
    # 复用豁免出口(锚/账本/摘要三面同让):interrupted 判定在 ack 判定之后、
    # 豁免出口之前。
    i_ack = src.index("_assistant_ack = _is_ack_anchor_text(text)")
    i_int = src.index('_item_interrupted = bool(getattr(item, "interrupted", False))')
    i_exit = src.index("if _assistant_ack:\n                    print(f\"[agent] ack-anchor-exempt")
    assert i_ack < i_int < i_exit
    # gen="interrupted" 落账轮(W1d)在 reply_ledger() 只回 llm-gen 的过滤下
    # 本就不进比对面——本票堵的是 item 路的 llm-gen 记录,双路径皆净。
    plugins_src = (
        (root / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py").read_text(encoding="utf-8")
    )
    assert 'if g == "llm"' in plugins_src  # 账本过滤仍在(票据侧碎片面已由本票封)
