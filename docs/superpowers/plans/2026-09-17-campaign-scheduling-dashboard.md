# 外呼战役调度增强 + 工作台仪表盘 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给外呼战役补上双层外呼时段窗（全局+任务级）、任务级最大并发、未接通自动重拨；给工作台首页补上并发量/呼叫量/接通率/时长分布/坐席排行/标记统计的聚合端点与仪表盘页面。

**Architecture:** 调度决策全部抽成 `campaign.py` 纯函数（沿用 `_in_gap_cooldown`/`item_status_for_call` 的既有模式），`campaign_tick` 加 `now` 注入参数；schema 走 `deps.build_engine()` 的 `_ensure_column` 幂等迁移；仪表盘是单端点 `GET /api/stats/dashboard`（Python 侧聚合，P0 不加表不引图表库）；web 两页只用现有 CSS 体系。所有持久化改动双仓（内存仓 `MemoryBusinessRepo` + SQL 仓）同步。

**Tech Stack:** FastAPI + SQLAlchemy（SQLite/PG 双方言）、pytest、Next.js static export（无新增依赖）。

## Global Constraints

- 工作树：`/Users/halo/Documents/bok/wa-worktrees/feat-campaign-dashboard`，分支 `feat/campaign-dashboard`，基线 commit `afe6b5d`。测试一律用工作树自己的 `.venv312/bin/python -m pytest -q`（主仓的 venv 是 editable 安装指向主树，勿用）。
- **SQL 方言可移植**（`tests/test_db_portability.py` 门禁）：新列禁用 sqlite 专有语法；DDL 与 `server_default` 同形（`DEFAULT ''`/`DEFAULT '[]'`/`DEFAULT '0'` 单引号字面量，SQLite/PG 双认）；迁移只写在 `apps/control-plane/control_plane/deps.py` `build_engine()` 的 `_ensure_column` 段。
- **双仓同步**：`packages/business-db/bok_voice_business_db/repository.py` 里内存仓与 SQL 仓两套实现（约 :872 与 :1682 两个 `create_campaign`）行为必须一致，测试参数化两个后端（沿用 `tests/test_campaign_repo.py` 现有 fixtures）。
- Python 风格：PEP 8、4 空格、`from __future__ import annotations`、签名 type hints；每个后端任务收尾跑 `python -m compileall -q apps packages services tools scripts`。
- **旧行为零变化红线**：`CampaignCreateRequest` 只增字段不删不改默认语义；`max_concurrency` 缺省=1（等于旧串行）；`call_windows` 缺省=`[]`（不限时段）；`redispatch` 缺省=空（不重拨）；`scripts_json` 的 `__speak_interval__`/`__narrowband__` 保留键与 dial 块键序不动；`tests/test_campaign_loop.py`、`tests/test_campaign_api.py`、`tests/test_campaign_repo.py`、`tests/test_campaign_site.py` 既有断言全绿。
- **时段语义**：窗口判定用**服务器本地时区 naive datetime**（运营语义）；`days` 为 ISO 星期 1..7（1=周一）；窗空=不限；全局窗与任务窗取**交集**（两层都过才起拨）；跨零点窗（`end <= start`）按「start→次日 end」处理。
- **并发语义**：`max_concurrency=0` 表示不限制；≥1 = 同刻至多 N 通在途（在途=dialing/in_call）。
- **重拨语义**：终态命中 `on` 集合且 `attempts < max_attempts` → 回 `pending` 等待 `interval_minutes` 后重拨（`attempts` 复用现有列，建仓为 1）；`max_attempts<=1` 或策略空=不重拨（旧行为：终态即终态）。
- web：改动后 `cd apps/web && npx tsc --noEmit && npm run build` 全绿；**不引入任何图表/新 npm 依赖**，图表用现有 `card`/`label` CSS + div 宽度条实现；TS 风格沿用 `Record<string, unknown>` 宽类型。
- 术语门禁：不得引入 `yue` 字面量（`tests/test_cantonese_terminology.py` 全仓扫描）。
- 提交：Conventional commits 带 scope（`feat(campaign):` / `feat(cp):` / `feat(web):` / `chore(schema):`），一任务一组逻辑提交；body 写根因/口径与验证证据。
- 时长统计口径：`started_at`=通话首次转 ACTIVE（dial-result answered）时刻；`ended_at`=终态时刻；`duration_s`=ended-started 秒，`started_at` 为空（未接通）则 0。全部 UTC naive，与 `created_at` 同域。

---

### Task 1: Schema 与仓储层新字段（campaigns 3 列 + call_sessions 3 列 + 双仓读写 + 迁移）

**Files:**
- Modify: `packages/business-db/bok_voice_business_db/models.py`（Campaign :159、CallSession :92）
- Modify: `packages/business-db/bok_voice_business_db/repository.py`（内存仓 `create_campaign` :872 / SQL 仓 `create_campaign` :1682、两处 `update_campaign`、两处 `_campaign_public`、两处 `update_call`）
- Modify: `apps/control-plane/control_plane/deps.py`（`build_engine()` `_ensure_column` 段，:89 附近）
- Modify: `scripts/.p0_supabase_schema.sql`（由 `scripts/dump_postgres_ddl.py` 重新生成）
- Test: `tests/test_campaign_repo.py`（扩展）、`tests/test_db_portability.py`（确认仍绿）

**Interfaces:**
- Produces（后续任务依赖的确切形状）:
  - `campaign` dict 新增键：`call_windows`（list[dict]，已解析）、`max_concurrency`（int）、`redispatch`（dict 或 {}）；列名 `call_windows_json`/`max_concurrency`/`redispatch_json`。
  - `repo.create_campaign(account_id, *, name, template_id, persona_id, language, gap_seconds, object_ids, scenarios=None, scripts=None, site_id="", call_windows=None, max_concurrency=1, redispatch=None)`。
  - `repo.update_call(call_id, **fields)` 白名单新增 `started_at`/`ended_at`/`duration_s`（datetime/None 与 int）。
  - 窗口形状：`[{"days": [1,2], "start": "08:00", "end": "18:00"}]`。

- [ ] **Step 1: 写失败测试**（`tests/test_campaign_repo.py` 追加；沿用文件内现有双仓 fixture 风格）

