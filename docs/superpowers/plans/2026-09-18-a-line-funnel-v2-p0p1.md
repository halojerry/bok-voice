# A 线漏斗 v2 P0+P1 实施计划（并行 worktree 版）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> 本计划按用户指示以 **3 个独立 worktree 并行 subagent** 执行（Task 1/2/3 各一），Task 4（集成验收）在合并后由主会话顺序执行。Spec: `docs/superpowers/specs/2026-09-18-a-line-funnel-v2-design.md`。

**Goal:** 卡步有出口（stall 阶梯）、模糊轮有路由字段（judge route）、查单/投诉有真动作（CP 跟进工单）。

**Architecture:** 全部为既有漏斗的增量：flow.py 加账本与纯函数，agent.py 加直念车道，control-plane 加新表+端点。零关键路径新增、零新模型依赖。

**Tech Stack:** Python 3.12 / SQLAlchemy 2 (DeclarativeBase) / FastAPI / pytest / livekit-agents 1.8。

## Global Constraints

- **kill-switch 合入初版一律默认关**：`BOK_ROUTE_JUDGE=0`、`BOK_STALL_LADDER=0`（代码默认值即 "0"；翻 1 是探针验收后的独立变更）。CP 端点无 env 闸（agent 侧动作才受 `BOK_TOOLS_FOLLOWUP` 管，本计划不实现 agent 侧动作）。
- **worktree 内禁止** `bok.py serve` / 任何 E2E / probe（单机 GPU 独占，防殭尸 worker）。只跑 pytest + `python -m compileall -q`。
- venv 复用主仓：`../voice-assistant/.venv312/bin/python`（worktree 位于主仓同级目录）。
- 测试自举模式照 `tests/test_flow_controller.py`：`sys.path.insert(0, ...)` 后直接 import。
- Conventional commits（scope: `feat(agent)` / `feat(cp)`），body 带根因/证据；每 task 至少一 commit。
- 术语门禁：禁新增 `yue` 字面量（`tests/test_cantonese_terminology.py`）；cantonese 全小写。
- DB 方言可移植：新表禁 sqlite 专有语法（`tests/test_db_portability.py` 门禁）。
- **勿动 `apps/control-plane/control_plane/schemas.py`**（主仓有未提交 WIP，避免合并冲突）；请求模型 inline 在 main.py 端点旁。
- P2 脊柱收编不在本计划（spec 分期约束：P0/P1 先在旧结构验证）。

---

### Task 1: Judge 路由字段（worktree `wt-judge-route`，分支 `feat/funnel-v2-judge-route`）

**Files:**
- Modify: `apps/agent/agent_runtime/flow.py`（`build_judge_messages` ~L1186、`parse_judge_output` ~L1230 附近新增函数）
- Modify: `apps/agent/agent_runtime/agent.py`（`_background_flow_judge`，grep `async def _background_flow_judge` 定位；`_judge_inflight` 定义处旁加 `_judge_route` dict）
- Create: `tests/test_judge_route.py`

**Interfaces:**
- Produces: `flow.build_judge_messages(*, current_index, total, overview_lines, goal, ref, next_goal, user_text, facts, route_enabled: bool = False) -> list[dict]`（新增仅 kwargs，默认 False 零行为漂移）
- Produces: `flow.JUDGE_ROUTES: frozenset[str]`、`flow.parse_judge_route(text: str) -> tuple[str, float]`（非法/缺失 → `("keep", 0.0)`）
- Produces: agent 内 `_judge_route: dict = {"route": "keep", "conf": 0.0, "step": -1}`（Task 4 消费；本 task 只写入+打点）

- [ ] **Step 1: 写失败测试** `tests/test_judge_route.py`

