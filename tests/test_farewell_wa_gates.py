"""C3+C4(2026-09-13):

C3a WA 报号轮落库——call-6f1c4ee3 实证 stash 路径整轮蒸发(turns 表报号轮
空洞);stash/flush 两路现在分别以 provider=wa-stash/wa-merged 落库在案
(hook 闭包内,代码位次+compileall 保证,本文件测 C3b 可测面)。

C3b 复述确认前号长闸——7 位错号被复述确认+客户「嗯嗯好的」假确认(call-
6f1c4ee3):粤=8 位港号(852+8=11 亦收)/zh=11 位手机/en 宽松;只挡复述
确认不挡捕获(「报出就收」铁律不动);BOK_WA_LEN_CHECK=0 关。

C4 道别分流——「拜拜」命中 REFUSE 令谈成通话标 declined:FAREWELL verdict
剥出,agent 侧 disposition 按业务结果(captured→scheduled,否则 polite_close)。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.agent import _wa_confirm_or_reask  # noqa: E402
from agent_runtime.flow import (  # noqa: E402
    FAREWELL,
    REFUSE,
    decide_advance,
    parse_steps,
)
from agent_runtime.flow import FlowController, should_auto_advance  # noqa: E402


# ---- C4 verdict ----


def test_farewell_vs_refuse():
    assert decide_advance("拜拜") == FAREWELL
    assert decide_advance("嗯，拜拜。拜拜。") == FAREWELL
    assert decide_advance("再见") == FAREWELL
    assert decide_advance("Bye bye") == FAREWELL
    # 拒绝优先:「别再打+拜拜」主体是拒绝,不是纯道别
    assert decide_advance("别再打来了，拜拜。") == REFUSE
    # 真拒绝仍是 REFUSE
    assert decide_advance("唔需要喇，唔该") == REFUSE
    assert decide_advance("不用了谢谢") == REFUSE


def test_farewell_never_advances():
    assert should_auto_advance(
        current=2, goal="核实购买平台", ref="咁你係喺邊個平台買？", user_text="拜拜", verdict=FAREWELL
    ) is False


def test_farewell_renders_without_guidance():
    fc = FlowController(steps=parse_steps('[{"goal":"通知","ref":"我哋係集運中轉倉","say":true}]'))
    fc.last_verdict = FAREWELL
    txt = fc.current_step_text()
    assert txt  # 渲染不炸;FAREWELL 无 verdict 指引(收线分流在 agent 侧)


# ---- C3b 号长闸 ----


def test_wa_len_gate_cantonese():
    # 7 位(粤)→ 请重讲,不复述
    out = _wa_confirm_or_reask("cantonese", "1233445")
    assert "完整" in out and "1233445" not in out
    # 8 位港号 → 正常复述
    out8 = _wa_confirm_or_reask("cantonese", "98765432")
    assert "收到" in out8 and "九八七六五四三二" in out8  # 粤线数字转粤语汉字逐位念
    # 852+8=11 → 收
    out11 = _wa_confirm_or_reask("cantonese", "85298765432")
    assert "收到" in out11 and "八五二九八七六五四三二" in out11


def test_wa_len_gate_zh_and_en():
    assert "13800000000" in _wa_confirm_or_reask("zh", "13800000000")  # 11 位手机
    reask = _wa_confirm_or_reask("zh", "1234567")
    assert "完整" in reask and "1234567" not in reask
    # en 宽松不校验:任意长度照复述
    assert "555123" in _wa_confirm_or_reask("en", "555123")


def test_wa_len_gate_kill_switch(monkeypatch):
    monkeypatch.setenv("BOK_WA_LEN_CHECK", "0")
    # 关闸后 7 位照复述(回旧行为;粤线数字转粤语汉字逐位念)
    assert "收到" in _wa_confirm_or_reask("cantonese", "1233445") and "一二三三四四五" in _wa_confirm_or_reask("cantonese", "1233445")