```python
class TestCampaignSchedulingFields:
    """战役调度三字段（2026-09-17）：双仓 roundtrip + 白名单 + 缺省旧行为。"""

    def test_create_with_scheduling_fields(self, repo):
        camp = repo.create_campaign(
            "acc-001", name="t", template_id="", persona_id="", language="zh",
            gap_seconds=5, object_ids=[], call_windows=[{"days": [1, 2], "start": "08:00", "end": "18:00"}],
            max_concurrency=3, redispatch={"max_attempts": 2, "interval_minutes": 30, "on": ["no_answer"]},
        )
        assert camp["call_windows"] == [{"days": [1, 2], "start": "08:00", "end": "18:00"}]
        assert camp["max_concurrency"] == 3
        assert camp["redispatch"] == {"max_attempts": 2, "interval_minutes": 30, "on": ["no_answer"]}

    def test_create_defaults_are_legacy(self, repo):
        camp = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                                    language="zh", gap_seconds=5, object_ids=[])
        assert camp["call_windows"] == []
        assert camp["max_concurrency"] == 1
        assert camp["redispatch"] == {}

    def test_update_whitelist_accepts_scheduling_fields(self, repo):
        camp = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                                    language="zh", gap_seconds=5, object_ids=[])
        updated = repo.update_campaign(camp["id"], max_concurrency=0,
                                       call_windows_json='[{"days":[6],"start":"09:00","end":"12:00"}]',
                                       redispatch_json='{"max_attempts":2,"interval_minutes":15,"on":["no_answer"]}')
        assert updated["max_concurrency"] == 0
        assert updated["call_windows"] == [{"days": [6], "start": "09:00", "end": "12:00"}]
        assert updated["redispatch"]["max_attempts"] == 2

    def test_update_call_time_columns(self, repo):
        call = repo.create_call(_manifest())  # 沿用文件内既有建通话 helper/fixture
        updated = repo.update_call(call["id"], started_at=_dt(0), ended_at=_dt(95), duration_s=95)
        assert updated["duration_s"] == 95 and updated["started_at"] is not None
```

（`_manifest()`/`_dt()` 若文件内没有现成 helper，按文件内既有建通话测试的写法补最小 helper：`_dt(offset_s)` 返回 naive UTC datetime。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/halo/Documents/bok/wa-worktrees/feat-campaign-dashboard && .venv312/bin/python -m pytest tests/test_campaign_repo.py -k Scheduling -q`
Expected: FAIL（`TypeError: unexpected keyword 'call_windows'` 或 KeyError）

- [ ] **Step 3: 最小实现**

models.py Campaign 追加（`site_id` 之后）：

```python
    # 外呼时段窗（2026-09-17 竞品对齐）：JSON 数组 [{"days":[1..7],"start":"HH:MM","end":"HH:MM"}]，
    # days=ISO 星期(1=周一)；空数组=不限。≤3 组，解析归一见 campaign.parse_call_windows。
    # server_default 与 deps._ensure_column 迁移 DDL 同形。
    call_windows_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    # 任务级最大并发：0=不限；≥1=同刻至多 N 通在途。缺省 1=旧串行行为。
    max_concurrency: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # 未接通自动重拨：{"max_attempts":2,"interval_minutes":30,"on":["no_answer"]}；空串=不重拨（旧行为）。
    redispatch_json: Mapped[str] = mapped_column(Text, default="", server_default="")
```

CallSession 追加（`session_reports_json` 之后）：

```python
    # 仪表盘时长统计（2026-09-17）：started_at=首次接通时刻；ended_at=终态时刻；
    # duration_s=接通秒数（未接通=0）。DateTime nullable 与 campaigns.finished_at 同形。
    started_at: Mapped[datetime | None] = mapped_column(DateTime, default=None, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, default=None, nullable=True)
    duration_s: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
```

deps.py `_ensure_column` 段末尾追加：

```python
                _ensure_column(conn, "campaigns", "call_windows_json",
                               "call_windows_json TEXT DEFAULT '[]'")
                _ensure_column(conn, "campaigns", "max_concurrency",
                               "max_concurrency INTEGER DEFAULT 1")
                _ensure_column(conn, "campaigns", "redispatch_json",
                               "redispatch_json TEXT DEFAULT ''")
                _ensure_column(conn, "call_sessions", "started_at",
                               "started_at DATETIME")
                _ensure_column(conn, "call_sessions", "ended_at",
                               "ended_at DATETIME")
                _ensure_column(conn, "call_sessions", "duration_s",
                               "duration_s INTEGER DEFAULT 0")
```

repository.py 两处 `create_campaign`：签名加 `call_windows: list[dict] | None = None, max_concurrency: int = 1, redispatch: dict | None = None`。内存仓行存 `"call_windows_json": json.dumps(call_windows or [], ensure_ascii=False)`、`"max_concurrency": int(max_concurrency or 0)`、`"redispatch_json": json.dumps(redispatch, ensure_ascii=False) if redispatch else ""`（两后端同存 JSON 串，解析单点在 `_campaign_public`）；SQL 仓按文件内既有 INSERT/columns 模式加三列。两处 `_campaign_public`：

```python
        out["max_concurrency"] = int(row.get("max_concurrency") or 0) if row.get("max_concurrency") is not None else 1
        try:
            out["call_windows"] = json.loads(row.get("call_windows_json") or "[]")
        except (TypeError, ValueError):
            out["call_windows"] = []
        if not isinstance(out["call_windows"], list):
            out["call_windows"] = []
        try:
            out["redispatch"] = json.loads(row.get("redispatch_json") or "")
        except (TypeError, ValueError):
            out["redispatch"] = {}
        if not isinstance(out["redispatch"], dict):
            out["redispatch"] = {}
```

两处 `update_campaign` 白名单元组加 `"call_windows_json", "max_concurrency", "redispatch_json"`。两处 `update_call` 白名单加 `"started_at", "ended_at", "duration_s"`（SQL 仓 `update_call` 对 DateTime 列已有字符串/datetime 兼容分支则沿用，没有则 datetime 直通）。文件头确认 `import json` 存在。

重新生成 Supabase 引导件：`.venv312/bin/python scripts/dump_postgres_ddl.py`（脚本自带干净库回环校验，输出零变更报错即过）。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_campaign_repo.py tests/test_db_portability.py -q`
Expected: PASS（含既有用例）

- [ ] **Step 5: 提交**

```bash
git add packages/business-db apps/control-plane/control_plane/deps.py scripts/.p0_supabase_schema.sql tests/test_campaign_repo.py
git commit -m "feat(campaign): 战役调度三字段与通话时长列（schema+双仓+幂等迁移）"
```

---

### Task 2: 调度决策纯函数（解析/窗口/重拨/并发）

**Files:**
- Modify: `apps/control-plane/control_plane/campaign.py`（只加函数，不动既有函数）
- Test: `tests/test_campaign_schedule.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 campaign dict 键（`call_windows`/`redispatch`/`max_concurrency`）。
- Produces（Task 3/4 依赖）:
  - `parse_call_windows(raw: Any) -> list[dict]`（≤3 组、days 1..7 排序去重、HH:MM、start<end 否则丢弃该项；非法输入恒返回 list）
  - `within_call_windows(now_local: datetime, windows: list[dict]) -> bool`（空窗=True；跨零点窗 end<=start 按 start→次日 end）
  - `redispatch_policy(campaign: dict) -> dict` → `{"max_attempts": int, "interval_minutes": float, "on": frozenset[str]}`；空/非法 → `{"max_attempts": 0, "interval_minutes": 0.0, "on": frozenset()}`（不重拨）
  - `redispatch_due(item: dict, campaign: dict, now: datetime | None = None) -> bool`（True=还**没**到重拨时刻；首拨 item 恒 False）
  - `concurrency_cap(campaign: dict) -> int`（0=不限；空/非法=1）
  - `localnow_naive() -> datetime`（服务器本地时区 naive）

- [ ] **Step 1: 写失败测试**（`tests/test_campaign_schedule.py` 新建）

```python
"""campaign 调度纯函数单测（2026-09-17）：时段窗/重拨/并发，全部注入 now，不碰真实时钟。"""
from __future__ import annotations