```python
"""judge 路由字段:route/conf 防御式解析 + prompt 扩展开关(漏斗 v2 P0)。"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.flow import JUDGE_ROUTES, build_judge_messages, parse_judge_output, parse_judge_route  # noqa: E402


def test_parse_route_full_line():
    route, conf = parse_judge_route("stay route=register_followup conf=0.8")
    assert route == "register_followup" and conf == 0.8


def test_parse_route_missing_defaults_keep():
    assert parse_judge_route("advance") == ("keep", 0.0)
    assert parse_judge_route("") == ("keep", 0.0)


def test_parse_route_invalid_falls_back():
    assert parse_judge_route("stay route=nonsense conf=0.5")[0] == "keep"
    # conf 越界夹取;垃圾值回落 0.0
    assert parse_judge_route("stay route=keep conf=9")[1] == 1.0
    assert parse_judge_route("stay route=keep conf=abc")[1] == 0.0


def test_route_vocab():
    assert JUDGE_ROUTES == frozenset({"keep", "degrade_question", "capture_contact", "register_followup", "transfer_human"})


def test_prompt_route_enabled():
    kw = dict(current_index=1, total=6, overview_lines=[], goal="g", ref="r", next_goal="n", user_text="u", facts=None)
    assert "route=" in build_judge_messages(**kw, route_enabled=True)[0]["content"]
    assert "route=" not in build_judge_messages(**kw)[0]["content"]


def test_parse_judge_output_backcompat():
    assert parse_judge_output("advance") == "confirm"
    assert parse_judge_output("objection") == "objection"
    assert parse_judge_output("stay") == "unclear"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd <worktree> && PYTHONPATH=apps/agent ../voice-assistant/.venv312/bin/python -m pytest tests/test_judge_route.py -q`
Expected: FAIL（ImportError: JUDGE_ROUTES）

- [ ] **Step 3: 实现 flow.py**

`build_judge_messages` 签名加 `route_enabled: bool = False`；在既有 sys 串末尾（例子之后、`if facts` 之前）追加：

```python
    if route_enabled:
        sys += (
            "\n若客户呢句含实质诉求，喺 verdict 后面补一段：route=X conf=0.0~1.0。"
            "route 只准係：register_followup（要查单/查进度/跟进登记）/"
            "capture_contact（愿意留联系方式）/transfer_human（指名要真人）/"
            "degrade_question（答非所问、听唔明客户讲咩）。冇诉求就输出 route=keep。"
        )
```

文件尾部（`parse_judge_output` 之后）新增：

```python
JUDGE_ROUTES = frozenset(
    {"keep", "degrade_question", "capture_contact", "register_followup", "transfer_human"}
)
_ROUTE_RE = re.compile(r"route\s*[=:]\s*([a-z_]+)")
_CONF_RE = re.compile(r"conf(?:idence)?\s*[=:]\s*([01](?:\.\d+)?)?")


def parse_judge_route(text: str) -> tuple[str, float]:
    """解析 judge 输出的路由字段(route/conf)。缺失/非法一律回落 ("keep", 0.0)
    ——旧格式输出、4B 格式漂移、9B 拒答都零行为漂移。"""
    t = (text or "").strip().lower()
    m = _ROUTE_RE.search(t)
    route = m.group(1) if m and m.group(1) in JUDGE_ROUTES else "keep"
    c = _CONF_RE.search(t)
    try:
        conf = float(c.group(1)) if c and c.group(1) else 0.0
    except ValueError:
        conf = 0.0
    return route, min(max(conf, 0.0), 1.0)
```

（`re` 已在 flow.py 顶部 import——确认即可。）

- [ ] **Step 4: agent.py 接线**

`_judge_inflight: dict = {"step": -1}` 定义旁加：

```python
    _judge_route: dict = {"route": "keep", "conf": 0.0, "step": -1}
```

`_background_flow_judge` 内：`route_enabled = os.environ.get("BOK_ROUTE_JUDGE", "0") == "1"`，传给 `build_judge_messages(...)`；拿到 raw 输出后：

