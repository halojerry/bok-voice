"""话术图(flow graph)纯函数:解析/校验/命中裁决(spec docs/superpowers/specs/2026-09-18-qa-flow-graph.md)。

模板可选携带 graph_json:意图节点(确定性关键词触发)+绑定边(play_qa 播罐头 /
jump_step 跳步 / notify_human 打铃,W4-T2 无负载)。CP 保存走 validate_flow_graph
(严格,错误列表→400);运行时走 parse_flow_graph(宽容,坏数据→空图零变化)与
pick_graph_action(每轮至多一个动作,priority 小者先)。关键词命中=**双侧归一**
(剥空白与中英标点 → casefold)后子串判定——ASR 转写常带标点(实测「我要。投诉。」),
归一后多字关键词才命中(I2,2026-09-18)。
设计契约见 spec §3/§4;消费方:control_plane.main(保存校验)、
agent_runtime.flow(装配解析)、agent_runtime.agent(每轮命中)。
Phase 3.4 意图引擎:意图可携可选 `judge.prompt`(判据片段)——关键词未中时由背景
9B 批量判定补位,命中 id 经 `judge_hit` 与关键词命中同权入裁决(见
`eligible_judge_intents`/`pick_graph_action`)。

P2.2 兜底(catch-all,bolna 式,2026-09-21):保留意图 id `"*"` = 常规意图**全未
命中**后的最后出口(`pick_catchall_action`)。形状三约束:keywords 必须为空、无
judge、其绑定 `once` 必须 false——validate 严格拒(CP 400),parse 宽容丢(坏
`"*"` 行=丢该意图/丢该绑定,宁空毋炸)。无 `"*"` 意图=落 LLM 兜底,逐字节同旧。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

GRAPH_VERSION = 1
GRAPH_MAX_BYTES = 65536
MAX_INTENTS = 64
MAX_BINDINGS = 128
MAX_KEYWORDS = 32
KEYWORD_MAX_CHARS = 64
LABEL_MAX_CHARS = 64
# Phase 3.4 意图判据(judge prompt)长度窗:运营写的「什么算命中」片段,1..400 字。
JUDGE_PROMPT_MAX_CHARS = 400
PRIORITY_MIN = 0
PRIORITY_MAX = 1000
DEFAULT_PRIORITY = 10
STEP_MAX = 999
ACTION_PLAY_QA = "play_qa"
ACTION_JUMP_STEP = "jump_step"
# W4-T2(2026-09-19):notify_human 打铃——CP assist 置 notified(坐席台「人工求助」),
# 无负载(不要求 qa_id/step);agent 侧不抢话,LLM 照常兜话,坐席旁听后手动接管。
ACTION_NOTIFY_HUMAN = "notify_human"
ACTIONS = {ACTION_PLAY_QA, ACTION_JUMP_STEP, ACTION_NOTIFY_HUMAN}
# P2.2(2026-09-21):保留意图 id `"*"`=兜底(catch-all,bolna 式 router 的
# unconditional 边)。**两轨合法形状都不匹配**(`_INTENT_ID_RE`/`_ID_RE`),
# 单独走支路;运营面语义=「以上都没接住的话,照这条做」,无该意图=落 LLM。
CATCHALL_INTENT_ID = "*"
_ID_RE = re.compile(r"^(?:int|bnd)_[0-9a-f]{8}$")
# 意图 id 硬规则(P2.2 放宽):原 spec §3 是 `int_<8 hex>`(web 画布机器生成)。
# P2.1 挖掘候选 id 是 snake_case 语义名(`whatsapp_contact`/`session_affirm`,
# scripts/probe_intent_mine.py `_ID_RE` 逐字节同款)——**不入硬规则则挖掘产物永远
# 保存不了**,故意图 id 放宽到 snake_case(旧 `int_<8 hex>` 是其子集 ⇒ 存量数据
# 零影响;形状坏仍 400)。绑定 id 保持 `bnd_<8 hex>`(机器生成,无人手写)。
_INTENT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# 命名**软校验**(只出警告绝不 400,见 graph_warnings):建议 `<主体>_<关系>` 形状
# (同词反向歧义防护,plan §46.3 医疗 KG 结论);画布机器占位 id `int_<8 hex>`
# 亦提示改名(可读性,不阻断)。
_ID_SHAPE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
_ID_MACHINE_RE = re.compile(r"^int_[0-9a-f]{8}$")

# 关键词匹配噪声(I2,2026-09-18):ASR 转写窗口带标点/空格(实测「我要。投诉。」
# 「我 要 投 诉」),运营写的关键词恒为连写形态 → 双侧剥噪声再 casefold 子串。
# 字符集经 re.escape 包成字符类,方括号/反斜杠/引号都唔会漏转义。
_PUNCT_NOISE_CHARS = (
    "，。！？、；：～…·—"
    "\u201c\u201d\u2018\u2019"
    "「」『』（）〈〉《》【】〔〕"
    ",.!?;:'\"()[]{}<>"
    "/\\|+*=^`#@$%&_-"
)
_PUNCT_NOISE_RE = re.compile(f"[{re.escape(_PUNCT_NOISE_CHARS)}\\s]+")


def normalize_graph_text(value: object) -> str:
    """关键词/用户话命中归一(纯函数):剥空白与中英标点 → casefold;空值安全。"""
    return _PUNCT_NOISE_RE.sub("", str(value or "")).casefold()


@dataclass
class FlowIntent:
    id: str = ""
    label: str = ""
    keywords: list[str] = field(default_factory=list)
    steps: list[int] = field(default_factory=list)  # 1-based;空=全程生效
    enabled: bool = True
    # Phase 3.4 意图引擎:运营写的判据片段(JSON `judge.prompt`)。空串=无判据=纯
    # 关键词确定性命中(Phase 2/3.3 行为,缺省零变化);非空才参与背景批量判定。
    judge_prompt: str = ""


@dataclass
class GraphBinding:
    id: str = ""
    intent: str = ""
    action: str = ""
    qa_id: str = ""  # action=play_qa
    step: int = 0  # action=jump_step,1-based
    # Phase 3.3 追问链:play_qa 播完当场跳到的步(1-based);None=无链(Phase 2 行为)。
    # 与 step 的钳制不同——可选字段,坏值丢字段不清退绑定(见 _then_jump_of 注释)。
    then_jump: int | None = None
    priority: int = DEFAULT_PRIORITY  # 小者先
    once: bool = False
    enabled: bool = True


@dataclass
class FlowGraphDoc:
    version: int = GRAPH_VERSION
    intents: list[FlowIntent] = field(default_factory=list)
    bindings: list[GraphBinding] = field(default_factory=list)

    def intent_by_id(self, intent_id: str) -> FlowIntent | None:
        return next((i for i in self.intents if i.id == intent_id), None)


def _as_bool(value: object, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _judge_prompt_of(raw: dict) -> str:
    """意图判据(Phase 3.4)宽容取:`judge` 在场且 `prompt` 是非空 str 才收,否则丢字段。

    与 `_then_jump_of` 同档——可选增强字段形状坏只丢字段,绝不清退整个意图(运营
    的意图+关键词仍然可用=Phase 2 行为)。**刻意不截断**:运营写了 401 字是 CP
    校验(严格)该拒的事,运行时静默砍掉尾部会静默改判据语义。
    """
    judge = raw.get("judge")
    if not isinstance(judge, dict):
        return ""
    prompt = judge.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        return ""
    return prompt


def _parse_catchall_intent(raw: dict) -> FlowIntent | None:
    """兜底意图(保留 id `"*"`)宽容解析:keywords 必须为空 + 无判据——违背即整项丢弃。

    「宁空毋炸」:坏兜底行(带关键词/带判据/字段形状坏)只丢这一个意图,常规意图
    照跑、图退「无兜底」=落 LLM,绝不因一行坏数据炸通话(严格档 validate 400)。
    `steps` 与常规意图同语义(空=全程):窄化的兜底只在指定步生效,缺省=全程。
    """
    raw_keywords = raw.get("keywords")
    if raw_keywords is not None and not isinstance(raw_keywords, list):
        return None
    if any(str(k or "").strip() for k in (raw_keywords or [])):
        return None  # 有实质词 = 不是兜底(运营配错),整项丢弃(判据同 validate)
    if _judge_prompt_of(raw):
        return None  # 兜底无判据(判据只补常规意图的模糊轮)
    raw_steps = raw.get("steps")
    if raw_steps is not None and not isinstance(raw_steps, list):
        return None
    steps = sorted({_as_int(s, -1) for s in raw_steps or [] if _as_int(s, -1) >= 1})
    return FlowIntent(
        id=CATCHALL_INTENT_ID,
        label=str(raw.get("label") or "")[:LABEL_MAX_CHARS],
        keywords=[],
        steps=steps,
        enabled=_as_bool(raw.get("enabled")),
        judge_prompt="",
    )


def _parse_intent(raw: object) -> FlowIntent | None:
    """单项宽容:缺 id/坏 id/非 dict/字段形状坏 → None(调用方跳过)。

    保留 id `"*"`(兜底意图,P2.2)走 `_parse_catchall_intent`——形状坏同样整项
    丢弃,常规意图零影响。
    """
    if not isinstance(raw, dict):
        return None
    intent_id = str(raw.get("id") or "")
    if intent_id == CATCHALL_INTENT_ID:
        return _parse_catchall_intent(raw)
    if not _INTENT_ID_RE.match(intent_id):
        return None
    raw_keywords = raw.get("keywords")
    raw_steps = raw.get("steps")
    # 形状坏(存在的非 list:数字/布尔/对象等 truthy 非可迭代)→ 整项跳过,
    # 与其它坏项同档;缺失/None 视为空值照旧(绝不抛 TypeError)
    if raw_keywords is not None and not isinstance(raw_keywords, list):
        return None
    if raw_steps is not None and not isinstance(raw_steps, list):
        return None
    keywords = [str(k) for k in raw_keywords or [] if str(k or "").strip()]
    steps = sorted({_as_int(s, -1) for s in raw_steps or [] if _as_int(s, -1) >= 1})
    return FlowIntent(
        id=intent_id,
        label=str(raw.get("label") or "")[:LABEL_MAX_CHARS],
        keywords=keywords,
        steps=steps,
        enabled=_as_bool(raw.get("enabled")),
        judge_prompt=_judge_prompt_of(raw),
    )


def _then_jump_of(raw: dict, action: str) -> int | None:
    """追问链目标(1-based):仅 play_qa 收;非 int(bool 是 int 子类要单列)/越界 → None。

    形态坏=丢字段(绑定本体退 Phase 2 行为)——最保守且绝无静默改目标;`step` 走钳制是
    因为它是 jump_step 的必填字段(钳制保可用性),两者不对称是刻意的。
    """
    if action != ACTION_PLAY_QA or "then_jump" not in raw:
        return None
    value = raw.get("then_jump")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= STEP_MAX else None


def _parse_binding(raw: object) -> GraphBinding | None:
    if not isinstance(raw, dict):
        return None
    binding_id = str(raw.get("id") or "")
    action = str(raw.get("action") or "")
    if not _ID_RE.match(binding_id) or action not in ACTIONS:
        return None
    intent = str(raw.get("intent") or "")
    if not intent:
        return None  # 悬空引用运行时不可执行,解析期直接丢
    once = _as_bool(raw.get("once"), default=False)
    if intent == CATCHALL_INTENT_ID and once:
        # P2.2:兜底绑定 once 必须 false(validate 严格 400 同源)——坏行直接丢,
        # 意图本身留着(=显式声明落 LLM),绝不因一条坏绑定炸通话。
        return None
    return GraphBinding(
        id=binding_id,
        intent=intent,
        action=action,
        qa_id=str(raw.get("qa_id") or ""),
        step=max(1, min(_as_int(raw.get("step"), 0), STEP_MAX)),
        then_jump=_then_jump_of(raw, action),
        priority=max(PRIORITY_MIN, min(_as_int(raw.get("priority"), DEFAULT_PRIORITY), PRIORITY_MAX)),
        once=once,
        enabled=_as_bool(raw.get("enabled")),
    )


def parse_flow_graph(raw: str | bytes | None) -> FlowGraphDoc:
    """宽容解析:永不抛错。整体坏(空/非 dict/版本不符)→空图;单项坏→逐项跳过。"""
    if isinstance(raw, bytes):  # 与 validate_flow_graph 同款解码,bytes 形参不虚设
        raw = raw.decode("utf-8", errors="replace")
    text = str(raw or "").strip()
    if not text:
        return FlowGraphDoc()
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):  # RecursionError:深嵌套体唔准逃逸 never-raise
        return FlowGraphDoc()
    if not isinstance(data, dict) or _as_int(data.get("version"), 0) != GRAPH_VERSION:
        return FlowGraphDoc()
    doc = FlowGraphDoc()
    raw_intents = data.get("intents")
    if isinstance(raw_intents, list):
        for item in raw_intents[:MAX_INTENTS]:
            intent = _parse_intent(item)
            if intent is not None:
                doc.intents.append(intent)
    raw_bindings = data.get("bindings")
    if isinstance(raw_bindings, list):
        intent_ids = {i.id for i in doc.intents}
        for item in raw_bindings[:MAX_BINDINGS]:
            binding = _parse_binding(item)
            if binding is not None and binding.intent in intent_ids:
                doc.bindings.append(binding)
    return doc


def validate_flow_graph(raw: str | bytes) -> list[str]:
    """严格校验(CP 保存用):返回错误列表,空=合法。空串=未启用,合法。

    P2.2 起额外两条硬门:①保留意图 `"*"` 的形状(keywords 必须为空/无 judge/
    其绑定 once=false/至多一次);②**孤儿意图门**——intents 非空时每个常规意图
    至少要有一条 enabled 绑定(空图豁免)。软性命名建议走 `graph_warnings`。
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = str(raw or "")
    if not text.strip():
        return []
    errors: list[str] = []
    if len(text.encode("utf-8")) > GRAPH_MAX_BYTES:
        errors.append(f"graph_json exceeds {GRAPH_MAX_BYTES} bytes")
        return errors  # 超限体唔再 json.loads:巨体/深嵌套解析白费,错误已定
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:  # 深嵌套体同走错误列表契约
        errors.append(f"invalid json: {exc}")
        return errors
    if not isinstance(data, dict):
        return ["graph_json must be a json object"]
    # version 必须**恰为** int 1:bool 是 int 子类,`True != 1` 为假会静默放行。
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != GRAPH_VERSION:
        errors.append(f"version must be {GRAPH_VERSION}")
    raw_intents = data.get("intents")
    raw_bindings = data.get("bindings")
    if not isinstance(raw_intents, list):
        errors.append("intents must be a list")
        raw_intents = []
    if not isinstance(raw_bindings, list):
        errors.append("bindings must be a list")
        raw_bindings = []
    if len(raw_intents) > MAX_INTENTS:
        errors.append(f"intents exceed {MAX_INTENTS}")
    if len(raw_bindings) > MAX_BINDINGS:
        errors.append(f"bindings exceed {MAX_BINDINGS}")
    seen_intents: set[str] = set()
    for idx, item in enumerate(raw_intents):
        if not isinstance(item, dict):
            errors.append(f"intents[{idx}] must be an object")
            continue
        intent_id = str(item.get("id") or "")
        # P2.2 兜底意图(保留 id "*")不属 `int_<8 hex>` 形状,单列支路;其余照旧。
        is_catchall = intent_id == CATCHALL_INTENT_ID
        if not is_catchall and not _INTENT_ID_RE.match(intent_id):
            errors.append(f"intents[{idx}].id malformed: {intent_id!r}")
        elif intent_id in seen_intents:
            errors.append(f"intents[{idx}].id duplicated: {intent_id}")
        seen_intents.add(intent_id)
        label = str(item.get("label") or "").strip()
        if not label or len(label) > LABEL_MAX_CHARS:
            errors.append(f"intents[{idx}].label must be 1-{LABEL_MAX_CHARS} chars")
        keywords = item.get("keywords")
        if is_catchall:
            # 兜底意图 keywords 必须为空(有词=不是兜底)。判据与宽容 parse **同一
            # 条**("剥空白后有无实质词"):缺省/空列表/纯空白项都合法,出现实质词
            # 或非 list 形状才 400——两轨对同一份数据结论一致,不出现「保存过但
            # 运行时被判坏行丢掉」的落差。
            has_words = (
                any(str(k or "").strip() for k in keywords)
                if isinstance(keywords, list)
                else keywords is not None
            )
            if has_words:
                errors.append(
                    f'intents[{idx}].keywords must be empty for intent "{CATCHALL_INTENT_ID}"'
                )
        elif not isinstance(keywords, list) or not keywords:
            errors.append(f"intents[{idx}].keywords must be a non-empty list")
        else:
            if len(keywords) > MAX_KEYWORDS:
                errors.append(f"intents[{idx}].keywords exceed {MAX_KEYWORDS}")
            for kidx, kw in enumerate(keywords):
                if not str(kw or "").strip() or len(str(kw)) > KEYWORD_MAX_CHARS:
                    errors.append(f"intents[{idx}].keywords[{kidx}] must be 1-{KEYWORD_MAX_CHARS} chars")
        steps = item.get("steps", [])
        if not isinstance(steps, list) or any(
            not isinstance(s, int) or isinstance(s, bool) or s < 1 or s > STEP_MAX for s in steps
        ):
            errors.append(f"intents[{idx}].steps must be ints in [1,{STEP_MAX}]")
        # enabled 与绑定边同档(bool 才收;非 bool 静默当默认值=运营勾选失效)。
        if "enabled" in item and not isinstance(item["enabled"], bool):
            errors.append(f"intents[{idx}].enabled must be bool")
        # Phase 3.4 判据:严格形状(CP 保存期拒)——运行时有宽容 parse 兜底,但运营面
        # 唔准静默存坏数据(判据坏=意图退纯关键词,运营以为写了判据却没生效)。
        # P2.2:兜底意图**不准有 judge**(判据只补常规意图的模糊轮,兜底无判据可言)。
        if "judge" in item:
            judge = item["judge"]
            if is_catchall:
                errors.append(
                    f'intents[{idx}].judge is not allowed for intent "{CATCHALL_INTENT_ID}"'
                )
            elif (
                not isinstance(judge, dict)
                or "prompt" not in judge
                or not isinstance(judge["prompt"], str)
                or not (1 <= len(judge["prompt"]) <= JUDGE_PROMPT_MAX_CHARS)
            ):
                errors.append(
                    f"intents[{idx}].judge.prompt must be a 1-{JUDGE_PROMPT_MAX_CHARS} char string"
                )
    seen_bindings: set[str] = set()
    bound_intents: set[str] = set()  # 有 enabled 绑定的意图 id(孤儿门用)
    for idx, item in enumerate(raw_bindings):
        if not isinstance(item, dict):
            errors.append(f"bindings[{idx}] must be an object")
            continue
        binding_id = str(item.get("id") or "")
        if not _ID_RE.match(binding_id):
            errors.append(f"bindings[{idx}].id malformed: {binding_id!r}")
        elif binding_id in seen_bindings:
            errors.append(f"bindings[{idx}].id duplicated: {binding_id}")
        seen_bindings.add(binding_id)
        intent = str(item.get("intent") or "")
        if intent not in seen_intents:
            errors.append(f"bindings[{idx}].intent references missing intent: {intent!r}")
        elif item.get("enabled", True) is not False:
            bound_intents.add(intent)
        action = str(item.get("action") or "")
        if action not in ACTIONS:
            errors.append(f"bindings[{idx}].action must be one of {sorted(ACTIONS)}: {action!r}")
        if action == ACTION_PLAY_QA and not str(item.get("qa_id") or "").strip():
            errors.append(f"bindings[{idx}] action=play_qa requires qa_id")
        # P2.2:兜底绑定 once 必须 false(每通至多一次的兜底=只在第一轮兜一次,
        # 语义上不是兜底;运营要「只兜一次」应写关键词意图)。
        if intent == CATCHALL_INTENT_ID and item.get("once") is True:
            errors.append(
                f'bindings[{idx}].once must be false for intent "{CATCHALL_INTENT_ID}"'
            )
        # Phase 3.3 追问链:then_jump 仅 play_qa 合法;[1,999] 闭区间;非 int(含 bool)拒。
        if action in (ACTION_PLAY_QA, ACTION_JUMP_STEP) and "then_jump" in item:
            if action != ACTION_PLAY_QA:
                errors.append(
                    f"bindings[{idx}].then_jump is only valid for action=play_qa: {action!r}"
                )
            else:
                _tj = item["then_jump"]
                if isinstance(_tj, bool) or not isinstance(_tj, int) or not (1 <= _tj <= STEP_MAX):
                    errors.append(f"bindings[{idx}].then_jump must be int in [1,{STEP_MAX}]")
        if action == ACTION_JUMP_STEP:
            step = item.get("step")
            if not isinstance(step, int) or isinstance(step, bool) or step < 1 or step > STEP_MAX:
                errors.append(f"bindings[{idx}] action=jump_step requires step in [1,{STEP_MAX}]")
        priority = item.get("priority", DEFAULT_PRIORITY)
        if not isinstance(priority, int) or isinstance(priority, bool) or not (PRIORITY_MIN <= priority <= PRIORITY_MAX):
            errors.append(f"bindings[{idx}].priority must be int in [{PRIORITY_MIN},{PRIORITY_MAX}]")
        for flag in ("once", "enabled"):
            if flag in item and not isinstance(item[flag], bool):
                errors.append(f"bindings[{idx}].{flag} must be bool")
    # P2.2 孤儿意图门(bolna 式「图必须走得通」的本地化):intents 非空时,每个常规
    # 意图(非 "*")至少要有一条 enabled 绑定——「配了意图没绑动作」=该意图永不可能
    # 触发,图是死的,保存期就拒(消灭静默无效配置)。**空图(无 intents)完全豁免**:
    # 存量空图模板的保存不得被 breaking;"*" 意图本身允许无绑定(=显式声明落 LLM)。
    if raw_intents:
        for idx, item in enumerate(raw_intents):
            if not isinstance(item, dict):
                continue
            intent_id = str(item.get("id") or "")
            if intent_id == CATCHALL_INTENT_ID or not _INTENT_ID_RE.match(intent_id):
                continue  # 兜底豁免;id 形状坏已单报,不再叠孤儿错
            if intent_id not in bound_intents:
                errors.append(f"intents[{idx}] has no enabled binding: {intent_id}")
    return errors


