# 追问链 / 答后跳转（Phase 3.3）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `graph_json` 的 `play_qa` 绑定扩可选 `then_jump`（1-based 步号）——罐头**播完当场**跳步，下一轮按新步走；字段缺席时绑定行为与 Phase 2 逐字节同，整闸继承 `BOK_FLOW_GRAPH`。

**Architecture:** 一处数据（`GraphBinding.then_jump`，纯函数双轨：`validate_flow_graph` 严格 → CP 保存 400 / `parse_flow_graph` 宽容 → 坏值丢字段）、一处执行（`apps/agent/agent_runtime/agent.py` graph 播放分支在 `_qa_canned_say` 成功返回后同步跑 Phase-2 位移三件套，落点抽成 `FlowController.apply_then_jump()` 保可测）、一处运营面（web 绑定行数字框，可选画布虚线边默认关）、一条探针腿。**不加 DB 列、不加 env、不改 CP schema**（`then_jump` 全在 `graph_json` 字符串里）。设计契约见 [spec §3](../specs/2026-09-18-flow-graph-phase3.md)。

**Tech Stack:** Python 3.12 (SQLAlchemy/FastAPI 不动) / TS+React / pytest + node:test。

## Global Constraints

- **零变化铁律**：无 `then_jump` 的图（存量全部）解析结果、校验结论、播放行为逐字节同——`parse_flow_graph` 产物多一个 `None` 默认字段、`validate_flow_graph` 不新增错误、agent 播放分支多一个 `if` 且条件恒假。单测钉死。
- **双轨分工**：`validate_flow_graph`（严格，CP 保存路径）三态：play_qa 带 `then_jump` 非 int（含 bool/字符串/None）→ 错；int 越界 [1,999] → 错；`jump_step` 绑带该键 → 错。`parse_flow_graph`（宽容，运行时）**只丢字段不清退绑定**、绝不抛。
- **无新 kill-switch、无新 env**：整闸就是既有的 `os.environ.get("BOK_FLOW_GRAPH", "1") == "1"`（agent.py:3736-3741 图块闸），`then_jump` 与图同生共死。`bok.py` 的 `BOK_FLOW_GRAPH` 白名单透传（tools/bok.py:1065-1067 已泛化为 `_apply_bok_passthrough_env`）已就位，零新增。
- **记账纪律**：play 成功才烧 `graph_fired`（既有行为，agent.py:3791 不动）；`then_jump` **不另立账本条目**——它是同一绑定的动作后缀；跳转**实际位移才**打 `jump` 日志 / 置 `set_flow_current`，无位移（同位 / closing 冻结 / 钳到同位）走既有 `FLOW_GRAPH jump_noop` 词表且不消耗任何额外额度。
- 术语门禁：唯一合法拼写 `cantonese`；`tests/test_cantonese_terminology.py` 扫全仓。
- Conventional commits scope **`qa-chain`**；一逻辑变更一提交。
- 每任务收尾：该任务测试绿 + `python -m compileall -q apps packages`（web 任务 `npx tsc --noEmit`）。
- **执行环境**：工作树 `/Users/halo/Documents/bok/voice-assistant-worktrees/flow-graph-phase3`，分支自 `origin/main` 起 **`qa-chain-phase33`**（前置 3.2 已合）。解释器用**主树** `/Users/halo/Documents/bok/voice-assistant/.venv312/bin/python`（工作树无 venv，与 3.1/3.2 同款，conftest 隔离已哨兵验证）；所有路径用绝对路径。
- 探针/栈类验收前先 `ps aux | grep agent_runtime` 确认为 0（殭尸 worker 跑旧代码=A/B 结论污染，2026-09-09 实证）。

## 勘误预检（读码实证，2026-09-18 —— 与 spec 逐条核对，先记不偷改）