```python
            jv = parse_judge_output(raw)
            if route_enabled:
                from .flow import parse_judge_route
                _rr, _cc = parse_judge_route(raw)
                _judge_route.update(route=_rr, conf=_cc, step=step_at)
```

既有 judge 打点行追加 ` route={_judge_route['route']} conf={_judge_route['conf']:.2f}`（仅 route_enabled 时）。

- [ ] **Step 5: 全量门禁 + commit**

Run: `PYTHONPATH=apps/agent:packages/core:packages/business-db ../voice-assistant/.venv312/bin/python -m pytest tests/ -q` 全绿；`../voice-assistant/.venv312/bin/python -m compileall -q apps packages`。

```bash
git add -A && git commit -m "feat(agent): judge 输出扩展 route/conf 路由字段(默认关 BOK_ROUTE_JUDGE=0)

根因: 模糊轮只有 unclear 一个出口,修复只能往规则层加正则(spec §1)。
防御式解析,旧格式输出零行为漂移;Task 4 工具层消费方接线。"
```

---

### Task 2: Stall 升级器（worktree `wt-stall-ladder`，分支 `feat/funnel-v2-stall-ladder`）

**Files:**
- Modify: `apps/agent/agent_runtime/flow.py`（`FlowController` ~L842 字段区+方法；模块级纯函数放 `should_auto_advance` 同区）
- Modify: `apps/agent/agent_runtime/agent.py`（turn 钩子内、DEFER 车道 `if (flow_ctrl.last_verdict == DEFER ...` ~L3588 之前插阶梯车道；罐头行放 `_farewell_line` ~L993 同区）
- Create: `tests/test_stall_ladder.py`

**Interfaces:**
- Produces: `flow.stall_ladder_level(streak: int) -> str`（`""|degrade|bypass|close`，阈值 3/5/8）
- Produces: `FlowController.step_streak: dict[int, int]`、`flow_ctrl.note_turn_outcome(verdict: str, step: int, turn_key: str) -> int`（UNCLEAR 计数/(step,turn) 去重/其它 verdict 清零）、`flow_ctrl.advance()` 内清当前步 streak
- Produces: `agent._stall_ladder_line(lang: str, level: str) -> str`（degrade/bypass 两级，三语）
- Consumes: 既有 `_say_script(session, tts_provider, _tts_cache, text)`、`_cancel_response_watchdog()`、`_schedule_call_end(delay, disposition=)`、`_wa_captured["on"]`、`closed`、`_t0`、`_turn_origin`、`cp.add_turn`（全部在 defer-ack 车道可见，照抄其姿势）

- [ ] **Step 1: 写失败测试** `tests/test_stall_ladder.py`

```python
"""stall 升级阶梯:纯函数阈值 + 账本去重/清零(漏斗 v2 P0,spec §3.1)。"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.flow import FlowController, stall_ladder_level  # noqa: E402


def test_level_thresholds():
    assert stall_ladder_level(0) == ""
    assert stall_ladder_level(2) == ""
    assert stall_ladder_level(3) == "degrade"
    assert stall_ladder_level(4) == "degrade"
    assert stall_ladder_level(5) == "bypass"
    assert stall_ladder_level(7) == "bypass"
    assert stall_ladder_level(8) == "close"
    assert stall_ladder_level(20) == "close"


def _fc() -> FlowController:
    return FlowController(steps=[])


def test_note_unclear_counts_and_dedupes_per_turn():
    fc = _fc()
    assert fc.note_turn_outcome("unclear", 3, "k1") == 1
    # 同轮双路(rule+judge)同 key 只计 1
    assert fc.note_turn_outcome("unclear", 3, "k1") == 1
    assert fc.note_turn_outcome("unclear", 3, "k2") == 2
    # 不同步各自计
    assert fc.note_turn_outcome("unclear", 4, "k3") == 1


def test_non_unclear_resets_step():
    fc = _fc()
    fc.note_turn_outcome("unclear", 3, "k1")
    fc.note_turn_outcome("unclear", 3, "k2")
    fc.note_turn_outcome("question", 3, "k3")  # 实质提问不算 stall,清零
    assert fc.step_streak.get(3, 0) == 0


def test_advance_clears_streak():
    fc = _fc()
    fc.note_turn_outcome("unclear", 0, "k1")
    fc.note_turn_outcome("unclear", 0, "k2")
    fc.advance()
    assert fc.step_streak.get(0, 0) == 0


def test_ladder_line_three_langs():
    from agent_runtime.agent import _stall_ladder_line  # noqa: E402
    for lang in ("zh", "cantonese", "en"):
        for level in ("degrade", "bypass"):
            assert _stall_ladder_line(lang, level)
```