def graph_warnings(graph: "str | bytes | FlowGraphDoc | None") -> list[str]:
    """意图 id 命名规范**软校验**(P2.2):只出人话提示,**绝不阻断保存**。

    与 `validate_flow_graph` 严格档**互不影响**(返回面分开=不破坏既有调用方):
    错误走 validate(400),本函数只喂提示面(P2.5 表单/保存响应)。三条规则:
    ①形状必须 `^[a-z][a-z0-9_]*$`——**这不是硬门**(validate 硬门是同一条正则,
    不符早已 400);本函数对**手改/存量/文档态**数据仍照报,便于审计旧的
    `int_<8 hex>` 之外的历史数据;
    ②建议 `<主体>_<关系>` 形状(下划线分段,防同词反向歧义,plan §46.3);
    ③画布机器占位 id `int_<8 hex>` 亦提示改名(可读性,非硬约束)。
    输入坏 JSON/空串 → 无警告(取舍:坏图由 validate 报);`"*"` 兜底意图豁免。
    喂原 JSON 串时按**原文档下标**取 id(宽容 parse 会丢坏行,坏 id 反而要提示)。
    """
    if isinstance(graph, FlowGraphDoc):
        items: list[tuple[int, str]] = [(i, it.id) for i, it in enumerate(graph.intents)]
    else:
        if isinstance(graph, bytes):
            graph = graph.decode("utf-8", errors="replace")
        text = str(graph or "").strip()
        items = []
        if text:
            try:
                data = json.loads(text)
            except (ValueError, RecursionError):
                data = None
            raw_intents = data.get("intents") if isinstance(data, dict) else None
            if isinstance(raw_intents, list):
                items = [
                    (idx, str(item.get("id") or ""))
                    for idx, item in enumerate(raw_intents[:MAX_INTENTS])
                    if isinstance(item, dict)
                ]
    out: list[str] = []
    for idx, intent_id in items:
        if intent_id == CATCHALL_INTENT_ID:
            continue
        if not _INTENT_ID_RE.match(intent_id):
            out.append(
                f"intents[{idx}].id `{intent_id}` 建议用小写字母、数字、下划线命名"
                f"（字母开头，形如 <主体>_<关系>）"
            )
        elif not _ID_SHAPE_RE.match(intent_id) or _ID_MACHINE_RE.match(intent_id):
            out.append(
                f"intents[{idx}].id `{intent_id}` 建议改成 <主体>_<关系> 形状"
                f"（如 refund_request），同一个词的正反关系才分得清"
            )
    return out


