"""罐头/分支文本内部指令守卫（纯函数，确定性判据，零 LLM）。

出处（M-22，2026-09-23 生产就绪验收 task-4 全批轮 Major M1）：生产三语模板的
分支行是写给 LLM 的「教练文案」（zh「说要核对订单才能确认到，去下一步问平台。」
/ en "admit the packing mistake, apologise sincerely, don't shift blame; …" /
粤「话係集运仓按收件登记资料来电核实，语气诚恳唔好驳；…」），A-① 分支罐头腿
（tts_cache 已物化）命中后 gen=script/provider=branch-canned **原样出声**——
内部指引被逐字念给客户（zh/s6 防诈×3 同句复读、en/s6 防诈×4 零真实应答）。

本模块给「可念台词」vs「内部指令」一个确定性判据单点，两处消费：

1. **agent 运行时出声前守卫**（kill 开关 ``BOK_CANNED_TEXT_GUARD``，默认开）：
   分支罐头/分支收线台词直念**出声前**检测，命中即拒出声——分支应答文本仍经
   渐进披露注入 prompt（教练文案的本来用途），本轮落回 LLM 正常生成；
   打点 ``CANNED_COACH_BLOCKED``。
2. **CP 模板保存校验**（与 ``flow_graph.validate_flow_graph`` 同族，400）：
   POST/PUT 携带 ``steps_json`` 时逐分支行检测，把「教练文案进罐头」挡在
   写入口。存量违例行清单交运营清理（本模块不改任何业务数据）。

判据设计（高精度优先，召回靠多信号 OR；误伤代价=罐头不出声落 LLM 重述，漏放
代价=内部指令出声事故，故每条信号都要求「客户-facing 台词几乎不可能出现」）：

- **残留指令括号族**：``【…】`` 动作前缀（【收线】/【跳第N步】/【留本步】…）由
  ``parse_branch_action`` 消费；应答文本**消费后仍见**【…】=内部标记残留。
- **中/粤教练头锚**（行首祈使动词=对坐席的指引）：说/说明/告诉/提示/提醒/建议/
  安抚/道歉/承认/解释/强调/表达/引导/口径/礼貌/语气 + 粤「话」（排除「话你知/
  话俾」等 customer-facing 顶真用法）。
- **英文教练头锚**：say/tell/suggest/admit/apologise… 行首祈使（排除 "Don't
  worry" 这类合法台词——**不收 don't**）。
- **高精度教练短语**（任意位置）：「语气诚恳」「答完带回」「提示客户」
  "stay sincere" "bring the conversation back" 等。

动作前缀剥离（``strip_branch_action_prefix``）是 ``flow.py:126
_BRANCH_ACTION_RE`` 的**逐字节镜像**——CP 不 import agent_runtime（云端镜像
不含 apps/agent；同款镜像先例：gap_proposals._BRANCH_COND_RE 镜像
flow.py:102）。改动作标记语法须三处同步：flow.py / flow-canvas.ts / 本文件。

纯函数：零 I/O、零全局状态、零时间依赖；同输入恒同输出。
"""

from __future__ import annotations

import re

__all__ = [
    "coach_hits",
    "is_internal_instruction",
    "strip_branch_action_prefix",
]

# ---- 残留指令括号族：【…】动作标记消费后仍残留 = 内部标记 ----
# 1-24 字括注（正常动作标记都短；超长括注更唔该拦）。
_COACH_BRACKET_RE = re.compile(r"【[^】]{1,24}】")

# ---- 中/粤教练头锚（行首=对坐席的祈使指引）----
# 「话」排除后接 你/我/妳/俾/題/题 的 customer-facing 用法（话你知/话俾你听/话题）。
_ZH_COACH_HEAD_RE = re.compile(
    r"^\s*(?:"
    r"说明|说(?!不定)|话(?![你我妳俾題题])|告诉|提示|提醒|建议|安抚|道歉|承认|"
    r"解释|強調|强调|表达|表達|引導|引导|口径|禮貌|礼貌|语气|語氣"
    r")"
)

# ---- 英文教练头锚（行首祈使；刻意不收 don't/do not——"Don't worry" 係合法台词）----
_EN_COACH_HEAD_RE = re.compile(
    r"^\s*(?:"
    r"say\b|tell\b|suggest\b|admit\b|apologise\b|apologize\b|explain\b|"
    r"reassure\b|acknowledge\b|briefly\b|listen\b|refer\b|guide\b|"
    r"stay\b|keep\b|go\s+to\s+(?:the\s+)?(?:next\s+step|step)"
    r")",
    re.IGNORECASE,
)

# ---- 高精度教练短语（任意位置；每条都係「念给客户听会穿帮」的指引语）----
_ZH_COACH_TOKEN_RE = re.compile(
    r"语气誠懇|语气诚恳|語氣真誠|语气真诚|語氣保持|语气保持|"
    r"唔好駁|唔好驳|不要爭辯|不要争辩|勿爭辯|勿争辩|不爭辯|不争辩|"
    r"答完帶|答完带|答完後帶|答完后带|"
    r"提示客戶|提示客户|提醒客戶|提醒客户|告訴客戶|告诉客户|"
    r"問清客戶|问清客户|向客戶|向客户|同客戶講|同客户讲|同客戶講|"
    r"如果客戶|如果客户|当客戶问|当客户问"
)

_EN_COACH_TOKEN_RE = re.compile(
    r"if\s+the\s+customer|when\s+the\s+customer|stay\s+sincere|"
    r"tone\s+sincere|don'?t\s+(?:shift|argue|blame)|"
    r"bring\s+the\s+conversation\s+back",
    re.IGNORECASE,
)


def strip_branch_action_prefix(resp: str) -> str:
    """应答首部【…】动作前缀消费（flow.py ``parse_branch_action`` 的镜像）。

    CP 保存校验只需「剥掉动作标记、检测余下文本」，唔需要动作值——识别到标记
    一律消费（与 flow.py 语义一致：识别到标记一律消费标记）。"""
    s = str(resp or "")
    m = re.match(r"^【\s*(?:收线|挂断|转人工|跳第\s*\d{1,3}\s*步|留本步)\s*】\s*", s)
    return s[m.end():] if m else s


def coach_hits(text: str) -> list[str]:
    """内部指令命中信号列表（诊断/CP 400 detail 用）；空列表=可念台词。"""
    t = str(text or "")
    if not t.strip():
        return []
    hits: list[str] = []
    if _COACH_BRACKET_RE.search(t):
        hits.append("bracket_marker")
    if _ZH_COACH_HEAD_RE.match(t):
        hits.append("zh_coach_head")
    if _EN_COACH_HEAD_RE.match(t):
        hits.append("en_coach_head")
    if _ZH_COACH_TOKEN_RE.search(t):
        hits.append("zh_coach_token")
    if _EN_COACH_TOKEN_RE.search(t):
        hits.append("en_coach_token")
    return hits


def is_internal_instruction(text: str) -> bool:
    """文本係「写给坐席/LLM 的内部指引」而非可念台词（bool 快捷）。"""
    return bool(coach_hits(text))