注：`agent.py` import 需要其依赖可导入——若模块级 import 过重，改从 `agent_runtime.agent` 源码内把 `_stall_ladder_line` 抽到 flow.py 同级的独立小模块 `agent_runtime/ladder_lines.py`（测试与 agent 双向 import 该模块；这是**允许的文件拆分**，其余不变）。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=apps/agent ../voice-assistant/.venv312/bin/python -m pytest tests/test_stall_ladder.py -q`
Expected: FAIL（ImportError: stall_ladder_level）

- [ ] **Step 3: 实现 flow.py**

模块级（`should_auto_advance` 附近）：

```python
STALL_DEGRADE_N, STALL_BYPASS_N, STALL_CLOSE_N = 3, 5, 8


def stall_ladder_level(streak: int) -> str:
    """同 step 连续 UNCLEAR 数 → 阶梯级别(spec §3.1: 3 降级问法/5 绕过留号/8 收线)。"""
    if streak >= STALL_CLOSE_N:
        return "close"
    if streak >= STALL_BYPASS_N:
        return "bypass"
    if streak >= STALL_DEGRADE_N:
        return "degrade"
    return ""
```

`FlowController` 字段区（`last_digits` 之后）：

```python
    # stall 升级账本(漏斗 v2,spec §3.1):同 step 连续 UNCLEAR 数;按 (step, turn_key)
    # 去重——rule 与 background judge 双路报同一轮只计 1。
    step_streak: dict[int, int] = field(default_factory=dict)
```

`__post_init__` 加 `self._streak_seen: set[str] = set()`。方法区：

```python
    def note_turn_outcome(self, verdict: str, step: int, turn_key: str) -> int:
        """每轮判决记账:UNCLEAR 且步未变 +1(同轮双路去重);其余清该步计数。"""
        if verdict != UNCLEAR:
            self.step_streak.pop(step, None)
            return 0
        key = f"{step}:{turn_key}"
        if key in self._streak_seen:
            return self.step_streak.get(step, 0)
        self._streak_seen.add(key)
        n = self.step_streak.get(step, 0) + 1
        self.step_streak[step] = n
        return n
```

`advance()` 首行加 `self.step_streak.pop(self.current, None)`；`enter_closing()` 加 `self.step_streak.clear()`。

- [ ] **Step 4: 实现 agent.py**

`_farewell_line` 同区新增（若 Step 1 注选择了独立模块则放该模块）：

```python
def _stall_ladder_line(lang: str, level: str) -> str:
    """stall 阶梯直念行(spec §3.1)。degrade=封闭问句引导;bypass=绕过留号。
    v1 无模板 degrade_hint 字段,通用罐头;三语,cantonese 全小写。"""
    _LINES = {
        "degrade": {
            "zh": "这样吧，您不用想那么多——我就问您一句，您答个「是」或者「不是」就行。",
            "cantonese": "噉啦，唔使諗咁多——我就問你一句，你答「係」定「唔係」就得㗎啦。",
            "en": "Let me keep this simple — a yes or no will do.",
        },
        "bypass": {
            "zh": "要不这样，您留个 WhatsApp 给我们，我们安排专人帮您跟进，好吗？",
            "cantonese": "不如噉，你留个 WhatsApp 俾我哋，我哋安排专人帮你跟进，好唔好？",
            "en": "How about you leave us your WhatsApp, and we'll have a specialist follow up with you?",
        },
    }
    table = _LINES.get(level) or _LINES["degrade"]
    return table.get(lang) or table["cantonese"]