1. **播放分支的 `_invalidate_stale_preemptive()` 在请求流上打不到任何东西（非冲突，但语义要写清）**：钩子拿到的是 `temp_mutable_chat_ctx`（每轮副本，livekit-agents `agent_activity.py:2599-2613`），`except StopResponse: return` —— 副本连同标记一起丢弃。跳步分支（jump_step）因**本轮继续走 LLM**才有失效效果；播放分支在 trio 之后立即 `raise StopResponse()`（agent.py:3797），标记进不了任何请求也会随副本蒸发。**结论：trio 照 spec 调（零成本、将来分支不再 raise 时正确），但承重件是 `jump_to` + `set_flow_current()`（后者才是下一轮 KV 前缀与【跳转进入】尾部的来源）；不要把本路径的 marker 当「抢跑已失效」的保证读。**
2. **spec §3 的探针判据「play 轮后下一轮 `template_step == then_jump`」需要一条额外轮次**：播放轮本体行在跳**之前**落库（`_qa_canned_say` 内 `cp.add_turn(..., template_step=flow_ctrl.current + 1)`，agent.py:3724），所以跳后步号只能在**下一轮**的 turns 行上看到。故 then-jump 腿 = 两轮（`play` + `after`），且 after 轮话术必须停在 UNCLEAR——`_CONFIRM_RE`（flow.py:310-314）含单字 `好/是/对/嗯/系/係`，after 轮一旦被判 CONFIRM 规则推进会把步号变成 5。**因此硬判据取 spec 指令里的「或」：同轮 `jump … via=then_jump` 日志** · **或** after 轮步号命中（前者确定性、后者受 ASR 影响），两个分量各自进报告信息位。
3. **`step` 钳制 vs `then_jump` 丢弃的不对称是刻意的**：`step` 是 `jump_step` 的必填字段（解析期 `max(1, min(..., STEP_MAX))` 钳制，flow_graph.py:137），`then_jump` 是可选链（坏值丢弃 = 退回 Phase 2 行为，最保守，且绝不把运营写的越界目标静默改成别的步）。若评审要求改为钳制，只动 `_then_jump_of()` 一处，其余契约（validate 严格、probe 判据、web 钳制）全不动。
4. **web 编辑器保存是「逐字段重建」而非整体透传**（page.tsx:1347-1366，`common` 只含 id/intent/priority/once/enabled）——**任何不进 `BindingDraft` 的字段编辑一次就蒸发**。故 T3 的 `then_jump` 必须进草稿类型，否则「打开既有追问链→改个名字保存」等于静默删链。
5. **`evaluate_leg` 现有图开启腿判据含 `nontrigger_silent`**（probe_flow_graph.py:307），then-jump 腿没有非触发轮 → 必须走独立 `elif` 判据分支，否则结构性 FAIL。
6. **无 Supabase/DB 改动**：`then_jump` 不落列（spec §3 否决项），spec §6.4「Supabase 列直连 apply」本增量不适用（与 3.2 同款）。
7. **T4 离线面实施注记（2026-09-18，真栈两腿未跑=控制器合并后执行）**：① then-jump 档轮次表**不含** `trigger`/`nontrigger` → `plan_rounds` 在该档忽略 `play_round`（「播」轮就是触发轮；`--no-play-round` 与之组合会零触发语空跑，已单测钉死）；② `evaluate_leg` 的 `all_events` **纳入 `after_events`**，故 kill 腿 absence 扫描覆盖跳后窗口（after 轮冒 `FLOW_GRAPH` 行同属越闸，已有专测）；③ 腿跳过判定放在 `run_leg` 建探针模板**之前**（无 QA 条目时连模板都不建），报告落 `{"skipped": "no_qa_or_audio"}` 且退出码 1；④ 离线测试落在独立文件 `tests/test_flow_graph_probe_then_jump.py`（协调口径，brief Step 1 原写「追加到 `tests/test_flow_graph_probe.py`」），另加一例对着 `agent_runtime.flow` 真 regex 逐族钉 `AFTER_TEXT` 的洁净性（默认话术不含确认/收线/异议/提问/挂断七族词与图触发词）；⑤ `print_leg` 窗口行按事件键存在性渲染（默认档仍 trigger/nontrigger/play 三行不变），归因关键词 then-jump 档切 `PLAY_KEYWORDS`。
8. **勘误 #8（2026-09-18 T4 fix-wave，review R1 Important）：「无 QA 条目/未物化 → 显式跳过」与实现不符，已真值化**——跳过只在**无 QA 条目**时发生（`plan_rounds` 的 `has_qa = bool(qa_id)`；无条目则触发语必然 `play_miss`，腿结构性空跑）；**条目在场但音频未物化时腿照跑**，运行时落 `FLOW_GRAPH play_miss`，`play_logged` 硬 FAIL、退出码 1（先 `tts-pregen --qa` 再来）。已同步修正三处文档表述：探针模块 docstring、`run_leg` 跳过打印 + `main` 跳过分支注释、`AGENTS.md`「话术图」条；报告键 `no_qa_or_audio` 保留为历史 token（语义收窄为「无条目」，不新增含义；新增键会破报告 schema 稳定性）。同波并入 review minors：M2（after 窗口 absence 面同受 `evidence_ok` 闸的补测）、M3（`post_jump_step_seen` 无假正例不变量入 docstring）、M4（`next_turn_step=False` 归因注释）、M5（`print_leg` 只渲染本腿真跑过的轮次）、M7（「四族词」→「七族词」，实为 `_CONFIRM`/`_QUESTION`/`_DEFER`/`_REFUSE`/`_FAREWELL`/`_DENY`/`_HANGUP` 七族）。
9. **勘误 #9（2026-09-18 final-review wave，T2-R1 裁定入档）：then_jump 位移分支不渲染当前步（跳时渲染=死+有害，R1 已删）**——原实现在 `apply_then_jump` 返 True 时调 `context_state.set_flow_current(flow_ctrl.current_step_text())`，R1 判定该调用两重错：①**死**——本分支紧接着 `raise StopResponse()`，本轮结构性无 `chat` 调用，`render_context_tail()` 永不跑，`set_flow_current` 只把一段无人消费的尾部写进 `ContextState`；②**有害**——它把 `_last_render_step`/`_just_advanced` 烧在**未被请求消费**的目标步上，于是**下一轮**流程块重渲染判定「已渲染过」→ 退成分支模式，目标步底稿（正稿）与【跳转进入】标记结构性失落。离线段实测（同调用序列，T2 报告 §4/§7.2）：带渲染行时下一轮尾部 225 字且底稿 False/【跳转进入】False；删除后 327 字两者皆 True。**修复=删除该行**，跳转承重件收敛为 `jump_to` 置的私有位移状态（`current`/`_entered_by_jump`）+ `_invalidate_stale_preemptive` spec marker（marker 随本轮 `turn_ctx` 副本蒸发，照调但**勿当抢跑失效保证读**）+ `FLOW_GRAPH jump … via=then_jump`/`jump_noop` 日志；渲染唯一落点=下一轮流程块首渲染。源级钉住同步**反转**：`tests/test_flow_graph_then_jump.py::test_agent_play_branch_wires_then_jump_before_stop_response` 由 `assert "context_state.set_flow_current(" in seg` 改为 `assert "current_step_text()" not in seg`（docstring 写明「位移同轮、渲染推迟」）。**Task 2 Step 3 的历史代码块保留原样**（历史不复写），仅在其顶加「勘误 #9」行标注；本文件 Task 4 Step 1 注释与 Step 4 实施项 4 两处「无 QA 条目或未物化 → 跳过」的陈旧表述已按勘误 #8 真值化改写。

---

### Task 1: 核心 — `then_jump` 字段（解析宽容 + 校验严格）

**Files:**
- Modify: `packages/core/bok_voice_core/flow_graph.py`（`GraphBinding` 61-70 加字段；`_parse_binding` 122-141 接线；新增模块级 `_then_jump_of()`；`validate_flow_graph` 绑定循环 256-261 之后插严格规则）
- Test: `tests/test_flow_graph_then_jump.py`（新文件，T2 追加同一文件）

