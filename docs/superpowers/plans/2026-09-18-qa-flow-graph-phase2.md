# QA 流程图执行引擎（Phase 2）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 模板可选携带 `graph_json`（意图关键词 → play_qa/jump_step 绑定），agent 每轮读图执行动作，/qa 画布可视化并编辑该图——画布画的=引擎跑的。

**Architecture:** 纯函数（解析/校验/命中裁决）放 `packages/core` 供 CP 与 agent 共用；运行时在 `on_user_turn_completed` 的 say 直念步之后、QA 快路之前插入 graph 块（增量、kill-switch 可整闸）；CP 零新端点（复用模板 PUT + 保存时严格校验）；web 画布增第三类节点（意图）与绑定边，编辑器经 `updateTemplate({graph_json})` 落库。设计契约见 [spec](../specs/2026-09-18-qa-flow-graph.md)。

**Tech Stack:** Python 3.12 (dataclasses, FastAPI, SQLAlchemy) / TypeScript + React + @xyflow/react v12 / pytest + node:test（tsc 转译装配）。

## Global Constraints

- 设计契约以 [spec](../specs/2026-09-18-qa-flow-graph.md) 为准；本 plan 与 spec 冲突时先改 spec 再改 plan。
- **术语门禁**：粤语规范值全时空唯一拼写 `cantonese`；门禁扫描全部跟踪文件（含 docs）。禁止出现旧拼写字面量。
- Python：PEP 8、4 空格、`from __future__ import annotations`、签名类型注解；改完跑 `python -m compileall -q apps packages services tools scripts`。
- **DB 迁移只落 `deps.build_engine()` 幂等段 `_ensure_column`**；新列/新表禁 sqlite 专有语法（`tests/test_db_portability.py` 门禁）。
- **零回归铁律**：无 `graph_json` 的模板逐轮行为不变；`BOK_FLOW_GRAPH=0` 整闸关回旧行为。
- `turns.gen` 列 VARCHAR(16)：不新增 gen 值（play 轮复用 `qa_fastpath`）；provider 新值 `graph-jump` / `graph-play`。
- kill-switch 读法与全仓同款：`os.environ.get("BOK_FLOW_GRAPH", "1") == "1"`。
- Conventional commits（scope 用 `flow-graph`）；一个逻辑变更一个提交。
- 每任务完成跑该任务测试 + `python -m compileall -q apps packages`（web 任务跑 `npx tsc --noEmit`）。

**执行环境**：隔离工作树（`superpowers:using-git-worktrees`），分支 `qa-flow-graph-phase2`。Python 测试需 bootstrap 过的 venv（`./scripts/bootstrap.sh`）。

---

### Task 1: core 纯模块 `flow_graph.py`（解析/校验/命中裁决）

**Files:**
- Create: `packages/core/bok_voice_core/flow_graph.py`
- Test: `tests/test_flow_graph_core.py`

**Interfaces:**
- Consumes: 无（纯 stdlib）。
- Produces（后续任务全部依赖，逐字对齐）:
  - `FlowIntent(id, label, keywords: list[str], steps: list[int], enabled: bool)`（steps 1-based，空=全程）
  - `GraphBinding(id, intent, action, qa_id, step, priority, once, enabled)`（action ∈ `{"play_qa","jump_step"}`，step 1-based，priority 小者先）
  - `FlowGraphDoc(version, intents, bindings)` + `.intent_by_id(id)`
  - `parse_flow_graph(raw: str) -> FlowGraphDoc`（宽容：永不抛错，坏数据→空图/逐项跳过）
  - `validate_flow_graph(raw: str | bytes) -> list[str]`（严格：返回错误列表，空=合法）
  - `pick_graph_action(doc, user_text: str, *, step_1based: int, fired: set[str]) -> GraphBinding | None`
  - 常量：`GRAPH_VERSION=1, GRAPH_MAX_BYTES=65536, MAX_INTENTS=64, MAX_BINDINGS=128, MAX_KEYWORDS=32, KEYWORD_MAX_CHARS=64, LABEL_MAX_CHARS=64, PRIORITY_MIN=0, PRIORITY_MAX=1000, DEFAULT_PRIORITY=10, ACTION_PLAY_QA="play_qa", ACTION_JUMP_STEP="jump_step"`

- [ ] **Step 1: 写失败测试**

```python
"""flow_graph 纯函数:解析宽容/校验严格/命中裁决(spec 2026-09-18 §3/§4)。"""
from __future__ import annotations

import json

from bok_voice_core.flow_graph import (
    GRAPH_MAX_BYTES,
    parse_flow_graph,
    pick_graph_action,
    validate_flow_graph,
)


def _good_doc() -> str:
    return json.dumps(
        {
            "version": 1,
            "intents": [
                {"id": "int_1a2b3c4d", "label": "投诉", "keywords": ["投诉", "举报"], "steps": [], "enabled": True},
                {"id": "int_2b3c4d5e", "label": "退款", "keywords": ["Refund"], "steps": [3], "enabled": True},
            ],
            "bindings": [
                {"id": "bnd_7e8f9a0b", "intent": "int_1a2b3c4d", "action": "jump_step", "step": 4, "priority": 10, "once": False, "enabled": True},
                {"id": "bnd_c1d2e3f4", "intent": "int_2b3c4d5e", "action": "play_qa", "qa_id": "qa-1", "priority": 5, "once": True, "enabled": True},
            ],
        },
        ensure_ascii=False,
    )


def test_parse_tolerant_empty_and_garbage():
    for raw in ("", "   ", "not json", "[1,2]", '{"version": 2}', '{"version": 1, "intents": "x"}'):
        doc = parse_flow_graph(raw)
        assert doc.intents == []
        assert doc.bindings == []


def test_parse_tolerant_skips_bad_items():
    raw = json.dumps(
        {
            "version": 1,
            "intents": [
                {"id": "bad-id", "label": "x", "keywords": ["k"], "steps": [], "enabled": True},  # id 格式坏→跳过
                {"id": "int_1a2b3c4d", "label": "ok", "keywords": ["退款"], "steps": [1, "z"], "enabled": True},  # 坏步号跳过、好步号保留
            ],
            "bindings": [{"id": "bnd_7e8f9a0b", "intent": "int_nope", "action": "jump_step", "step": 2}],
        }
    )
    doc = parse_flow_graph(raw)
    assert [i.id for i in doc.intents] == ["int_1a2b3c4d"]
    assert doc.intents[0].steps == [1]
    assert doc.bindings == []  # 引用不存在的意图→解析期丢弃（运行时宽容优先于数据保真）


def test_validate_strict_good_and_errors():
    assert validate_flow_graph(_good_doc()) == []
    assert validate_flow_graph("") == []  # 空串=未启用,合法
    errs = validate_flow_graph("not json")
    assert errs and "json" in errs[0]
    # 超限
    assert validate_flow_graph(json.dumps({"version": 1, "intents": [{"id": f"int_{i:08x}", "label": "x", "keywords": ["k"]} for i in range(65)], "bindings": []}))
    # 引用缺失意图 / 坏 action / priority 越界
    errs = validate_flow_graph(
        json.dumps(
            {
                "version": 1,
                "intents": [{"id": "int_1a2b3c4d", "label": "x", "keywords": ["k"], "steps": [], "enabled": True}],
                "bindings": [
                    {"id": "bnd_7e8f9a0b", "intent": "int_nope", "action": "jump_step", "step": 2},
                    {"id": "bnd_c1d2e3f4", "intent": "int_1a2b3c4d", "action": "shout", "step": 2},
                    {"id": "bnd_d2e3f4a5", "intent": "int_1a2b3c4d", "action": "jump_step", "step": 2, "priority": 9999},
                ],
            }
        )
    )
    joined = "\n".join(errs)
    assert "int_nope" in joined and "shout" in joined and "priority" in joined


def test_validate_size_cap():
    assert any("bytes" in e for e in validate_flow_graph("x" * (GRAPH_MAX_BYTES + 1)))


def test_pick_priority_scope_casefold_once():
    doc = parse_flow_graph(_good_doc())
    # step=3:两意图均命中(step 空=全程 / steps=[3]);priority 5 先于 10
    hit = pick_graph_action(doc, "我想退款", step_1based=3, fired=set())
    assert hit is not None and hit.action == "play_qa"
    # casefold:关键词 "Refund" 命中 "REFUND my order"
    assert pick_graph_action(doc, "please REFUND my order", step_1based=3, fired=set()) is not None
    # step=2:退款意图(steps=[3])不命中 → 只剩投诉绑定
    hit2 = pick_graph_action(doc, "我要投诉", step_1based=2, fired=set())
    assert hit2 is not None and hit2.action == "jump_step"
    # once 已消耗 → 跳过
    assert pick_graph_action(doc, "我想退款", step_1based=3, fired={"bnd_c1d2e3f4"}) is None
    # 空文本/空图 → None
    assert pick_graph_action(doc, "", step_1based=1, fired=set()) is None
    assert pick_graph_action(parse_flow_graph(""), "退款", step_1based=1, fired=set()) is None
```