```

turn 钩子：advance 块内维护记账——规则路径拿到 `verdict` 后（advance 判定之后、`context_state.set_flow_current(...)` 附近）：未发生推进且 `verdict in (UNCLEAR,)` 时 `_streak = flow_ctrl.note_turn_outcome(verdict, flow_ctrl.current, turn_key)`，其中 `turn_key = f"{user_text}:{int(_t0)}"`；发生推进或其它 verdict 时同样调 `note_turn_outcome`（清零语义在方法内）。judge 路径（`_background_flow_judge` 内 judge 返回 `jv` 后、仍同步同轮）：`flow_ctrl.note_turn_outcome(jv, step_at, same_turn_key)`（把 turn_key 通过闭包/参数传入 `_background_flow_judge`，签名加 `turn_key: str = ""`）。

DEFER 车道**之前**插阶梯车道（结构照抄 defer-ack）：

```python
            # ---- stall 升级阶梯(漏斗 v2,spec §3.1):同 step 连续 UNCLEAR 有出口。
            # 3 降级问法 / 5 绕过留号 / 8 主动收线——全部 _say_script 直念零 TTFT。
            # BOK_STALL_LADDER=0 回退(合入初版默认关)。
            if (
                os.environ.get("BOK_STALL_LADDER", "0") == "1"
                and flow_ctrl.has_steps
                and not flow_ctrl.done
                and not flow_ctrl.closing
                and not closed.is_set()
            ):
                _lvl = stall_ladder_level(flow_ctrl.step_streak.get(flow_ctrl.current, 0))
                if _lvl == "bypass" and _wa_captured["on"]:
                    _lvl = "close"  # 号码已在手,留号无意义 → 直接收线
                if _lvl:
                    print(
                        f"[stall-ladder] step={flow_ctrl.current + 1} level={_lvl} "
                        f"streak={flow_ctrl.step_streak.get(flow_ctrl.current, 0)} (call {room_name})",
                        flush=True,
                    )
                    if _lvl == "close":
                        flow_ctrl.enter_closing()
                        _line = _farewell_line(object_name, language_state.lang)
                        _schedule_call_end(8.0, disposition="polite_close")
                    else:
                        _line = _stall_ladder_line(language_state.lang, _lvl)
                    context_state.set_last_reply(_line)
                    _turn_origin["gen"] = "script"
                    _turn_origin["provider"] = f"stall-{_lvl}"
                    try:
                        _sl_ms = int((time.monotonic() - _t0) * 1000)
                        await cp.add_turn(
                            call_id, "user", user_text, language=language_state.lang,
                            line="a", speaker="customer",
                            template_step=(int(flow_ctrl.current) + 1) if flow_ctrl.has_steps else 0,
                            started_ms=_sl_ms, ended_ms=_sl_ms,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    _cancel_response_watchdog()
                    await _say_script(session, tts_provider, _tts_cache, _line)
                    raise StopResponse()
```

注意：`stall_ladder_level`、`StopResponse`、`object_name`、`_t0` 等名字以钩子区实际可见符号为准（先 grep 确认 `from .flow import` 行与 defer 车道上下文，`object_name` 若不存在用装配区对象显示名变量——grep `display_name` 用法）。

- [ ] **Step 5: 全量门禁 + commit**

Run: `PYTHONPATH=apps/agent:packages/core:packages/business-db ../voice-assistant/.venv312/bin/python -m pytest tests/ -q` 全绿；`../voice-assistant/.venv312/bin/python -m compileall -q apps packages`。

```bash
git add -A && git commit -m "feat(agent): stall 升级阶梯 3/5/8 直念接管(默认关 BOK_STALL_LADDER=0)

根因: call-af30d9de judge=unclear 连续 7 轮无出口,平台问句重复 4 遍至客户放弃。
(3,turn) 去重双路计数;阶梯级直念零 TTFT;bypass 已捕获号码自动升 close。"
```

---

### Task 3: CP 跟进工单端点（worktree `wt-followups`，分支 `feat/funnel-v2-followups`）

**Files:**
- Modify: `packages/business-db/bok_voice_business_db/models.py`（`FillerEntry` ~L388 之后加模型）
- Modify: `packages/business-db/bok_voice_business_db/repository.py`（照 qa_entries 方法区加 3 个方法；先 grep `def list_qa_entries` 定位风格）
- Modify: `apps/control-plane/control_plane/main.py`（`POST /api/calls/{call_id}` 路由附近加新路由；**勿动 schemas.py**）
- Create: `tests/test_followups.py`

**Interfaces:**
- Produces: 表 `call_followups`（`models.create_all` 自动建，deps.py:69 无需 _ensure_column）；`repo.create_followup(*, call_id, account_id, object_id="", kind="followup", note="", created_by="") -> dict`、`repo.find_open_followup(call_id, kind) -> dict | None`、`repo.list_followups(account_id="", call_id="", limit=100) -> list[dict]`
- Produces: `POST /api/calls/{call_id}/followups`，body `{"kind": "track_order|complaint|followup", "note": "..."}`；幂等（同 call 同 kind 有 open 单 → 原样返回 `created: false`）；审计 `followup.create`
- Consumes（Task 4 之后）: agent 侧 `ControlPlaneClient` 加 `create_followup(...)`（不在本 task）

- [ ] **Step 0: 读范式**（先做，不写码）

Read `models.py` L337-388（QaEntry/FillerEntry 模型风格）、`repository.py` 中 `list_qa_entries`/`create_*` 方法区、`main.py` 中 `POST /api/calls` 路由的**鉴权块**（identity_gate/机器通道/deny_cross_account 用法——grep `@app.post("/api/calls")`）与 `_audit` 签名（main.py:364）、任一现网 CP 测试文件的 client/fixture 姿势（grep -l "TestClient" tests/）。

- [ ] **Step 1: 写失败测试** `tests/test_followups.py`（fixture 姿势照 Step 0 读到的现有 CP 测试；断言集如下）

```python
def test_create_and_idempotent(client):  # fixture 名照现有测试
    r1 = client.post("/api/calls/{CID}/followups", json={"kind": "complaint", "note": "货损"})
    assert r1.status_code == 200 and r1.json()["created"] is True
    r2 = client.post("/api/calls/{CID}/followups", json={"kind": "complaint"})
    assert r2.status_code == 200 and r2.json()["created"] is False
    assert r2.json()["id"] == r1.json()["id"]  # 幂等返回同一 open 单


def test_kind_whitelist(client):
    r = client.post("/api/calls/{CID}/followups", json={"kind": "nonsense"})
    assert r.status_code == 400


def test_missing_call_404(client):
    assert client.post("/api/calls/nope/followups", json={"kind": "followup"}).status_code == 404
```

（CID 用测试内先建的一通 call；若现网测试有造数 helper 照用。auth-on 场景断言追加在各自的 fixture 变体里——机器通道 token 过、跨账号 user 403/404，姿势照现有 RBAC 测试。）

- [ ] **Step 2: 跑测试确认失败** — `PYTHONPATH=apps/control-plane:packages/core:packages/business-db ../voice-assistant/.venv312/bin/python -m pytest tests/test_followups.py -q` → FAIL（404 路由不存在）

- [ ] **Step 3: 实现** models.py：

```python
class CallFollowup(Base):
    """跟进工单(漏斗 v2 工具层,spec §3.3):查单/投诉/跟进登记 → 人工跟办。

    v1 只有「登记+人工跟进」一档(无真实订单数据源,接入后插同一 action 槽位)。
    status: open=待跟办 / done=已办结 / cancelled=作废。kind 白名单三值。
    """

    __tablename__ = "call_followups"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    call_id: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True, default="acc-001")
    object_id: Mapped[str] = mapped_column(String(64), default="")
    kind: Mapped[str] = mapped_column(String(16), default="followup")
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_by: Mapped[str] = mapped_column(String(64), default="")  # ''=机器通道
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
```

repository.py 三个方法（id 用与其它 create 相同的 uuid 姿势；返回 dict 与 repo 其它方法同构）。main.py 端点：请求模型 inline `class CreateFollowupRequest(BaseModel): kind: str = "followup"; note: str = ""`；鉴权块照抄 `POST /api/calls`（机器通道直通；user/admin 走 `deny_cross_account`）；`kind not in {"track_order","complaint","followup"}` → 400；call 不存在 → 404；`find_open_followup` 命中 → 200 `created:false` 原单；否则建单 + `_audit("followup.create", subject_type="call", subject_id=call_id, account_id=…, call_id=call_id, detail={"kind": …, "followup_id": …})` → 200 `created:true` + 行 dict。

- [ ] **Step 4: 门禁** — `pytest tests/ -q` 全绿（含 `test_db_portability`）+ `compileall -q apps packages` + `../voice-assistant/.venv312/bin/python scripts/dump_postgres_ddl.py` 重跑（新表进 `scripts/.p0_supabase_schema.sql`，AGENTS.md 规约）并把产物一并提交。

- [ ] **Step 5: commit**

```bash
git add -A && git commit -m "feat(cp): call_followups 表 + POST /api/calls/{id}/followups(漏斗 v2 工具层)