**Interfaces（T2/T3/T4 依赖，逐字）:**
- Produces: `GraphBinding.then_jump: int | None = None`（字段追加在 `once` 之后、`enabled` 之前或之后皆可——全仓唯一构造点是 `_parse_binding` 且全关键字，flow_graph.py:132）。
- Produces: `_then_jump_of(raw: dict, action: str) -> int | None`（模块私有；仅 `ACTION_PLAY_QA` 收，非 int/bool 或越界 [1,999] → `None`）。
- Produces（语义）：`validate_flow_graph` 对 `action == ACTION_JUMP_STEP` 且 `"then_jump" in item` → 追加错误 `bindings[i].then_jump is only valid for action=play_qa`；对 `action == ACTION_PLAY_QA` 且键在场 → 非 int（含 bool）或越界 → 追加 `bindings[i].then_jump must be int in [1,999]`。

- [ ] **Step 1: 失败测试**（新文件 `tests/test_flow_graph_then_jump.py`）

```python
"""追问链（Phase 3.3）：`then_jump` 解析宽容 / 校验严格（spec §3）。"""
from __future__ import annotations

import json

from bok_voice_core.flow_graph import STEP_MAX, parse_flow_graph, validate_flow_graph

_INTENT = {"id": "int_2b3c4d5e", "label": "退款", "keywords": ["退款"], "steps": [], "enabled": True}


def _doc(**binding_over: object) -> str:
    binding: dict = {
        "id": "bnd_c1d2e3f4",
        "intent": "int_2b3c4d5e",
        "action": "play_qa",
        "qa_id": "qa-1",
        "priority": 10,
        "once": False,
        "enabled": True,
    }
    binding.update(binding_over)
    return json.dumps({"version": 1, "intents": [_INTENT], "bindings": [binding]}, ensure_ascii=False)


def test_parse_then_jump_roundtrip_and_absent_default():
    assert parse_flow_graph(_doc(then_jump=4)).bindings[0].then_jump == 4
    assert parse_flow_graph(_doc(then_jump=STEP_MAX)).bindings[0].then_jump == STEP_MAX
    # 缺席 = None（存量图逐字节不变；旧断言面不受影响）
    assert parse_flow_graph(_doc()).bindings[0].then_jump is None


def test_parse_then_jump_tolerant_drops_bad_value_keeps_binding():
    """坏值只丢字段、绑定本体照活（宽容契约：绝不整条丢弃、绝不抛、绝不隐式转字符串）。"""
    for bad in (0, -1, STEP_MAX + 1, "4", True, False, None, 4.5, [4], {"step": 4}):
        doc = parse_flow_graph(_doc(then_jump=bad))
        assert len(doc.bindings) == 1, bad
        assert doc.bindings[0].then_jump is None, bad
        assert doc.bindings[0].qa_id == "qa-1", bad


def test_parse_then_jump_ignored_on_jump_step_binding():
    """宽容面：jump_step 绑带上带该键 → 解析期直接忽略（严格面才报错）。"""
    doc = parse_flow_graph(_doc(action="jump_step", step=4, then_jump=4))
    assert doc.bindings[0].action == "jump_step"
    assert doc.bindings[0].then_jump is None


def test_binding_without_then_jump_unchanged():
    """零变化：无链字段的绑定各字段照旧（含 step 钳制/priority 默认/once 默认）。"""
    b = parse_flow_graph(_doc()).bindings[0]
    assert (b.id, b.intent, b.action, b.qa_id, b.step, b.priority, b.once, b.enabled) == (
        "bnd_c1d2e3f4", "int_2b3c4d5e", "play_qa", "qa-1", 0, 10, False, True,
    )


def test_validate_then_jump_accepted_on_play_qa():
    assert validate_flow_graph(_doc()) == []            # 缺省合法（零变化）
    assert validate_flow_graph(_doc(then_jump=1)) == []
    assert validate_flow_graph(_doc(then_jump=4)) == []
    assert validate_flow_graph(_doc(then_jump=STEP_MAX)) == []


def test_validate_then_jump_range_and_type():
    for bad in (0, -3, STEP_MAX + 1, "4", True, False, None, 4.5, [4]):
        errs = validate_flow_graph(_doc(then_jump=bad))
        assert any("then_jump" in e for e in errs), bad


def test_validate_then_jump_rejected_on_jump_step():
    errs = validate_flow_graph(_doc(action="jump_step", step=4, then_jump=4))
    assert any("then_jump" in e and "jump_step" in e for e in errs), errs
    # 连带对照：step 合法只报 then_jump 一条，不叠无关错
    assert len([e for e in errs if "then_jump" in e]) == 1
```

- [ ] **Step 2: 红** — Run: `<主树>/.venv312/bin/python -m pytest tests/test_flow_graph_then_jump.py -v` → FAIL（dataclass 无字段 / validate 不报错）
- [ ] **Step 3: 实现** — flow_graph.py：

```python
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
```

`_parse_binding` 的返回体在 `priority=` 之前插 `then_jump=_then_jump_of(raw, action),`；`validate_flow_graph` 在 `action == ACTION_PLAY_QA and not str(item.get("qa_id") ...)` 那条之后插：

```python
        # Phase 3.3 追问链:then_jump 仅 play_qa 合法;[1,999] 闭区间;非 int(含 bool)拒。
        if action in (ACTION_PLAY_QA, ACTION_JUMP_STEP) and "then_jump" in item:
            if action != ACTION_PLAY_QA:
                errors.append(f"bindings[{idx}].then_jump is only valid for action=play_qa")
            else:
                _tj = item["then_jump"]
                if isinstance(_tj, bool) or not isinstance(_tj, int) or not (1 <= _tj <= STEP_MAX):
                    errors.append(f"bindings[{idx}].then_jump must be int in [1,{STEP_MAX}]")
```

- [ ] **Step 4: 绿 + 零回归** — Run: `pytest tests/test_flow_graph_then_jump.py tests/test_flow_graph_core.py tests/test_template_graph_field.py -v`（**`test_flow_graph_core.py` 现有 13 例零改动全绿=零变化基线**）+ `python -m compileall -q packages apps`
- [ ] **Step 5: Commit** — `feat(qa-chain): graph binding then_jump — tolerant parse (drop bad) + strict validate (play_qa only, [1,999])`

---

### Task 2: 运行时 — 播放成功后同步跳（位移三件套 + 记账纪律）