from datetime import datetime

from control_plane.campaign import (concurrency_cap, parse_call_windows,
                                    redispatch_due, redispatch_policy,
                                    within_call_windows)

NOW = datetime(2026, 9, 17, 10, 0, 0)  # 周四


def test_parse_call_windows_normalizes_and_caps_at_three():
    raw = [{"days": [3, 1, 1, 9], "start": "8:05", "end": "18:00"},
           {"days": [], "start": "08:00", "end": "18:00"},          # days 空 → 丢
           {"days": [1], "start": "18:00", "end": "08:00"},         # start>=end → 丢（跨零点显式不支持解析层）
           {"days": [6], "start": "09:00", "end": "12:00"},
           {"days": [7], "start": "09:00", "end": "12:00"},
           {"days": [2], "start": "09:00", "end": "12:00"}]         # 超出 3 组 → 截断
    assert parse_call_windows(raw) == [
        {"days": [1, 3], "start": "08:05", "end": "18:00"},
        {"days": [6], "start": "09:00", "end": "12:00"},
        {"days": [7], "start": "09:00", "end": "12:00"}]
    assert parse_call_windows(None) == []
    assert parse_call_windows("not-json") == []
    assert parse_call_windows([{"days": "1", "start": "08:00", "end": "09:00"}]) == []


def test_within_call_windows():
    windows = [{"days": [4], "start": "08:00", "end": "18:00"}]  # 周四
    assert within_call_windows(NOW, windows) is True
    assert within_call_windows(NOW.replace(hour=7, minute=59), windows) is False
    assert within_call_windows(NOW.replace(hour=18, minute=0, second=1), windows) is False
    assert within_call_windows(NOW.replace(hour=18, minute=0), windows) is True   # 端点含
    assert within_call_windows(NOW, []) is True                                    # 空窗=不限
    assert within_call_windows(NOW.replace(day=18), windows) is False              # 周五不在 days
    assert within_call_windows(NOW, "bad") is False                                # 非法窗=不放行


def test_redispatch_policy_and_due():
    camp = {"redispatch": {"max_attempts": 3, "interval_minutes": 30, "on": ["no_answer"]}}
    policy = redispatch_policy(camp)
    assert policy["max_attempts"] == 3 and policy["on"] == frozenset({"no_answer"})
    assert redispatch_policy({})["max_attempts"] == 0
    assert redispatch_policy({"redispatch": "junk"})["on"] == frozenset()
    # attempts=1 的首拨 item 永远不算「等重拨」
    assert redispatch_due({"attempts": 1, "status": "pending", "updated_at": NOW.isoformat()}, camp, NOW) is False
    item = {"attempts": 2, "status": "pending", "updated_at": (NOW.replace minute=0)}.isoformat()}  # 见下
```

（最后一行写成正式版：）

```python
def test_redispatch_due():
    camp = {"redispatch": {"max_attempts": 3, "interval_minutes": 30, "on": ["no_answer"]}}
    item_recent = {"attempts": 2, "status": "pending",
                   "updated_at": datetime(2026, 9, 17, 9, 50).isoformat()}
    item_due = {"attempts": 2, "status": "pending",
                "updated_at": datetime(2026, 9, 17, 9, 29).isoformat()}
    assert redispatch_due(item_recent, camp, NOW) is True    # 距上次终态 10min < 30min → 还没到
    assert redispatch_due(item_due, camp, NOW) is False      # ≥30min → 到点可拨
    assert redispatch_due(item_due, {}, NOW) is False        # 无策略 → 不等（但不该被调用）
    stale = {"attempts": 2, "status": "pending", "updated_at": "garbage"}
    assert redispatch_due(stale, camp, NOW) is False         # 解析失败=放行（不卡死名单）


def test_concurrency_cap():
    assert concurrency_cap({"max_concurrency": 0}) == 0
    assert concurrency_cap({"max_concurrency": 4}) == 4
    assert concurrency_cap({}) == 1
    assert concurrency_cap({"max_concurrency": "x"}) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_campaign_schedule.py -q`
Expected: FAIL（ImportError: cannot import name 'parse_call_windows'）

- [ ] **Step 3: 最小实现**（campaign.py 模块级追加；`import` 区补 `from datetime import datetime, timezone, timedelta` 已有 datetime/timezone，补 timedelta 不需要则不加）

```python
MAX_CALL_WINDOWS = 3