def pick_graph_action(
    doc: FlowGraphDoc,
    user_text: str,
    *,
    step_1based: int,
    fired: set[str],
    judge_hit: str | None = None,
) -> GraphBinding | None:
    """图引擎每轮唯一裁决点:确定性命中 + (Phase 3.4)背景判据命中。

    确定性命中:enabled 意图 + 关键词归一化子串(双侧剥标点/空格+casefold)
    + 步号 scope;绑定按 (priority, id) 升序取首个,once 且已 fired 的跳过。

    `judge_hit`(Phase 3.4):背景判据判定给出的意图 id,**与关键词命中同权**入
    `hit_ids`——只补模糊轮(关键词未中),绝不豁免任何守卫:该意图照过 enabled +
    步 scope 检查(逐个查 `doc.intents`,id 不在图里=no-op),绑定照过 enabled/once
    资格与 (priority,id) 排序。`user_text` 为空时整体不裁决(判据语义依附于话语)。
    无命中返回 None。
    """
    if not doc.intents or not user_text:
        return None
    text = normalize_graph_text(user_text)
    hit_ids: set[str] = set()
    for intent in doc.intents:
        if not intent.enabled:
            continue
        if intent.id == CATCHALL_INTENT_ID:
            continue  # P2.2 兜底不参与关键词路(无词);它只在常规全未命中后出场
        if intent.steps and step_1based not in intent.steps:
            continue
        for kw in intent.keywords:
            token = normalize_graph_text(kw)
            if token and token in text:
                hit_ids.add(intent.id)
                break
    if judge_hit:
        # 与关键词路**同两道守卫**(enabled/步 scope);绑定资格与排序在下游统一收口
        # ——判据结果只是「多了一个命中意图」,唔係特权通道。
        jintent = doc.intent_by_id(judge_hit)
        if (
            jintent is not None
            and jintent.enabled
            and (not jintent.steps or step_1based in jintent.steps)
        ):
            hit_ids.add(jintent.id)
    if not hit_ids:
        return None
    candidates = [
        b
        for b in doc.bindings
        if b.enabled and b.intent in hit_ids and not (b.once and b.id in fired)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda b: (b.priority, b.id))
    return candidates[0]


