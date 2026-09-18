"""话术图(flow graph)纯函数:解析/校验/命中裁决(spec docs/superpowers/specs/2026-09-18-qa-flow-graph.md)。

模板可选携带 graph_json:意图节点(确定性关键词触发)+绑定边(play_qa 播罐头 /
jump_step 跳步)。CP 保存走 validate_flow_graph(严格,错误列表→400);运行时走
parse_flow_graph(宽容,坏数据→空图零变化)与 pick_graph_action(每轮至多一个动作,
priority 小者先)。设计契约见 spec §3/§4;消费方:control_plane.main(保存校验)、
agent_runtime.flow(装配解析)、agent_runtime.agent(每轮命中)。
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
PRIORITY_MIN = 0
PRIORITY_MAX = 1000
DEFAULT_PRIORITY = 10
STEP_MAX = 999
ACTION_PLAY_QA = "play_qa"
ACTION_JUMP_STEP = "jump_step"
ACTIONS = {ACTION_PLAY_QA, ACTION_JUMP_STEP}
_ID_RE = re.compile(r"^(?:int|bnd)_[0-9a-f]{8}$")


@dataclass
class FlowIntent:
    id: str = ""
    label: str = ""
    keywords: list[str] = field(default_factory=list)
    steps: list[int] = field(default_factory=list)  # 1-based;空=全程生效
    enabled: bool = True


@dataclass
class GraphBinding:
    id: str = ""
    intent: str = ""
    action: str = ""
    qa_id: str = ""  # action=play_qa
    step: int = 0  # action=jump_step,1-based
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


def _parse_intent(raw: object) -> FlowIntent | None:
    """单项宽容:缺 id/坏 id/非 dict → None(调用方跳过)。"""
    if not isinstance(raw, dict):
        return None
    intent_id = str(raw.get("id") or "")
    if not _ID_RE.match(intent_id):
        return None
    keywords = [str(k) for k in raw.get("keywords") or [] if str(k or "").strip()]
    steps = sorted({_as_int(s, -1) for s in raw.get("steps") or [] if _as_int(s, -1) >= 1})
    return FlowIntent(
        id=intent_id,
        label=str(raw.get("label") or "")[:LABEL_MAX_CHARS],
        keywords=keywords,
        steps=steps,
        enabled=_as_bool(raw.get("enabled")),
    )


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
    return GraphBinding(
        id=binding_id,
        intent=intent,
        action=action,
        qa_id=str(raw.get("qa_id") or ""),
        step=max(1, min(_as_int(raw.get("step"), 0), STEP_MAX)),
        priority=max(PRIORITY_MIN, min(_as_int(raw.get("priority"), DEFAULT_PRIORITY), PRIORITY_MAX)),
        once=_as_bool(raw.get("once"), default=False),
        enabled=_as_bool(raw.get("enabled")),
    )


def parse_flow_graph(raw: str | bytes | None) -> FlowGraphDoc:
    """宽容解析:永不抛错。整体坏(空/非 dict/版本不符)→空图;单项坏→逐项跳过。"""
    text = str(raw or "").strip()
    if not text:
        return FlowGraphDoc()
    try:
        data = json.loads(text)
    except ValueError:
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
    """严格校验(CP 保存用):返回错误列表,空=合法。空串=未启用,合法。"""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = str(raw or "")
    if not text.strip():
        return []
    errors: list[str] = []
    if len(text.encode("utf-8")) > GRAPH_MAX_BYTES:
        errors.append(f"graph_json exceeds {GRAPH_MAX_BYTES} bytes")
    try:
        data = json.loads(text)
    except ValueError as exc:
        errors.append(f"invalid json: {exc}")
        return errors
    if not isinstance(data, dict):
        return ["graph_json must be a json object"]
    if data.get("version") != GRAPH_VERSION:
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
        if not _ID_RE.match(intent_id):
            errors.append(f"intents[{idx}].id malformed: {intent_id!r}")
        elif intent_id in seen_intents:
            errors.append(f"intents[{idx}].id duplicated: {intent_id}")
        seen_intents.add(intent_id)
        label = str(item.get("label") or "").strip()
        if not label or len(label) > LABEL_MAX_CHARS:
            errors.append(f"intents[{idx}].label must be 1-{LABEL_MAX_CHARS} chars")
        keywords = item.get("keywords")
        if not isinstance(keywords, list) or not keywords:
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
    seen_bindings: set[str] = set()
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
        action = str(item.get("action") or "")
        if action not in ACTIONS:
            errors.append(f"bindings[{idx}].action must be one of {sorted(ACTIONS)}: {action!r}")
        if action == ACTION_PLAY_QA and not str(item.get("qa_id") or "").strip():
            errors.append(f"bindings[{idx}] action=play_qa requires qa_id")
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
    return errors


def pick_graph_action(
    doc: FlowGraphDoc,
    user_text: str,
    *,
    step_1based: int,
    fired: set[str],
) -> GraphBinding | None:
    """确定性命中:enabled 意图 + 关键词 casefold 子串 + 步号 scope;绑定按
    (priority, id) 升序取首个,once 且已 fired 的跳过。无命中返回 None。"""
    if not doc.intents or not user_text:
        return None
    text = user_text.lower()
    hit_ids: set[str] = set()
    for intent in doc.intents:
        if not intent.enabled:
            continue
        if intent.steps and step_1based not in intent.steps:
            continue
        for kw in intent.keywords:
            token = kw.strip().lower()
            if token and token in text:
                hit_ids.add(intent.id)
                break
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
