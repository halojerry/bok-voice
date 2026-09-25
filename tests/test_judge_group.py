"""W9 JudgeGroup prompt 规则回归的 CI 面(2026-09-24)。

离线腿(恒跑,零模型零 HTTP):selftest 进程内 + 评分纯函数 + 库形状 + 生产渲染标记。
真栈腿(BOK_JUDGE_GROUP_LIVE=1 才跑):本地 4B 车道跑轮,过线率 ≥85%——改
`_SHARED_RESPONSE_RULES`/跳步禁讲/共享指引/判官模型后手动放开跑。

已知读数(2026-09-24 首跑):4B 100%/92%/100% 三跑(obj-zh-fraud 身份步质疑轮偶发
跳问平台=步纪律采样方差);9B 92%(comp-zh-complain 投诉轮复述档位表=赔偿纪律
真破防,双判官同卷的价值所在)。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import judge_group_eval as jge  # noqa: E402


def test_selftest_in_process_exit_zero():
    assert jge.selftest() == 0


def test_bank_shape():
    assert len(jge.BANK) >= 12
    ids = [c["id"] for c in jge.BANK]
    assert len(ids) == len(set(ids))
    fams = {c["family"] for c in jge.BANK}
    assert fams == {"objection", "compensation", "jump"}
    for c in jge.BANK:
        assert str(c.get("user") or "").strip()
        if c["family"] == "jump":
            assert "jump_to" in c, f"{c['id']}: jump 族缺 jump_to"
        # 每条至少一个硬断言面(禁词/长度/复读门),纯 expect_soft 也能过但库层面
        # 不允许零硬面用例(空断言=假绿)。
        assert (
            c.get("forbid")
            or c.get("max_chars") is not None
            or c.get("max_sim_last") is not None
            or (c.get("expect_any") and not c.get("expect_soft"))
        ), f"{c['id']}: 零硬断言面"


def test_grade_forbid_and_expect():
    case = {"forbid": ["倍"], "expect_any": ["抱歉"], "max_chars": None}
    g = jge._grade(case, "很抱歉给您添麻烦了")
    assert g["ok"] and g["expect_hit"] == ["抱歉"]
    g2 = jge._grade(case, "抱歉,这个赔2倍")
    assert not g2["ok"] and g2["forbid_hit"] == ["倍"]


def test_grade_expect_soft_is_informational():
    case = {"forbid": ["倍"], "expect_any": ["不存在词"], "expect_soft": True, "max_chars": None}
    g = jge._grade(case, "好的,马上为您登记办理")
    assert g["ok"], "expect_soft 轮:禁词干净即过(落词面是信息位)"


def test_grade_length_gate():
    case = {"forbid": [], "expect_any": ["好"], "max_chars": 10}
    assert jge._grade(case, "好的")["ok"]
    assert not jge._grade(case, "好的没问题我马上给您登记办理一下请稍等")["ok"]


def test_grade_verbatim_repeat_gate():
    last = "为了核实，您是在拼多多、淘宝还是京东买的呢？"
    case = {"forbid": [], "expect_any": ["客服"], "max_chars": None, "max_sim_last": 0.9}
    ok_reply = "我是集运中转仓客服，通过官方渠道联系您，可回拨核实。"
    assert jge._grade(case | {"last_reply": last}, ok_reply)["ok"]
    # 逐字复读上一句(换零个字)=复读门破防(安抚锚是【你上一句】重复控制的行为面)
    assert not jge._grade(case | {"last_reply": last}, last + " ")["ok"]


def test_jump_prompt_markers_offline():
    """跳步族的生产渲染锚:【跳转进入】点名被跳步 +【禁讲清单】照录原话 + 总览图规则行。"""
    j1 = next(c for c in jge.BANK if c["id"] == "jump-zh-complain")
    fc = jge._build_controller(j1)
    assert fc._jump_skipped == [2, 3, 4]
    assert "直接跳入后面某一步" in fc.flow_overview()
    tail = jge._build_messages(j1)[-1]["content"]
    assert "【跳转进入】" in tail and "【禁讲清单】" in tail
    assert "拼多多、淘宝还是京东" in tail  # 禁讲清单照录被跳步问句原话


def test_shared_rules_marker_in_system():
    """objection/compensation 族的断言锚:共享应答规则恒在静态前缀。"""
    system = jge._build_messages(jge.BANK[0])[0]["content"]
    assert "【身份与来电质疑】" in system
    assert "【赔偿数字纪律】" in system


def test_live_round_gated(monkeypatch, capsys):
    """真栈腿:默认 skip;BOK_JUDGE_GROUP_LIVE=1 且栈在跑时放开(4B 车道 ≥85%)。"""
    import os

    if os.environ.get("BOK_JUDGE_GROUP_LIVE") != "1":
        import pytest

        pytest.skip("BOK_JUDGE_GROUP_LIVE!=1 (live judge-group round opt-in)")
    monkeypatch.setattr(
        sys, "argv", ["judge_group_eval.py", "--models", "4b", "--min-pass", "0.85"]
    )
    assert jge.main() == 0, f"live round FAIL:\n{capsys.readouterr().out[-3000:]}"