**Files:**
- Modify: `apps/agent/agent_runtime/flow.py`（`FlowController` 加 `apply_then_jump()`，紧邻 `jump_to`（flow.py:952-964）之后）
- Modify: `apps/agent/agent_runtime/agent.py`（graph 播放分支 3779-3802：`graph_fired.add` + `FLOW_GRAPH play` 打印之后、`raise StopResponse()` 之前插跳转块）
- Test: `tests/test_flow_graph_then_jump.py`（追加；含源级接线钉住，姿势同 `tests/test_flow_graph_runtime.py:86-99`）

**Interfaces（T4/审查依赖，逐字）:**
- Consumes: T1 的 `GraphBinding.then_jump: int | None`。
- Produces: `FlowController.apply_then_jump(then_jump_1based: int | None) -> bool` —— `None`/无步骤/closing/同位 → `False` 且**零副作用**；实际位移 → `True`（`jump_to(then_jump - 1)` 内部置 `current`/`_just_advanced`/`_entered_by_jump`）。
- Produces（日志词表，**新增 `via=` 修饰词、不新增 kind**，故 `RE_FLOW_GRAPH`(probe 95) 不动）：
  - 位移：`FLOW_GRAPH jump binding=<id> step=<N> via=then_jump`
  - 无位移：`FLOW_GRAPH jump_noop binding=<id> step=<目标 + 1> via=then_jump`
- 明确不做：`jump_step` 分支（3713-3741）一行不改（它有同款内联位移判定，重构=风险无收益）；播放分支**不置** `_flow_step_before = -1` 哨兵——本分支以 `StopResponse` 收尾，QA 快路块（3772+）结构性到不了。

- [ ] **Step 1: 失败测试**（追加到 `tests/test_flow_graph_then_jump.py`）

```python
import json
from pathlib import Path

from agent_runtime.flow import FlowController

_TEMPLATE = {
    "steps_json": json.dumps(
        [{"goal": f"第{i}步", "ref": f"第{i}步说法"} for i in range(1, 7)], ensure_ascii=False
    ),
    "graph_json": json.dumps(
        {
            "version": 1,
            "intents": [{"id": "int_2b3c4d5e", "label": "退款", "keywords": ["退款"],
                         "steps": [], "enabled": True}],
            "bindings": [{"id": "bnd_c1d2e3f4", "intent": "int_2b3c4d5e", "action": "play_qa",
                          "qa_id": "qa-1", "then_jump": 4, "priority": 10, "once": False,
                          "enabled": True}],
        },
        ensure_ascii=False,
    ),
}


def test_apply_then_jump_moves_and_marks_entry():
    fc = FlowController.from_template(_TEMPLATE, None)
    assert fc.graph.bindings[0].then_jump == 4     # 解析面先过（T1 契约）
    assert fc.apply_then_jump(4) is True           # 实际位移
    assert fc.current == 3                         # 1-based 4 → 0-based 3
    assert fc._entered_by_jump is True             # 走 jump_to → 尾部【跳转进入】(I3)
    assert "跳转进入" in fc.current_step_text()


def test_apply_then_jump_noop_family_zero_side_effect():
    fc = FlowController.from_template(_TEMPLATE, None)
    assert fc.apply_then_jump(None) is False and fc.current == 0 and fc._entered_by_jump is False
    assert fc.apply_then_jump(1) is False and fc.current == 0   # 同位 no-op
    assert fc._entered_by_jump is False and fc._just_advanced is False
    fc.enter_closing()
    assert fc.apply_then_jump(4) is False and fc.current == 0   # closing 冻结
    empty = FlowController.from_template({"steps_json": "[]"}, None)
    assert empty.apply_then_jump(4) is False                    # 无步骤话术


def test_apply_then_jump_clamps_to_done():
    fc = FlowController.from_template(_TEMPLATE, None)
    assert fc.apply_then_jump(99) is True   # 越界钳到 len(steps)（jump_to 内既有钳制）
    assert fc.current == 6 and fc.done


def test_agent_play_branch_wires_then_jump_before_stop_response():
    """源级钉住（播放分支在 entrypoint 闭包内，离线起不了真栈；姿势同 I1 装配面测试）：
    play 成功 → 位移三件套必须在 **本轮收尾 raise** 之前（同一轮内同步跳）。"""
    src = (Path(__file__).resolve().parents[1] / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    start = src.index("flow_ctrl.apply_then_jump(")
    stop = src.index("raise StopResponse()", start)   # 播放分支收尾 raise（注释无关锚）
    seg = src[start:stop]
    assert "_gbinding.then_jump" in src
    assert "_invalidate_stale_preemptive(" in seg
    assert "context_state.set_flow_current(" in seg
    assert "via=then_jump" in src
    assert "FLOW_GRAPH jump_noop" in seg   # 无位移档不吞日志
```

- [ ] **Step 2: 红** — FAIL（`apply_then_jump` 不存在 / agent 无接线）
- [ ] **Step 3: 实现** —

flow.py（`jump_to` 之后）：

```python
    def apply_then_jump(self, then_jump_1based: int | None) -> bool:
        """play_qa 绑定的答后跳转(spec Phase 3.3 §3):播完当场跳到 then_jump 步。

        返回**是否实际位移**——调用方只在实际位移时打 `FLOW_GRAPH jump` 日志 / 置
        `set_flow_current()` / 宣告 provider（未位移=零副作用，与 jump_step 分支
        「未位移不烧 once」同纪律）。None/无步骤/closing/同位/越界钳到同位 全返 False。
        """
        if then_jump_1based is None:
            return False
        before = self.current
        self.jump_to(int(then_jump_1based) - 1)   # 1-based → 0-based；钳制/closing 冻结在 jump_to 内
        return self.current != before
```

agent.py 播放分支（把 3779-3802 的后半段替换为）：

> **勘误 #9**：本块渲染行 `context_state.set_flow_current(flow_ctrl.current_step_text())` 已在 R1 删除（跳时渲染=死+有害，见勘误 #9）；下块保留为历史原文，勿照抄。

