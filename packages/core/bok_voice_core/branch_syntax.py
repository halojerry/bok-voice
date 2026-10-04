"""话术分支语法共享契约（2026-10-04 C1 单源化）——四实现唯一的正则权威源。

背景：分支行语法（正稿/分支/注意 + 动作标记）曾有四份拷贝各自为政——
flow.py（运行时）、lib/flow-canvas.ts（画布 round-trip）、
control_plane/gap_proposals.py（挖掘镜像）、scripts/seed/prepare_csc_data.py
（训练料）——繁体锚「如果客戶」只在训练料拷贝里被认，运行时静默丢弃
种子粤语模板的整层分支（scripts/probes/probe_s2s_vs_cascade.py 记录的实弹漂移）。

本模块收编正则三件套 + 动作常量 + parse_branch_action 纯函数：
  - flow.py re-export（模块别名 `_BRANCH_LINE_RE` 等保持旧名，钉测试不破）；
  - gap_proposals.py / prepare_csc_data.py 直接 import（CP 不 import
    agent_runtime 的红线不变——单源家在 packages/core，三方都已是其消费方）；
  - lib/flow-canvas.ts 无法 import Python，仍是逐语义镜像——由
    tests/test_branch_syntax_parity.py 的源级 pin 钉住与本文件的字面一致。

锚词集（简繁并收，行为变化 2026-10-04 拍板）：如果客户/如果客戶/
If the customer/When the customer；动作标记 收线/收線、挂断/掛斷、
转人工/轉人工（跳第N步/留本步 两形同字）。序列化规范形恒简体锚
（gap_proposals.propose / flow-canvas.serializeStepRef 同款策略）——
解析认两形、写出一形，round-trip 不变量 parse(serialize(parse(x))) 不破。
"""
from __future__ import annotations

import re

# 分支行：锚词 + 条件(1..120 非贪婪) + \s*→\s* + 应答(首个非空白字符起)。
BRANCH_LINE_RE = re.compile(
    r"^(?:如果客户|如果客戶|(?:If|When)\s+the\s+customer)\s*(?P<cond>.{1,120}?)\s*→\s*(?P<resp>\S.*)$",
    re.IGNORECASE,
)

# 注意行：操作性事实（恒注入，不入台词）。
NOTE_LINE_RE = re.compile(r"^(?:注意|Notes?)\s*[:：]\s*(?P<note>.+)$", re.IGNORECASE)

# 动作常量：与 agent.py 派发臂一一对应（refuse=收尾态+定时挂断、
# handoff=打铃不抢话、jump=跳步、hold=本轮不推进）。
BRANCH_ACTION_HOLD = "hold"
BRANCH_ACTION_REFUSE = "refuse"
BRANCH_ACTION_HANDOFF = "handoff"
BRANCH_ACTION_JUMP = "jump"

# 分支应答首部动作标记：标记内空白容错（【 收线 】/【跳第 3 步】）；
# step 限 1-3 位数字，越界值退回默认语义而非报错——运营手滑不该把整条
# 分支变成引擎不认的死行，更不该把标记念出声。
BRANCH_ACTION_RE = re.compile(
    r"^【\s*(?P<kind>收线|收線|挂断|掛斷|转人工|轉人工|跳第\s*(?P<step>\d{1,3})\s*步|留本步)\s*】\s*"
)

# 未被识别的指令行告警用锚词清单（flow.py _warn_unparsed_directive 的文案）。
BRANCH_ANCHOR_HINT = "如果客户/如果客戶/If the customer/When the customer"


def parse_branch_action(resp: str) -> tuple[str, int, str]:
    """应答首部动作前缀 → (action, step, text)。识别到标记一律消费标记。

    - 【收线】/【收線】/【挂断】/【掛斷】→ ("refuse", 0, 余下文本.strip())
      ——收线台词走直念,剥首尾空白
    - 【转人工】/【轉人工】→ ("handoff", 0, 余下文本)
    - 【跳第N步】→ ("jump", N, 余下文本);N < 1(如「跳第0步」)→ ("", 0,
      余下文本):标记已消费、退回默认语义——既不跳步,也不把标记念出声
    - 【留本步】→ ("hold", 0, 余下文本)
    - 无标记 / resp 为空 → ("", 0, resp 原样):逐字节不动(现状语义)
    """
    s = str(resp or "")
    m = BRANCH_ACTION_RE.match(s)
    if not m:
        return ("", 0, s)
    kind = m.group("kind")
    rest = s[m.end():]
    if kind in ("收线", "收線", "挂断", "掛斷"):
        return (BRANCH_ACTION_REFUSE, 0, rest.strip())
    if kind in ("转人工", "轉人工"):
        return (BRANCH_ACTION_HANDOFF, 0, rest)
    if kind == "留本步":
        return (BRANCH_ACTION_HOLD, 0, rest)
    # 跳第N步:N<1(含 0/前导零以外的非法组合交给 \d{1,3} 已拦)退默认语义
    step = int(m.group("step") or 0)
    if step < 1:
        return ("", 0, rest)
    return (BRANCH_ACTION_JUMP, step, rest)
