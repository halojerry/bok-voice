"""QA 同条目冷却 + expr 尾巴剥账本（真机第一批 C 组，2026-09-30 call-4392c7bb 实证）。

病理：①「怎么赔偿」「钱怎么给我」两问命中同一条 QA，同一答案一字不差播两遍
（qa-fastpath 17:08:18/17:08:32）——客户换问法=要新答案，重播旧罐头=答非
所问；②interrupted 补账行 transcript 落了 `="expression" label="calm"/>`——
ExprAware 标记被流截断后的**无前缀尾巴**，_EXPR_SYNC_RE 只匹配 <expr 开头
形态，漏。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.agent import _clean_transcript  # noqa: E402

AGENT_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")


def test_expr_truncated_tail_stripped():
    """截断尾巴（无 <expr 前缀）也要剥——账本不再落标记残片。"""
    assert _clean_transcript('好嘅你等="expression" label="calm"/>') == "好嘅你等"


def test_normal_text_not_eaten():
    """正文普通 ="xxx" 形态不被误吃（锚定 expr 属性名）。"""
    assert _clean_transcript('佢話="你好" 無問題') == '佢話="你好" 無問題'


def test_expr_full_tag_still_stripped():
    """完整标记形态不回归。"""
    assert _clean_transcript('<expr type="expression" label="calm"/>好的') == "好的"


def test_qa_canned_cooldown_gate_pinned():
    """源级 pin：_qa_canned_say 同条目冷却门（BOK_QA_CANNED_COOLDOWN_S）。"""
    body_start = AGENT_SRC.index("async def _qa_canned_say")
    body_end = AGENT_SRC.index("\n            # ---- 话术图引擎", body_start)
    body = AGENT_SRC[body_start:body_end]
    assert '_qa_canned_last["id"]' in body, "冷却账本须在 qa_canned_say 体内"
    assert 'BOK_QA_CANNED_COOLDOWN_S' in body, "冷却窗 env 旋钮"
    assert "QA_CANNED_COOLDOWN" in body, "冷却触发打点"