```python
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
                        # ---- 追问链（Phase 3.3，spec §3）：罐头播完当场同步跳，下一轮
                        # 按新步走。复用 Phase 2 位移三件套与记账纪律：实际位移才置
                        # 上下文/打 jump 日志；同位/closing/钳到同位 → jump_noop 且零额外
                        # 消耗（then_jump 是同一绑定的动作后缀，不另立 once 账本条目）。
                        # 注：本分支以 StopResponse 收尾，_invalidate_stale_preemptive 的
                        # 标记随本轮 turn_ctx 副本蒸发（承重件是 set_flow_current——下一轮
                        # KV 前缀与【跳转进入】尾部的来源）；照 spec 调用，零成本。
                        if _gbinding.then_jump:
                            _tj_target = int(_gbinding.then_jump) - 1
                            if flow_ctrl.apply_then_jump(_gbinding.then_jump):
                                _invalidate_stale_preemptive(
                                    f"流程跳转 → 第 {flow_ctrl.current + 1} 步"
                                )
                                context_state.set_flow_current(flow_ctrl.current_step_text())
                                print(
                                    f"FLOW_GRAPH jump binding={_gbinding.id} "
                                    f"step={flow_ctrl.current + 1} via=then_jump",
                                    flush=True,
                                )
                            else:
                                print(
                                    f"FLOW_GRAPH jump_noop binding={_gbinding.id} "
                                    f"step={_tj_target + 1} via=then_jump",
                                    flush=True,
                                )
                        raise StopResponse()  # 压掉本轮 LLM(WA 累积同款)
```

- [ ] **Step 4: 绿 + 零回归** — Run: `pytest tests/test_flow_graph_then_jump.py tests/test_flow_graph_core.py tests/test_flow_graph_runtime.py tests/test_flow_controller.py -v` 然后**全量** `pytest tests -q`（当前基线（3.2 后 1477+ 与 3.3 新增）全绿，不写死数字）+ `python -m compileall -q apps packages services scripts`
- [ ] **Step 5: Commit** — `feat(qa-chain): play_qa then_jump — same-turn jump after canned playback (displacement-accounted)`

---

### Task 3: web — 绑定行「播完后跳到第 N 步」+（可选）画布虚线边

**Files:**
- Modify: `apps/web/lib/qa-canvas.ts`（`GraphBinding` 57-66 加 `then_jump?: number`；239-259 绑定边派生处加**默认关**的第二条边）
- Modify: `apps/web/app/(app)/qa/page.tsx`（`BindingDraft` 172-180 + `bindingToDraft` 182-193 + `addBinding` 1308-1321 + `submit` 1347-1366 + 绑定行 UI 1508-1537 + 保存前约束注释 98-102）
- Modify（仅可选边开启时需要）: `apps/web/components/qa-canvas-view.tsx`（245-261 绑定边样式分支加 `deletable` 收窄）
- Test: `cd apps/web && npx tsc --noEmit && npm test && npm run build`（现有 `test/qa-canvas.test.mjs` 全绿=默认关的证明）

**Interfaces（逐字）:**
- Consumes: T1 的字段 + CP 严格校验（play_qa 专属、`[1,999]`）。
- Produces: `GraphBinding.then_jump?: number`（可选；缺省=无链）。
- Produces（草稿契约）：`BindingDraft.then_jump: number`，**`0` = 不跳**；保存时仅在 `>0` 且 `action === "play_qa"` 时写键，值 `clampInt(row.then_jump, 1, Math.max(stepCount, 1))`——`jump_step` 行**绝不写该键**（写了 CP 必 400，勘误预检 4 之外的第二个必 400 陷阱）。
- Produces（可选边，**默认关** = 零视觉零行为变化）：模块级 `const THEN_JUMP_EDGE_ENABLED = false;`；开启时 id `thenjump:<bindingId>`、`source: intent:<intent>`、`target: step:<then_jump-1 钳制>`、`data: { kind: "binding" }`、`label: 播完→第N步`。删除面安全：`bindingIdOfEdge("thenjump:x")` 返回 `""` → `removeBinding` 走 no-op（page.tsx:148-151 + 810-815），且 `deletable: false` 时连右键菜单都不进；意图删除时该边随 `dropIntentFromDoc` 的绑定一起消失（派生式，无残留）。

- [ ] **Step 1: 实现（类型 + 草稿）** — `qa-canvas.ts`：

```ts
export interface GraphBinding {
  id: string;
  intent: string;
  action: "play_qa" | "jump_step";
  qa_id?: string;
  step?: number;
  /** Phase 3.3 追问链：仅 play_qa；1-based 步号，播完罐头当场跳到该步；缺省=无链。 */
  then_jump?: number;
  priority: number;
  once: boolean;
  enabled: boolean;
}
```

`page.tsx` 草稿三处：

```ts
type BindingDraft = {
  id: string;
  action: "play_qa" | "jump_step";
  qa_id: string;
  step: number;
  then_jump: number;   // 0=不跳（仅 play_qa 有意义；jump_step 行恒 0）
  priority: number;
  once: boolean;
  enabled: boolean;
};

function bindingToDraft(b: GraphBinding, stepCount: number): BindingDraft {
  const action = b.action === "jump_step" ? "jump_step" : "play_qa";
  return {
    id: b.id,
    action,
    qa_id: String(b.qa_id ?? ""),
    step: clampInt(Number(b.step ?? 1), 1, Math.max(stepCount, 1)),
    // 追问链（Phase 3.3）：0=不跳；越界按当前真实步数收口（同 step 的 M28 姿势）。
    then_jump: clampInt(Number(b.then_jump ?? 0), 0, Math.max(stepCount, 1)),
    priority: clampInt(Number(b.priority ?? 10), 0, 1000),
    once: b.once === true,
    enabled: b.enabled !== false,
  };
}
```

`addBinding` 新增行加 `then_jump: 0`；`submit` 的 play_qa 分支：

