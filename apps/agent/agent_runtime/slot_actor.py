"""D1 槽位化 actor（第一性原理重构，2026-10-01）——A 线回复 LLM 的槽位渲染层。

背景（计划档 docs/superpowers/plans/2026-10-01-first-principles-rework.md
§一/§三/§七）：现状 prompt 协议占比 93.9-97.7%（R15 全 prompt 一万余字符、
system 静态前缀 3433 字符）；9B prefill 实测 311 tok/s（冷 miss 5888 tok≈18.9s）。
本层把 actor 的任务从「整本剧本+全规则」重定义为「编排器给槽、只生成一两句话」：

- system = 角色卡（人设压缩 + facts_line + 语言块 + 口吻/长度规则 + 2 条压缩
  回应范例，目标 250-450c、硬帽 ``SLOT_SYSTEM_MAX_CHARS``=600c）；
- 每轮 user 消息 = 任务块（当前步/命中分支/事实槽/8 字锚，目标 100-250c）
  + 客户话（润色后文本，账本键 raw 照旧）。

设计契约（D1 规格照办，见 tests/test_slot_*）：

- 编排职责（推进/收线/verdict/纪律/总览/禁讲清单/记忆摘要）全撤出 prompt、
  归 FlowController；本模块只做纯渲染，绝不读 FlowController 状态——当步视图
  由 ``flow.FlowController.slot_step_view()`` 在编排点算好，经
  ``ContextState.set_slot_step()`` 传入。绝不包含：话术流程总览、
  ``flow._SHARED_RESPONSE_RULES``、步骤纪律、verdict 指引、状态标记
  （【通知已念】/【新一步】/【跳转进入】/【禁讲清单】）、记忆摘要。
- 任务块随当轮 user 消息注入、经 ContextState 既有 ``record_applied_tail``
  冻结入史（livekit_plugins.ContextAwareLLM.chat 原机制复用，零 out-of-band
  交付、零账本语义改动）；历史里的旧任务块已冻结照留——它们是会话记忆不是协议。
- 闸 ``BOK_SLOT_ACTOR`` 默认 "0"：A 线装配点（agent.py entrypoint）读一次置
  ``ContextState.slot_mode``；"0"=旧路径逐字节不变（本模块零调用）。B 线
  interpret.py 不接。
- 进 prompt 的共享指引一律标准书面中文；粤语风格块仅 cantonese 通话渲染
  （tests/test_agent_language_follow.test_zh_prompt_purity_no_cantonese_marks）。
"""
from __future__ import annotations

import os

# 角色卡硬帽（规格：目标 250-450c、硬帽 600c）。超帽确定性降级：先弃范例块，
# 仍超再尾部硬截「…」——同一输入恒同一字节（KV 前缀稳定、离线可测）。
SLOT_SYSTEM_MAX_CHARS = 600

# 口吻/长度规则（标准书面中文，三语通话无条件渲染）。与旧 【回复节奏】/
# 【回复长度】/【应答准则】压缩对应：只留「一两句短句 + 答所问 + 带回当前步」。
_SLOT_TONE_RULE = (
    "【口吻与长度】每次只回一两句短句（四十字内）：先直接回答客户问的事，"
    "再用一句带回当前要办的事。"
)

# 2 条压缩回应范例（~90c）：每条=答所问一句+带回一句（旧【回应范例】两段化）。
_SLOT_EXAMPLES = (
    "【回应范例】先答客户问的、再一句带回当前步：客户问「你们是哪里的」→"
    "「我们是帮你收发转运的集运仓库，今天想核对一件货件的理赔。」"
    "客户说「不记得了」→「没关系，我帮您一起核对订单。」"
)

# 语言块（三语）。措辞纪律同旧前缀：共享区标准书面中文；港式词表压缩版只在
# cantonese 通话渲染（zh 通话字节里不得出现粤语特征字）。
_SLOT_LANG_BLOCKS = {
    "zh": (
        "【语言】用自然口语的普通话回复，像打电话那样说，短句口语，"
        "不要用书面语或播音腔。"
    ),
    "cantonese": (
        "【语言】全程港式粵語（香港客服腔），直接輸出繁體字，唔用書面語/普通話。"
        "港式用詞：唔該晒、唔好意思、我哋、而家、啱啱、幫你睇返。"
        "集運業務：貨叫「你件貨／集運件」，服務講「速遞」，唔好講「包裹／快遞」。"
        "報號碼逐個讀（「尾號七八九零」），唔好用阿拉伯數字。"
    ),
    "en": (
        "【Language】Reply in natural spoken English only (like on a phone call); "
        "keep sentences short; do not mix in any Chinese words."
    ),
}


def slot_actor_enabled() -> bool:
    """槽位化 actor 总闸（默认 "0"）。A 线装配点读一次——读法与 BOK_TAIL_SLIM
    同款（字面量、缺省旧档）；置位后角色卡/任务块走上游渲染，旧协议族不再进 prompt。"""
    return os.environ.get("BOK_SLOT_ACTOR", "0") == "1"