def pick_catchall_action(
    doc: FlowGraphDoc,
    *,
    step_1based: int,
    fired: set[str],
) -> GraphBinding | None:
    """兜底意图(P2.2,bolna 式 catch-all)裁决:常规意图**全未命中之后**才调用。

    命中条件(全部满足):图里有 `"*"` 意图 + 该意图 enabled + 步 scope 含当前步
    (空=全程)+ 至少一条 enabled 且未 fired(once)的绑定;多绑定按 (priority,id)
    升序取首个——与 `pick_graph_action` 逐字同款排序,零新语义。
    无 `"*"` 意图 / 无启用绑定 → None(调用方照旧落 LLM,逐字节同旧)。

    **与 judge 的关系**:判据只补常规意图的模糊轮(在 `pick_graph_action` 内),
    兜底是它之后的最后出口——调用方顺序恒为 pick_graph_action → pick_catchall_action。
    """
    if not doc.intents:
        return None
    intent = doc.intent_by_id(CATCHALL_INTENT_ID)
    if intent is None or not intent.enabled:
        return None
    if intent.steps and step_1based not in intent.steps:
        return None
    candidates = [
        b
        for b in doc.bindings
        if b.enabled and b.intent == CATCHALL_INTENT_ID and not (b.once and b.id in fired)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda b: (b.priority, b.id))
    return candidates[0]


def eligible_judge_intents(
    doc: FlowGraphDoc,
    *,
    step_1based: int,
    fired: set[str],
) -> list[FlowIntent]:
    """背景判据判定的候选意图(纯函数,Phase 3.4):**单点资格预筛**。

    四条全过才算候选:①`enabled`;②`judge_prompt` 非空(运营写了判据);③步 scope
    (空=全程);④至少一条可触发绑定(enabled + intent 对得上 + once 未 fired)。
    空列表=冇嘢可判=调用方零调度——**无 judge 数据故恒空**=全默认档行为逐字节
    唔变的结构性保证(判定任务零创建、9B 专线零调用)。P2.2 兜底意图永不进候选
    (无判据正是它的形状约束;判据只补常规意图的模糊轮)。
    """
    if not doc.intents:
        return []
    out: list[FlowIntent] = []
    for intent in doc.intents:
        if intent.id == CATCHALL_INTENT_ID:
            continue
        if not intent.enabled or not intent.judge_prompt:
            continue
        if intent.steps and step_1based not in intent.steps:
            continue
        if any(
            b.enabled and b.intent == intent.id and not (b.once and b.id in fired)
            for b in doc.bindings
        ):
            out.append(intent)
    return out