```ts
      if (row.action === "play_qa") {
        if (!qaRows.some((q) => String(q.id) === row.qa_id)) {
          return setError(`第 ${i + 1} 条绑定还没有选择要播的快答条目。`);
        }
        // 追问链只在 >0 时写键：0/jump_step 行绝不带 then_jump（CP 严格校验会 400）。
        const chain = row.then_jump > 0
          ? { then_jump: clampInt(row.then_jump, 1, Math.max(stepCount, 1)) }
          : {};
        nextBindings.push({ ...common, action: "play_qa", qa_id: row.qa_id, ...chain });
      } else {
```

- [ ] **Step 2: 实现（UI 行）** — play_qa 且 `stepCount >= 1` 时，在快答下拉之后加一个数字框（受控、`min=1 max=stepCount`、空/NaN 落 0）：

```tsx
                      <label className="flex items-center gap-1 text-[11px] muted">
                        播完后跳到
                        <input
                          type="number"
                          min={1}
                          max={stepCount}
                          placeholder="不跳"
                          className="input w-16 px-1.5 py-0.5 text-xs"
                          value={row.then_jump === 0 ? "" : row.then_jump}
                          disabled={readOnly}
                          onChange={(e) => {
                            const n = Number(e.target.value);
                            patchRow(row.id, { then_jump: Number.isFinite(n) ? n : 0 });
                          }}
                        />
                        步
                      </label>
```

随之把 98-102 的前端约束注释补一行（`then_jump` 仅 play_qa、[1,999]、jump_step 带则 CP 400）——注释是契约面，别只写进 UI 提示。

- [ ] **Step 3（可选 stretch，默认关）** — `qa-canvas.ts` 绑定边循环里追加（`THEN_JUMP_EDGE_ENABLED` 为 `false` 时整段不执行，`deriveGraph` 输出与今逐字节同，`test/qa-canvas.test.mjs` 的边缘数量断言不动）：

```ts
// 追问链第二边（可选展示，默认关）：play_qa 播完 → 跳到第 N 步。id 前缀 thenjump:
// 令 bindingIdOfEdge 返 ""（删除路径安全 no-op）；开启时须把 qa-canvas-view 的
// 绑定边样式分支加 `deletable: canEditGraph && !String(e.id).startsWith("thenjump:")`
// ——链在绑定行里改，展示边不承担删除语义。
const THEN_JUMP_EDGE_ENABLED = false;
```

- [ ] **Step 4: 验证** — `cd apps/web && npx tsc --noEmit && npm test && npm run build` 全净；dev 冒烟（`npx next dev` 或节点 UI）：给一条 play_qa 绑定填「播完后跳到 4」→ 保存 → 刷新仍在 → 清空 → 保存 → 检查库里 `graph_json` **无 `then_jump` 键**；再打开一条**他人已配链的图**改名保存 → 链仍在（勘误预检 4 的验收）。
- [ ] **Step 5: Commit** — `feat(qa-chain): web binding editor — 播完后跳到第 N 步 field (+ optional canvas edge, default off)`

---

### Task 4: 探针腿 + AGENTS.md + 全量验收 + PR

**Files:**
- Modify: `scripts/probe_flow_graph.py`（`build_graph_json` 332-368 加 `then_jump` 形参；新增纯函数 `plan_rounds` / `post_jump_step_seen`；`evaluate_leg` 260-319 加 then-jump 判据分支；`run_leg` 457 + `_run_leg_with_stack` 480-664 接轮次表与 after 窗口；`print_leg` 667-697 打新信息位；`selftest` 703-785 补正反例；`main` 789-829 加 `--then-jump`/`--after-text`）
- Modify: `AGENTS.md`（「话术图」条（line 52）追加 3.3 半句：字段/执行点/记账纪律/探针腿/继承 kill-switch）
- Test: `tests/test_flow_graph_probe.py`（追加纯函数腿）

**Interfaces（逐字）:**
- `build_graph_json(qa_id: str, *, then_jump: int | None = None) -> str` —— 默认 `None` 时输出与今逐字节同（旧腿/旧断言零变化）；非空时给 `play_qa` 绑定加 `"then_jump": N`。
- `plan_rounds(*, then_jump, has_qa, trigger_text, nontrigger_text, play_text, after_text, play_round) -> list[tuple[str, str]]` —— `then_jump` 档：`has_qa` 真 → `[("play", play_text), ("after", after_text)]`，假 → `[]`（腿跳过）；默认档：`trigger`/`nontrigger`（+ `play` 信息位轮）。
- `post_jump_step_seen(turns: list[dict], then_jump: int) -> bool` —— 存在 `template_step == then_jump` 且 `provider != "graph-play"` 的转写行（graph-play 本体行在跳前落库，恒不算）。
- `evaluate_leg(..., then_jump: int | None = None, ..., after_events: list[dict] | None = None)` —— 新分支：
  - `play_logged`（硬）：`play` 窗口内有 `kind=="play"`（`play_miss` → FAIL，本腿主判据就是「播+跳」）。
  - `then_jump_effective`（硬）：同轮 `jump` 且 `step == then_jump` 且 `via == "then_jump"` **或** `post_jump_step_seen`（勘误预检 2 的 OR 语义）。
  - 信息位：`info["then_jump_logged"]` / `info["next_turn_step"]`；kill 腿（`expect_off=True`）判据面**不变**（零 `FLOW_GRAPH` 行 + 零 graph 轮，观测前提 `evidence.ok` 照闸）。
  - 默认档（`then_jump is None`）判据面逐字节同（不加任何 check，`set(checks)` 单测钉死）。

- [ ] **Step 1: 失败测试**（追加到 `tests/test_flow_graph_probe.py`；`_ev`/`_turns`/`_JUMP`/`_EV_OK` 已有）

