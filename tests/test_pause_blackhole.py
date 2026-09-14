"""C1 暂停黑洞修复(2026-09-13,call-15a2f586 实证):

旧版 supervisor 暂停 = 26s 纯静默:暂停期用户轮不落库(turns 表空洞)、
flow 静默推进(rule=auto/judge 两路照跑 step4→6)、resume 边沿轮被吞。
修复三件:暂停进入脚本直念一句可听交代(_pause_ack_line,零 TTFT)/
暂停期用户轮照落库 gen=paused+整轮丢弃(hook 顶部冻结,WA 累积/detect/
rule 推进/judge 全部让位)/背景 judge 完成时暂停中不推进。

hook 冻结逻辑在 entrypoint 局部(PausableAgent 闭包),单测覆盖可测面:
播报文案纯函数 + WA flush/judge 的暂停闸经由全量回归兜底;实机 30s 暂停
场景验收待起栈(轮次落库/无静默推进/resume 出声)。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.agent import _pause_ack_line  # noqa: E402


def test_pause_ack_line_three_langs_no_action_verbs():
    zh = _pause_ack_line("zh")
    can = _pause_ack_line("cantonese")
    en = _pause_ack_line("en")
    # 三语齐、非空、短(≤14 字/词,一句可听交代不是台词)
    assert zh and can and en
    assert len(zh) <= 14 and len(can) <= 14 and len(en.split()) <= 6
    # 万能话术原则:零动作动词(「查/核实/睇/处理」类不出现——暂停语境
    # 未知,动作承诺会穿帮)
    for line in (zh, can, en):
        for verb in ("查", "核实", "睇", "处理", "check", "verify", "look"):
            assert verb not in line, f"action verb {verb!r} in {line!r}"
    # 语言对应:粤语带粤字,英文是英文
    assert "稍等" in zh
    assert "陣" in can
    assert "moment" in en.lower()
    # 未知语言回落 zh
    assert _pause_ack_line("xx") == zh