def build_slot_system(
    *,
    persona: dict | None = None,
    template: dict | None = None,
    facts: str = "",
    language: str = "zh",
) -> str:
    """渲染槽位角色卡（纯函数）：人设 base 压缩 + facts_line + 语言块 + 口吻规则 + 范例。

    - ``facts`` = agent 侧 ``flow.facts_line(object_card)`` 的输出（已知客户信息行）；
    - 人设压缩保留 name/company/tone（tone_override 优先，与 ``_instructions`` 同序），
      丢弃旧 _instructions 的「角色基调/回复语言」两大段——语言由语言块承担、
      口吻由口吻规则承担。
    - 严格确定性：同一输入恒同一字节；超硬帽按上面注释确定性降级。
    """
    name = str((persona or {}).get("name") or "Bok Voice")
    company = str((persona or {}).get("company") or "")
    tone = str((template or {}).get("tone_override") or (persona or {}).get("tone") or "")
    head = f"你是{name}，代表{company}。" if company else f"你是{name}。"
    if tone:
        head += f"语气：{tone}。"
    lang_block = _SLOT_LANG_BLOCKS.get(str(language or "").strip().lower(), _SLOT_LANG_BLOCKS["zh"])
    facts_text = str(facts or "").strip()
    parts = [head, facts_text, lang_block, _SLOT_TONE_RULE, _SLOT_EXAMPLES]
    text = "\n".join(p for p in parts if p)
    if len(text) > SLOT_SYSTEM_MAX_CHARS:
        # 降级①：弃范例块；facts 按剩余预算确定性截断（语言/口吻块保底——
        # 它们是每轮都需要的回复契约，绝不被超长 facts 挤掉）。
        fixed = "\n".join(p for p in (head, lang_block, _SLOT_TONE_RULE) if p)
        budget = SLOT_SYSTEM_MAX_CHARS - len(fixed) - 3  # 3 个换行分隔
        if facts_text and budget >= 2:
            facts_trimmed = (
                facts_text if len(facts_text) <= budget else facts_text[: budget - 1] + "…"
            )
            parts = [head, facts_trimmed, lang_block, _SLOT_TONE_RULE]
        else:
            parts = [head, lang_block, _SLOT_TONE_RULE]
        text = "\n".join(p for p in parts if p)
        if len(text) > SLOT_SYSTEM_MAX_CHARS:
            # 降级②：极端人设/语言块仍超帽 → 确定性硬截（尾「…」标截断）。
            text = text[: SLOT_SYSTEM_MAX_CHARS - 1] + "…"
    return text


def build_slot_task_block(
    *,
    view: dict | None = None,
    object_brief: str = "",
    call_facts: list[str] | tuple[str, ...] = (),
    whatsapp_note: str = "",
    anchor: str = "",
) -> str:
    """渲染当轮任务块（纯函数，目标 100-250c）：当前步 + 事实槽 + 8 字锚。

    - ``view`` = ``FlowController.slot_step_view()`` 的结构化槽位（编排点算好，
      本函数零状态）；state ∈ steps/closing/done。
    - 事实槽三源照 D1 规格：对象档案行（``_object_brief``）/【通话中客户已讲】
      （``add_call_fact`` 账本）/【已记录客户 WhatsApp】（有才带）。
    - 绝不渲染：总览/共享规则/纪律/verdict 指引/状态标记/记忆摘要（规格铁律）。
    """
    v = dict(view or {})
    lines: list[str] = []
    state = str(v.get("state") or "")
    if state == "closing":
        lines.append(
            "【当前状态】客户已表示结束/拒绝——只讲一句礼貌收尾（感谢+再见），"
            "不推销不挽留、不提问不提流程。"
        )
    elif state == "done":
        lines.append(
            "【当前状态】话术流程已走完——继续回答客户问题、确认后续安排，"
            "不要主动讲再见。"
        )
    elif state == "steps" and v.get("step_no"):
        no = int(v.get("step_no") or 0)
        total = int(v.get("total") or 0)
        goal = str(v.get("goal") or "").strip()
        line = f"【当前步】第{no}/{total}步" if total else f"【当前步】第{no}步"
        if goal:
            line += f"：{goal}"
        lines.append(line)
        script = str(v.get("script") or "").strip()
        if script:
            lines.append("【本步底稿】" + script)
        branch = str(v.get("branch") or "").strip()
        if branch:
            lines.append("【本步应对】" + branch)
    brief = str(object_brief or "").strip()
    if brief:
        # 多行档案折成单行（行界=语义单元，用「；」保内容不丢）。
        lines.append("【客户资料】" + "；".join(brief.splitlines()))
    facts = [str(f).strip() for f in (call_facts or []) if str(f).strip()]
    if facts:
        lines.append("【通话中客户已讲】" + "；".join(facts))
    note = str(whatsapp_note or "").strip()
    if note:
        # WA 已捕获状态行（有才带）。只留号码本身——旧尾部那条「（复述…）」
        # 长注不进槽位：冻结入史随轮积累，每轮 8c×十余轮 ≈ 80c 是 R15 全 prompt
        # 的直接组分（复述指令由角色卡口吻规则与号码本来承担）。
        lines.append(f"【已记录客户 WhatsApp】{note}")
    if str(anchor or "").strip():
        lines.append(str(anchor).strip())
    return "\n".join(lines)


def compose_slot_user_message(task_block: str, body: str) -> str:
    """槽位模式 user 消息组装：任务块在前、客户话在后（D1 规格形状）。

    chat 冻结点（ContextAwareLLM.chat）与投机预热（PrefillSpeculator.
    on_stable_prefix）共用本函数——顺序单点，防两处组装分叉令预烧 KV 白费。
    """
    task = str(task_block or "")
    text = str(body or "")
    if task and text:
        return f"{task}\n\n{text}"
    return task or text
