"""A2/A3 长稿护栏(2026-09-13,plan 甲.2/甲.3):

A2a say-step 长度护栏——运营手滑再灌 147/160 字直念稿(28-30s 独白的数据
根源)时只念首句+打点 SAY_STEP_TOO_LONG;BOK_SAY_STEP_LIMIT=0 关。
A2b 回复长度铁律进静态前缀(livekit_plugins,语言纯度门禁覆盖)。
A3 饿死兜底——连续 2 轮零 assistant 输出后第 3 轮直念 ≤15 字短承接
(hook 闭包内,代码位次保证;本文件测可测面)。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.agent import _say_step_cap, _starve_ack_line  # noqa: E402


LONG = (
    "首先，我们会先核实您这件货品的订单金额：如果金额不足 100 元，我们会根据香港速递条例，"
    "帮您申请 300 到 600 元的赔偿；金额大于 200 元的，就按货价 2 至 3 倍赔偿；"
    "如果金额超过 1000 元，除了按原价赔偿之外，另外再加快递保险补偿 300 元给您。您看这个方案可以接受吗？"
)


def test_say_step_cap_truncates_to_first_sentence(capsys):
    # 首句早结束(≤limit+40 窗内)→ 念首句;无早句读的长文 → 硬切(下一例)
    t = "您好，这里是集运中转仓，有一件您的货件在打包期间遗失了，我们会负责赔偿。" + "另外还要跟您讲很多很多的后续细节内容" * 8 + "。"
    out = _say_step_cap(t)
    assert out.endswith("。") and "后续细节" not in out
    assert len(out) < 80
    assert "SAY_STEP_TOO_LONG" in capsys.readouterr().out


def test_say_step_cap_late_sentence_hard_cut(capsys):
    # 首个句号在 120 字窗外(如 LONG 的三档长稿)→ 硬切 80 字,同样打点
    out = _say_step_cap(LONG)
    assert len(out) == 80
    assert "SAY_STEP_TOO_LONG" in capsys.readouterr().out


def test_say_step_cap_short_text_untouched():
    short = "我们会按香港速递条例给您赔偿，最低 300 元起。您看这个方案可以接受吗？"
    assert _say_step_cap(short) == short


def test_say_step_cap_no_punct_hard_cut(capsys):
    t = "无标点长文本" * 30
    out = _say_step_cap(t)
    assert len(out) == 80  # 无句读 → 硬切到 limit
    assert "SAY_STEP_TOO_LONG" in capsys.readouterr().out


def test_say_step_cap_kill_switch(monkeypatch):
    monkeypatch.setenv("BOK_SAY_STEP_LIMIT", "0")
    assert _say_step_cap(LONG) == LONG


def test_starve_ack_line_short_and_safe():
    for lang, line in (
        ("zh", _starve_ack_line("zh")),
        ("cantonese", _starve_ack_line("cantonese")),
        ("en", _starve_ack_line("en")),
    ):
        # ≤15 字/词量级,零动作动词(只表态「还在、在听」)
        assert line
        assert len(line) <= 22 if lang != "en" else len(line.split()) <= 7
        for verb in ("查", "核实", "睇", "处理", "check", "verify"):
            assert verb not in line, f"{lang}: {line!r}"
    assert "係嘅" in _starve_ack_line("cantonese")
    assert "在的" in _starve_ack_line("zh")
    assert _starve_ack_line("xx") == _starve_ack_line("zh")