```python
def test_plan_rounds_then_jump_leg_and_default_zero_change():
    kw = dict(trigger_text="我要投诉", nontrigger_text="好的好的", play_text="我要退款",
              after_text="我知道了，你说")
    assert pfg.plan_rounds(then_jump=4, has_qa=True, play_round=True, **kw) == [
        ("play", "我要退款"), ("after", "我知道了，你说")]
    # 无 QA 条目 → 空表：腿跳过必须显式，不许以「没跑出东西」空过成 PASS
    # （勘误 #8 真值化：音频未物化不在此列——条目在场则腿照跑，play_miss 由 play_logged 硬 FAIL，先 tts-pregen --qa）
    assert pfg.plan_rounds(then_jump=4, has_qa=False, play_round=True, **kw) == []
    # 默认档（无 then_jump）逐字节同旧
    assert pfg.plan_rounds(then_jump=None, has_qa=True, play_round=True, **kw) == [
        ("trigger", "我要投诉"), ("nontrigger", "好的好的"), ("play", "我要退款")]
    assert pfg.plan_rounds(then_jump=None, has_qa=False, play_round=True, **kw) == [
        ("trigger", "我要投诉"), ("nontrigger", "好的好的")]


def test_post_jump_step_seen_excludes_play_rows():
    play_row = {"role": "assistant", "provider": "graph-play", "template_step": 2, "transcript": "罐头"}
    next_row = {"role": "assistant", "provider": "", "template_step": 4, "transcript": "好的"}
    assert pfg.post_jump_step_seen([play_row, next_row], 4) is True
    assert pfg.post_jump_step_seen([next_row], 4) is True
    # 只有播放轮本体（步号=跳前步）→ 不算；graph-play 行恒不计入（哪怕步号撞上）
    assert pfg.post_jump_step_seen([play_row], 4) is False
    assert pfg.post_jump_step_seen([{**play_row, "template_step": 4}], 4) is False


def test_evaluate_leg_then_jump_hard_checks():
    play_ms = {"kind": "play", "binding": "bnd_c1d2e3f4", "qa": "qa-1"}
    jmp = {"kind": "jump", "binding": "bnd_c1d2e3f4", "step": "4", "via": "then_jump"}
    kw = dict(expect_off=False, target_step=4, then_jump=4, trigger_events=[],
              nontrigger_events=[], after_events=[], evidence=_EV_OK)

    by_log = pfg.evaluate_leg(play_events=[play_ms, jmp], turns=_turns("graph-play", 2), **kw)
    assert by_log["pass"] is True
    assert by_log["checks"]["then_jump_effective"] is True
    assert by_log["info"]["then_jump_logged"] is True and by_log["info"]["next_turn_step"] is False

    by_next = pfg.evaluate_leg(play_events=[play_ms], turns=_turns("", 4), **kw)
    assert by_next["pass"] is True and by_next["info"]["next_turn_step"] is True

    # play_miss（罐头未物化）→ play_logged 硬 FAIL：本腿主判据就是「播+跳」
    miss = pfg.evaluate_leg(play_events=[{"kind": "play_miss", "binding": "bnd_c1d2e3f4"}],
                            turns=_turns("graph-play", 2), **kw)
    assert miss["pass"] is False and miss["checks"]["play_logged"] is False

    # 播了但没跳（无日志、下一轮也没新步号）→ then_jump_effective FAIL
    dead = pfg.evaluate_leg(play_events=[play_ms], turns=_turns("graph-play", 2), **kw)
    assert dead["pass"] is False and dead["checks"]["then_jump_effective"] is False


def test_evaluate_leg_then_jump_kill_leg_and_default_shape():
    # kill 腿判据面不变（仍 absence-based + evidence 前提）
    clean = pfg.evaluate_leg(expect_off=True, target_step=4, then_jump=4,
                             trigger_events=[], nontrigger_events=[], play_events=[],
                             after_events=[], turns=[], evidence=_EV_OK)
    assert clean["pass"] is True
    assert set(clean["checks"]) == {"evidence_ok", "killswitch_no_logs", "killswitch_no_graph_turns"}
    dirty = pfg.evaluate_leg(expect_off=True, target_step=4, then_jump=4,
                             trigger_events=[], nontrigger_events=[],
                             play_events=[{"kind": "play", "binding": "b"}],
                             after_events=[], turns=[], evidence=_EV_OK)
    assert dirty["checks"]["killswitch_no_logs"] is False
    # 默认档（then_jump=None）判据面逐字节同旧
    plain = pfg.evaluate_leg(expect_off=False, target_step=4, trigger_events=[_JUMP],
                             nontrigger_events=[], play_events=[],
                             turns=_turns("graph-jump", 4), evidence=_EV_OK)
    assert set(plain["checks"]) == {"evidence_ok", "jump_logged", "trigger_turn_provider",
                                    "nontrigger_silent"}


def test_build_graph_json_then_jump_binding():
    with_tj = json.loads(pfg.build_graph_json("qa-1", then_jump=4))
    play = next(b for b in with_tj["bindings"] if b["action"] == "play_qa")
    assert play["then_jump"] == 4
    # 默认调用（旧签名）一个 then_jump 键都不带 → 旧腿/旧断言零变化
    assert all("then_jump" not in b for b in json.loads(pfg.build_graph_json("qa-1"))["bindings"])
```

- [ ] **Step 2: 红 → Step 3: 实现** — probe 五处改动：
  1. `build_graph_json(..., then_jump=None)`：`bindings.append({... "qa_id": qa_id, **({"then_jump": int(then_jump)} if then_jump else {}), ...})`。
  2. `plan_rounds` + `post_jump_step_seen` 两个纯函数（放纯函数区，`--selftest` 可直测）。
  3. `evaluate_leg` 新形参 + `elif then_jump is not None:` 分支（`all_events` 纳入 `after_events`）。
  4. `_run_leg_with_stack`：轮次表改走 `plan_rounds(...)`；`name_to_window` 按轮次名通用化（新增 `after` 窗口）；`after_events` 传入 `evaluate_leg`；**`then_jump` 腿且 `plan_rounds` 为空时**（勘误 #8 真值化：只在**无 QA 条目**时；条目在场但音频未物化走腿本体、`play_miss` 硬 FAIL）打印 `[flow-graph] then-jump 腿跳过：无 QA 条目（先建 QA 条目，再 python tools/bok.py tts-pregen --qa 物化音频）`，报告落 `{"skipped": "no_qa_or_audio"}` 并**退出码 1**（未评估 ≠ PASS）。
  5. `main`：`--then-jump`（store_true，`then_jump=TARGET_STEP`）、`--after-text`（默认 `"我知道了，你说"`——刻意避开七族词：`_CONFIRM_RE` 单字（好/是/对/嗯/系/係，命中即规则推进 4→5）、`_QUESTION_RE`、`_DEFER_RE`、`_REFUSE_RE`/`_FAREWELL_RE`/`_DENY_RE`/`_HANGUP_RE`，以及图触发词（退款/投诉 类）——见勘误预检 2；after 轮只为信息位，硬判据可回落同轮日志）；`selftest` 补 3 例（then-jump 正例 / play_miss 反例 / 无跳反例）。
