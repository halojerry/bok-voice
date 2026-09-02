"""对话流程控制:话术模板分步推进 + 对象变量渲染。

职责划分(agent 编排层):
- 通话建立:把对象变量(姓名/单号/物流公司)+ 模板步骤渲染成"流程蓝图"。
- 每轮用户说完:规则判定客户状态与是否推进,维护 current_step。
- 把"当前这一步"的 goal/ref 注入本轮 system,让 LLM 只回应当前步。
绝不把整份话术当逐字稿塞给 LLM/TTS。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# 客户状态判定结果
CONFIRM = "confirm"       # 确认/认可当前步 → 可推进下一步
OBJECTION = "objection"   # 有异议/否认/不配合 → 停留本步应对
QUESTION = "question"     # 提问/要解释 → 停留本步解答
OFFTOPIC = "offtopic"     # 明显无关/要挂断/怀疑诈骗 → 不强推
UNCLEAR = "unclear"       # 判断不清 → 停留,自然应对


@dataclass
class FlowStep:
    goal: str = ""
    ref: str = ""


def parse_steps(steps_json: str) -> list[FlowStep]:
    """解析模板 steps_json 为步骤列表(兼容 dict 或空)。"""
    if not steps_json:
        return []
    try:
        arr = json.loads(steps_json)
    except Exception:
        return []
    if not isinstance(arr, list):
        return []
    out: list[FlowStep] = []
    for s in arr:
        if isinstance(s, dict):
            out.append(FlowStep(goal=str(s.get("goal") or ""), ref=str(s.get("ref") or "")))
        elif isinstance(s, str):
            out.append(FlowStep(goal="", ref=s))
    return [s for s in out if s.goal.strip() or s.ref.strip()]


def object_vars(object_card: dict | None) -> dict[str, str]:
    """从对象卡提取话术变量;缺的留空(由上层标"待确认",不编造)。"""
    oc = object_card or {}
    name = str(oc.get("display_name") or "").strip()
    tracking = str(oc.get("tracking_no") or "").strip()
    courier = str(oc.get("courier") or "").strip()
    tail = tracking[-4:] if len(tracking) >= 4 else tracking
    return {
        "姓名": name,
        "名字": name,
        "快递单号": tracking,
        "快递尾号": tail,
        "物流公司": courier,
        "快递公司": courier,
        "电话": str(oc.get("phone") or "").strip(),
    }


def render_template_text(text: str, vars_map: dict[str, str]) -> str:
    """替换 {变量} 占位;缺的变量保留原占位(LLM 会向客户询问而非编造)。"""
    if not text:
        return text
    def _repl(m: "re.Match[str]") -> str:
        key = m.group(1).strip()
        val = vars_map.get(key, "")
        return val if val else m.group(0)
    return re.sub(r"\{([^{}]+)\}", _repl, text)


def facts_line(object_card: dict | None) -> str:
    """生成已知事实段:姓名/单号/物流公司,缺的标'待确认'。"""
    v = object_vars(object_card)
    parts = []
    parts.append(f"姓名:{v['姓名'] or '待确认'}")
    parts.append(f"快递单号:{v['快递单号'] or '待确认'}")
    parts.append(f"物流公司:{v['物流公司'] or '待确认'}")
    return "已知客户信息:" + " ".join(parts) + "。信息缺失时向客户确认,不要编造。"


# ---- 每轮推进判定(规则为主,轻量;避免每轮额外一次 LLM 调用) ----

# 客户"确认/认可当前步"的信号(按语言)
_CONFIRM_RE = re.compile(
    r"(是我的|是我|是的|对的|对|没错|嗯嗯|嗯好|好啊|可以|好的|没问题|系啊|係啊|系我|係我|系既|係既|"
    r"是我的快递|对呀|对阿|是的呀|嗯|系|係|yes|yeah|correct|right)",
    re.IGNORECASE,
)
# 否认/不是本人/没买过 → 异议
_DENY_RE = re.compile(
    r"(不是|没有|不是我|我没|唔系|唔係|唔关我事|不关我事|没买过|冇买过|没有买过|骗子|诈骗|报警|"
    r"no|not me|wrong|never)",
    re.IGNORECASE,
)
# 提问/要解释 → question(在 confirm 之后判,避免"是吗"被当确认)
_QUESTION_RE = re.compile(
    r"(\?|？|怎么|如何|为啥|为什么|几时|几耐|多久|边度|哪里|点解|为什么赔|怎么赔|要多久|真假|"
    r"what|how|why|when|where|really)",
    re.IGNORECASE,
)
# 强异议/不想继续/威胁 → objection/offtopic
_HANGUP_RE = re.compile(r"(不用了|不需要|别再打|别打|不要打|挂|拉黑|投诉|再见|拜拜|唔使|唔使啦|"
    r"stop|don't call|leave me|bye)", re.IGNORECASE)


def decide_advance(user_text: str) -> str:
    """判定客户对当前这一步的反应,决定停留/推进。"""
    t = user_text.strip()
    if not t:
        return UNCLEAR
    # 1) 挂断/强拒绝 → objection(停留,让 LLM 简短得体收尾/不强推)
    if _HANGUP_RE.search(t):
        return OBJECTION
    # 2) 明确否认/不是本人 → objection(优先于确认词,避免"不是,是我…"误判)
    if _DENY_RE.search(t):
        return OBJECTION
    # 3) 确认/认可 → confirm(先于提问:客户"是我的,然后呢?"主体是确认)
    if _CONFIRM_RE.search(t):
        return CONFIRM
    # 4) 提问 → question(停留本步解答)
    if _QUESTION_RE.search(t):
        return QUESTION
    return UNCLEAR


@dataclass
class FlowController:
    """一通通话内的流程状态:载入步骤、按客户话推进。"""

    steps: list[FlowStep] = field(default_factory=list)
    current: int = 0  # 0-based;== len(steps) 表示流程已走完

    @classmethod
    def from_template(cls, template: dict | None, object_card: dict | None) -> "FlowController":
        steps = parse_steps((template or {}).get("steps_json", ""))
        fc = cls(steps=steps)
        fc.vars_map = object_vars(object_card)
        return fc

    def __post_init__(self) -> None:
        self.vars_map: dict[str, str] = {}

    @property
    def has_steps(self) -> bool:
        return len(self.steps) > 0

    @property
    def done(self) -> bool:
        return self.has_steps and self.current >= len(self.steps)

    def on_user_turn(self, user_text: str) -> None:
        """每轮用户话后调用:结合当前步决定是否推进。"""
        if not self.has_steps or self.done:
            return
        verdict = decide_advance(user_text)
        # 只有"确认/认可当前步"才推进;其它(异议/提问/不清)停留本步应对。
        if verdict == CONFIRM:
            # 最后一步确认后即完成(不推进到越界)
            if self.current < len(self.steps):
                self.current += 1

    def current_step_text(self) -> str:
        """渲染当前步(含变量替换)给本轮 system;流程完成则空。"""
        if not self.has_steps or self.done:
            return ""
        step = self.steps[self.current]
        lines = [f"流程第 {self.current + 1}/{len(self.steps)} 步"]
        if step.goal:
            lines.append(f"这一步要达成:{render_template_text(step.goal, self.vars_map)}")
        if step.ref:
            lines.append(f"参考要点:{render_template_text(step.ref, self.vars_map)}")
        lines.append(
            "只围绕当前这一步回应用户;说清楚后停下等用户回应,不要替用户回答,"
            "不要跳到下一步,不要一口气讲完整个流程。"
        )
        return "\n".join(lines)

    def flow_overview(self) -> str:
        """流程总览(注入基础 system,让 LLM 知道全貌但不照读)。"""
        if not self.has_steps:
            return ""
        lines = [f"对话按 {len(self.steps)} 步流程推进,每步等用户确认后再进下一步:"]
        for i, s in enumerate(self.steps, 1):
            g = render_template_text(s.goal, self.vars_map) if s.goal else s.ref
            lines.append(f"第{i}步:{g}")
        return "\n".join(lines)