def parse_call_windows(raw: Any) -> list[dict]:
    """外呼时段窗归一（纯函数）：≤3 组、days⊆{1..7} 升序去重、HH:MM、start<end。

    逐项校验，非法项静默丢弃不抛——运营表单一个错字不废整单（与 create 端点
    scenarios 白名单同哲学）。非 list/解析失败 → []（不限时段语义由调用方空表表达）。
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict) or len(out) >= MAX_CALL_WINDOWS:
            break
        days = entry.get("days")
        if not isinstance(days, list):
            continue
        norm_days = sorted({int(d) for d in days
                            if isinstance(d, (int, str)) and str(d).isdigit() and 1 <= int(d) <= 7})
        start = _hhmm(entry.get("start"))
        end = _hhmm(entry.get("end"))
        if not norm_days or start is None or end is None or start >= end:
            continue
        out.append({"days": norm_days, "start": start, "end": end})
    return out


def _hhmm(value: Any) -> str | None:
    """"08:00"/"08:00:00" → "08:00"；非法 → None。"""
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def within_call_windows(now_local: datetime, windows: Any) -> bool:
    """now（本地 naive）落任一窗 → True；窗空 → True（不限）；窗形状非法 → False。

    解析层已保证 start<end（跨零点窗不支持），这里按分钟比较、端点含。
    """
    parsed = parse_call_windows(windows) if not (windows and isinstance(windows, list)
                                                 and all(isinstance(w, dict) and "start" in w for w in windows)) else windows
    if not parsed:
        return True
    minute_of_day = now_local.hour * 60 + now_local.minute
    iso_weekday = now_local.isoweekday()
    for window in parsed:
        if iso_weekday not in {int(d) for d in window.get("days", [])}:
            continue
        start = _hhmm_to_minute(window.get("start"))
        end = _hhmm_to_minute(window.get("end"))
        if start is None or end is None:
            continue
        if start <= minute_of_day <= end:
            return True
    return False


def _hhmm_to_minute(value: Any) -> int | None:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return None


def redispatch_policy(campaign: dict) -> dict:
    """redispatch 解析（纯函数）：空/非法 → 不重拨（max_attempts=0）。"""
    raw = campaign.get("redispatch")
    if not isinstance(raw, dict):
        return {"max_attempts": 0, "interval_minutes": 0.0, "on": frozenset()}
    try:
        max_attempts = max(0, int(raw.get("max_attempts") or 0))
    except (TypeError, ValueError):
        max_attempts = 0
    try:
        interval = max(0.0, float(raw.get("interval_minutes") or 0.0))
    except (TypeError, ValueError):
        interval = 0.0
    allowed = {"no_answer", "rejected", "failed"}
    outcomes = frozenset(str(x) for x in (raw.get("on") or []) if str(x) in allowed)
    return {"max_attempts": max_attempts, "interval_minutes": interval, "on": outcomes}


def redispatch_due(item: dict, campaign: dict, now: datetime | None = None) -> bool:
    """pending item 还没到重拨时刻 → True（本轮跳过）。首拨（attempts≤1）恒 False。

    updated_at 解析不出 → False（放行，不因脏数据卡死名单）。
    """
    try:
        attempts = int(item.get("attempts") or 1)
    except (TypeError, ValueError):
        attempts = 1
    if attempts <= 1:
        return False
    policy = redispatch_policy(campaign)
    if policy["max_attempts"] <= 0 or policy["interval_minutes"] <= 0:
        return False
    updated = _parse_updated_at(item.get("updated_at"))
    if updated is None:
        return False
    now = now if now is not None else _utcnow_naive()
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    return (now - updated).total_seconds() < policy["interval_minutes"] * 60


def concurrency_cap(campaign: dict) -> int:
    """max_concurrency：0=不限；空/非法=1（旧串行行为）。"""
    try:
        return max(0, int(campaign.get("max_concurrency") if campaign.get("max_concurrency") is not None else 1))
    except (TypeError, ValueError):
        return 1


def localnow_naive() -> datetime:
    """服务器本地时区 naive datetime（时段窗判定用，运营语义）。"""
    return datetime.now().astimezone().replace(tzinfo=None)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_campaign_schedule.py tests/test_campaign_loop.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add apps/control-plane/control_plane/campaign.py tests/test_campaign_schedule.py
git commit -m "feat(campaign): 时段窗/重拨/并发调度纯函数（可注入 now 全量单测）"
```

---

### Task 3: 调度循环接线 + 集成探针

**Files:**
- Modify: `apps/control-plane/control_plane/campaign.py`（`campaign_tick`/`_tick_campaign`）
- Create: `scripts/probe_campaign_schedule.py`
- Test: `tests/test_campaign_loop.py`（扩展，沿用文件内 fake repo/fake dispatcher 模式）

**Interfaces:**
- Consumes: Task 2 全部纯函数；Task 1 的 `repo.get_settings()["campaign"]["call_windows"]`（全局窗，读侧缺省 []）。
- Produces: `campaign_tick(repo=None, *, dispatcher=None, now: datetime | None = None) -> dict`（新增仅 kwargs `now`，UTC naive 或本地 naive 皆可收——内部窗口判定统一 `now` 不带 tz 视为**本地**，冷却比较沿用 `_utcnow_naive` 域）。

- [ ] **Step 1: 写失败测试**（`tests/test_campaign_loop.py` 追加；复用文件内既有 fake repo/dispatcher fixtures，这里给行为规格）

1. `test_tick_no_dispatch_outside_window`：campaign `call_windows=[{"days":[ NOW.isoweekday() ],"start":"23:00","end":"23:59"}]`，注入 `now`=窗口外 → `started==0`，无 item 离开 pending。
2. `test_tick_dispatch_inside_window`：同窗但 `now`=窗内 → `started==1`。
3. `test_global_window_intersects_task_window`：`repo.get_settings()` 返回含 `{"campaign": {"call_windows": [现在窗外]}}` → 即使任务窗内也不起拨；全局窗空 → 只看任务窗。
4. `test_concurrency_two_slots`：`max_concurrency=2`、名单 3 条、fake dispatcher 把 call 置 ENDED（已拨完）→ 第一轮 `started==1`（旧 item 不在途），手工把已拨 item 置 in_call 后下一轮 `started` 使在途=2；第 3 条在在途=2 时不起（`inflight>=cap`）。
5. `test_redispatch_cycle`：fake call ENDED+`disposition="no_answer"`，campaign `redispatch={"max_attempts":2,"interval_minutes":30,"on":["no_answer"]}` → 收割后 item 回 `pending`（attempts 不变=2）；30 分钟内 tick 不起拨；把 `updated_at` 拨早 31 分钟后 tick 起拨且 `attempts==2`；再次 no_answer 收割后（attempts 已=2 = max）item 保持 `no_answer` 不回 pending。
6. `test_waiting_redispatch_keeps_campaign_open`：场景 5 中等待重拨期间（全名单都在等）campaign **不**置 done。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_campaign_loop.py -k "window or concurrency or redispatch" -q`
Expected: FAIL（`campaign_tick() got an unexpected keyword argument 'now'` 或行为断言失败）

- [ ] **Step 3: 最小实现**

`campaign_tick` 签名加 `now: datetime | None = None`，透传 `_tick_campaign(repo, campaign, dispatcher, out, now=now)`。`_tick_campaign` 改造（保留①收割②并发③收尾结构）：

```python
async def _tick_campaign(repo, campaign: dict, dispatcher: Dispatcher,
                         out: dict, now: datetime | None = None) -> None:
    from .main import _TERMINAL_CALL_STATUSES

    now_local = (now if now is not None else localnow_naive())
    if now_local.tzinfo is not None:  # 统一剥 tz：窗口判定按 naive
        now_local = now_local.replace(tzinfo=None)
    policy = redispatch_policy(campaign)

    # ① 收割：进行中 item 的通话已终态 → 回写 item 结果（新增：命中重拨策略回 pending）。
    for item in [i for i in repo.list_items(campaign["id"])
                 if i.get("status") in _INFLIGHT_ITEM_STATUSES]:
        call = repo.get_call(str(item.get("call_id") or ""))
        if not call:
            continue
        if str(call.get("status") or "") not in _TERMINAL_CALL_STATUSES:
            continue
        result = item_status_for_call(call)
        updates = {"status": result,
                   "last_error": str(call.get("disposition") or "")[:250],
                   "updated_at": _utcnow_iso()}
        try:
            attempts = int(item.get("attempts") or 1)
        except (TypeError, ValueError):
            attempts = 1
        if (result in policy["on"] and attempts < policy["max_attempts"]):
            updates["status"] = "pending"  # 回 pending 等待 interval_minutes 后重拨
        repo.update_item(str(item["id"]), **updates)
        out["harvested"] += 1

    items = repo.list_items(campaign["id"])
    inflight = sum(1 for i in items if i.get("status") in _INFLIGHT_ITEM_STATUSES)
    pending = [i for i in items if i.get("status") == "pending"]
    if not pending and inflight == 0:
        repo.update_campaign(str(campaign["id"]), status="done",
                             finished_at=_utcnow_iso())
        out["finished"] += 1
        return
    if inflight > 0:
        return  # 并发槽位被占（数量见下），本轮不再起拨
    cap = concurrency_cap(campaign)
    if cap and inflight >= cap:
        return
    if not _in_gap_cooldown(items, campaign):
        # 时段窗：全局窗（settings.campaign.call_windows）∩ 任务窗，两层都过才起拨。
        global_windows = _global_call_windows(repo)
        if within_call_windows(now_local, global_windows) and \
           within_call_windows(now_local, campaign.get("call_windows")):
            waiting = [i for i in pending if redispatch_due(i, campaign, now)]
            eligible = [i for i in pending if not redispatch_due(i, campaign, now)]
            if eligible:
                slots = (cap - inflight) if cap else len(eligible)
                for item in eligible[:max(1, slots if cap else len(eligible))]:
                    await _start_call(repo, campaign, item, dispatcher)
                    out["started"] += 1
                return
    # pending 全在等重拨/窗未到/冷却中：保持现状（campaign 不判 done）
```

配套小函数：

```python
def _global_call_windows(repo) -> list[dict]:
    """全局外呼时段窗：settings.campaign.call_windows（空=不限）。读失败恒 []。"""
    try:
        section = (repo.get_settings() or {}).get("campaign") or {}
        windows = section.get("call_windows") if isinstance(section, dict) else None
    except Exception:  # noqa: BLE001 - 设置读取失败不阻拨
        return []
    return parse_call_windows(windows)
```

（并发语义说明写进 docstring：`cap=0` 不限时、`slots=cap-inflight`；一轮起拨后下一轮巡检再补位，天然逐步填满 N 槽。）

- [ ] **Step 4: 集成探针**（`scripts/probe_campaign_schedule.py` 新建——真 SQL 仓 + 真循环 + fake dispatcher，不依赖 LiveKit/HTTP）

脚本规格（写成可执行 `if __name__ == "__main__":`，`逐断言 PASS/FAIL 打印 + 汇总 + 非零退出码`）：
1. `tempfile` 建 sqlite 文件 + `DATABASE_URL=sqlite:///...` 经 `control_plane.deps.build_engine()` 构造 SQL 仓（类名以 `repository.py` 内 SQL 仓实际类名为准，构造方式照抄 `tests/test_campaign_repo.py` 的 SQL fixture）。
2. 建 3 个带电话对象 → 建 campaign（`max_concurrency=2`、`call_windows` 覆盖当前本地时刻的窗、`redispatch={"max_attempts":2,"interval_minutes":0.05,"on":["no_answer"]}`、`gap_seconds=0`）→ `update_campaign(status="running")`。
3. fake dispatcher：记录派发、把对应 call `update_call(status="ended", disposition="no_answer")`（模拟未接通）。
4. 断言：窗内 tick 起拨第 1 通；收割回 pending（等 3s 间隔）；等 4s 后 tick 重拨且 attempts=2；再 no_answer 后（attempts=2=max）保持终态；campaign 最终 done。
5. 窗外断言：另建一窗不含当前的 campaign → tick `started==0`。

- [ ] **Step 5: 跑测试与探针确认通过**

Run: `.venv312/bin/python -m pytest tests/test_campaign_loop.py tests/test_campaign_schedule.py -q && .venv312/bin/python scripts/probe_campaign_schedule.py`
Expected: 全 PASS，探针打印 5 组断言 PASS

- [ ] **Step 6: 提交**

```bash
git add apps/control-plane/control_plane/campaign.py scripts/probe_campaign_schedule.py tests/test_campaign_loop.py
git commit -m "feat(campaign): 循环接入双层时段窗/并发槽位/自动重拨 + SQL 仓集成探针"
```

---

### Task 4: HTTP 契约（创建扩展 + PUT 编辑端点）

**Files:**
- Modify: `apps/control-plane/control_plane/main.py`（`CampaignCreateRequest` :1844、`create_campaign` :1868、campaigns 区段末尾 :1992 附近加 PUT）
- Test: `tests/test_campaign_api.py`（扩展）

**Interfaces:**
- Consumes: Task 1 repo 字段、Task 2 `parse_call_windows`。
- Produces:
  - `POST /api/campaigns` 请求新字段：`call_windows: list[dict] = []`、`max_concurrency: int = 1`、`redispatch: dict = {}`；响应 campaign dict 含解析后的 `call_windows`/`max_concurrency`/`redispatch`。
  - `PUT /api/campaigns/{campaign_id}`：body 同名字段（全部 optional）+ `name/template_id/persona_id/language/gap_seconds/site_id`；`status=="running"` → 409（对齐竞品「运行中锁定」）；404/越权同既有端点；audit `campaign.update`。

- [ ] **Step 1: 写失败测试**（`tests/test_campaign_api.py` 追加，沿用文件内 TestClient fixture）

```python
def test_create_campaign_with_scheduling_payload(client):
    body = _campaign_body()  # 文件内既有合法 body helper
    body.update({"call_windows": [{"days": [1, 2, 3, 4, 5], "start": "08:00", "end": "18:00"},
                                   {"days": [6], "start": "09:00", "end": "12:00"},
                                   {"days": [7], "start": "bad", "end": "x"},
                                   {"days": [1], "start": "10:00", "end": "11:00"}],
                 "max_concurrency": 0,
                 "redispatch": {"max_attempts": 2, "interval_minutes": 30, "on": ["no_answer", "junk"]}})
    camp = client.post("/api/campaigns", json=body).json()
    assert len(camp["call_windows"]) == 3          # 非法窗丢、超 3 截断
    assert camp["max_concurrency"] == 0
    assert camp["redispatch"]["on"] == ["no_answer"]  # 非法结果名剔除


def test_update_campaign_rejects_running(client):
    camp = _create_and_start(client)               # 文件内既有模式：建波→start
    resp = client.put(f"/api/campaigns/{camp['id']}", json={"gap_seconds": 9})
    assert resp.status_code == 409


def test_update_campaign_edits_paused(client):
    camp = _create(client)
    resp = client.put(f"/api/campaigns/{camp['id']}",
                      json={"max_concurrency": 3,
                            "call_windows": [{"days": [1], "start": "08:00", "end": "12:00"}]})
    assert resp.status_code == 200
    assert resp.json()["max_concurrency"] == 3
    assert resp.json()["call_windows"] == [{"days": [1], "start": "08:00", "end": "12:00"}]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_campaign_api.py -k "scheduling or update_campaign" -q`
Expected: FAIL（字段被 Pydantic 忽略/404 PUT 不存在）

- [ ] **Step 3: 最小实现**

`CampaignCreateRequest` 追加三个字段（`narrowband` 之后）；`create_campaign` 端点在 `repo.create_campaign(...)` 调用处加：

```python
        call_windows=parse_call_windows(req.call_windows),
        max_concurrency=max(0, int(req.max_concurrency or 0)),
        redispatch=_clean_redispatch(req.redispatch),
```

模块级 helper（`_progress` 附近）：

```python
_REDISPATCH_OUTCOMES = ("no_answer", "rejected", "failed")


def _clean_redispatch(raw: dict | None) -> dict:
    """重拨策略清洗：max_attempts≥0、interval_minutes>0 才有意义、on 白名单剔除。"""
    if not isinstance(raw, dict):
        return {}
    try:
        max_attempts = max(0, int(raw.get("max_attempts") or 0))
    except (TypeError, ValueError):
        return {}
    try:
        interval = float(raw.get("interval_minutes") or 0)
    except (TypeError, ValueError):
        return {}
    if max_attempts <= 0 or interval <= 0:
        return {}
    on = [str(x) for x in (raw.get("on") or []) if str(x) in _REDISPATCH_OUTCOMES]
    return {"max_attempts": max_attempts, "interval_minutes": interval, "on": on}


class CampaignUpdateRequest(BaseModel):
    name: str | None = None
    template_id: str | None = None
    persona_id: str | None = None
    language: str | None = None
    gap_seconds: int | None = None
    site_id: str | None = None
    call_windows: list[dict] | None = None
    max_concurrency: int | None = None
    redispatch: dict | None = None


@app.put("/api/campaigns/{campaign_id}")
def update_campaign(campaign_id: str, req: CampaignUpdateRequest, request: Request) -> dict:
    """改战役配置：running 拒改（409，运行中时段/并发锁定——对齐惜客通语义）；
    draft/paused/stopped 可改。字段只增不改默认语义。"""
    _gate_page(request, "campaigns")
    camp = deny_cross_account(request, _repo().get_campaign(campaign_id))
    if not camp:
        raise HTTPException(404, "campaign not found")
    if str(camp.get("status") or "") == "running":
        raise HTTPException(409, "campaign is running — pause it first")
    fields: dict = {k: v for k, v in {
        "name": req.name, "template_id": req.template_id, "persona_id": req.persona_id,
        "language": req.language, "gap_seconds": req.gap_seconds, "site_id": req.site_id,
    }.items() if v is not None}
    if req.call_windows is not None:
        fields["call_windows_json"] = json.dumps(
            parse_call_windows(req.call_windows), ensure_ascii=False)
    if req.max_concurrency is not None:
        fields["max_concurrency"] = max(0, int(req.max_concurrency))
    if req.redispatch is not None:
        cleaned = _clean_redispatch(req.redispatch)
        fields["redispatch_json"] = json.dumps(cleaned, ensure_ascii=False) if cleaned else ""
    updated = _repo().update_campaign(campaign_id, **fields) or camp
    _audit("campaign.update", subject_type="campaign", subject_id=campaign_id,
           account_id=str(camp.get("account_id") or ""),
           detail={"fields": sorted(fields)})
    return updated
```

import 区补 `parse_call_windows`（`from .campaign import parse_call_windows`）与确认 `json` 已 import。注意 `update_campaign` 路由函数名与 repo 方法重名无碍（模块命名空间不同），但 FastAPI 端点函数名唯一即可。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_campaign_api.py tests/test_campaign_loop.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add apps/control-plane/control_plane/main.py tests/test_campaign_api.py
git commit -m "feat(cp): 战役创建带时段/并发/重拨 + PUT 编辑端点（running 锁定）"
```

---

### Task 5: 工作台统计端点 + 通话时长落点

**Files:**
- Modify: `apps/control-plane/control_plane/main.py`（5 个 call 终态转移点 + 新端点；锚点行：:1464 abandoned 清扫、:1524 report 收尾、:1697 dial-result answered→ACTIVE、:1699 dial-result 失败→ENDED、:4022/:4044 主管接管/挂断）
- Test: `tests/test_stats_dashboard.py`（新建）

**Interfaces:**
- Produces: `GET /api/stats/dashboard?account_id=` →

```json
{
  "concurrency": {"current": 2},
  "calls": {"today": 12, "total": 340, "answered": 210, "answer_rate": 0.6176},
  "duration_buckets": {"0-15": 40, "15-30": 55, "30-60": 70, "60-90": 25, "90+": 20},
  "agents": [{"user_id": "u-1", "name": "阿明", "calls": 30, "answered": 21}],
  "tags": {"disposition": {"completed": 100, "no_answer": 88}, "whatsapp": {"captured": 12, "offered": 5, "handled": 3}}
}
```

口径：`current`=status==active 计数；`answered`=ENDED 且 disposition 不在 {no_answer,rejected,failed}；`answer_rate`=answered/ENDED 总数（ENDED=0 → 0.0）；`today`=created_at 落**本地**自然日；`duration_buckets` 只统计 `duration_s>0` 的通话，桶界 [0,15)/[15,30)/[30,60)/[60,90)/[90+,∞)（`90+` 含 90）；`agents` 按 created_by 分组（空串=战役单剔除），join users 显示名，按 calls 降序截 8。gate 用 `_gate_page(request, "calls")` + `scoped_account`（工作台全角色可见）。

- [ ] **Step 1: 写失败测试**（`tests/test_stats_dashboard.py` 新建；建数据用文件内 TestClient + repo 注入模式，参考 `tests/test_campaign_api.py` 的 client fixture 与 `tests/test_session_reports.py` 的造通话方式）

```python
def test_dashboard_aggregates(client_with_repo):
    repo = client_with_repo.repo
    _seed_call(repo, status="active", created_days_ago=0)                      # 并发 1
    _seed_call(repo, status="ended", disposition="completed", duration_s=45,
               created_days_ago=0, created_by="u-1")                           # 今日接通 30-60 桶
    _seed_call(repo, status="ended", disposition="no_answer", duration_s=0,
               created_days_ago=0, created_by="u-1")                           # 未接通
    _seed_call(repo, status="ended", disposition="completed", duration_s=120,
               created_days_ago=2, created_by="u-2")                           # 昨天不计今日、90+ 桶
    _seed_call(repo, status="ended", disposition="", whatsapp_status="captured",
               duration_s=20, created_days_ago=0, created_by="u-1")
    data = client_with_repo.client.get("/api/stats/dashboard").json()
    assert data["concurrency"] == {"current": 1}
    assert data["calls"]["today"] == 4 and data["calls"]["total"] == 5
    assert data["calls"]["answered"] == 3
    assert data["duration_buckets"]["30-60"] == 1 and data["duration_buckets"]["90+"] == 1
    assert data["duration_buckets"]["15-30"] == 1
    assert [a["user_id"] for a in data["agents"]] == ["u-1", "u-2"]
    assert data["agents"][0]["calls"] == 3 and data["agents"][0]["answered"] == 2
    assert data["tags"]["whatsapp"]["captured"] == 1
    assert data["tags"]["disposition"]["no_answer"] == 1
```

（`_seed_call` helper：直接 `repo.create_call(...)` + `repo.update_call(...)` 按参数落 status/disposition/duration_s/created_by；created_at 直改——内存仓 dict 可写，SQL 仓用 `update_call` 不覆盖 created_at 时允许测试里经 ORM/session 改或接受「today 判定走 created_at、SQL fixture 用裸 SQL UPDATE」，以文件内现成手段为准，测试注明口径。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_stats_dashboard.py -q`
Expected: FAIL（404）

- [ ] **Step 3: 最小实现**

终态转移点改造：模块级加 helper（`_progress` 附近）：

```python
def _call_end_fields(call: dict) -> dict:
    """通话终态时间戳落点（2026-09-17 仪表盘口径）：ended_at=now、duration_s=ended-started。

    started_at 为空（未接通/从未 ACTIVE）→ duration_s=0。全部 UTC naive，与 created_at 同域。
    """
    from .campaign import _parse_updated_at, _utcnow_naive
    ended = _utcnow_naive()
    started = _parse_updated_at(call.get("started_at"))
    duration = int((ended - started).total_seconds()) if started and ended >= started else 0
    return {"ended_at": ended, "duration_s": duration}
```

五个转移点各自在既有 `update_call(...)` 调用上加 `**_call_end_fields(call)`（:1699/:4022/:4044 处先取 `call` 局部变量已有；:1464/:1524 同理，:1524 处若函数内变量名不同以实际为准）；:1697 answered→ACTIVE 处加 `started_at=_utcnow_naive()`（import 自 `.campaign` 或复用 main 内已有 `_utcnow_iso` 族——用 `_utcnow_naive` datetime 形态，DateTime 列直收）。

新端点（reports 区段附近）：

```python
_DURATION_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("0-15", 0, 15), ("15-30", 15, 30), ("30-60", 30, 60), ("60-90", 60, 90), ("90+", 90, None))
_ANSWERED_EXCLUDED = {"no_answer", "rejected", "failed"}


def _duration_bucket(duration_s: int) -> str | None:
    for name, low, high in _DURATION_BUCKETS:
        if duration_s >= low and (high is None or duration_s < high):
            return name
    return None


@app.get("/api/stats/dashboard")
def stats_dashboard(request: Request, account_id: str = "acc-001") -> dict:
    """工作台仪表盘单端点（2026-09-17）。口径见 plan Task 5；P0 全量 Python 聚合。"""
    _gate_page(request, "calls")
    account_id = scoped_account(request, account_id)
    calls = _repo().list_calls(account_id)
    now_local = datetime.now().astimezone()
    today_prefix = now_local.strftime("%Y-%m-%d")
    ended = [c for c in calls if str(c.get("status") or "") in
             (CallStatus.ENDED.value, CallStatus.FAILED.value)]
    answered = [c for c in ended if str(c.get("disposition") or "") not in _ANSWERED_EXCLUDED]
    buckets = {name: 0 for name, _, _ in _DURATION_BUCKETS}
    for call in answered:
        bucket = _duration_bucket(int(call.get("duration_s") or 0))
        if bucket:
            buckets[bucket] += 1
    by_agent: dict[str, dict] = {}
    for call in calls:
        uid = str(call.get("created_by") or "")
        if not uid:
            continue
        slot = by_agent.setdefault(uid, {"user_id": uid, "name": uid, "calls": 0, "answered": 0})
        slot["calls"] += 1
        if call in answered:
            slot["answered"] += 1
    for user in _repo().list_users():
        slot = by_agent.get(str(user.get("id") or ""))
        if slot:
            slot["name"] = str(user.get("display_name") or user.get("username") or slot["name"])
    disposition_counts: dict[str, int] = {}
    whatsapp_counts: dict[str, int] = {}
    for call in calls:
        if d := str(call.get("disposition") or ""):
            disposition_counts[d] = disposition_counts.get(d, 0) + 1
        if w := str(call.get("whatsapp_status") or ""):
            whatsapp_counts[w] = whatsapp_counts.get(w, 0) + 1
    return {
        "concurrency": {"current": sum(1 for c in calls if str(c.get("status") or "") == CallStatus.ACTIVE.value)},
        "calls": {"today": sum(1 for c in calls
                               if str(c.get("created_at") or "").startswith(today_prefix)),
                  "total": len(calls), "answered": len(answered),
                  "answer_rate": round(len(answered) / len(ended), 4) if ended else 0.0},
        "duration_buckets": buckets,
        "agents": sorted(by_agent.values(), key=lambda a: (-a["calls"], a["user_id"]))[:8],
        "tags": {"disposition": disposition_counts, "whatsapp": whatsapp_counts},
    }
```

（`list_users`/用户字典键名以 repository 实际方法为准——先 grep `def list_users`；若无该方法则 fallback `_gate` 下已注入的 users 查询函数，在实现时以实际为准并在 report 里注明所选调用。`call in answered` 是身份比较——改为按 call id 集合比较避免 dict 相等陷阱：`answered_ids = {c["id"] for c in answered}`。）
`datetime` 已在 main.py import 区（确认补 `from datetime import datetime` 若缺）。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_stats_dashboard.py tests/test_session_reports.py tests/test_campaign_api.py -q`
Expected: PASS（时长落点改动不破坏既有结算/拨号结果断言）

- [ ] **Step 5: 提交**

```bash
git add apps/control-plane/control_plane/main.py tests/test_stats_dashboard.py
git commit -m "feat(cp): /api/stats/dashboard 工作台聚合端点 + 通话 started/ended/duration 落点"
```

---

### Task 6: Web 工作台仪表盘

**Files:**
- Modify: `apps/web/lib/api.ts`（api 对象内 `reportsSummary` 旁加一个方法）
- Modify: `apps/web/components/dashboard-page.tsx`（重写展示层，保留 AppShell/加载/错误骨架与快捷入口）
- Test: 无组件测试基建——门禁为 `npx tsc --noEmit && npm run build`

**Interfaces:**
- Consumes: Task 5 端点形状（plan Task 5 的 JSON 即契约）。
- Produces: `api.statsDashboard()`。

- [ ] **Step 1: api.ts**（`reportsSummary` 行后）

```ts
  // 工作台仪表盘聚合（2026-09-17）：并发/呼叫量/接通率/时长分布/坐席排行/标记。
  statsDashboard: () => request<Record<string, unknown>>("/api/stats/dashboard"),
```

- [ ] **Step 2: dashboard-page.tsx 重写展示层**

规格（保留文件头 `"use client"`、`DashboardPage` 外壳、`useAccount`/`useControlPlaneReady` 冷启动自愈逻辑不动；数据源换成 `Promise.all([api.statsDashboard(), api.listCalls(accountId, "")])`）：
1. 六张 KPI 卡（`grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6`）：当前并发（`concurrency.current`）、今日呼叫（副行 `累计 {total}`）、客户接通率（`answer_rate` 百分比一位小数，副行 `接通 {answered}`）、今日接通量（副行「通话中 {current}」）、标记总数（`tags` 两 map 求和）、最近会话数（沿用 calls.length）。
2. 时长分布卡：`duration_buckets` 五桶横条（div 宽度 = `count / maxCount * 100%`，`h-2 rounded bg-accent`，桶名+数量右侧），空数据画零条。
3. 坐席排行卡：表格（排名/坐席/呼叫/接通），无数据显示「暂无数据」。
4. 标记统计卡：两组表（disposition 用中文映射：completed=已完成、declined=婉拒、abandoned=超时放弃、no_answer=未接听、rejected=拒接、failed=失败、transferred=转人工，未知键原样显示；whatsapp：offered=已邀约、captured=已捕获、handled=已对接）。
5. 最近会话列表 + 快捷入口 + 控制面状态：原样保留。
6. 所有数字用 `?? "—"` 兜底；接口 404/字段缺失不白屏（`stats` 状态可空，渲染全兜底）。

- [ ] **Step 3: 构建门禁**

Run: `cd apps/web && npx tsc --noEmit && npm run build`
Expected: 双绿

- [ ] **Step 4: 提交**

```bash
git add apps/web/lib/api.ts apps/web/components/dashboard-page.tsx
git commit -m "feat(web): 工作台仪表盘六卡+时长分布/坐席排行/标记统计（CSS 条形，零新依赖）"
```

---

### Task 7: Web 战役表单与详情（时段/并发/重拨/编辑）

**Files:**
- Modify: `apps/web/lib/api.ts`（`deleteCampaign` 后加 `updateCampaign`）
- Modify: `apps/web/app/(app)/campaigns/page.tsx`（向导第①步 + 详情头部 + 编辑弹层 + attempts 列）
- Test: 门禁 `npx tsc --noEmit && npm run build`

**Interfaces:**
- Consumes: Task 4 的 POST/PUT 契约；campaign 响应键 `call_windows`/`max_concurrency`/`redispatch`、item 键 `attempts`。

- [ ] **Step 1: api.ts**

```ts
  updateCampaign: (id: string, body: Record<string, unknown>) =>
    request<Record<string, unknown>>(`/api/campaigns/${id}`, { method: "PUT", body: JSON.stringify(body) }),
```

- [ ] **Step 2: campaigns/page.tsx**

1. 向导第①步（基本信息）追加三组控件（state 与提交 body 同步扩展）：
   - 「外呼时段」：最多 3 行，每行 = 星期一~日 checkbox（ISO 1..7）+ `<input type="time">` 起/止；「+ 添加时段」按钮在 3 行后隐藏；行可删。
   - 「最大并发」：`<input type="number" min={0}>`，帮助文案「0 = 不限制；默认 1 = 逐通串行」。
   - 「自动重拨」：开关（默认关）+ 开时展示 max_attempts（number 1..5）、interval_minutes（number ≥1）、结果勾选（未接听/拒接/失败 → no_answer/rejected/failed）。
   - 提交 body：`call_windows`（过滤 days 空行）、`max_concurrency`、`redispatch`（关=不传）。
2. 详情：头部展示时段摘要（「周一~五 08:00-18:00 · 周六 09:00-12:00」格式的紧凑串：days 连续段用「周一~周五」、否则「/」分隔）+「并发 {n===0?'不限':n}」+「重拨 {max}次/{interval}分」；名单表加「次」列（`attempts`）。
3. 编辑：status 非 running 的详情页显示「编辑配置」按钮 → 弹层三字段预填 → `api.updateCampaign` 成功后刷新详情；409/4xx 走页面既有错误提示路径。

- [ ] **Step 3: 构建门禁**

Run: `cd apps/web && npx tsc --noEmit && npm run build`
Expected: 双绿

- [ ] **Step 4: 提交**

```bash
git add apps/web/lib/api.ts "apps/web/app/(app)/campaigns/page.tsx"
git commit -m "feat(web): 战役时段/并发/重拨表单与配置编辑（running 锁定提示）"
```

---

### Task 8: 集成收尾（全量门禁 + 文档）

**Files:**
- Modify: `AGENTS.md`（campaigns 段一句话 + 首页一句话）
- Verify: 全仓门禁

- [ ] **Step 1: 全量 Python 门禁**

Run: `.venv312/bin/python -m compileall -q apps packages services tools scripts && .venv312/bin/python -m pytest -q`
Expected: 0 失败（含术语门禁/DB 可移植门禁）

- [ ] **Step 2: web 门禁**

Run: `cd apps/web && npx tsc --noEmit && npm run build`
Expected: 双绿

- [ ] **Step 3: Supabase 引导件回环校验**

Run: `.venv312/bin/python scripts/dump_postgres_ddl.py`
Expected: 产物含新 6 列，干净库应用 + build_engine 双跑零变更

- [ ] **Step 4: 文档**

AGENTS.md「Architecture Boundaries」campaigns 相关行补一句：战役支持双层外呼时段窗（全局 settings.campaign + 任务 call_windows，≤3 组取交集）、任务级 max_concurrency（0=不限）、未接通自动重拨（redispatch，attempts 复用）；首页工作台数据源 `/api/stats/dashboard`。

```bash
git add AGENTS.md
git commit -m "docs(agents): 战役调度与工作台仪表盘口径备注"
```

---

## Self-Review 记录

- 覆盖核对：需求四项（首页八模块 P0 可实现子集、战役时段、并发、重拨）→ T1-T7 全覆盖；标记统计 P0 以 disposition+whatsapp 兜底（意向档位体系留 P2，超出本计划）；在线坐席 P0 以坐席排行替代（presence 留 P2）——两处降级已在任务接口注释与 AGENTS.md 文档行如实标注。
- 占位扫描：无 TBD/TODO；Task 3 探针与 Task 5 聚合里的「以文件内实际类名/方法名为准」是**实现期核对点**而非行为留白，均给出了 fallback 路径与判定准则。
- 类型一致性：`call_windows`（解析后 list）/`call_windows_json`（列，JSON 串）/`redispatch`/`redispatch_json`/`max_concurrency` 在 T1 列名、T2 纯函数、T3 循环、T4 API、T7 web 五处口径一致；`attempts` 复用既有列不新增。