- [ ] **Step 4: 纯函数绿 + selftest** — `pytest tests/test_flow_graph_probe.py tests/test_flow_graph_core.py tests/test_flow_graph_then_jump.py -v`；`python scripts/probe_flow_graph.py --selftest`（exit 0）
- [ ] **Step 5: 实弹两腿**（真栈）：

```bash
# 前置：至少一条 QA 条目且音频已物化（闸门只认缓存有音频的条目）
<主树>/.venv312/bin/python tools/bok.py tts-pregen --qa
ps aux | grep agent_runtime            # 必须为 0（殭尸 worker 污染 A/B）
<主树>/.venv312/bin/python tools/bok.py serve

# 腿 1（图开启）：期望 play_logged=1 then_jump_effective=1
<主树>/.venv312/bin/python scripts/probe_flow_graph.py --then-jump --lang zh

# 腿 2（kill）：须先把 serve 重启成 BOK_FLOW_GRAPH=0（env 进程级定死，探针不代重启）
#   BOK_FLOW_GRAPH=0 python tools/bok.py serve
<主树>/.venv312/bin/python scripts/probe_flow_graph.py --then-jump --expect-off --lang zh
```

  判读：腿 1 报告须同时有 `FLOW_GRAPH play` 与 `FLOW_GRAPH jump … via=then_jump`（或 after 轮步号命中），JSON → `reports/flow-graph/`；哑轮/首声只记信息位。腿 2 若 FAIL，先核对 worker pid env 是否有 `BOK_FLOW_GRAPH`（bok.py:1060-1067 白名单），再怀疑引擎。
- [ ] **Step 6: AGENTS.md** — 「话术图」条追加半句：`play_qa` 绑定可选 `then_jump`（1-based，仅 play_qa，[1,999]）→ 罐头**播完当场** `apply_then_jump`（同轮位移三件套、实际位移才打 `FLOW_GRAPH jump … via=then_jump`，同位/closing → `jump_noop`；`then_jump` 不另立 `graph_fired` 条目）；整闸继承 `BOK_FLOW_GRAPH`（无独立开关、无新 env）；探针腿 `probe_flow_graph.py --then-jump`（+ `--expect-off` kill 腿）。
- [ ] **Step 7: 全量验收 + PR** —
  - `<主树>/.venv312/bin/python -m pytest tests/ -q`（全绿）
  - `python -m compileall -q apps packages services tools scripts`
  - `cd apps/web && npx tsc --noEmit && npm test && npm run build`；`cd services/realtime-translation && npm test`
  - `python scripts/probe_flow_graph.py --selftest` + 上表两腿实跑（含报告落盘）
  - 三语 E2E 抽一条（`E2E_ONLY=zh .venv312/bin/python scripts/e2e_trilingual_livekit.py`）确认通话链路零回归
  - **无 Supabase/DB 动作**（勘误预检 6）；push → PR（base `main`）→ CI 绿（pytest / node test / web tsc+build / Launcher smoke / gitleaks）→ 按 PR 合并
- [ ] **Step 8: Commit** — `feat(qa-chain): probe then-jump leg + AGENTS note`

---

## Self-Review

- **Spec §3 覆盖**：字段形状/不加 qa_entries 列 → T1（`GraphBinding` 单点，零 DB/CP schema 改动）；执行点（播放成功后同步跳 + Phase-2 位移三件套与记账纪律）→ T2（`apply_then_jump` 返回实际位移，未位移零副作用 + `jump_noop`）；`graph_fired` 交互（绑定 id 由 play 消费、then_jump 不另立条目）→ T2 分支保持既有 `graph_fired.add` 在 play 成功处、跳转块内零账本写入；validate 三态 → T1；probe 腿（play 轮 / 下一轮步号 / kill 腿）→ T4；web 数字框 + 可选虚线边默认关 → T3。
- **零变化有专测**：`test_binding_without_then_jump_unchanged`、`test_validate_then_jump_accepted_on_play_qa`（缺省 `[]`）、`test_evaluate_leg_then_jump_kill_leg_and_default_shape`（判据集不变）、`test_plan_rounds_…default_zero_change`、`test_build_graph_json_then_jump_binding`（默认无键）、既有 `test_flow_graph_core.py` 13 例与 `test/qa-canvas.test.mjs` 零改动全绿。kill-switch 与图同闸（无新 env，符合 spec §6.2「语义变更 + 开关各自独立」的既有闸复用）。
- **类型/签名一致**：`GraphBinding.then_jump: int | None`（T1）↔ `apply_then_jump(int | None) -> bool`（T2）↔ `GraphBinding.then_jump?: number` + `BindingDraft.then_jump: number`（T3，0=不跳）↔ `build_graph_json(..., *, then_jump: int | None)` / `evaluate_leg(..., then_jump=None, after_events=None)`（T4）。`via=then_jump` 复用既有 `jump`/`jump_noop` kind 词表（`RE_FLOW_GRAPH` 与既有断言零改动）。
- **无占位符**：所有步都给了可执行命令与真实行号锚点；T1/T2/T4 的测试与实现给到逐字代码。
- **未验证项（如实标注）**：① 探针两腿未在真栈跑过（本轮只写计划，无栈）；② 可选画布虚线边默认关，其开启后的视觉/删除手感未实测（stretch，不阻塞验收）；③ 勘误预检 1 关于「marker 随 turn_ctx 副本蒸发」是从 livekit-agents 1.8 源码（`agent_activity.py:2599-2613`）读出的结论，未打点实证——如需固证，腿 1 跑时挂 `BOK_LLM_MSG_DEBUG=1` 看下一轮请求指纹有无 `[流程状态]`。
