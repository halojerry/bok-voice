"""意向规则纯函数（W4，2026-09-19）：条件校验/评估单点，CLI/CP/agent 三方共用。

语义（接口冻结，plan doc §7）：
- 规则行形状（intent_rules 表行的宽容视图）：{id, account_id(''=全局), name,
  intent_code, label, disposition, priority(小者先), enabled, conditions:[...]}；
- 条件 = {"fact": 键 ∈ INTENT_FACTS, "op": gte|lte|eq, "value": 数值|布尔}，
  全部条件 AND 命中才算规则命中；facts 缺键 = 条件不成立（保守）；
- eval_intent_rules(facts, rules)：按 (priority, id) 升序取第一条 enabled 且
  全条件命中的规则，返回 {intent_code, label, disposition}；无命中返回 None。
- 确定性纪律：纯子串/数值比对、无正则无 LLM——与话术图关键词同一家族。
"""

from __future__ import annotations

from typing import Any

# facts 键白名单（agent 挂断快照的允许键；条件引用白名单外键 = 校验失败）。
INTENT_FACTS: dict[str, str] = {
    "duration_s": "通话时长秒",
    "nudge_fired": "沉默心跳已发次数",
    "watchdog_fired": "响应看门狗触发次数",
    "storm_rounds": "打断风暴静听轮数",
    "repeat_count": "REPEAT verdict 累计",
    "refuse_count": "REFUSE verdict 累计",
    "objection_count": "OBJECTION verdict 累计",
    "confirm_count": "CONFIRM verdict 累计",
    "question_count": "QUESTION verdict 累计",
    "step_max": "到达的最大话术步（1-based）",
    "wa_captured": "WhatsApp/微信已捕获（布尔）",
    "graph_notifies": "notify_human 动作已触发次数",
}

_OPS = ("gte", "lte", "eq")


def parse_rule_row(row: dict[str, Any]) -> dict[str, Any]:
    """表行 → 宽容规则视图（坏 conditions 逐条丢弃；结构坏=空规则永不命中）。"""
    raw_conds = row.get("conditions")
    conds: list[dict[str, Any]] = []
    if isinstance(raw_conds, str):
        import json

        try:
            raw_conds = json.loads(raw_conds)
        except Exception:
            raw_conds = None
    if isinstance(raw_conds, list):
        for c in raw_conds:
            if isinstance(c, dict):
                conds.append(c)
    return {
        "id": str(row.get("id") or ""),
        "account_id": str(row.get("account_id") or ""),
        "name": str(row.get("name") or ""),
        "intent_code": str(row.get("intent_code") or ""),
        "label": str(row.get("label") or ""),
        "disposition": str(row.get("disposition") or ""),
        "priority": _as_int(row.get("priority"), 10),
        "enabled": row.get("enabled") is not False,
        "conditions": conds,
    }


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _cond_ok(cond: dict[str, Any], facts: dict[str, Any]) -> bool:
    fact = str(cond.get("fact") or "")
    op = str(cond.get("op") or "")
    if fact not in INTENT_FACTS or op not in _OPS:
        return False
    if fact not in facts:
        return False  # 缺键保守不命中
    actual = facts[fact]
    expected = cond.get("value")
    if op == "eq":
        if isinstance(expected, bool) or isinstance(actual, bool):
            return bool(actual) == bool(expected)
        try:
            return float(actual) == float(expected)
        except (TypeError, ValueError):
            return str(actual) == str(expected)
    try:
        a, b = float(actual), float(expected)
    except (TypeError, ValueError):
        return False
    return a >= b if op == "gte" else a <= b


def eval_intent_rules(facts: dict[str, Any], rules: list[dict[str, Any]]) -> dict[str, Any] | None:
    """挂断快照 × 规则集 → 首个命中（(priority,id) 升序）或 None。

    返回 {intent_code, label, disposition}；disposition 可能空串（只打意向码
    不覆盖 disposition）。bad row（缺 intent_code）跳过。
    """
    parsed = [parse_rule_row(r) for r in rules or []]
    parsed.sort(key=lambda r: (r["priority"], r["id"]))
    for rule in parsed:
        if not rule["enabled"] or not rule["intent_code"]:
            continue
        if all(_cond_ok(c, facts) for c in rule["conditions"]):
            return {
                "intent_code": rule["intent_code"],
                "label": rule["label"],
                "disposition": rule["disposition"],
            }
    return None


def validate_conditions(raw: Any) -> list[str]:
    """CP 保存校验（严格轨）：conditions 必须是条件数组，返回错误串列表（空=合法）。"""
    errs: list[str] = []
    if not isinstance(raw, list):
        return ["conditions must be an array"]
    if len(raw) > 12:
        return ["conditions must have at most 12 items"]
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            errs.append(f"conditions[{i}] must be an object")
            continue
        fact = str(c.get("fact") or "")
        op = str(c.get("op") or "")
        if fact not in INTENT_FACTS:
            errs.append(f"conditions[{i}].fact must be one of {sorted(INTENT_FACTS)}")
        if op not in _OPS:
            errs.append(f"conditions[{i}].op must be one of {list(_OPS)}")
        if "value" not in c:
            errs.append(f"conditions[{i}].value is required")
        elif isinstance(c.get("value"), str):
            errs.append(f"conditions[{i}].value must be number or boolean")
    return errs
