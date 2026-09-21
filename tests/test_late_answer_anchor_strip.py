"""L2 晚到补答的尾部锚剥离（2026-09-21，§20.5/§22）。

背景：晚到补答由 ``late_answer_cb`` 从 tee 直投 ``_say_script``，结构性绕过主回复
流出口的 ``_StripTailAnchorStream`` / ``_RepeatSelfGuardStream``——真栈 B 臂实证把
``【你上一句】「唔好意思，`` 整段念出了声（turns 落库可查）。修法 = 投递点先过
``strip_tail_anchor_text``（首个标签起截断，标签前正文保留）。

本文件钉两件事：①纯函数语义（无标签原样/有标签截断/正文保留/多次出现取首个）；
②agent.py 的 ``_late_answer_say`` 在 ``_say_script`` 之前调用它（结构锚，摘掉即红）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_runtime.providers.livekit_plugins import _TAIL_ANCHOR_LABEL, strip_tail_anchor_text

ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")


def test_no_label_returns_unchanged():
    assert strip_tail_anchor_text("好嘅，我幫你查下。") == "好嘅，我幫你查下。"
    assert strip_tail_anchor_text("") == ""


def test_label_strips_from_first_occurrence():
    text = f"我即刻幫你查。\n\n{_TAIL_ANCHOR_LABEL}「唔好意思，"
    assert strip_tail_anchor_text(text) == "我即刻幫你查。"


def test_label_only_strips_to_empty():
    assert strip_tail_anchor_text(f"{_TAIL_ANCHOR_LABEL}「abc") == ""


def test_second_label_is_moot_first_wins():
    # 首标签即截断——锚块是框架尾部模板，模型输出含标签即为复刻，不存在合法包含。
    text = f"正文甲{_TAIL_ANCHOR_LABEL}「乙」{_TAIL_ANCHOR_LABEL}「丙"
    assert strip_tail_anchor_text(text) == "正文甲"


def test_digits_before_label_untouched():
    # 数字零降级铁律：标签前正文里的数字逐字保留。
    text = f"你單號係 377890。{_TAIL_ANCHOR_LABEL}「唔該"
    assert strip_tail_anchor_text(text) == "你單號係 377890。"


def test_late_answer_say_strips_before_say():
    """结构锚：agent.py 的 _late_answer_say 必须在 _say_script 之前剥锚。"""
    m = re.search(
        r"async def _late_answer_say.*?(?=\n    (?:async def |def )|\n    [A-Za-z_]+ =)",
        AGENT_SRC,
        re.S,
    )
    assert m, "_late_answer_say 未找到"
    body = m.group(0)
    assert "_strip_tail_anchor_text(text)" in body, "补答投递点未调用锚剥离（L2 回归）"
    assert "_say_script(" in body
    strip_line = body.index("_strip_tail_anchor_text(text)")
    say_line = body.index("_say_script(")
    assert strip_line < say_line, "剥离必须发生在出声之前"
    # 剥弹顺序也不能反：看门狗先拆（防 4s 闸），随后剥锚、出声。
    cancel_line = body.index("_cancel_response_watchdog()")
    assert cancel_line < strip_line, "拆看门狗必须最先（PR #128 的既有约定）"


@pytest.mark.parametrize(
    "bad", [None, "   ", "\n\t"]
)
def test_degenerate_inputs_safe(bad):
    # None/空白进 → 原样出（str() 兜底 + rstrip 不炸），不产生新文本。
    out = strip_tail_anchor_text(bad)
    assert isinstance(out, str)