注意第三条测试中「超限 intents」那行：`validate_flow_graph` 应返回非空错误列表——断言写成：

```python
assert validate_flow_graph(json.dumps({"version": 1, "intents": [{"id": f"int_{i:08x}", "label": "x", "keywords": ["k"]} for i in range(65)], "bindings": []}))
```

即真值断言（返回了错误）。若 lint 报「assert on list」可改 `assert len(...)>0`。

> **勘误（2026-09-18 执行期）**：Step 1 fixture 里「退款」意图只带英文关键词 `["Refund"]`，
> 与同文件断言 `pick_graph_action(doc, "我想退款", step_1based=3, fired=set())` 期望命中
> **自相矛盾**（中文语料结构性匹配不上纯英文词）。执行时已修为 `["退款", "Refund"]`
> （中文主词 + 英文变体），见 `tests/test_flow_graph_core.py`；本 plan 保留原样作执行档案，
> 「契约以实现的测试为准」。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_flow_graph_core.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bok_voice_core.flow_graph'`

- [ ] **Step 3: 实现 `packages/core/bok_voice_core/flow_graph.py`**

```python
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
        return [f"invalid json: {exc}"]
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_flow_graph_core.py -v && .venv312/bin/python -m compileall -q packages`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add packages/core/bok_voice_core/flow_graph.py tests/test_flow_graph_core.py
git commit -m "feat(flow-graph): core pure module — parse/validate/pick (spec §3)"
```

---

### Task 2: DB 层 — `graph_json` 列 + 迁移 + 仓库透传

**Files:**
- Modify: `packages/business-db/bok_voice_business_db/models.py`（`ConversationTemplate`，hotwords 列之后）
- Modify: `apps/control-plane/control_plane/deps.py`（`build_engine()` 幂等段，`cluster_head_id` 那行之后追加）
- Modify: `packages/business-db/bok_voice_business_db/repository.py`（SQL `create_template` ~:598 / `update_template` allowed 集 ~:621；InMemory 同名两法 ~:1548/:1569）
- Test: `tests/test_template_graph_field.py`

**Interfaces:**
- Consumes: Task 1 无依赖（本任务纯数据层）。
- Produces: `ConversationTemplate.graph_json: str`（默认 ""）经 `_to_dict`（泛化列反射，自动透出）进全部模板 API 响应；`create_template`/`update_template`（SQL+InMemory）接受 `graph_json` 键。

- [ ] **Step 1: 写失败测试**（fixture 照抄 `tests/test_qa_cluster_field.py` 的 `_sql_repo` 姿势）

```python
"""graph_json 数据层:迁移补列 / create+update 透传 / 默认空串(spec §5)。"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-owner")

_DOC = '{"version":1,"intents":[{"id":"int_1a2b3c4d","label":"投诉","keywords":["投诉"],"steps":[],"enabled":true}],"bindings":[]}'


def _sql_repo(tmpdir: str):
    os.environ["DATABASE_URL"] = f"sqlite:///{tmpdir}/graph.db"
    from sqlalchemy.orm import sessionmaker

    from control_plane import deps
    from bok_voice_business_db.repository import SqlAlchemyBusinessRepository

    engine = deps.build_engine()
    assert engine is not None
    assert deps.build_engine() is not None  # 二跑不炸=幂等
    repo = SqlAlchemyBusinessRepository(sessionmaker(bind=engine, expire_on_commit=False)())
    repo.engine = engine  # type: ignore[attr-defined]
    return repo


def test_migration_adds_graph_json(tmp_path):
    repo = _sql_repo(str(tmp_path))
    import sqlalchemy as sa

    with repo.engine.connect() as conn:  # type: ignore[attr-defined]
        cols = {c["name"] for c in sa.inspect(conn).get_columns("conversation_templates")}
    assert "graph_json" in cols


def test_create_update_roundtrip(tmp_path):
    repo = _sql_repo(str(tmp_path))
    tpl = repo.create_template({"account_id": "acc-001", "name": "t", "graph_json": _DOC})
    assert repo.get_template(tpl["id"])["graph_json"] == _DOC
    # update 白名单放行;空串=清空
    repo.update_template(tpl["id"], {"graph_json": ""})
    assert repo.get_template(tpl["id"])["graph_json"] == ""
    repo.update_template(tpl["id"], {"graph_json": _DOC})
    assert repo.get_template(tpl["id"])["graph_json"] == _DOC


def test_default_empty_and_inmemory_parity():
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    tpl = repo.create_template({"account_id": "acc-001", "name": "m"})
    assert tpl["graph_json"] == ""
    repo.update_template(tpl["id"], {"graph_json": _DOC})
    assert repo.get_template(tpl["id"])["graph_json"] == _DOC
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_template_graph_field.py -v`
Expected: FAIL（列不存在 / 透传缺键）

- [ ] **Step 3: 实现**

`models.py` — `ConversationTemplate` 的 `hotwords` 列之后加：

```python
    # 话术图(2026-09-18 Phase 2):意图节点+绑定边 JSON(spec
    # docs/superpowers/specs/2026-09-18-qa-flow-graph.md);空串=未启用,
    # 运行时零变化。校验/解析见 packages/core/bok_voice_core/flow_graph.py。
    graph_json: Mapped[str] = mapped_column(Text, default="")
```

`deps.py` — `build_engine()` 幂等段里 `_ensure_column(conn, "qa_entries", "cluster_head_id", ...)` 之后追加：

```python
        _ensure_column(
            conn, "conversation_templates", "graph_json", "graph_json TEXT DEFAULT ''"
        )
```

`repository.py` 四处：
- SQL `create_template`（~:598）：构造参数加 `graph_json=data.get("graph_json", ""),`
- SQL `update_template`（~:621）：`allowed` 集合加 `"graph_json"`
- InMemory `create_template`（~:1548）：同名键取值透传（照该方法既有字段姿势）
- InMemory `update_template`（~:1569）：`allowed` 集合加 `"graph_json"`

- [ ] **Step 4: 跑测试确认通过 + 方言门禁**

Run: `.venv312/bin/python -m pytest tests/test_template_graph_field.py tests/test_db_portability.py -v && .venv312/bin/python -m compileall -q apps packages`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add packages/business-db/bok_voice_business_db/models.py packages/business-db/bok_voice_business_db/repository.py apps/control-plane/control_plane/deps.py tests/test_template_graph_field.py
git commit -m "feat(flow-graph): graph_json column — idempotent migration + repo passthrough"
```

---

### Task 3: CP API — schema 字段 + 保存校验 + 审计

**Files:**
- Modify: `apps/control-plane/control_plane/schemas.py`（`TemplateRequest` ~:134 / `UpdateTemplateRequest` ~:149）
- Modify: `apps/control-plane/control_plane/main.py`（`create_template` ~:3612 / `update_template` ~:3628；文件顶部 import 区）
- Test: `tests/test_template_graph_api.py`

**Interfaces:**
- Consumes: Task 1 `validate_flow_graph`；Task 2 的列/透传。
- Produces: `POST /api/templates` 与 `PUT /api/templates/{id}` 接受 `graph_json`；非法 → `400 {"error": "invalid_graph_json", "detail": [...]}`；空串=清空（合法）；审计 detail 增 `graph_saved`。

- [ ] **Step 1: 写失败测试**（client fixture 照抄 `tests/test_qa_canned_status.py` 的 `_client_and_repo` 姿势）

```python
"""模板 graph_json API:保存校验 400 / exclude_unset 部分更新 / 空串清空 / 审计布尔。"""
from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-canned")

_GOOD = {
    "version": 1,
    "intents": [{"id": "int_1a2b3c4d", "label": "投诉", "keywords": ["投诉"], "steps": [], "enabled": True}],
    "bindings": [{"id": "bnd_7e8f9a0b", "intent": "int_1a2b3c4d", "action": "jump_step", "step": 4}],
}


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    client = TestClient(app).__enter__()
    return client, repo


def test_create_with_graph_and_invalid_rejected(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    ok = client.post("/api/templates", json={"name": "t", "graph_json": json.dumps(_GOOD)})
    assert ok.status_code == 200, ok.text
    assert json.loads(ok.json()["graph_json"]) == _GOOD
    bad = client.post("/api/templates", json={"name": "t2", "graph_json": "{not json"})
    assert bad.status_code == 400
    assert bad.json()["detail"]["error"] == "invalid_graph_json"


def test_put_partial_update_semantics(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    tpl = client.post("/api/templates", json={"name": "t", "graph_json": json.dumps(_GOOD)}).json()
    # 只传 name——exclude_unset 不得抹掉 graph_json
    client.put(f"/api/templates/{tpl['id']}", json={"name": "t2"})
    assert repo.get_template(tpl["id"])["graph_json"] != ""
    # 显式空串=清空
    client.put(f"/api/templates/{tpl['id']}", json={"graph_json": ""})
    assert repo.get_template(tpl["id"])["graph_json"] == ""
    # PUT 非法图 → 400 且旧值保留
    client.put(f"/api/templates/{tpl['id']}", json={"graph_json": json.dumps(_GOOD)})
    bad = client.put(f"/api/templates/{tpl['id']}", json={"graph_json": '{"version":9}'})
    assert bad.status_code == 400
    assert repo.get_template(tpl["id"])["graph_json"] == json.dumps(_GOOD)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_template_graph_api.py -v`
Expected: FAIL（schema 无该字段→pydantic 忽略未知键→断言取不到）

- [ ] **Step 3: 实现**

`schemas.py`：两个请求模型各加一行（`hotwords` 之后）：

```python
    graph_json: str = ""
```

`main.py` 顶部 import 区加 `from bok_voice_core.flow_graph import validate_flow_graph`，模块级 helper（放 template 端点前）：

```python
def _validate_graph_field(raw: str) -> list[str]:
    """模板 graph_json 保存校验:空串=未启用放行;非法返回错误列表(→400)。"""
    if not str(raw or "").strip():
        return []
    return validate_flow_graph(str(raw))
```

`create_template`（`_repo().create_template(...)` 之前）：

```python
    _graph_errors = _validate_graph_field(req.graph_json)
    if _graph_errors:
        raise HTTPException(400, {"error": "invalid_graph_json", "detail": _graph_errors[:5]})
```

`update_template`（`payload = req.model_dump(exclude_unset=True)` 之后、`_repo().update_template` 之前）：

```python
    if "graph_json" in payload:
        _graph_errors = _validate_graph_field(str(payload.get("graph_json") or ""))
        if _graph_errors:
            raise HTTPException(400, {"error": "invalid_graph_json", "detail": _graph_errors[:5]})
```

两处 `_audit(...)` 的 `detail={...}` 各加 `"graph_saved": bool(req.graph_json),`（update 处用 `bool(payload.get("graph_json"))`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_template_graph_api.py tests/test_template_graph_field.py -v && .venv312/bin/python -m compileall -q apps packages`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/control_plane/schemas.py apps/control-plane/control_plane/main.py tests/test_template_graph_api.py
git commit -m "feat(flow-graph): template API accepts graph_json with strict save validation"
```

---

### Task 4: FlowController — graph 装配 + `jump_to` + fired 账本

**Files:**
- Modify: `apps/agent/agent_runtime/flow.py`（顶部 import；`FlowController` dataclass ~:845 字段区；`from_template` ~:866；`advance()` ~:924 之后）
- Test: `tests/test_flow_graph_runtime.py`

**Interfaces:**
- Consumes: Task 1 `FlowGraphDoc` / `parse_flow_graph`。
- Produces（Task 5 依赖）:
  - `FlowController.graph: FlowGraphDoc`（from_template 从 `template["graph_json"]` 宽容解析）
  - `FlowController.graph_fired: set[str]`（动作成功执行才记账）
  - `FlowController.jump_to(idx: int) -> None`（钳制 `[0, len(steps)]`、`closing`/同位 no-op、置 `_just_advanced=True`）

- [ ] **Step 1: 写失败测试**

```python
"""FlowController 图装配与 jump_to(spec §4.2/§4.3)。"""
from __future__ import annotations

import json

from agent_runtime.flow import FlowController

_TEMPLATE = {
    "steps_json": json.dumps(
        [{"goal": f"第{i}步", "ref": f"第{i}步说法"} for i in range(1, 7)],
        ensure_ascii=False,
    ),
    "graph_json": json.dumps(
        {
            "version": 1,
            "intents": [{"id": "int_1a2b3c4d", "label": "投诉", "keywords": ["投诉"], "steps": [], "enabled": True}],
            "bindings": [{"id": "bnd_7e8f9a0b", "intent": "int_1a2b3c4d", "action": "jump_step", "step": 4}],
        },
        ensure_ascii=False,
    ),
}


def test_from_template_parses_graph():
    fc = FlowController.from_template(_TEMPLATE, None)
    assert len(fc.graph.intents) == 1
    assert fc.graph_fired == set()
    assert fc.current == 0


def test_from_template_garbage_graph_is_empty():
    fc = FlowController.from_template({"steps_json": "[]", "graph_json": "garbage"}, None)
    assert fc.graph.intents == []
    fc2 = FlowController.from_template({"steps_json": "[]"}, None)
    assert fc2.graph.intents == []


def test_jump_to_clamps_and_respects_closing():
    fc = FlowController.from_template(_TEMPLATE, None)
    fc.jump_to(3)  # 0-based → 第 4 步
    assert fc.current == 3
    # 钳制:越界跳到 done(== len)
    fc.jump_to(99)
    assert fc.current == 6
    assert fc.done
    # closing 冻结
    fc2 = FlowController.from_template(_TEMPLATE, None)
    fc2.enter_closing()
    fc2.jump_to(3)
    assert fc2.current == 0
    # 同位 no-op 不置 _just_advanced
    fc3 = FlowController.from_template(_TEMPLATE, None)
    fc3.jump_to(0)
    assert fc3.current == 0 and not fc3._just_advanced
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_flow_graph_runtime.py -v`
Expected: FAIL — `FlowController` 无 `graph` 属性 / 无 `jump_to`

- [ ] **Step 3: 实现 `flow.py`**

顶部 import 区加：

```python
from bok_voice_core.flow_graph import FlowGraphDoc, parse_flow_graph
```

`FlowController` dataclass 字段区（`last_digits` 之后）加：

```python
    # 话术图(2026-09-18 Phase 2):意图节点+绑定边;空图=零变化。from_template
    # 宽容解析 template["graph_json"](坏 JSON/坏版本→空图,spec §3 校验双轨)。
    graph: FlowGraphDoc = field(default_factory=FlowGraphDoc)
    # 本通已成功执行的绑定 id(jump 实际位移 / play_qa 实际播出才记;once 绑定
    # 依此每通至多一次,spec §4.3)。agent.py graph 块写入。
    graph_fired: set[str] = field(default_factory=set)
```

`from_template` 加一行（`fc.vars_map = ...` 之后）：

```python
        fc.graph = parse_flow_graph(str((template or {}).get("graph_json") or ""))
```

`advance()` 方法之后加：

```python
    def jump_to(self, idx: int) -> None:
        """跳到任意步(话术图 jump_step,spec §4.2)。镜像 advance 的副作用包
        (置 _just_advanced → 【新一步】/底稿首轮重渲染),外加钳制与冻结:
        closing 后流程不再被图移动;同位跳转 no-op;允许跳到 done(== len(steps))。"""
        if not self.has_steps or self.closing:
            return
        target = max(0, min(int(idx), len(self.steps)))
        if target == self.current:
            return
        self.current = target
        self._just_advanced = True
```

- [ ] **Step 4: 跑测试确认通过（含既有 flow 回归）**

Run: `.venv312/bin/python -m pytest tests/test_flow_graph_runtime.py tests/test_flow_controller.py tests/test_prompt_objection_rules.py -v && .venv312/bin/python -m compileall -q apps packages`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent/agent_runtime/flow.py tests/test_flow_graph_runtime.py
git commit -m "feat(flow-graph): FlowController graph assembly + jump_to + fired ledger"
```

---

### Task 5: agent 插线 — `QaIndex.by_id` + 罐头播放共用件 + graph 块

**Files:**
- Modify: `apps/agent/agent_runtime/qa_gate.py`（`QaIndex` ~:44，`match` 之后加 `by_id`）
- Modify: `apps/agent/agent_runtime/agent.py`（顶部 import；`on_user_turn_completed` 内 say 直念步快路块之后、QA 快路块 `if _qa_index is not None and _tts_cache is not None:`（~:3635）之前）
- Test: `tests/test_qa_gate_byid.py`（by_id 单测；graph 块自身由 Task 9 探针实弹验收——hook 闭包不可单测，与全仓现状一致）

**Interfaces:**
- Consumes: Task 4 的 `flow_ctrl.graph` / `flow_ctrl.graph_fired` / `flow_ctrl.jump_to`；Task 1 `pick_graph_action`。
- Produces: `QaIndex.by_id(entry_id: str) -> dict | None`；闭包 helper `_qa_pcm_for(text)` 与 `_qa_canned_say(entry, pcm, *, provider) -> bool`（QA 快路改为委托同一条出口——行为逐字节不变）；turns 账本 provider 新值 `graph-jump` / `graph-play`。

- [ ] **Step 1: 写失败测试**

```python
"""QaIndex.by_id:话术图 play_qa 绑定按条目 id 直取(spec §4.1)。"""
from agent_runtime.qa_gate import QaIndex


def test_by_id_hits_and_misses():
    entries = [
        {"id": "qa-1", "question_text": "怎么退款", "answer_text": "稍等帮您查"},
        {"id": "", "question_text": "无id", "answer_text": "x"},
        {"id": "qa-3", "question_text": "", "answer_text": "空问题不入索引"},
    ]
    idx = QaIndex(entries)
    hit = idx.by_id("qa-1")
    assert hit is not None and hit["answer_text"] == "稍等帮您查"
    assert idx.by_id("qa-missing") is None
    assert idx.by_id("") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_qa_gate_byid.py -v`
Expected: FAIL — `QaIndex` 无 `by_id`

- [ ] **Step 3a: 实现 `qa_gate.py` 的 `by_id`**（`match` 方法之后）

```python
    def by_id(self, entry_id: str) -> dict | None:
        """按条目 id 直取(话术图 play_qa 绑定,spec §4.1);无命中返回 None。"""
        wanted = str(entry_id or "")
        if not wanted:
            return None
        for entry, _q, _v in self._items:
            if str(entry.get("id") or "") == wanted:
                return entry
        return None
```

- [ ] **Step 3b: `agent.py` 顶部 import**（与既有 `from .qa_gate import ...` 相邻）

```python
from bok_voice_core.flow_graph import pick_graph_action
```

- [ ] **Step 3c: 插入共用件 + graph 块 + 改造 QA 快路**

在 `on_user_turn_completed` 内、say 直念步快路块结束之后、QA 快路块 opener `if _qa_index is not None and _tts_cache is not None:` 之前，插入以下三段（顺序勿变——helper 先于使用点）：

```python
            # ---- 罐头播放共用件(2026-09-18 话术图 Phase 2):QA 快路与
            # graph play_qa 同一条出口,抽自 QA 快路原位(行为逐字节不变)。
            def _qa_pcm_for(text: str):
                voice = getattr(tts_provider, "resolved_voice", lambda: "")()
                model = getattr(tts_provider, "resolved_model", lambda: "")()
                if not text or _tts_cache is None:
                    return None
                return _tts_cache.lookup(
                    text, voice=voice, model=model,
                    speed=getattr(tts_provider, "resolved_speed", lambda: 1.0)(),
                )

            async def _qa_canned_say(entry: dict, pcm, *, provider: str) -> bool:
                answer = str(entry.get("answer_text") or "").strip()
                try:
                    await session.interrupt()  # ① 作废停着的抢跑快照
                except Exception:  # noqa: BLE001
                    pass
                # ② 手动补 user 轮(StopResponse 轮 item_added 唔会触发)
                await self._try_append_user_message(new_message)
                # ③ 回声守卫预锚(正常 playout 完才置,快路要立即生效)
                context_state.set_last_reply(answer)
                _turn_origin["gen"] = "qa_fastpath"
                _turn_origin["provider"] = provider
                # ④ 落库 user 轮(paused 分支同款)
                try:
                    _step = (int(flow_ctrl.current) + 1) if flow_ctrl.has_steps else 0
                    _now = int((time.monotonic() - _t0) * 1000)
                    await cp.add_turn(
                        call_id, "user", user_text, language=language_state.lang,
                        line="a", speaker="customer",
                        template_step=_step, started_ms=_now, ended_ms=_now,
                    )
                except Exception:  # noqa: BLE001
                    pass
                _spawn_report(cp.qa_hit(str(entry.get("id") or "")))
                # ⑤ 快路不经 tts_provider,首音频回调唔会拆看门狗——显式拆
                _cancel_response_watchdog()
                await session.say(
                    answer, audio=frames_iter(pcm, _tts_cache.sample_rate)
                )
                return True

            # ---- 话术图引擎(spec 2026-09-18;插在 say 直念步之后、QA 快路之前,
            # precedence: REFUSE>DEFER>say>graph>QA 快路;BOK_FLOW_GRAPH=0 整闸,
            # 空图零成本零变化)----
            _gbinding = None
            if (
                os.environ.get("BOK_FLOW_GRAPH", "1") == "1"
                and flow_ctrl.graph.intents
                and user_text
            ):
                _gbinding = pick_graph_action(
                    flow_ctrl.graph,
                    user_text,
                    step_1based=(int(flow_ctrl.current) + 1),
                    fired=flow_ctrl.graph_fired,
                )
            if _gbinding is not None:
                if _gbinding.action == "jump_step":
                    # 1-based 存储转 0-based;同位跳转 no-op 不记账(spec §4.3 防环)
                    _gtarget = int(_gbinding.step or 1) - 1
                    if _gtarget != flow_ctrl.current:
                        flow_ctrl.graph_fired.add(_gbinding.id)
                        flow_ctrl.jump_to(_gtarget)
                        _invalidate_stale_preemptive(
                            f"流程跳转 → 第 {flow_ctrl.current + 1} 步"
                        )
                        context_state.set_flow_current(flow_ctrl.current_step_text())
                        print(
                            f"FLOW_GRAPH jump binding={_gbinding.id} "
                            f"step={flow_ctrl.current + 1}",
                            flush=True,
                        )
                        # 本轮继续回答(新步指引);provider 标记 assistant 轮
                        _turn_origin["provider"] = "graph-jump"
                    else:
                        print(
                            f"FLOW_GRAPH jump_noop binding={_gbinding.id} "
                            f"step={_gtarget + 1}",
                            flush=True,
                        )
                else:  # play_qa:条目在场且音频已物化才播;miss 放行 LLM 不消耗 once
                    _ge = (
                        _qa_index.by_id(_gbinding.qa_id)
                        if _qa_index is not None and _gbinding.qa_id
                        else None
                    )
                    _gp = _qa_pcm_for(str((_ge or {}).get("answer_text") or ""))
                    if (
                        _ge is not None
                        and _gp is not None
                        and await _qa_canned_say(_ge, _gp, provider="graph-play")
                    ):
                        flow_ctrl.graph_fired.add(_gbinding.id)
                        print(
                            f"FLOW_GRAPH play binding={_gbinding.id} "
                            f"qa={_gbinding.qa_id}",
                            flush=True,
                        )
                        raise StopResponse()  # 压掉本轮 LLM(WA 累积同款)
                    print(
                        f"FLOW_GRAPH play_miss binding={_gbinding.id} "
                        f"qa={_gbinding.qa_id}",
                        flush=True,
                    )
```

注意：上面 `_qa_canned_say` 尾部 `session.say(... audio=frames_iter(pcm, _tts_cache.sample_rate))` 中的帧适配调用，**必须照 QA 快路块现用法逐字替换**——现块写的是 `audio=frames_aiter(pcm_to_frames(_qa_pcm, _tts_cache.sample_rate))`，实现时以 QA 快路现行 import 与调用为准（frames 适配器函数名以当前源码为准），不得新造函数名。

随后把 QA 快路块 hit 分支改为委托同一条出口（原 `_qa_voice/_qa_model/_qa_pcm` 三行解析与 ①-⑥ 序列整体删除，替换为）：

```python
                    if _qa_entry is not None:
                        _qa_answer = str(_qa_entry.get("answer_text") or "").strip()
                        _qa_pcm = _qa_pcm_for(_qa_answer)
                        if _qa_pcm is not None:
                            print(f"QA_FASTPATH hit=1 entry={_qa_entry.get('id')} score={_qa_score:.2f}", flush=True)
                            if await _qa_canned_say(_qa_entry, _qa_pcm, provider="qa-fastpath"):
                                raise StopResponse()
                        print(f"QA_FASTPATH hit=0 reason=no_audio entry={_qa_entry.get('id')}", flush=True)
                        _qa_bump("no_audio")
```

其余 QA 快路代码（`_qa_exclude_reason`、`_qa_index.match`、`_qa_bump` 计数）一律不动。

- [ ] **Step 4: 跑测试 + 全量回归**

Run: `.venv312/bin/python -m pytest tests/test_qa_gate_byid.py tests/test_qa_gate.py tests/test_flow_controller.py -v && .venv312/bin/python -m pytest tests/ -q 2>&1 | tail -3 && .venv312/bin/python -m compileall -q apps packages`
Expected: by_id PASS；全量 pytest 无新增失败（基线=origin/main 全绿）

- [ ] **Step 5: Commit**

```bash
git add apps/agent/agent_runtime/qa_gate.py apps/agent/agent_runtime/agent.py tests/test_qa_gate_byid.py
git commit -m "feat(flow-graph): runtime wire — graph block + shared canned-play exit (BOK_FLOW_GRAPH)"
```

---

### Task 6: web lib — `parseGraphDoc` + deriveGraph 意图节点/绑定边

**Files:**
- Modify: `apps/web/lib/qa-canvas.ts`（常量区 ~:48；类型区；`deriveGraph` ~:79）
- Test: `apps/web/test/qa-canvas.test.mjs`（追加用例，复用文件顶部既有 tsc 转译装配）

**Interfaces:**
- Consumes: 模板 API 响应里的 `graph_json` 字符串（Task 3）。
- Produces（Task 7/8 依赖）:
  - `INTENT_X = -280`、`INTENT_STACK_Y = 140`
  - `interface GraphIntent { id; label; keywords: string[]; steps: number[]; enabled: boolean }`、`interface GraphBinding { id; intent; action: "play_qa" | "jump_step"; qa_id?; step?; priority; once; enabled }`、`interface GraphDoc { version; intents: GraphIntent[]; bindings: GraphBinding[] }`
  - `parseGraphDoc(raw: string | null | undefined): GraphDoc`（宽容：坏 JSON→空 doc）
  - `deriveGraph(rows, steps, opts)` opts 增 `graph?: GraphDoc`；返回节点增 `type: "intent"`（id `intent:<id>`，data `{ intent }`）；返回边增 `data.kind: "binding"`（id `bind:<id>`，label=意图 label 退首关键词；悬空引用不画）
  - `CanvasNodeData` 的 kind 联合与 `CanvasEdge` data.kind 联合各自扩 `"intent"` / `"binding"`

- [ ] **Step 1: 写失败测试**（追加到 `apps/web/test/qa-canvas.test.mjs`，沿用文件内既有 import 的模块命名空间）

```js
// ---- 话术图 Phase 2:意图节点/绑定边派生 ----
const GRAPH_DOC = {
  version: 1,
  intents: [
    { id: "int_1a2b3c4d", label: "投诉", keywords: ["投诉"], steps: [], enabled: true },
    { id: "int_2b3c4d5e", label: "退款", keywords: ["退款"], steps: [3], enabled: false },
  ],
  bindings: [
    { id: "bnd_7e8f9a0b", intent: "int_1a2b3c4d", action: "jump_step", step: 4, priority: 10, once: false, enabled: true },
    { id: "bnd_c1d2e3f4", intent: "int_2b3c4d5e", action: "play_qa", qa_id: "nope", priority: 5, once: true, enabled: true },
    { id: "bnd_d2e3f4a5", intent: "int_nope", action: "jump_step", step: 2, priority: 9, once: false, enabled: true }, // 悬空→不画
  ],
};

test("parseGraphDoc tolerant", () => {
  assert.deepEqual(parseGraphDoc(""), { version: 1, intents: [], bindings: [] });
  assert.deepEqual(parseGraphDoc("garbage").intents, []);
  assert.equal(parseGraphDoc(JSON.stringify(GRAPH_DOC)).intents.length, 2);
});

test("deriveGraph intent nodes anchored to scope step", () => {
  const steps = parseTemplateSteps(JSON.stringify([{ goal: "g1", ref: "r1" }, { goal: "g2", ref: "r2" }, { goal: "g3", ref: "r3" }]));
  const graph = deriveGraph([], steps, { graph: parseGraphDoc(JSON.stringify(GRAPH_DOC)) });
  const nodes = graph.nodes.filter((n) => n.type === "intent");
  assert.equal(nodes.length, 2); // 禁用意图照渲染(视图置灰)
  const complain = nodes.find((n) => n.id === "intent:int_1a2b3c4d");
  assert.equal(complain.position.x, INTENT_X);
  assert.equal(complain.position.y, 0); // 全程意图锚第 1 步(step:global 下方基线)
  const refund = nodes.find((n) => n.id === "intent:int_2b3c4d5e");
  assert.equal(refund.data.intent.enabled, false);
});

test("deriveGraph binding edges skip dangling and carry label", () => {
  const steps = parseTemplateSteps(JSON.stringify([{ goal: "g1", ref: "r1" }, { goal: "g2", ref: "r2" }, { goal: "g3", ref: "r3" }]));
  const graph = deriveGraph([], steps, { graph: parseGraphDoc(JSON.stringify(GRAPH_DOC)) });
  const bindEdges = graph.edges.filter((e) => e.data?.kind === "binding");
  assert.equal(bindEdges.length, 1); // 悬空 intent 与悬空 qa_id 均不画
  assert.equal(bindEdges[0].id, "bind:bnd_7e8f9a0b");
  assert.equal(bindEdges[0].source, "intent:int_1a2b3c4d");
  assert.equal(bindEdges[0].target, "step:3"); // 1-based 第 4 步 → 0-based step:3
  assert.equal(bindEdges[0].label, "投诉");
});

test("graph absent = zero intent nodes/edges", () => {
  const steps = parseTemplateSteps(JSON.stringify([{ goal: "g", ref: "r" }]));
  const graph = deriveGraph([], steps, {});
  assert.equal(graph.nodes.filter((n) => n.type === "intent").length, 0);
  assert.equal(graph.edges.filter((e) => e.data?.kind === "binding").length, 0);
});
```

（`INTENT_X`/`parseGraphDoc` 加入文件顶部既有的解构 import。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd apps/web && npm test 2>&1 | tail -5`
Expected: 新用例 FAIL（INTENT_X 未导出 / deriveGraph 不产意图节点）

- [ ] **Step 3: 实现 `qa-canvas.ts`**

常量区追加：

```ts
export const INTENT_X = -280;       // 意图节点道：脊柱左侧、全程通用泳道（-560）之右
export const INTENT_STACK_Y = 140;  // 同锚点多个意图的纵向堆叠节距
```

类型区追加：

```ts
export interface GraphIntent {
  id: string;
  label: string;
  keywords: string[];
  steps: number[]; // 1-based；空=全程
  enabled: boolean;
}
export interface GraphBinding {
  id: string;
  intent: string;
  action: "play_qa" | "jump_step";
  qa_id?: string;
  step?: number;
  priority: number;
  once: boolean;
  enabled: boolean;
}
export interface GraphDoc {
  version: number;
  intents: GraphIntent[];
  bindings: GraphBinding[];
}

export function parseGraphDoc(raw: string | null | undefined): GraphDoc {
  const empty: GraphDoc = { version: 1, intents: [], bindings: [] };
  const text = String(raw || "").trim();
  if (!text) return empty;
  try {
    const data = JSON.parse(text) as Partial<GraphDoc>;
    if (!data || data.version !== 1 || !Array.isArray(data.intents) || !Array.isArray(data.bindings)) return empty;
    return { version: 1, intents: data.intents, bindings: data.bindings };
  } catch {
    return empty;
  }
}
```

`deriveGraph` 的 opts 类型加 `graph?: GraphDoc`；`CanvasNodeData`/`CanvasEdgeData` 的 kind 联合扩 `"intent"` / `"binding"`。在 qaNodes 循环之后、edges 派生之前插入意图节点派生，edges 段末尾追加绑定边派生：

```ts
  // 意图节点（话术图 Phase 2）：锚定首个生效步（全程锚第 1 步），同锚点纵向堆叠；
  // 禁用意图照渲染（视图置灰）。位置优先取用户拖过的 localStorage 坐标。
  const intentNodes: CanvasIntentNode[] = [];
  const stackY = new Map<number, number>();
  for (const intent of opts.graph?.intents ?? []) {
    const stepIdx = Math.min(Math.max((intent.steps[0] ?? 1) - 1, 0), Math.max(steps.length - 1, 0));
    const baseY = stepNodes.find((n) => n.id === `step:${stepIdx}`)?.position.y ?? 0;
    const stack = stackY.get(stepIdx) ?? 0;
    stackY.set(stepIdx, stack + INTENT_STACK_Y);
    intentNodes.push({
      id: `intent:${intent.id}`,
      type: "intent" as const,
      position: opts.positions[`intent:${intent.id}`] ?? { x: INTENT_X, y: baseY + stack },
      data: { kind: "intent", intent },
    });
  }
```

```ts
  // 绑定边：意图 → 目标（play_qa→QA 条目节点 / jump_step→步骤节点）。
  // 悬空引用不画（条目已删/步号越界），kind=binding，label=意图 label 退首关键词。
  const nodeIds = new Set([...stepNodes, ...qaNodes, ...intentNodes].map((n) => n.id));
  for (const b of opts.graph?.bindings ?? []) {
    if (!b.enabled) continue;
    const src = `intent:${b.intent}`;
    const tgt = b.action === "jump_step"
      ? `step:${Math.min(Math.max((b.step ?? 1) - 1, 0), Math.max(steps.length - 1, 0))}`
      : String(b.qa_id || "");
    if (!nodeIds.has(src) || !tgt || !nodeIds.has(tgt)) continue;
    const intent = (opts.graph?.intents ?? []).find((i) => i.id === b.intent);
    edges.push({
      id: `bind:${b.id}`,
      source: src,
      target: tgt,
      sourceHandle: null,
      targetHandle: null,
      label: intent?.label || intent?.keywords?.[0] || "",
      data: { kind: "binding" },
    });
  }
```

函数返回值节点数组并入 `intentNodes`（`[...stepNodes, ...qaNodes, ...intentNodes]`）。`CanvasIntentNode` 类型按文件内 `CanvasQaNode` 同款姿势声明（type 字面量 `"intent"`）。

- [ ] **Step 4: 跑测试确认通过 + tsc**

Run: `cd apps/web && npm test 2>&1 | tail -3 && npx tsc --noEmit`
Expected: 全 PASS（原 15 + 新 4）

- [ ] **Step 5: Commit**

```bash
git add apps/web/lib/qa-canvas.ts apps/web/test/qa-canvas.test.mjs
git commit -m "feat(flow-graph): web lib — parseGraphDoc + intent nodes/binding edges"
```

---

### Task 7: web 画布 — intent 节点渲染 + 调色盘 + 选中回调

**Files:**
- Modify: `apps/web/components/qa-canvas-view.tsx`（`nodeTypes` 常量；props；edges/nodes memo；调色盘浮层）

**Interfaces:**
- Consumes: Task 6 的 `GraphDoc`/`INTENT_X`/派生节点 `type:"intent"`；页面传入的新 props。
- Produces（Task 8 依赖）:
  - props 增：`graphDoc: GraphDoc`、`canEditGraph: boolean`、`onAddIntent(flowPos: {x,y}): void`、`onOpenIntentEditor(intentId: string): void`
  - 调色盘：左下浮动「＋ 意图」按钮（`canEditGraph=false` 不渲染），点击经 `useReactFlow().screenToFlowPosition` 取视口中心坐标回调 `onAddIntent`
  - 点选 intent 节点 → `onOpenIntentEditor(intent.id)`
  - intent 节点可拖拽（位置进既有 `onNodesChange`→`setPositions`→localStorage 链路，id 稳定无需新键）
  - 绑定边渲染：`type: "floating"`（或默认 bezier）、虚线 amber（`strokeDasharray: "6 4"`、`stroke: "#d97706"`）、`deletable: canEditGraph`（删除=从图移除绑定，右键菜单/onEdgesDelete 沿既有簇边删除模式）；spine 边 `selectable:false deletable:false` 语义不变

- [ ] **Step 1: 实现**

1. `nodeTypes` 旁新增 `intentNodeTypes`（或并入同一常量对象）：

```tsx
const IntentNode = memo(function IntentNode({ data, selected }: NodeProps) {
  const d = data as { intent: GraphIntent };
  return (
    <div
      className={`w-[220px] rounded-lg border bg-white px-3 py-2 shadow-sm ${
        d.intent.enabled ? "border-amber-300" : "border-slate-200 opacity-50"
      } ${selected ? "ring-2 ring-amber-400" : ""}`}
    >
      <div className="flex items-center gap-1.5 text-[13px] font-medium text-slate-800">
        <span aria-hidden>🎯</span>
        <span className="truncate">{d.intent.label || "(未命名意图)"}</span>
        {!d.intent.enabled && <span className="ml-auto text-[10px] text-slate-400">已停用</span>}
      </div>
      <div className="mt-1 truncate text-[11px] text-slate-500">
        {d.intent.keywords.join(" / ")}
      </div>
    </div>
  );
});
```

（Handle 不渲染——绑定边从节点整体边缘出发，React Flow v12 允许无边节点但**边必须有端点锚**；若 v12 报 008 错误则按 QA 节点既有姿势补 `<Handle type="target" position={Position.Top} isConnectable={false} />` 与 source 同理——以运行报错为准，报错即补，两个 Handle 都要恒渲染。）

2. edges memo 的样式分支加 binding 档：

```tsx
if (e.data?.kind === "binding") {
  return {
    ...base,
    selectable: true,
    deletable: canEditGraph,
    style: { stroke: "#d97706", strokeWidth: 1.6, strokeDasharray: "6 4" },
    labelStyle: { fill: "#92400e", fontSize: 11 },
    labelBgStyle: { fill: "#fef3c7" },
  };
}
```

（`canEditGraph` 加入该 memo 的依赖数组；`graph`/`graphDoc` 变化必须触发 nodes/edges 重算——把 `graphDoc` 加入两个 memo 的 deps。）

3. 调色盘浮层（画布容器内、与 MiniMap 同层）：

```tsx
{canEditGraph && (
  <div className="absolute bottom-4 left-4 z-10 flex gap-2">
    <button
      type="button"
      className="rounded-md border border-amber-300 bg-amber-50 px-3 py-1.5 text-xs font-medium text-amber-800 hover:bg-amber-100"
      onClick={() => {
        const bounds = wrapperRef.current?.getBoundingClientRect();
        const pos = screenToFlowPosition({
          x: (bounds?.left ?? 0) + (bounds?.width ?? 800) / 2,
          y: (bounds?.top ?? 0) + (bounds?.height ?? 600) / 2,
        });
        onAddIntent(pos);
      }}
    >
      ＋ 意图
    </button>
  </div>
)}
```

（`useReactFlow` 的 `screenToFlowPosition` 取自组件内既有 `useReactFlow()` 调用；若无则在组件顶部补 `const { screenToFlowPosition } = useReactFlow();`。`wrapperRef` 为画布外层 div 的既有 ref；无则补。）

4. intent 节点 `onNodeClick`/`onSelectionChange` 分支：`node.type === "intent"` → `onOpenIntentEditor(node.id.replace("intent:", ""))`（沿既有节点点击处理的 if 链追加）。

5. `onNodesDelete`/右键菜单对 intent 节点：回调 `onDeleteIntent(intentId)`（Task 8 实现=从图删意图连带绑定）；无 `canEditGraph` 时不进删除路径。

- [ ] **Step 2: 验证**

Run: `cd apps/web && npx tsc --noEmit && npm run build 2>&1 | tail -3`
Expected: 0 error，构建成功

- [ ] **Step 3: Commit**

```bash
git add apps/web/components/qa-canvas-view.tsx
git commit -m "feat(flow-graph): canvas intent nodes + binding edges + palette"
```

---

### Task 8: web 编辑器 — 意图模态 + 保存（PUT graph_json）+ 权限门

**Files:**
- Modify: `apps/web/app/(app)/qa/page.tsx`（graph state、编辑器模态、增删改 handler、`canEditGraph`）
- Modify: `apps/web/lib/api.ts`（若无泛化 updateTemplate 包装则已有 `updateTemplate`（~:255），直接复用，不新增）

**Interfaces:**
- Consumes: Task 6 `GraphDoc`/`parseGraphDoc`；Task 7 的四个回调 props；api.ts `updateTemplate(id, body)`；`session`（`useSession()` 既有，含 `role`/`user_id`）。
- Produces: `/qa` 画布完整可编辑——加意图（`int_`+8hex 客户端生成）、编辑关键词/步号 scope/绑定/优先级/once/enabled、删意图连带绑定、保存=PUT `{ graph_json }` 单字段（exclude_unset 语义）、乐观更新+失败回滚+错误 toast（沿 `connectCluster` 既有回滚模式）。

- [ ] **Step 1: 实现**

1. state（与既有 `templates/canned` 并列）：

```tsx
const [graphDoc, setGraphDoc] = useState<GraphDoc>({ version: 1, intents: [], bindings: [] });
const [editorIntentId, setEditorIntentId] = useState<string | null>(null); // null=关；""=新建草稿
const [draftPos, setDraftPos] = useState<{ x: number; y: number } | null>(null);
```

模板列表加载时对选中模板 `getTemplate(id)` 后 `setGraphDoc(parseGraphDoc(tpl.graph_json))`。

2. 权限（B3 口径：user 只改自己的）：

```tsx
const me = useSession();
const activeTemplate = templates.find((t) => t.id === selectedTemplateId) as
  | { id: string; owner_user_id?: string }
  | undefined;
const canEditGraph =
  !!activeTemplate &&
  (me.role !== "user" || (activeTemplate.owner_user_id ?? "") === me.user_id);
```

3. 保存（乐观+回滚，模式照 `connectCluster`）：

```tsx
const saveGraph = useCallback(
  async (next: GraphDoc) => {
    if (!selectedTemplateId) return;
    const prev = graphDoc;
    setGraphDoc(next); // 乐观
    try {
      await updateTemplate(selectedTemplateId, {
        graph_json: JSON.stringify(next),
      });
    } catch {
      setGraphDoc(prev); // 回滚
      alert("保存流程图失败，请重试");
    }
  },
  [selectedTemplateId, graphDoc],
);
```

4. 增/删 handler：

```tsx
const genId = (prefix: "int_" | "bnd_") =>
  prefix + Array.from(crypto.getRandomValues(new Uint8Array(4)))
    .map((b) => b.toString(16).padStart(2, "0")).join("");

const addIntent = useCallback(
  (pos: { x: number; y: number }) => {
    const intent: GraphIntent = { id: genId("int_"), label: "", keywords: [], steps: [], enabled: true };
    void saveGraph({ ...graphDoc, intents: [...graphDoc.intents, intent] });
    setDraftPos(pos);
    setEditorIntentId(intent.id);
  },
  [graphDoc, saveGraph],
);

const deleteIntent = useCallback(
  (intentId: string) => {
    void saveGraph({
      ...graphDoc,
      intents: graphDoc.intents.filter((i) => i.id !== intentId),
      bindings: graphDoc.bindings.filter((b) => b.intent !== intentId),
    });
  },
  [graphDoc, saveGraph],
);
```

（`addIntent` 的 `pos` 由 Task 7 调色盘传入；实现上该意图节点的自定义坐标落 localStorage 走既有 `onNodeDragStop` 链路，也可直接把 `{[`intent:${intent.id}`]: pos}` 并进 `positions` state——两选一，保持与既有拖拽持久化同源。）

5. 编辑器模态（`editorIntentId !== null` 时渲染，样式沿既有条目编辑模态）：受控表单字段=标签（input）/关键词（input，逗号分隔 ↔ `keywords` 数组）/生效步骤（「全程」checkbox + 步号多选 chips，1-based）/绑定列表（每行：动作 select `play_qa|jump_step`；play_qa→QA 条目 select（数据源=既有 rows）；jump_step→步号 select；优先级 number；once checkbox；enabled checkbox；删除行按钮）+「添加绑定」按钮 + 意图级 enabled toggle + 删除意图按钮。确认=组装该意图新值 → `saveGraph(替换后的 doc)` → 关模态。**新建草稿（`draftPos !== null`）且用户清空标签+关键词后点确认 = 视为放弃，回滚删掉该草稿意图。**

6. 画布接线：

```tsx
<QaCanvasView
  /* …既有 props… */
  graphDoc={graphDoc}
  canEditGraph={canEditGraph}
  onAddIntent={addIntent}
  onOpenIntentEditor={(id) => setEditorIntentId(id)}
  onDeleteIntent={deleteIntent}
/>
```

- [ ] **Step 2: 验证**

Run: `cd apps/web && npx tsc --noEmit && npm run build 2>&1 | tail -3`
Expected: 0 error，构建成功

- [ ] **Step 3: 手工冒烟（dev 栈）**

`python tools/bok.py serve` → `/qa` 画布：加意图（关键词「投诉」+绑定 jump 第 4 步）→ 保存 → 刷新页面图还在 → DB `conversation_templates.graph_json` 已落 → 发起模拟通话说「我要投诉」→ agent.log 见 `FLOW_GRAPH jump`。

- [ ] **Step 4: Commit**

```bash
git add "apps/web/app/(app)/qa/page.tsx"
git commit -m "feat(flow-graph): intent editor modal + graph save with optimistic rollback"
```

---

### Task 9: 探针 + 回归 + 文档 + PR

**Files:**
- Create: `scripts/probe_flow_graph.py`（骨架照 `scripts/probe_offscript_soak.py`：真栈、TTS 现场合成推流、哨兵+JSON 落盘 `reports/flow-graph/`）
- Modify: `AGENTS.md`（「话术分步推进」条目族内新增一条话术图条目）
- Modify: `docs/RUNTIME_TOPOLOGY.md`（数据流：模板 `graph_json` → 装配 → 每轮 graph 块）
- Modify: `docs/superpowers/specs/2026-09-17-qa-canvas-design.md` §12（Phase 2 行加「已由 2026-09-18-qa-flow-graph spec 升格为图执行引擎」注）

**Interfaces:**
- Consumes: Task 1-8 全部。
- Produces: 可重复的实弹验收 + 文档齐全 + PR。

- [ ] **Step 1: 探针脚本**

复用 `probe_offscript_soak.py` 的栈启动/TTS 推流/哨兵骨架（先读该脚本，import 或同款姿势复用其 helper），场景：

1. 经 CP API 建带图模板（zh，6 步，`graph_json` = 意图「投诉」→ jump_step 4 + 意图「退款」→ play_qa `<现存 QA 条目 id>`（若无带录音条目则该腿只记信息位））
2. 建模拟通话（该模板）→ 等开场白 → 推触发语音「我要投诉」→ 断言窗口内 agent.log 出现 `FLOW_GRAPH jump` 且后续 assistant turns 行 `provider=graph-jump`、`template_step=4`
3. 推非触发语音（如「好的好的」）→ 断言窗口内零新增 `FLOW_GRAPH` 行
4. `BOK_FLOW_GRAPH=0` 重启栈（探针 `--expect-off` 档，或文档注明手动 A/B 须重启 serve）→ 推同样触发语音 → 断言全程零 `FLOW_GRAPH`
5. 退出码：主判据=②③④全过；任何 `FLOW_GRAPH play_miss` 只记信息位（罐头未物化的明确降级路径）

- [ ] **Step 2: 全量验收**

```bash
.venv312/bin/python -m pytest tests/ -q 2>&1 | tail -3
.venv312/bin/python -m compileall -q apps packages services tools scripts
cd services/realtime-translation && npm test && cd ../..
cd apps/web && npx tsc --noEmit && npm run build && cd ../..
.venv312/bin/python scripts/e2e_trilingual_livekit.py   # 无图模板零回归（需栈与模型）
```

Expected: pytest 全绿（基线无回归）、compileall 零输出、npm test 过、web 构建过、E2E 绿。

- [ ] **Step 3: 文档**

- `AGENTS.md`「话术分步推进」条目后新增一条：话术图一句话语义（graph_json 契约、插点 precedence、jump 三件套、kill-switch、探针名、消费模块清单）——写法对齐该文件既有条目密度。
- `docs/RUNTIME_TOPOLOGY.md`：模板数据流加 `graph_json` 一行。
- Phase-1 spec §12 Phase 2 行加升格注。

- [ ] **Step 4: Commit + PR**

```bash
git add scripts/probe_flow_graph.py AGENTS.md docs/RUNTIME_TOPOLOGY.md docs/superpowers/specs/2026-09-17-qa-canvas-design.md
git commit -m "feat(flow-graph): live probe + docs (AGENTS/RUNTIME_TOPOLOGY/spec cross-ref)"
git push -u origin qa-flow-graph-phase2
gh pr create --fill --base main
```

CI 全绿后按仓库惯例本地合并（用户拍板过「按pr合并 绿了本地合到main」）。

---

## Self-Review

- Spec coverage：§3 契约→Task 1/2/3；§4 运行时→Task 4/5；§5 迁移→Task 2；§6 CP→Task 3；§7 web→Task 6/7/8；§8 探针→Task 9；§10 风险→各任务回退与守卫。无缺口。
- Placeholder scan：Task 5 的 frames 适配器函数名与 Task 7 的 Handle 姿势标注「以现行源码为准」——这是**对既有代码的对齐指令**而非空缺，两处均已给出期望形状与判断标准。
- Type consistency：`pick_graph_action`/`FlowGraphDoc`/`by_id`/`jump_to`/`parseGraphDoc`/`INTENT_X` 在 Task 1/4/5/6 间签名逐字一致；provider 值 `graph-jump`/`graph-play` 在 Task 5 与 spec §4.3、Task 9 断言一致。