根因: call-91a6b8c9 客户要查单,系统无任何后续动作能力(装查)。
v1=登记+人工跟进一档;幂等防叠;审计 followup.create;方言可移植。"
```

---

### Task 4（合并后，主会话顺序执行，不在并行范围）: agent 侧消费接线 + 验收

1. 合并顺序 `feat/funnel-v2-judge-route` → `feat/funnel-v2-stall-ladder` → `feat/funnel-v2-followups`（1/2 都动 flow.py+agent.py，先合 1；3 全 disjoint）；合并后主仓跑全量 pytest + web tsc/build 兜底。
2. `ControlPlaneClient.create_followup` + judge `route=register_followup && conf ≥ 0.7` → 异步建单 + `_say_script` 三语确认语（`BOK_TOOLS_FOLLOWUP` 闸，默认 0）+ 幂等（`created:false` 不重复播确认）。
3. judge `route=degrade_question && conf ≥ 0.7` → 提前触发阶梯 degrade 级。
4. 验收：三套 E2E + `probe_offscript_soak.py` A/B（主判据：同 step 重复问句数↓、give-up 无收线=0、followup 触发命中）+ `probe_latency_soak.py` 零回归（GPU 独占，串行跑）。
5. kill-switch 翻默认 1 的独立 commit（探针过后）。

## Self-Review 记录

- spec 覆盖：§3.1→Task 2；§3.2→Task 1；§3.3（CP 半边）→Task 3、（agent 半边）→Task 4.2；§4 延迟不变量→Task 2 车道全部直念；§5 闸→两 task 默认值 "0" ✓；§3.4 P2→明确排除 ✓。
- 类型一致：`stall_ladder_level`/`note_turn_outcome(verdict, step, turn_key)`/`parse_judge_route -> tuple[str, float]` 前后一致 ✓。
- 占位扫描：Task 3 Step 0 为「读真实范式再落码」的显式步骤（防臆造 fixture），非 TBD；Task 2 的 `object_name` 等符号名标注了「以 grep 为准」的防漂移指令。
