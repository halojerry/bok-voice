# 外呼战役 + 名册认领 + 官方 SIP 外播（模拟联调档）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打通「AI 主动外呼客户 → 话术通话 → 自动挂断 → 号码自动落盘名册 → 认领 → 自动下一通」闭环；SIP 按官方姿势接入，本周期跑 mock 档模拟联调（真语音客户），real 档即插即用。

**Architecture:** 三波交付。Wave 1 名册（roster_entries 新表 + captured 自动入册 + 认领 UI）；Wave 2 SIP 外播（dialer 双后端：real=官方 CreateSIPParticipant / mock=CP 派生真语音 mock 客户子进程；agent 外呼模式=先进房→拨号→接通才 session.start）；Wave 3 campaign（campaigns/campaign_items 新表 + CP 后台串行循环 + UI + E2E）。

**Tech Stack:** FastAPI + SQLAlchemy（SQL 可移植）/ livekit-agents 1.8.0 + livekit-api SIP API / Next.js static export / pytest + node --test。

**Spec:** `docs/superpowers/specs/2026-09-12-outbound-campaign-roster-design.md`

## Global Constraints

- **worktree**：`/Users/halo/Documents/bok/va-campaign`（branch `feat/outbound-campaign`）。所有路径相对该根。
- **测试命令**：`cd /Users/halo/Documents/bok/va-campaign && /Users/halo/Documents/bok/voice-assistant/.venv312/bin/python -m pytest <target> -q`（tests/conftest.py 会把 worktree 的 packages/apps 插到 sys.path[0]，勿用主仓 .venv 的 site-packages 解析）。
- **Python 风格**：PEP 8、4 空格、`from __future__ import annotations`、签名带 type hints；改完跑 `python -m compileall -q apps packages services tools scripts`（用上述 venv python）。
- **SQL 可移植**：新表靠 `models.create_all`；新列走 `deps.py build_engine()` 的 `_ensure_column`（先查列存在）；禁 sqlite 专有语法（`tests/test_db_portability.py` 门禁）。
- **粤语规范值 = `cantonese`**：任何新代码/文档/测试禁旧拼写（`tests/test_cantonese_terminology.py` 扫全部跟踪文件含 .md）。
- **语言三态** `zh / cantonese / en`；channel 值域 `whatsapp / wechat`。
- **官方组件优先**：real 档拨号只用官方 `livekit.api` SIP API（`create_sip_participant`）；不引入 Twilio SDK/自造 SIP 栈。
- **env kill-switch 惯例**：`BOK_SIP_MODE`（缺省 mock，优先于 settings DB）。
- **Web 验收**：`cd apps/web && npx tsc --noEmit && npm run build`（worktree 首次需 `npm ci`）。
- **Conventional commits**（带 scope）；每 task 一提交。
- **E2E 不许假绿**：mock 客户用真 TTS sidecar 合成语音（复用 `scripts/e2e_real_customer.py` 的 `tts_pcm` 姿势）。
- **单飞防叠/失败不阻主链**：子进程派生复用 `apps/control-plane/control_plane/pregen.py` 的 `_spawn_detached` 模式。

---

### Task 1: RosterEntry 模型 + repo 方法（双后端）

**Files:**
- Modify: `packages/business-db/bok_voice_business_db/models.py`（CallSession 类后追加）
- Modify: `packages/business-db/bok_voice_business_db/repository.py`（SqlAlchemy 527 行 settings 前插入 roster 段；InMemory 641 类内加同款）
- Test: `tests/test_roster_repo.py`（新建）

**Interfaces:**
- Produces（后续 task 依赖的精确签名）:
  - `repo.upsert_roster_entry(*, account_id: str, call_id: str, object_id: str, channel: str, number: str, display_name: str = "", summary: str = "") -> dict`
  - `repo.list_roster(account_id: str = "acc-001", status: str = "", channel: str = "") -> list[dict]`
  - `repo.get_roster_entry(entry_id: str) -> dict | None`
  - `repo.update_roster_entry(entry_id: str, **fields) -> dict | None`
  - roster dict 键：`id/account_id/call_id/object_id/channel/number/display_name/summary/status/claimed_by/claimed_at/created_at`（datetime 序列化为 ISO 字符串，与 `_call_to_dict` 同款）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_roster_repo.py
from __future__ import annotations

from bok_voice_business_db.repository import InMemoryBusinessRepository


def _repo() -> InMemoryBusinessRepository:
    return InMemoryBusinessRepository()


def test_upsert_creates_and_dedupes():
    repo = _repo()
    a = repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-a", object_id="obj-1",
        channel="whatsapp", number="64320111", display_name="陈生", summary="s1",
    )
    assert a["status"] == "unclaimed"
    # 同 object+channel+number 重复捕获：更新来源，不新建
    b = repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-b", object_id="obj-1",
        channel="whatsapp", number="64320111", display_name="陈生", summary="s2",
    )
    assert b["id"] == a["id"] and b["call_id"] == "call-b" and b["summary"] == "s2"
    assert len(repo.list_roster()) == 1
    # handled 后新捕获另起新行
    repo.update_roster_entry(a["id"], status="handled")
    c = repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-c", object_id="obj-1",
        channel="whatsapp", number="64320111",
    )
    assert c["id"] != a["id"] and c["status"] == "unclaimed"


def test_claim_flow():
    repo = _repo()
    e = repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-a", object_id="obj-1",
        channel="wechat", number="12345678",
    )
    repo.update_roster_entry(e["id"], status="claimed", claimed_by="acc-001")
    got = repo.get_roster_entry(e["id"])
    assert got["status"] == "claimed" and got["claimed_by"] == "acc-001"
    assert repo.list_roster(status="claimed", channel="wechat")[0]["id"] == e["id"]
    assert repo.list_roster(channel="whatsapp") == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/halo/Documents/bok/va-campaign && /Users/halo/Documents/bok/voice-assistant/.venv312/bin/python -m pytest tests/test_roster_repo.py -q`
Expected: FAIL `AttributeError: ... has no attribute 'upsert_roster_entry'`

- [ ] **Step 3: 模型 + repo 实现**

`models.py`（CallSession 类之后；`_uuid` 风格沿用文件头现有 import——`datetime` 已有）：

```python
class RosterEntry(Base):
    """名册（认领池）：通话中捕获的客户 WhatsApp/微信号码，专员认领后对接。

    去重键 = account+object+channel+number 且 status != handled；captured 自动入册。
    status: unclaimed(待认领) / claimed(已认领) / handled(已对接)。
    """
    __tablename__ = "roster_entries"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    call_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    object_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    channel: Mapped[str] = mapped_column(String(16), default="whatsapp")
    number: Mapped[str] = mapped_column(String(64), default="")
    display_name: Mapped[str] = mapped_column(String(255), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="unclaimed")
    claimed_by: Mapped[str] = mapped_column(String(64), default="")
    claimed_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
```

`repository.py` SqlAlchemyBusinessRepository（`# ---- settings ----` 前插入；`_uuid()` 文件内已有，`utcnow` 从 models import 已有）：

```python
    # ---- roster（名册认领池）----

    @staticmethod
    def _roster_to_dict(row: models.RosterEntry) -> dict:
        return {
            "id": row.id, "account_id": row.account_id, "call_id": row.call_id,
            "object_id": row.object_id, "channel": row.channel, "number": row.number,
            "display_name": row.display_name, "summary": row.summary,
            "status": row.status, "claimed_by": row.claimed_by,
            "claimed_at": row.claimed_at.isoformat() if row.claimed_at else "",
            "created_at": row.created_at.isoformat() if row.created_at else "",
        }

    def upsert_roster_entry(self, *, account_id: str, call_id: str, object_id: str,
                            channel: str, number: str, display_name: str = "",
                            summary: str = "") -> dict:
        from sqlalchemy import and_
        row = (
            self.session.query(models.RosterEntry)
            .filter(and_(
                models.RosterEntry.account_id == account_id,
                models.RosterEntry.object_id == object_id,
                models.RosterEntry.channel == channel,
                models.RosterEntry.number == number,
                models.RosterEntry.status != "handled",
            ))
            .order_by(models.RosterEntry.created_at.desc())
            .first()
        )
        if row is None:
            row = models.RosterEntry(
                id=f"roster-{_uuid()[:8]}", account_id=account_id, call_id=call_id,
                object_id=object_id, channel=channel, number=number,
                display_name=display_name, summary=summary,
            )
            self.session.add(row)
        else:
            row.call_id = call_id
            if display_name:
                row.display_name = display_name
            if summary:
                row.summary = summary
        self.session.commit()
        return self._roster_to_dict(row)

    def list_roster(self, account_id: str = "acc-001", status: str = "",
                    channel: str = "") -> list[dict]:
        from sqlalchemy import and_
        q = self.session.query(models.RosterEntry).filter(
            models.RosterEntry.account_id == account_id)
        conds = []
        if status:
            conds.append(models.RosterEntry.status == status)
        if channel:
            conds.append(models.RosterEntry.channel == channel)
        if conds:
            q = q.filter(and_(*conds))
        rows = q.order_by(models.RosterEntry.created_at.desc()).all()
        return [self._roster_to_dict(r) for r in rows]

    def get_roster_entry(self, entry_id: str) -> dict | None:
        row = self.session.get(models.RosterEntry, entry_id)
        return self._roster_to_dict(row) if row else None

    def update_roster_entry(self, entry_id: str, **fields) -> dict | None:
        row = self.session.get(models.RosterEntry, entry_id)
        if not row:
            return None
        for key in ("call_id", "object_id", "channel", "number", "display_name",
                    "summary", "status", "claimed_by", "claimed_at"):
            if key in fields and fields[key] is not None:
                setattr(row, key, fields[key])
        self.session.commit()
        return self._roster_to_dict(row)
```

InMemoryBusinessRepository 加同语义实现（`self.roster: dict[str, dict] = {}` 于 `__init__`（settings 初始化行旁），列表按 created_at 逆序，upsert 扫 dict 找 `status != "handled"` 的同键行）：

```python
    def upsert_roster_entry(self, *, account_id, call_id, object_id, channel, number,
                            display_name="", summary=""):
        for row in self.roster.values():
            if (row["account_id"] == account_id and row["object_id"] == object_id
                    and row["channel"] == channel and row["number"] == number
                    and row["status"] != "handled"):
                row["call_id"] = call_id
                if display_name:
                    row["display_name"] = display_name
                if summary:
                    row["summary"] = summary
                return dict(row)
        from datetime import datetime, timezone
        entry = {
            "id": f"roster-{uuid.uuid4().hex[:8]}", "account_id": account_id,
            "call_id": call_id, "object_id": object_id, "channel": channel,
            "number": number, "display_name": display_name, "summary": summary,
            "status": "unclaimed", "claimed_by": "", "claimed_at": "",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.roster[entry["id"]] = entry
        return dict(entry)

    def list_roster(self, account_id="acc-001", status="", channel=""):
        rows = [dict(r) for r in self.roster.values() if r["account_id"] == account_id
                and (not status or r["status"] == status)
                and (not channel or r["channel"] == channel)]
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)

    def get_roster_entry(self, entry_id):
        row = self.roster.get(entry_id)
        return dict(row) if row else None

    def update_roster_entry(self, entry_id, **fields):
        row = self.roster.get(entry_id)
        if not row:
            return None
        row.update({k: v for k, v in fields.items() if v is not None})
        return dict(row)
```

注意 InMemory 已 import 的 `uuid`（create_call 用过 `_uuid` 的话沿用其内部姿势；没有就 `import uuid`）。新表由 `models.create_all` 自动建，**无需迁移段**。

- [ ] **Step 4: 跑测试通过**

Run: 同 Step 2。Expected: 2 passed

- [ ] **Step 5: 全量回归 + 提交**

```bash
cd /Users/halo/Documents/bok/va-campaign
/Users/halo/Documents/bok/voice-assistant/.venv312/bin/python -m pytest tests/test_roster_repo.py tests/test_db_portability.py -q
/Users/halo/Documents/bok/voice-assistant/.venv312/bin/python -m compileall -q packages
git add packages/business-db tests/test_roster_repo.py
git commit -m "feat(db): 名册 roster_entries 表+双后端 repo——captured 自动入册数据层"
```

---

### Task 2: 捕获渠道透传（flow 纯函数 + 上报链）

**Files:**
- Modify: `apps/agent/agent_runtime/flow.py`（`detect_whatsapp_signal` 附近）
- Modify: `apps/agent/agent_runtime/control_plane.py:118`（`report_whatsapp`）
- Modify: `apps/agent/agent_runtime/agent.py`（两处 report 调用点：~1303 accumulate-flush、~2417 即时路径）
- Modify: `apps/control-plane/control_plane/main.py:919`（`report_whatsapp` 端点 + captured 自动入册）
- Modify: `apps/control-plane/control_plane/schemas.py`（`WhatsAppCaptureRequest` 加 channel）
- Test: `tests/test_roster_channel.py`（新建）

**Interfaces:**
- Produces:
  - `flow.channel_from_text(user_text: str) -> str`（`"wechat"` | `"whatsapp"`）
  - `ControlPlaneClient.report_whatsapp(call_id: str, number: str = "", channel: str = "") -> None`
  - `WhatsAppCaptureRequest` 新字段 `channel: str = ""`；CP 端点 captured 时自动 `upsert_roster_entry`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_roster_channel.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.flow import channel_from_text


def test_channel_from_text():
    assert channel_from_text("我微信是一二三四五六七八") == "wechat"
    assert channel_from_text("加我WeChat好朋友") == "wechat"
    assert channel_from_text("我WhatsApp係六四三二零一一") == "whatsapp"
    assert channel_from_text("冇講渠道，净係报数") == "whatsapp"
    assert channel_from_text("") == "whatsapp"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `... -m pytest tests/test_roster_channel.py -q`
Expected: FAIL `ImportError: cannot import name 'channel_from_text'`

- [ ] **Step 3: 实现**

`flow.py`（`_WHATSAPP_DECLINE` 定义旁）：

```python
# 捕获渠道判定：客户报号码句里讲嘅係微信定 WhatsApp——微信系词命中 → wechat,
# 否则 whatsapp（缺省主渠道,与对象 contact_channel 缺省一致）。名册 channel 数据源。
_WECHAT_MARK = re.compile(r"(微信|wechat|we\s?chat)", re.IGNORECASE)


def channel_from_text(user_text: str) -> str:
    """捕获号码时客户讲的渠道：微信系词 → wechat，否则 whatsapp。"""
    return "wechat" if _WECHAT_MARK.search(user_text or "") else "whatsapp"
```

`control_plane.py`（client）：

```python
    async def report_whatsapp(self, call_id: str, number: str = "", channel: str = "") -> None:
        # ...(原实现),json body 加 "channel": channel
```

（读原函数，body dict 多放一个键。）

`schemas.py:175` `WhatsAppCaptureRequest` 加字段：

```python
class WhatsAppCaptureRequest(BaseModel):
    # ...(原 number 字段),新增:
    channel: str = ""   # whatsapp | wechat,缺省由 CP 按对象 contact_channel 推断
```

`agent.py` 两处调用点：
- 即时路径（~2417，`_report()` 内）：`await cp.report_whatsapp(call_id, _num, channel=channel_from_text(<该轮传给 detect_whatsapp_signal 的同一个文本变量>))`——在该 handler 内向上找 `detect_whatsapp_signal(` 的实参变量名（通常是 `user_text` / `text`），用同一变量。
- accumulate-flush 路径（~1303）：flush 时原句可能已不在作用域——在**检测处**（`sig = detect_whatsapp_signal(...)` 同块）把渠道存进闭包账本 `_wa_channel = {"v": "whatsapp"}`，flush 处 `await cp.report_whatsapp(call_id, num, channel=_wa_channel["v"])`。

`main.py` `report_whatsapp` 端点（919）：

```python
    if number:
        # 名册自动入册（Wave1）：captured 带号码 → upsert；channel 归一——
        # 优先 agent 上报（客户原话渠道词），缺省按对象 contact_channel 推断。
        channel = (req.channel or "").strip().lower()
        if channel not in ("whatsapp", "wechat"):
            obj_channel = ""
            if call.get("object_id"):
                obj = _repo().get_object(call.get("object_id")) or {}
                obj_channel = str(obj.get("contact_channel") or "")
            channel = "wechat" if "微信" in obj_channel or "wechat" in obj_channel.lower() else "whatsapp"
        display_name = ""
        summary = ""
        try:
            obj = _repo().get_object(call.get("object_id") or "") or {}
            display_name = str(obj.get("display_name") or "")
            settlement = _repo().get_settlement(call_id) or {}
            summary = str(settlement.get("summary") or "")[:300]
        except Exception:
            pass
        _repo().upsert_roster_entry(
            account_id=call.get("account_id", "acc-001"), call_id=call_id,
            object_id=call.get("object_id", ""), channel=channel, number=number,
            display_name=display_name, summary=summary,
        )
```

（放在现有 `updated = _repo().update_call(...)` 之后、`_audit` 之前；captured 分支两处（新捕获/改口覆写）都覆盖——直接放在函数末尾 `return` 前以 `if number:` 守卫即可，offered 无号码自然跳过。）

- [ ] **Step 4: 跑测试 + 全量回归**

Run: `... -m pytest tests/test_roster_channel.py tests/test_flow_controller.py -q`，再全量 `tests/ -q`
Expected: 全绿（现有 whatsapp 端点测试若断言 body 需同步补 channel 键——按失败信息修）

- [ ] **Step 5: 提交**

```bash
git add apps/agent apps/control-plane tests/test_roster_channel.py
git commit -m "feat(cp,agent): 捕获渠道透传+captured 自动入册名册——channel_from_text 纯函数"
```

---

### Task 3: 名册 API（列表/认领/释放/已对接）

**Files:**
- Modify: `apps/control-plane/control_plane/main.py`（whatsapp 端点后新增路由）
- Modify: `apps/web/lib/api.ts`（`api` 对象加 roster 方法）
- Test: `tests/test_roster_api.py`（新建；用 FastAPI TestClient + InMemory repo monkeypatch，参考现有 CP 端点测试的 client/repo 替换姿势——搜 `TestClient` 找同款）

**Interfaces:**
- Produces:
  - `GET /api/roster?account_id=&status=&channel=` → `list[dict]`
  - `POST /api/roster/{entry_id}/claim` body `{"claimed_by": str}` → entry
  - `POST /api/roster/{entry_id}/unclaim` → entry
  - `POST /api/roster/{entry_id}/handled` body `{"handled": bool}` → entry（true=已对接并同步来源 call `whatsapp_status=handled`；false=撤销回 unclaimed、call 回 captured/offered）
  - web: `api.listRoster(status?, channel?)` / `api.rosterClaim(id, claimedBy)` / `api.rosterUnclaim(id)` / `api.rosterHandled(id, handled)`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_roster_api.py
from __future__ import annotations

from bok_voice_business_db.repository import InMemoryBusinessRepository


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient
    from control_plane.main import app, _repo
    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    return TestClient(app), repo


def test_roster_claim_flow(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    entry = repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-x", object_id="obj-1",
        channel="whatsapp", number="64320111",
    )
    rows = client.get("/api/roster").json()
    assert rows[0]["id"] == entry["id"]
    r = client.post(f"/api/roster/{entry['id']}/claim", json={"claimed_by": "acc-001"})
    assert r.status_code == 200 and r.json()["status"] == "claimed"
    r = client.post(f"/api/roster/{entry['id']}/unclaim")
    assert r.json()["status"] == "unclaimed" and r.json()["claimed_by"] == ""
    r = client.post(f"/api/roster/{entry['id']}/handled", json={"handled": True})
    assert r.json()["status"] == "handled"
    r = client.get("/api/roster", params={"status": "handled"})
    assert len(r.json()) == 1
    assert client.post("/api/roster/roster-nope/handled", json={"handled": True}).status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

Run: `... -m pytest tests/test_roster_api.py -q`
Expected: FAIL（404 路由不存在）

- [ ] **Step 3: 实现端点**

`main.py`（`mark_whatsapp_handled` 端点后；`_audit` 沿用现有姿势）：

```python
class RosterClaimRequest(BaseModel):
    claimed_by: str = "acc-001"


class RosterHandledRequest(BaseModel):
    handled: bool = True


@app.get("/api/roster")
def list_roster(account_id: str = "acc-001", status: str = "", channel: str = "") -> list[dict]:
    return _repo().list_roster(account_id, status=status, channel=channel)


@app.post("/api/roster/{entry_id}/claim")
def roster_claim(entry_id: str, req: RosterClaimRequest) -> dict:
    from datetime import datetime, timezone
    entry = _repo().update_roster_entry(
        entry_id, status="claimed", claimed_by=req.claimed_by,
        claimed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    if not entry:
        raise HTTPException(404, "roster entry not found")
    _audit("roster.claim", subject_type="roster", subject_id=entry_id,
           account_id=req.claimed_by, detail={"claimed_by": req.claimed_by})
    return entry


@app.post("/api/roster/{entry_id}/unclaim")
def roster_unclaim(entry_id: str) -> dict:
    entry = _repo().update_roster_entry(entry_id, status="unclaimed", claimed_by="", claimed_at=None)
    if not entry:
        raise HTTPException(404, "roster entry not found")
    _audit("roster.unclaim", subject_type="roster", subject_id=entry_id, account_id="acc-001")
    return entry


@app.post("/api/roster/{entry_id}/handled")
def roster_handled(entry_id: str, req: RosterHandledRequest) -> dict:
    entry = _repo().get_roster_entry(entry_id)
    if not entry:
        raise HTTPException(404, "roster entry not found")
    if req.handled:
        entry = _repo().update_roster_entry(entry_id, status="handled") or entry
        # 同步来源通话（与通话内横幅停止逻辑同源）
        if entry.get("call_id"):
            try:
                _repo().update_call(entry["call_id"], whatsapp_status="handled")
            except Exception:
                pass
    else:
        entry = _repo().update_roster_entry(entry_id, status="unclaimed", claimed_by="", claimed_at=None) or entry
        if entry.get("call_id"):
            call = _repo().get_call(entry["call_id"]) or {}
            back = "captured" if str(call.get("customer_whatsapp") or "").strip() else "offered"
            try:
                _repo().update_call(entry["call_id"], whatsapp_status=back)
            except Exception:
                pass
    _audit("roster.handled", subject_type="roster", subject_id=entry_id,
           account_id="acc-001", detail={"handled": req.handled})
    return entry
```

（InMemory `update_roster_entry` 需允许显式清空 claimed_at——`v is not None` 过滤会挡 `claimed_at=None`。给 InMemory 版加白名单：`claimed_by=""` 可写、`claimed_at` 走 `fields.get("claimed_at", ...) or ""`。实现时按测试断言修正：unclaim 后 `claimed_by==""`；SQL 版 `claimed_at=None` 正常。）

`api.ts`（`api` 对象内，supervisor 方法附近）：

```ts
  listRoster: (status = "", channel = "") =>
    request<Record<string, unknown>[]>(`/api/roster?status=${status}&channel=${channel}`),
  rosterClaim: (id: string, claimedBy = "acc-001") =>
    request<Record<string, unknown>>(`/api/roster/${id}/claim`, { method: "POST", body: JSON.stringify({ claimed_by: claimedBy }) }),
  rosterUnclaim: (id: string) =>
    request<Record<string, unknown>>(`/api/roster/${id}/unclaim`, { method: "POST" }),
  rosterHandled: (id: string, handled: boolean) =>
    request<Record<string, unknown>>(`/api/roster/${id}/handled`, { method: "POST", body: JSON.stringify({ handled }) }),
```

- [ ] **Step 4: 跑测试通过 + 回归**（`tests/test_roster_api.py` + 全量）

- [ ] **Step 5: 提交**

```bash
git add apps/control-plane apps/web/lib/api.ts tests/test_roster_api.py
git commit -m "feat(cp): 名册认领 API——claim/unclaim/handled 联动通话横幅状态"
```

---

### Task 4: /roster 名册页 + 导航

**Files:**
- Create: `apps/web/app/(app)/roster/page.tsx`
- Modify: `apps/web/components/StageHeader.tsx:8-17`（NAV_ITEMS 加名册/外呼两项）
- Test: 手动验收 + `npx tsc --noEmit && npm run build`

**Interfaces:**
- Consumes: Task 3 的 `api.listRoster/rosterClaim/rosterUnclaim/rosterHandled`
- Produces: `/roster` 页面（后续 campaign 结果页链接到它）

- [ ] **Step 1: 加导航项**

`StageHeader.tsx` NAV_ITEMS（「会话」后加）：

```ts
  { href: "/roster", label: "名册" },
  { href: "/campaigns", label: "外呼" },
```

（外呼路由 Task 12 才建页——Next 静态导出对未知路由的 nav 点击会 404，可接受一周内；或本 task 先只加名册、外呼项留 Task 12 一起加。**选择：本 task 只加名册项**，外呼项归 Task 12。）

- [ ] **Step 2: 写页面（参照 `apps/web/app/(app)/objects/page.tsx` 的客户端组件姿势：useEffect 拉数、loading/empty 态用 `apps/web/components/app-shell.tsx` 导出的 `LoadingState/EmptyState`、样式类沿用 `text-sm muted` 等既有类）**

```tsx
"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { LoadingState, EmptyState } from "@/components/app-shell";

type RosterEntry = {
  id: string; call_id: string; object_id: string; channel: string; number: string;
  display_name: string; summary: string; status: string; claimed_by: string;
  claimed_at: string; created_at: string;
};

const STATUS_LABEL: Record<string, string> = {
  unclaimed: "待认领", claimed: "已认领", handled: "已对接",
};
const CHANNEL_LABEL: Record<string, string> = { whatsapp: "WhatsApp", wechat: "微信" };

export default function RosterPage() {
  const [rows, setRows] = useState<RosterEntry[] | null>(null);
  const [status, setStatus] = useState("");
  const [channel, setChannel] = useState("");
  const [copied, setCopied] = useState("");

  const reload = useCallback(async () => {
    setRows(null);
    try {
      setRows(await api.listRoster(status, channel) as RosterEntry[]);
    } catch {
      setRows([]);
    }
  }, [status, channel]);

  useEffect(() => { void reload(); }, [reload]);

  const act = async (fn: () => Promise<unknown>) => { await fn(); await reload(); };
  const copy = async (number: string) => {
    await navigator.clipboard.writeText(number);
    setCopied(number);
    setTimeout(() => setCopied(""), 1500);
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-medium">名册 · 认领池</h1>
        <select value={status} onChange={(e) => setStatus(e.target.value)} className="rounded border px-2 py-1 text-sm">
          <option value="">全部状态</option>
          <option value="unclaimed">待认领</option>
          <option value="claimed">已认领</option>
          <option value="handled">已对接</option>
        </select>
        <select value={channel} onChange={(e) => setChannel(e.target.value)} className="rounded border px-2 py-1 text-sm">
          <option value="">全部渠道</option>
          <option value="whatsapp">WhatsApp</option>
          <option value="wechat">微信</option>
        </select>
        <button onClick={() => void reload()} className="rounded border px-2 py-1 text-sm">刷新</button>
      </div>
      {rows === null ? <LoadingState /> : rows.length === 0 ? <EmptyState label="名册暂无条目" /> : (
        <table className="w-full text-sm">
          <thead><tr className="text-left muted">
            <th className="py-1">客户</th><th>渠道</th><th>号码</th><th>摘要</th>
            <th>状态</th><th>认领人</th><th>来源</th><th />
          </tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className="border-t">
                <td className="py-1.5">{r.display_name || r.object_id || "—"}</td>
                <td>{CHANNEL_LABEL[r.channel] || r.channel}</td>
                <td className="font-mono">{r.number}</td>
                <td className="max-w-64 truncate" title={r.summary}>{r.summary || "—"}</td>
                <td>{STATUS_LABEL[r.status] || r.status}</td>
                <td>{r.status === "claimed" ? r.claimed_by : "—"}</td>
                <td>{r.call_id ? <Link className="underline" href={`/calls`}>{r.call_id}</Link> : "—"}</td>
                <td className="whitespace-nowrap">
                  <button onClick={() => void copy(r.number)} className="mr-2 rounded border px-2 py-0.5">
                    {copied === r.number ? "已复制" : "复制"}
                  </button>
                  {r.status === "unclaimed" && (
                    <button onClick={() => void act(() => api.rosterClaim(r.id))} className="mr-2 rounded border px-2 py-0.5">认领</button>
                  )}
                  {r.status === "claimed" && (
                    <>
                      <button onClick={() => void act(() => api.rosterHandled(r.id, true))} className="mr-2 rounded border px-2 py-0.5">标已对接</button>
                      <button onClick={() => void act(() => api.rosterUnclaim(r.id))} className="rounded border px-2 py-0.5">释放</button>
                    </>
                  )}
                  {r.status === "handled" && (
                    <button onClick={() => void act(() => api.rosterHandled(r.id, false))} className="rounded border px-2 py-0.5">撤销</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
```

- [ ] **Step 3: 构建验收**

```bash
cd /Users/halo/Documents/bok/va-campaign/apps/web && npm ci && npx tsc --noEmit && npm run build
```

（npm ci 仅 worktree 首次；后续直接 tsc+build。）

- [ ] **Step 4: 提交**

```bash
git add apps/web
git commit -m "feat(web): 名册页——筛选/认领/复制号码/标已对接"
```

---

### Task 5: dialer 薄层（双后端 + 状态映射纯函数）

**Files:**
- Create: `apps/agent/agent_runtime/dialer.py`
- Test: `tests/test_dialer.py`（新建）

**Interfaces:**
- Produces:
  - 常量 `OUT_ANSWERED="answered" / OUT_NO_ANSWER="no_answer" / OUT_REJECTED="rejected" / OUT_FAILED="failed"`
  - `@dataclass DialOutcome: status: str; participant_identity: str = ""; detail: str = ""`
  - `map_sip_status_code(code: int) -> str`（486/603→rejected、408/480→no_answer、其余→failed）
  - `resolve_dial_mode(env: Mapping[str, str], settings: Mapping[str, dict]) -> str`（`BOK_SIP_MODE` env > settings["sip"]["mode"] > "mock"；非法值回落 mock）
  - `async dial_outbound(ctx, *, number: str, mode: str, cp_base: str, call_id: str, scenario: str = "", language: str = "", script: list[str] | None = None, ringing_timeout_s: float = 30.0, trunk_id: str = "") -> DialOutcome`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_dialer.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.dialer import (
    OUT_FAILED, OUT_NO_ANSWER, OUT_REJECTED, map_sip_status_code, resolve_dial_mode,
)


def test_map_sip_status_code():
    assert map_sip_status_code(486) == OUT_REJECTED
    assert map_sip_status_code(603) == OUT_REJECTED
    assert map_sip_status_code(408) == OUT_NO_ANSWER
    assert map_sip_status_code(480) == OUT_NO_ANSWER
    assert map_sip_status_code(500) == OUT_FAILED
    assert map_sip_status_code(0) == OUT_FAILED


def test_resolve_dial_mode():
    assert resolve_dial_mode({"BOK_SIP_MODE": "real"}, {}) == "real"
    assert resolve_dial_mode({}, {"sip": {"mode": "real"}}) == "real"
    assert resolve_dial_mode({}, {}) == "mock"
    assert resolve_dial_mode({"BOK_SIP_MODE": "bogus"}, {"sip": {"mode": "real"}}) == "mock"
    assert resolve_dial_mode({}, {"sip": {}}) == "mock"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `... -m pytest tests/test_dialer.py -q` → FAIL `ModuleNotFoundError: agent_runtime.dialer`

- [ ] **Step 3: 实现 dialer.py**

```python
"""SIP 外播拨号薄层（spec 2026-09-12 Wave2）。

统一四态出口，双后端:
- real:官方 livekit.api CreateSIPParticipant(wait_until_answered=True),
  SipCallError 按 SIP 码映射(486/603 拒接、408/480 无人接、5xx trunk 故障)。
- mock:CP 派生 scripts/mock_callee.py 子进程(真 TTS 客户语音)进房,
  agent wait_for_participant;超时=no_answer、进房后 1.5s 内离房且零音频=rejected。

官方铁律:USER_UNAVAILABLE/SIP_TRUNK_FAILURE(即 no_answer/failed)RoomIO 不自动收,
调用方必须 ctx.shutdown()。ANSWERED 才准 session.start(开场白时序=接通后)。
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass

OUT_ANSWERED = "answered"
OUT_NO_ANSWER = "no_answer"
OUT_REJECTED = "rejected"
OUT_FAILED = "failed"

_REJECT_CODES = (486, 603)
_NO_ANSWER_CODES = (408, 480)


@dataclass
class DialOutcome:
    status: str
    participant_identity: str = ""
    detail: str = ""


def map_sip_status_code(code: int) -> str:
    if code in _REJECT_CODES:
        return OUT_REJECTED
    if code in _NO_ANSWER_CODES:
        return OUT_NO_ANSWER
    return OUT_FAILED


def resolve_dial_mode(env, settings) -> str:
    mode = str(env.get("BOK_SIP_MODE", "") or "").strip().lower()
    if mode in ("mock", "real"):
        return mode
    mode = str(((settings or {}).get("sip") or {}).get("mode") or "").strip().lower()
    return mode if mode in ("mock", "real") else "mock"


async def dial_outbound(ctx, *, number: str, mode: str, cp_base: str, call_id: str,
                        scenario: str = "", language: str = "",
                        script: list[str] | None = None,
                        ringing_timeout_s: float = 30.0, trunk_id: str = "") -> DialOutcome:
    if mode == "real":
        return await _dial_real(ctx, number=number, trunk_id=trunk_id,
                                ringing_timeout_s=ringing_timeout_s)
    return await _dial_mock(ctx, number=number, cp_base=cp_base, call_id=call_id,
                            scenario=scenario, language=language, script=script,
                            ringing_timeout_s=ringing_timeout_s)


async def _dial_real(ctx, *, number: str, trunk_id: str, ringing_timeout_s: float) -> DialOutcome:
    from livekit.api import CreateSIPParticipantRequest
    identity = f"sip-{number}"
    try:
        await ctx.api.sip.create_sip_participant(
            CreateSIPParticipantRequest(
                sip_trunk_id=trunk_id, sip_call_to=number,
                room_name=ctx.room.name, participant_identity=identity,
                wait_until_answered=True,
                ringing_timeout=ringing_timeout_s,
            )
        )
    except Exception as exc:  # livekit.api.SipCallError —— 属性防御式读取
        code = int(getattr(exc, "sip_status_code", 0) or 0)
        return DialOutcome(status=map_sip_status_code(code), participant_identity=identity,
                           detail=f"{type(exc).__name__}: {exc}")
    await _wait_participant(ctx, identity, 10.0)
    return DialOutcome(status=OUT_ANSWERED, participant_identity=identity)


async def _wait_participant(ctx, identity: str, timeout_s: float):
    """wait_for_participant 兼容包装:1.8 若无 timeout kwarg 则退 asyncio.wait_for。"""
    try:
        return await ctx.wait_for_participant(identity=identity, timeout=timeout_s)
    except TypeError:
        return await asyncio.wait_for(ctx.wait_for_participant(identity=identity), timeout_s)


async def _dial_mock(ctx, *, number: str, cp_base: str, call_id: str, scenario: str,
                     language: str, script: list[str] | None,
                     ringing_timeout_s: float) -> DialOutcome:
    import aiohttp

    identity = f"sip-mock-{number}"
    payload = {
        "room": ctx.room.name, "number": number, "identity": identity,
        "scenario": scenario or "answer", "language": language or "cantonese",
        "script": script or [], "ring_delay_s": 3.0,
        "ringing_window_s": ringing_timeout_s,
    }
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{cp_base}/api/sip/mock/callee", json=payload) as resp:
            if resp.status != 200:
                return DialOutcome(status=OUT_FAILED, detail=f"mock spawn http {resp.status}")
    try:
        await _wait_participant(ctx, identity, ringing_timeout_s + 5)
    except (TimeoutError, asyncio.TimeoutError):
        return DialOutcome(status=OUT_NO_ANSWER, participant_identity=identity,
                           detail="mock callee never joined")
    # 接通前离房判定(reject 剧本):1.5s 窗内该 participant 离开 → 拒接语义
    left = asyncio.Event()

    def _on_disc(p) -> None:
        if getattr(p, "identity", "") == identity:
            left.set()

    ctx.room.on("participant_disconnected", _on_disc)
    try:
        try:
            await asyncio.wait_for(left.wait(), timeout=1.5)
            return DialOutcome(status=OUT_REJECTED, participant_identity=identity,
                               detail="callee left before audio")
        except asyncio.TimeoutError:
            pass
    finally:
        ctx.room.off("participant_disconnected", _on_disc)
    return DialOutcome(status=OUT_ANSWERED, participant_identity=identity)
```

- [ ] **Step 4: 跑测试通过** → `... -m pytest tests/test_dialer.py -q`

- [ ] **Step 5: 提交**

```bash
git add apps/agent/agent_runtime/dialer.py tests/test_dialer.py
git commit -m "feat(agent): SIP 外播 dialer 薄层——官方 real/CP 派生 mock 双后端四态出口"
```

---

### Task 6: CP mock 客户派生端点 + mock_callee.py 真语音脚本

**Files:**
- Create: `scripts/mock_callee.py`
- Modify: `apps/control-plane/control_plane/main.py`（新增 `POST /api/sip/mock/callee`）
- Modify: `tools/bok.py`（`down` 清理段：按进程名 `mock_callee.py` 补杀一行——找到现有 orphan sweep 段加 `pkill -f "scripts/mock_callee.py" || true` 同款姿势）
- Test: `tests/test_mock_callee.py`（新建：参数/时间线纯函数）

**Interfaces:**
- Consumes: 无（独立）
- Produces:
  - `POST /api/sip/mock/callee` body `{room, number, identity, scenario, language, script: [str], ring_delay_s, ringing_window_s}` → `{ok: true, pid: int}`（404=无凭据/房间缺失）
  - `scripts/mock_callee.py` CLI：`--url --token --identity --scenario --script-json --language --ring-delay --ringing-window --hangup-after-turns`
  - `mock_callee.plan_timeline(scenario: str, *, ring_delay_s: float, lines: int) -> list[tuple[str, float]]`（纯函数：`[(event, at_s)]`，供测试与日志）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_mock_callee.py
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mock_callee.py"
_spec = importlib.util.spec_from_file_location("mock_callee", _SCRIPT)
mock_callee = importlib.util.module_from_spec(_spec)
sys.modules["mock_callee"] = mock_callee
_spec.loader.exec_module(mock_callee)


def test_plan_timeline():
    tl = mock_callee.plan_timeline("answer", ring_delay_s=3.0, lines=2)
    assert tl[0] == ("join", 3.0)
    assert [e for e, _ in tl if e == "speak"] == ["speak", "speak"]
    assert tl[-1][0] == "leave"
    # no_answer:永不进房
    tl2 = mock_callee.plan_timeline("no_answer", ring_delay_s=3.0, lines=2)
    assert [e for e, _ in tl2] == ["exit"]
    # reject:进房即走
    tl3 = mock_callee.plan_timeline("reject", ring_delay_s=3.0, lines=2)
    assert [e for e, _ in tl3] == ["join", "leave"]
    # hangup_mid:说完首句后走
    tl4 = mock_callee.plan_timeline("hangup_mid", ring_delay_s=3.0, lines=3)
    assert [e for e, _ in tl4].count("speak") == 1 and tl4[-1][0] == "leave"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `... -m pytest tests/test_mock_callee.py -q` → FAIL 文件不存在

- [ ] **Step 3: 实现 scripts/mock_callee.py**

结构（音频发布复用 `scripts/e2e_real_customer.py` 的 rtc 姿势——先读该文件 155 行 `tts_pcm` 与其进房/发布段，照搬 import 与帧推送写法）：

```python
#!/usr/bin/env python3
"""mock SIP 客户(模拟联调档):CP 派生的真语音被叫。

剧本四型:answer(进房+按台词 TTS 轮播) / no_answer(永不进房睡满响铃窗) /
reject(进房即离) / hangup_mid(说 1 句后离房)。日志打结构化行 MOCK_CALLEE 供 E2E 断言。
"""
from __future__ import annotations

import argparse
import asyncio
import json


def plan_timeline(scenario: str, *, ring_delay_s: float, lines: int) -> list[tuple[str, float]]:
    t = float(ring_delay_s)
    if scenario == "no_answer":
        return [("exit", 1e9)]
    if scenario == "reject":
        return [("join", t), ("leave", t + 0.5)]
    if scenario == "hangup_mid":
        return [("join", t), ("speak", t + 0.8), ("leave", t + 6.0)]
    tl = [("join", t)]
    at = t + 0.8
    for _ in range(max(1, lines)):
        tl.append(("speak", at))
        at += 6.0
    tl.append(("leave", at + 2.0))
    return tl
```

`main()`：argparse 解析上表参数 → `plan_timeline` → 按 timeline 执行：
- `join`：`rtc.Room()` + `await room.connect(url, token)`；创建 audio source（照搬 e2e_real_customer 的 `rtc.AudioSource(...)` + `rtc.LocalAudioTrack.publish` 姿势）；
- `speak`：取 script 下一句 → `tts_pcm(line, language)`（从 e2e_real_customer.py 复制函数：POST `http://127.0.0.1:8788` TTS sidecar，语言参数 zh/cantonese/en）→ source 逐帧 push（e2e 同款 frame 循环）；
- `leave`：`await room.disconnect()` 后退出；
- `exit`：直接 sleep(ringing_window) 退出（never join）。
- 每步 `print(f"MOCK_CALLEE event={name} at={at:.1f} identity={identity}", flush=True)`；`room.on("disconnected")` → 立即收尾退出（房间被删/agent 收线时自杀，不留殭尸）。

- [ ] **Step 4: 实现 CP 派生端点**

`main.py`（nodes 端点前；spawn 复用 pregen 的 detached 姿势）：

```python
class MockCalleeRequest(BaseModel):
    room: str
    number: str
    identity: str = ""
    scenario: str = "answer"          # answer | no_answer | reject | hangup_mid
    language: str = "cantonese"
    script: list[str] = []
    ring_delay_s: float = 3.0
    ringing_window_s: float = 35.0


@app.post("/api/sip/mock/callee")
def spawn_mock_callee(req: MockCalleeRequest) -> dict:
    """模拟联调档:派生 mock 客户子进程(同 pregen detached 姿势,失败不阻拨号主链)。"""
    import subprocess
    from pathlib import Path
    from livekit.api import AccessToken

    identity = req.identity or f"sip-mock-{req.number}"
    key = getattr(app.state, "lk_key", "") or os.environ.get("LIVEKIT_API_KEY", "")
    secret = getattr(app.state, "lk_secret", "") or os.environ.get("LIVEKIT_API_SECRET", "")
    if not key or not secret:
        raise HTTPException(404, "livekit credentials not configured")
    lk_url = (getattr(app.state, "lk_url", "") or os.environ.get("LIVEKIT_URL", "")
              or "ws://127.0.0.1:7880")
    at = AccessToken(api_key=key, api_secret=secret).with_identity(identity).with_name("Mock Callee")
    at.with_grants(permissions={"room_join": True, "room_list": True,
                                "can_publish": True, "can_subscribe": True})
    at = at.with_expiry(3600)
    token = at.to_jwt()
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "mock_callee.py"
    repo_root = Path(__file__).resolve().parents[2]
    cmd = [
        sys.executable, str(script_path),
        "--url", lk_url, "--token", token, "--identity", identity,
        "--scenario", req.scenario, "--language", req.language,
        "--script-json", json.dumps(req.script, ensure_ascii=False),
        "--ring-delay", str(req.ring_delay_s),
        "--ringing-window", str(req.ringing_window_s),
    ]
    log_path = repo_root / "runtime" / "logs" / "mock-callee.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    with open(log_path, "ab") as logf:
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                cwd=str(repo_root), env=env, start_new_session=True)
    _audit("sip.mock_callee_spawn", subject_type="room", subject_id=req.room,
           account_id="acc-001", detail={"scenario": req.scenario, "pid": proc.pid})
    return {"ok": True, "pid": proc.pid, "identity": identity}
```

（`AccessToken` 的 grants 构造以 `/api/token` 现有代码为准照搬——若该处用 `at.with_grants(...)` 之外姿势（如 `AccessToken(...)` 带 can_publish 参数），与之一致。`sys` 已在 main.py import。）

`tools/bok.py` down：找到 orphan sweep 段（grep `pkill\|agent_runtime`），补：

```python
    run(["pkill", "-f", "scripts/mock_callee.py"])  # mock 客户随栈清理
```

（照该文件现有错误容忍姿势包 try/except。）

- [ ] **Step 5: 跑测试 + 提交**

```bash
... -m pytest tests/test_mock_callee.py -q
... -m python -m compileall -q scripts apps
git add scripts/mock_callee.py apps/control-plane/control_plane/main.py tools/bok.py tests/test_mock_callee.py
git commit -m "feat(cp,scripts): mock SIP 客户——CP 派生真语音被叫(四剧本)+down 清理"
```

---

### Task 7: agent 外呼模式（metadata dial 块 + 接通才 start + 失败收线）

**Files:**
- Modify: `apps/agent/agent_runtime/agent.py`（entrypoint 1130 起：metadata 解析后插拨号段；`session.start`（~2828）前无改动——拨号段在装配前 return/继续分流）
- Modify: `apps/agent/agent_runtime/control_plane.py`（client 加 `report_dial_result`）
- Modify: `apps/control-plane/control_plane/main.py`（新增 `POST /api/calls/{call_id}/dial-result`）
- Test: `tests/test_dial_result_api.py`（新建）

**Interfaces:**
- Consumes: Task 5 `dial_outbound/DialOutcome/OUT_*`、Task 6 mock 端点
- Produces:
  - job metadata 契约：`{"call_id": str, "dial": {"to": str, "mode": "mock"|"real", "scenario": str, "language": str, "trunk_id": str, "campaign_item_id": str}}`
  - `ControlPlaneClient.report_dial_result(call_id: str, status: str, detail: str = "") -> None`
  - `POST /api/calls/{call_id}/dial-result` body `{status, detail}`：answered→call ACTIVE；no_answer/rejected/failed→call ENDED+disposition（Task 11 扩展 campaign item 联动）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_dial_result_api.py
from __future__ import annotations

from bok_voice_business_db.repository import InMemoryBusinessRepository


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient
    from control_plane.main import app
    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    return TestClient(app), repo


def _make_call(repo, session_id: str) -> dict:
    # 造 call 的姿势照抄 tests/ 现有用例:先 grep "repo.create_call(" tests/ 找
    # SessionManifest 最小构造(字段名以 packages/core/bok_voice_core/types.py 为准),
    # 通常形如 SessionManifest(session_id=..., account_id="acc-001")。
    from bok_voice_core.types import SessionManifest
    return repo.create_call(SessionManifest(session_id=session_id, account_id="acc-001"))


def test_dial_result_endpoint(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    _make_call(repo, "call-dialtest")
    r = client.post("/api/calls/call-dialtest/dial-result",
                    json={"status": "no_answer", "detail": "timeout"})
    assert r.status_code == 200
    call = repo.get_call("call-dialtest")
    assert call["status"] == "ended" and call["disposition"] == "no_answer"
    r = client.post("/api/calls/call-nope/dial-result", json={"status": "answered"})
    assert r.status_code == 404


def test_dial_result_answered_activates(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    _make_call(repo, "call-ans")
    r = client.post("/api/calls/call-ans/dial-result", json={"status": "answered"})
    assert r.status_code == 200
    assert repo.get_call("call-ans")["status"] == "active"
```

- [ ] **Step 2: 跑测试确认失败** → 404 路由不存在

- [ ] **Step 3: 实现**

CP 端点（`main.py`，whatsapp 端点后）：

```python
class DialResultRequest(BaseModel):
    status: str = ""      # answered | no_answer | rejected | failed
    detail: str = ""


@app.post("/api/calls/{call_id}/dial-result")
def report_dial_result(call_id: str, req: DialResultRequest) -> dict:
    """agent 外呼拨号结果上报:answered→ACTIVE;三失败态→ENDED+disposition。

    (Wave3 扩展点:campaign item 按 call_id 联动状态。)
    """
    call = _repo().get_call(call_id)
    if not call:
        raise HTTPException(404, "call not found")
    if str(call.get("status") or "") in _TERMINAL_CALL_STATUSES:
        return call
    status = str(req.status or "").strip()
    if status == "answered":
        updated = _repo().update_call(call_id, status=CallStatus.ACTIVE.value) or call
    elif status in ("no_answer", "rejected", "failed"):
        updated = _repo().update_call(call_id, status=CallStatus.ENDED.value,
                                      disposition=status) or call
    else:
        updated = call
    _audit("call.dial_result", subject_type="call", subject_id=call_id,
           account_id=str(call.get("account_id", "acc-001")),
           detail={"status": status, "detail": (req.detail or "")[:120]})
    return updated
```

（`CallStatus.ACTIVE` 若枚举名不同，以 `models.py`/core 现有值为准——grep `class CallStatus`。）

client（`control_plane.py` agent 侧，照 `report_whatsapp` 姿势）：

```python
    async def report_dial_result(self, call_id: str, status: str, detail: str = "") -> None:
        # POST /api/calls/{call_id}/dial-result {"status": status, "detail": detail}
```

agent.py 拨号段（entrypoint 内、context resolve（`except Exception as e: context resolve failed` 块）**之后**、FlowController 装配之前插入）：

```python
    # ---- 外呼模式（spec Wave2）:metadata 带 dial 块 → 先拨号、接通才继续装配。
    # 官方两段式:agent 先进房 → CreateSIPParticipant(或 mock)→ wait_for_participant
    # → session.start。拨号失败必须手动收线(RoomIO 对 no_answer/failed 不自动收)。
    _dial = dict(_job_meta.get("dial") or {})
    if _dial:
        from .dialer import OUT_ANSWERED, dial_outbound, resolve_dial_mode
        _settings = {}
        try:
            _settings = await cp.get_settings()
        except Exception:
            pass
        _mode = str(_dial.get("mode") or "") or resolve_dial_mode(os.environ, _settings or {})
        _outcome = await dial_outbound(
            ctx, number=str(_dial.get("to") or ""), mode=_mode, cp_base=cp_base,
            call_id=call_id, scenario=str(_dial.get("scenario") or ""),
            language=str(_dial.get("language") or ""),
            trunk_id=str(_dial.get("trunk_id") or ""),
        )
        print(f"[dial] outcome={_outcome.status} detail={_outcome.detail} (call {room_name})", flush=True)
        try:
            await cp.report_dial_result(call_id, _outcome.status, _outcome.detail)
        except Exception as exc:  # pragma: no cover
            print(f"[dial] report failed: {exc!r} (call {room_name})", flush=True)
        if _outcome.status != OUT_ANSWERED:
            # 收线:通话置终态 + 删房(房间不删,真电话对端会一直听静音——官方警告)
            try:
                await cp.end_call(call_id, disposition=_outcome.status)
            except Exception:
                pass
            try:
                from livekit.api import DeleteRoomRequest
                await ctx.api.room.delete_room(DeleteRoomRequest(room=ctx.room.name))
            except Exception:
                pass
            ctx.shutdown()
            return
        # mock 档时长保险丝(spec:max_call_duration_s,real 档由 API 参数承担)
        _fuse_s = float(_dial.get("max_call_duration_s") or 0)
        if _mode == "mock" and _fuse_s > 0:
            async def _duration_fuse() -> None:
                await asyncio.sleep(_fuse_s)
                print(f"[dial] duration fuse fired (call {room_name})", flush=True)
                try:
                    await cp.end_call(call_id, disposition="completed")
                finally:
                    from livekit.api import DeleteRoomRequest
                    try:
                        await ctx.api.room.delete_room(DeleteRoomRequest(room=ctx.room.name))
                    except Exception:
                        pass
            asyncio.create_task(_duration_fuse())
```

（`cp.get_settings` 若 client 无此方法，加一个 GET `/api/settings` 透传小方法。`cp.end_call` 已存在。）

- [ ] **Step 4: 跑测试 + 回归**（`tests/test_dial_result_api.py` + 全量；agent.py 属运行时，compileall 覆盖语法）

- [ ] **Step 5: 提交**

```bash
git add apps/agent apps/control-plane tests/test_dial_result_api.py
git commit -m "feat(agent,cp): 外呼模式——接通才装配会话/失败三态收线删房/时长保险丝"
```

---

### Task 8: SIP 设置面（settings DB + env 开关 + web 卡片）

**Files:**
- Modify: `packages/business-db/bok_voice_business_db/models.py`（GlobalSetting 加 `sip_json`）
- Modify: `packages/business-db/bok_voice_business_db/repository.py`（get/save/default_settings 双后端）
- Modify: `apps/control-plane/control_plane/deps.py`（`_ensure_column` 补 sip_json）
- Modify: `apps/control-plane/control_plane/main.py`（SettingsRequest/GET/PUT 加 sip）
- Modify: `apps/web/app/(app)/settings/page.tsx`（SIP 卡片）
- Test: `tests/test_sip_settings.py`（新建）

**Interfaces:**
- Produces:
  - settings `sip` 段默认值：`{"mode": "mock", "trunk_id": "", "address": "", "auth_username": "", "auth_password": "", "numbers": [], "ringing_timeout_s": 30, "max_call_duration_s": 600}`
  - `GET/PUT /api/settings` 含 `sip` 键；`auth_password` 走既有 secret 掩码逻辑（PUT 时空值保留旧值——照 `secret_keys` 现有实现把 "sip" 加进 kind 循环）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_sip_settings.py
from __future__ import annotations

from bok_voice_business_db.repository import InMemoryBusinessRepository


def test_default_settings_has_sip():
    s = InMemoryBusinessRepository().get_settings()
    assert s["sip"]["mode"] == "mock"
    assert s["sip"]["ringing_timeout_s"] == 30
    assert s["sip"]["max_call_duration_s"] == 600


def test_save_settings_roundtrip():
    repo = InMemoryBusinessRepository()
    s = repo.get_settings()
    s["sip"] = {**s["sip"], "mode": "real", "trunk_id": "ST_x", "auth_password": "sekret"}
    repo.save_settings(s)
    got = repo.get_settings()
    assert got["sip"]["mode"] == "real" and got["sip"]["auth_password"] == "sekret"
```

- [ ] **Step 2: 确认失败** → FAIL `KeyError: 'sip'`

- [ ] **Step 3: 实现**

`models.py` GlobalSetting 加列：`sip_json: Mapped[str] = mapped_column(Text, default="")`。
`deps.py` 迁移段（现有 `_ensure_column` 调用列表追加）：

```python
            _ensure_column(conn, "global_settings", "sip_json", "sip_json TEXT NOT NULL DEFAULT ''")
```

SQL repo `get_settings` 返回 dict 加：`"sip": json.loads(row.sip_json or "{}") or self.default_settings()["sip"]`；`save_settings` 加：`row.sip_json = json.dumps(settings.get("sip") or self.default_settings()["sip"], ensure_ascii=False)`；`default_settings()`（591 行，读现有结构后）加 `"sip": {...上表默认值...}`。InMemory 同步（settings dict 初始化行并入默认）。

CP `main.py`：`SettingsRequest`（读现有定义，asr/llm/tts/vad 是嵌套 BaseModel）加：

```python
class SipSettingsModel(BaseModel):
    mode: str = "mock"
    trunk_id: str = ""
    address: str = ""
    auth_username: str = ""
    auth_password: str = ""
    numbers: list[str] = []
    ringing_timeout_s: int = 30
    max_call_duration_s: int = 600
```

`SettingsRequest` 加 `sip: SipSettingsModel = SipSettingsModel()`；GET/PUT 两端点把 `"sip": req.sip.model_dump()` 并入 new_values（PUT 的 secret 保留循环把 `("sip",)` 加进 kind 元组，`secret_keys` 已含 `api_key`——`auth_password` 补进集合）。

web settings 页（读现有 asr/llm 卡片结构，加一张同款 SIP 卡：mode 下拉 mock/real + 文本字段组 + 数字字段；`saveSettings` body 的 `sip` 键带上）。

- [ ] **Step 4: 测试 + web 构建 + 提交**

```bash
... -m pytest tests/test_sip_settings.py tests/test_db_portability.py -q
cd apps/web && npx tsc --noEmit && npm run build
git add packages apps tools tests/test_sip_settings.py
git commit -m "feat(settings): SIP 配置面——mode/trunk/超时默认值+web 卡片+env 开关接线"
```

---

### Task 9: Campaign/CampaignItem 模型 + repo

**Files:**
- Modify: `packages/business-db/bok_voice_business_db/models.py`
- Modify: `packages/business-db/bok_voice_business_db/repository.py`（双后端）
- Test: `tests/test_campaign_repo.py`（新建）

**Interfaces:**
- Produces:
  - `repo.create_campaign(account_id: str, *, name: str, template_id: str, persona_id: str, language: str, gap_seconds: int, object_ids: list[str], scenarios: dict[str, str] | None = None) -> dict`（同时建 items，phone 取对象 phone，scenario 存 mock 剧本钩子）
  - `repo.get_campaign/update_campaign/list_campaigns(account_id: str = "acc-001", status: str = "")`
  - `repo.list_items(campaign_id) / get_item(item_id) / update_item(item_id, **fields) / find_item_by_call(call_id) -> dict | None`
  - item dict 键：`id/campaign_id/seq/object_id/phone/status/call_id/attempts/last_error/scenario/updated_at`；status 值域 `pending|dialing|in_call|done|no_answer|rejected|failed|skipped`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_campaign_repo.py
from __future__ import annotations

from bok_voice_business_db.repository import InMemoryBusinessRepository


def _repo_with_object():
    repo = InMemoryBusinessRepository()
    obj = repo.create_object("acc-001", {"display_name": "陈生", "phone": "+85264320111"})
    return repo, obj


def test_create_campaign_with_items():
    repo, obj = _repo_with_object()
    c = repo.create_campaign(
        "acc-001", name="催件第一波", template_id="", persona_id="",
        language="cantonese", gap_seconds=5, object_ids=[obj["id"]],
        scenarios={obj["id"]: "no_answer"},
    )
    assert c["status"] == "draft" and c["gap_seconds"] == 5
    items = repo.list_items(c["id"])
    assert len(items) == 1
    assert items[0]["phone"] == "+85264320111"
    assert items[0]["status"] == "pending" and items[0]["scenario"] == "no_answer"
    repo.update_item(items[0]["id"], status="dialing", call_id="call-z")
    assert repo.find_item_by_call("call-z")["id"] == items[0]["id"]
    repo.update_campaign(c["id"], status="running")
    assert repo.list_campaigns(status="running")[0]["id"] == c["id"]
```

- [ ] **Step 2: 确认失败** → AttributeError

- [ ] **Step 3: 实现模型 + repo**

`models.py`（RosterEntry 后）：

```python
class Campaign(Base):
    """外呼战役:对象名单串行逐个拨(spec Wave3)。status: draft/running/paused/done/stopped。"""
    __tablename__ = "campaigns"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    template_id: Mapped[str] = mapped_column(String(64), default="")
    persona_id: Mapped[str] = mapped_column(String(64), default="")
    language: Mapped[str] = mapped_column(String(16), default="zh")
    status: Mapped[str] = mapped_column(String(16), default="draft")
    gap_seconds: Mapped[int] = mapped_column(Integer, default=5)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)


class CampaignItem(Base):
    """战役单条:一个对象一通。scenario=mock 剧本钩子(测试/演练用,生产空)。"""
    __tablename__ = "campaign_items"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    object_id: Mapped[str] = mapped_column(String(64), default="")
    phone: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    call_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    last_error: Mapped[str] = mapped_column(String(255), default="")
    scenario: Mapped[str] = mapped_column(String(16), default="")
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
```

repo（双后端，`create_campaign` 内逐 object 查 phone：`self.get_object(oid)` 空 phone 则 item.phone="" 且 status="skipped"、last_error="对象无电话"；`_campaign_to_dict/_item_to_dict` 序列化 datetime 为 ISO；`Integer` 已在 models import）。方法签名照 Interfaces 块；SQL 实现照 Task 1 roster 段同款（query/filter/commit），InMemory 用 `self.campaigns: dict` / `self.campaign_items: dict`。

- [ ] **Step 4: 测试 + 提交**

```bash
... -m pytest tests/test_campaign_repo.py tests/test_db_portability.py -q
git add packages tests/test_campaign_repo.py
git commit -m "feat(db): campaigns/campaign_items 表+repo——串行战役数据层(mock 剧本钩子)"
```

---

### Task 10: campaign 循环（状态机纯函数 + CP 后台任务）

**Files:**
- Create: `apps/control-plane/control_plane/campaign.py`
- Modify: `apps/control-plane/control_plane/main.py`（startup 挂 `_campaign_loop`，与 reaper 并列 150 行处）
- Test: `tests/test_campaign_loop.py`（新建）

**Interfaces:**
- Consumes: Task 9 repo 方法、Task 7 dial-result 端点、`_lkapi_client()/has_active_dispatch`（main.py 现有）
- Produces:
  - `campaign.item_status_for_call(call: dict) -> str`（纯函数）
  - `campaign.campaign_tick(repo, *, dispatcher=None) -> dict`（一轮巡检：终态收割→串行下一通→done 判定；dispatcher 可注入 fake）
  - `_campaign_loop()`：5s 周期跑 `campaign_tick`，异常吞掉记 warning

- [ ] **Step 1: 写失败测试**

```python
# tests/test_campaign_loop.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "control-plane"))

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.campaign import campaign_tick, item_status_for_call


def test_item_status_for_call():
    assert item_status_for_call({"status": "ended", "disposition": ""}) == "done"
    assert item_status_for_call({"status": "ended", "disposition": "completed"}) == "done"
    assert item_status_for_call({"status": "ended", "disposition": "no_answer"}) == "no_answer"
    assert item_status_for_call({"status": "ended", "disposition": "declined"}) == "done"
    assert item_status_for_call({"status": "failed", "disposition": ""}) == "failed"


def test_tick_harvests_terminal_and_starts_next(monkeypatch):
    repo = InMemoryBusinessRepository()
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    c = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                             language="zh", gap_seconds=5, object_ids=[obj["id"]])
    repo.update_campaign(c["id"], status="running")
    items = repo.list_items(c["id"])
    # 手工把 item 拨中并配一通已结束通话 → tick 应收割终态
    call = repo.create_call(_manifest("call-c1"))
    repo.update_call(call["id"], status="ended", disposition="no_answer")
    repo.update_item(items[0]["id"], status="dialing", call_id=call["id"])
    dispatched: list[tuple] = []

    async def fake_dispatch(room: str, metadata: str) -> None:
        dispatched.append((room, metadata))

    import asyncio
    out = asyncio.run(campaign_tick(repo, dispatcher=fake_dispatch))
    assert repo.get_item(items[0]["id"])["status"] == "no_answer"
    # 名单已尽 → campaign done
    assert repo.get_campaign(c["id"])["status"] == "done"
    assert out == {"harvested": 1, "started": 0, "finished": 1}


def _manifest(session_id: str):
    from bok_voice_core.types import SessionManifest
    # 最小构造;字段以 packages/core/bok_voice_core/types.py 为准(照 test_dial_result_api 同款)
    return SessionManifest(session_id=session_id, account_id="acc-001")
```

（`SessionManifest` 构造以真实签名为准——先 grep packages/core 找构造最小参数。）

- [ ] **Step 2: 确认失败** → ModuleNotFoundError control_plane.campaign

- [ ] **Step 3: 实现 campaign.py**

```python
"""外呼战役串行循环（spec 2026-09-12 Wave3）。

5s 巡检:①收割——dialing/in_call 的 item 其 call 已终态 → item 落结果;
②串行——无进行中 item 且有 pending → create_call + create_dispatch(metadata 带 dial 块)
置 dialing;③名单尽 → campaign done。dispatcher 可注入(fake 供单测)。
拨号结果联动:agent dial-result 端点(Wave3 扩展)直接写 item 状态,收割段幂等跳过。
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Awaitable, Callable

from bok_voice_core.types import CallStatus
from bok_voice_obs.logging import get_logger

from .dispatch_utils import has_active_dispatch

log = get_logger("control-plane.campaign", component="control-plane", service="control-plane")

POLL_S = 5.0

Dispatcher = Callable[[str, str], Awaitable[None]]


def item_status_for_call(call: dict) -> str:
    if str(call.get("status") or "") == CallStatus.FAILED.value:
        return "failed"
    d = str(call.get("disposition") or "")
    return {"no_answer": "no_answer", "rejected": "rejected", "failed": "failed"}.get(d, "done")


async def _default_dispatcher(room: str, metadata: str) -> None:
    from .main import _lkapi_client  # 延迟 import 防 main↔campaign 循环

    client = _lkapi_client()
    if client is None:
        raise RuntimeError("livekit credentials missing")
    try:
        if await has_active_dispatch(client, room):
            return
        from livekit.api import CreateAgentDispatchRequest
        # metadata kwarg 若 livekit.api 版本无此字段,以 webhook redispatch 处现有
        # CreateAgentDispatchRequest 用法为准(main.py:1706-1710),metadata 拼进
        # agent_name 之外的正确通道;实在无通道则退 token RoomAgentDispatch 路线。
        await client.agent_dispatch.create_dispatch(
            CreateAgentDispatchRequest(agent_name="bok-voice", room=room, metadata=metadata)
        )
    finally:
        await client.aclose()


async def campaign_tick(repo=None, *, dispatcher: Dispatcher | None = None) -> dict:
    from .main import _TERMINAL_CALL_STATUSES, _repo

    repo = repo or _repo()
    dispatcher = dispatcher or _default_dispatcher
    out = {"harvested": 0, "started": 0, "finished": 0}
    for c in repo.list_campaigns("acc-001", status="running"):
        items = repo.list_items(c["id"])
        for it in [i for i in items if i["status"] in ("dialing", "in_call")]:
            call = repo.get_call(it.get("call_id") or "")
            if not call:
                continue
            if str(call.get("status") or "") in _TERMINAL_CALL_STATUSES:
                repo.update_item(it["id"], status=item_status_for_call(call),
                                 last_error=str(call.get("disposition") or ""))
                out["harvested"] += 1
        items = repo.list_items(c["id"])
        if any(i["status"] in ("dialing", "in_call") for i in items):
            continue  # 串行:一路进行中
        pending = [i for i in items if i["status"] == "pending"]
        if not pending:
            from datetime import datetime, timezone
            repo.update_campaign(c["id"], status="done",
                                 finished_at=datetime.now(timezone.utc).replace(tzinfo=None))
            out["finished"] += 1
            continue
        await _start_call(repo, c, pending[0], dispatcher)
        out["started"] += 1
    return out


async def _start_call(repo, campaign: dict, item: dict, dispatcher: Dispatcher) -> None:
    from .main import create_call as create_call_endpoint  # 延迟 import(同步 handler 直调)
    from .schemas import CreateCallRequest

    req = CreateCallRequest(
        account_id=campaign.get("account_id", "acc-001"),
        object_id=item.get("object_id", ""),
        persona_id=campaign.get("persona_id", "") or None,
        language=campaign.get("language", "zh"),
        mode="production", direction="sip",
    )
    call = create_call_endpoint(req)
    call_id = str(call.get("id") or "")
    if item.get("phone"):
        repo.update_call(call_id, contact_phone=str(item["phone"]))
    settings = repo.get_settings() or {}
    sip = dict(settings.get("sip") or {})
    dial = {
        "to": str(item.get("phone") or ""),
        "mode": str(os.environ.get("BOK_SIP_MODE", "") or sip.get("mode") or "mock"),
        "scenario": str(item.get("scenario") or ""),
        "language": campaign.get("language", "zh"),
        "trunk_id": str(sip.get("trunk_id") or ""),
        "campaign_item_id": str(item.get("id") or ""),
        "max_call_duration_s": int(sip.get("max_call_duration_s") or 600),
    }
    metadata = json.dumps({"call_id": call_id, "dial": dial}, ensure_ascii=False)
    try:
        await dispatcher(call_id, metadata)
    except Exception as exc:
        repo.update_item(item["id"], status="failed", call_id=call_id,
                         last_error=f"dispatch: {exc}"[:250])
        return
    repo.update_item(item["id"], status="dialing", call_id=call_id)
    log.info("campaign_call_started", extra={"event": "campaign.call_started",
              "data": {"campaign": campaign["id"], "item": item["id"], "call": call_id}})


async def _campaign_loop() -> None:
    while True:
        try:
            await campaign_tick()
        except Exception as exc:  # noqa: BLE001 - 单轮失败不杀循环
            log.warning("campaign_tick_failed", extra={"event": "campaign.tick.error",
                        "data": {"error": str(exc)}})
        await asyncio.sleep(POLL_S)
```

（`create_call_endpoint` 是同步 FastAPI handler——直接函数调用即可；`CreateCallRequest` 字段以 schemas.py 现有定义为准（mode/direction 若是枚举字面量照改）。`repo.list_campaigns` 首参 account_id——按 Task 9 签名。）

`main.py` startup（150 行 `create_task(_reaper_loop())` 旁，函数体内延迟 import 防循环）：

```python
        from .campaign import _campaign_loop
        asyncio.get_event_loop().create_task(_campaign_loop())
```

- [ ] **Step 4: 测试 + 提交**

```bash
... -m pytest tests/test_campaign_loop.py -q
git add apps/control-plane tests/test_campaign_loop.py
git commit -m "feat(cp): campaign 串行循环——终态收割/自动下一通/幂等状态机+后台任务"
```

---

### Task 11: campaign API + dial-result 联动 item

**Files:**
- Modify: `apps/control-plane/control_plane/main.py`（campaign 路由 + dial-result 扩展）
- Test: `tests/test_campaign_api.py`（新建）

**Interfaces:**
- Produces:
  - `POST /api/campaigns` body `{account_id, name, object_ids: [str], template_id?, persona_id?, language, gap_seconds?, scenarios?: {object_id: scenario}}` → campaign（draft）
  - `POST /api/campaigns/{id}/start | pause | stop` → campaign
  - `GET /api/campaigns` → `list[dict]`（带 items 汇总：`{"total": n, "done": n, ...}`）
  - `GET /api/campaigns/{id}` → `{**campaign, "items": [...], "progress": {...}}`
  - dial-result 端点扩展：answered→item in_call；三失败态→item 终态（find_item_by_call）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_campaign_api.py（client/repo 替换姿势同 test_roster_api）
from __future__ import annotations

from bok_voice_business_db.repository import InMemoryBusinessRepository


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient
    from control_plane.main import app
    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    return TestClient(app), repo


def test_campaign_crud_and_dial_result(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    r = client.post("/api/campaigns", json={
        "name": "波次一", "object_ids": [obj["id"]], "language": "zh",
    })
    assert r.status_code == 200
    cid = r.json()["id"]
    assert r.json()["status"] == "draft"
    r = client.post(f"/api/campaigns/{cid}/start")
    assert r.json()["status"] == "running"
    r = client.post(f"/api/campaigns/{cid}/pause")
    assert r.json()["status"] == "paused"
    detail = client.get(f"/api/campaigns/{cid}").json()
    assert detail["progress"]["total"] == 1 and detail["items"][0]["status"] == "pending"
    assert client.get("/api/campaigns").json()[0]["id"] == cid
    # dial-result 联动:answered → item in_call
    from control_plane.schemas import CreateCallRequest  # 造一通绑到 item
    call = _create_call_via(repo, obj)
    repo.update_item(detail["items"][0]["id"], status="dialing", call_id=call["id"])
    r = client.post(f"/api/calls/{call['id']}/dial-result", json={"status": "answered"})
    assert r.status_code == 200
    assert repo.get_item(detail["items"][0]["id"])["status"] == "in_call"
    r = client.post(f"/api/calls/{call['id']}/dial-result", json={"status": "no_answer"})
    assert repo.get_item(detail["items"][0]["id"])["status"] == "no_answer"
```

（`_create_call_via` 用 repo.create_call(manifest) 照 test_dial_result_api 同款。）

- [ ] **Step 2: 确认失败** → 404

- [ ] **Step 3: 实现**

`main.py`（roster 路由后）：

```python
class CampaignCreateRequest(BaseModel):
    account_id: str = "acc-001"
    name: str = ""
    object_ids: list[str] = []
    template_id: str = ""
    persona_id: str = ""
    language: str = "zh"
    gap_seconds: int = 5
    scenarios: dict[str, str] = {}


@app.post("/api/campaigns")
def create_campaign(req: CampaignCreateRequest) -> dict:
    if not req.object_ids:
        raise HTTPException(400, "object_ids 不能为空")
    camp = _repo().create_campaign(
        req.account_id, name=req.name, template_id=req.template_id,
        persona_id=req.persona_id, language=req.language,
        gap_seconds=req.gap_seconds, object_ids=req.object_ids,
        scenarios={k: v for k, v in req.scenarios.items()
                   if v in ("answer", "no_answer", "reject", "hangup_mid")},
    )
    _audit("campaign.create", subject_type="campaign", subject_id=camp["id"],
           account_id=req.account_id, detail={"objects": len(req.object_ids)})
    return camp


@app.get("/api/campaigns")
def list_campaigns(account_id: str = "acc-001") -> list[dict]:
    out = []
    for c in _repo().list_campaigns(account_id):
        items = _repo().list_items(c["id"])
        c["progress"] = _progress(items)
        out.append(c)
    return out


@app.get("/api/campaigns/{campaign_id}")
def get_campaign(campaign_id: str) -> dict:
    camp = _repo().get_campaign(campaign_id)
    if not camp:
        raise HTTPException(404, "campaign not found")
    items = _repo().list_items(campaign_id)
    camp["items"] = items
    camp["progress"] = _progress(items)
    return camp


@app.post("/api/campaigns/{campaign_id}/start")
def campaign_start(campaign_id: str) -> dict:
    return _campaign_transition(campaign_id, "running")


@app.post("/api/campaigns/{campaign_id}/pause")
def campaign_pause(campaign_id: str) -> dict:
    return _campaign_transition(campaign_id, "paused")


@app.post("/api/campaigns/{campaign_id}/stop")
def campaign_stop(campaign_id: str) -> dict:
    return _campaign_transition(campaign_id, "stopped")


def _campaign_transition(campaign_id: str, status: str) -> dict:
    camp = _repo().get_campaign(campaign_id)
    if not camp:
        raise HTTPException(404, "campaign not found")
    cur = str(camp.get("status") or "")
    allowed = {"running": ("draft", "paused"), "paused": ("running",),
               "stopped": ("running", "paused", "draft")}
    if cur not in allowed[status]:
        raise HTTPException(409, f"cannot {status} from {cur}")
    updated = _repo().update_campaign(campaign_id, status=status) or camp
    _audit(f"campaign.{status}", subject_type="campaign", subject_id=campaign_id,
           account_id=str(camp.get("account_id", "acc-001")))
    return updated


def _progress(items: list[dict]) -> dict:
    p = {"total": len(items)}
    for key in ("pending", "dialing", "in_call", "done", "no_answer", "rejected", "failed", "skipped"):
        p[key] = sum(1 for i in items if i.get("status") == key)
    p["answered"] = p["done"] + p["no_answer"] + p["rejected"]  # 粗口径:拨出去有结果
    return p
```

dial-result 端点（Task 7 的函数体内、`_audit` 前加 item 联动）：

```python
    if status in ("answered", "no_answer", "rejected", "failed"):
        item = _repo().find_item_by_call(call_id)
        if item and item.get("status") in ("dialing", "in_call"):
            item_status = "in_call" if status == "answered" else status
            _repo().update_item(item["id"], status=item_status,
                                last_error=(req.detail or "")[:250])
```

- [ ] **Step 4: 测试 + 回归 + 提交**

```bash
... -m pytest tests/test_campaign_api.py tests/test_dial_result_api.py -q
git add apps/control-plane tests/test_campaign_api.py
git commit -m "feat(cp): campaign API——建/启停/进度+dial-result 联动 item 状态"
```

---

### Task 12: /campaigns 外呼页

**Files:**
- Create: `apps/web/app/(app)/campaigns/page.tsx`
- Modify: `apps/web/components/StageHeader.tsx`（NAV_ITEMS 加 `{ href: "/campaigns", label: "外呼" }`）
- Modify: `apps/web/lib/api.ts`（campaign 方法组）
- Test: `npx tsc --noEmit && npm run build`

**Interfaces:**
- Consumes: Task 11 API、`api.listObjects/listTemplates/listPersonas`（现有）
- Produces: web: `api.createCampaign(body) / startCampaign(id) / pauseCampaign(id) / stopCampaign(id) / listCampaigns() / getCampaign(id)`

- [ ] **Step 1: api.ts 方法**

```ts
  createCampaign: (body: unknown) => request<Record<string, unknown>>("/api/campaigns", { method: "POST", body: JSON.stringify(body) }),
  listCampaigns: () => request<Record<string, unknown>[]>("/api/campaigns"),
  getCampaign: (id: string) => request<Record<string, unknown>>(`/api/campaigns/${id}`),
  startCampaign: (id: string) => request<Record<string, unknown>>(`/api/campaigns/${id}/start`, { method: "POST" }),
  pauseCampaign: (id: string) => request<Record<string, unknown>>(`/api/campaigns/${id}/pause`, { method: "POST" }),
  stopCampaign: (id: string) => request<Record<string, unknown>>(`/api/campaigns/${id}/stop`, { method: "POST" }),
```

- [ ] **Step 2: 页面**（客户端组件，姿势同 Task 4 的 roster 页；骨架如下，字段渲染照 roster 页同类写法补全）

```tsx
"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { LoadingState, EmptyState } from "@/components/app-shell";

type Campaign = {
  id: string; name: string; status: string; language: string; gap_seconds: number;
  created_at: string; items?: Item[]; progress?: Record<string, number>;
};
type Item = { id: string; object_id: string; phone: string; status: string; call_id: string; scenario: string };
type Obj = { id: string; display_name: string; phone: string };
type Ref = { id: string; name: string };

const STATUS_LABEL: Record<string, string> = {
  draft: "草稿", running: "进行中", paused: "已暂停", done: "已完成", stopped: "已停止",
};
const ITEM_LABEL: Record<string, string> = {
  pending: "待拨", dialing: "拨号中", in_call: "通话中", done: "完成",
  no_answer: "无人接", rejected: "拒接", failed: "失败", skipped: "跳过",
};

export default function CampaignsPage() {
  const [list, setList] = useState<Campaign[] | null>(null);
  const [detail, setDetail] = useState<Record<string, Campaign>>({});
  const [objects, setObjects] = useState<Obj[]>([]);
  const [refs, setRefs] = useState<{ templates: Ref[]; personas: Ref[] }>({ templates: [], personas: [] });
  const [form, setForm] = useState({ name: "", object_ids: [] as string[], template_id: "", persona_id: "", language: "zh", gap_seconds: 5 });
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    try { setList(await api.listCampaigns() as Campaign[]); } catch { setList([]); }
  }, []);

  useEffect(() => {
    void reload();
    void api.listObjects().then((o) => setObjects(o as Obj[])).catch(() => setObjects([]));
    // templates/personas 列表方法名以现有 api.ts 为准(话术/人设页在用的那个)
  }, [reload]);

  // running 战役 3s 轮询:列表 + 已展开详情
  useEffect(() => {
    const t = setInterval(() => {
      void reload();
      for (const id of Object.keys(detail)) void api.getCampaign(id).then((d) =>
        setDetail((prev) => ({ ...prev, [id]: d as Campaign }))).catch(() => {});
    }, 3000);
    return () => clearInterval(t);
  }, [reload, detail]);

  const create = async () => {
    setBusy(true);
    try {
      const c = await api.createCampaign(form) as Campaign;
      setForm({ ...form, name: "", object_ids: [] });
      await reload();
      setDetail((prev) => ({ ...prev, [c.id]: c }));
    } finally { setBusy(false); }
  };
  const act = async (id: string, fn: (id: string) => Promise<unknown>) => { await fn(id); await reload(); };

  const toggleObj = (id: string) => setForm((f) => ({
    ...f,
    object_ids: f.object_ids.includes(id) ? f.object_ids.filter((x) => x !== id) : [...f.object_ids, id],
  }));

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-medium">外呼战役</h1>
      <section className="space-y-2 rounded border p-4">
        <div className="flex flex-wrap items-center gap-2">
          <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })}
                 placeholder="战役名称" className="rounded border px-2 py-1 text-sm" />
          <select value={form.language} onChange={(e) => setForm({ ...form, language: e.target.value })} className="rounded border px-2 py-1 text-sm">
            <option value="zh">中文</option><option value="cantonese">粤语</option><option value="en">English</option>
          </select>
          <input type="number" min={1} value={form.gap_seconds}
                 onChange={(e) => setForm({ ...form, gap_seconds: Number(e.target.value) || 5 })}
                 className="w-20 rounded border px-2 py-1 text-sm" />
          <span className="text-xs muted">间隔秒</span>
          <button disabled={busy || !form.object_ids.length || !form.name} onClick={() => void create()}
                  className="rounded border px-3 py-1 text-sm disabled:opacity-50">创建战役（{form.object_ids.length} 对象）</button>
        </div>
        <div className="max-h-48 overflow-auto text-sm">
          {objects.map((o) => (
            <label key={o.id} className={`mr-3 inline-flex items-center gap-1 ${o.phone ? "" : "opacity-40"}`}>
              <input type="checkbox" disabled={!o.phone} checked={form.object_ids.includes(o.id)}
                     onChange={() => toggleObj(o.id)} />
              {o.display_name}{o.phone ? `（${o.phone}）` : "（无电话）"}
            </label>
          ))}
        </div>
      </section>
      {list === null ? <LoadingState /> : list.length === 0 ? <EmptyState label="暂无战役" /> : list.map((c) => (
        <section key={c.id} className="space-y-2 rounded border p-4">
          <div className="flex flex-wrap items-center gap-3">
            <span className="font-medium">{c.name || c.id}</span>
            <span className="text-xs muted">{STATUS_LABEL[c.status] || c.status}</span>
            <span className="text-xs muted">
              {c.progress?.answered ?? 0}/{c.progress?.total ?? 0} 有结果
            </span>
            {c.status === "draft" && <button onClick={() => void act(c.id, api.startCampaign)} className="rounded border px-2 py-0.5 text-sm">启动</button>}
            {c.status === "running" && <button onClick={() => void act(c.id, api.pauseCampaign)} className="rounded border px-2 py-0.5 text-sm">暂停</button>}
            {(c.status === "running" || c.status === "paused") && <button onClick={() => void act(c.id, api.stopCampaign)} className="rounded border px-2 py-0.5 text-sm">停止</button>}
            <button onClick={() => void api.getCampaign(c.id).then((d) => setDetail((p) => ({ ...p, [c.id]: d as Campaign })))}
                    className="rounded border px-2 py-0.5 text-sm">详情</button>
          </div>
          {detail[c.id]?.items && (
            <table className="w-full text-sm">
              <thead><tr className="text-left muted"><th className="py-1">对象</th><th>电话</th><th>状态</th><th>剧本</th><th>通话</th></tr></thead>
              <tbody>
                {detail[c.id].items!.map((it) => (
                  <tr key={it.id} className="border-t">
                    <td className="py-1.5">{objects.find((o) => o.id === it.object_id)?.display_name || it.object_id || "—"}</td>
                    <td className="font-mono">{it.phone || "—"}</td>
                    <td>{ITEM_LABEL[it.status] || it.status}</td>
                    <td>{it.scenario || "—"}</td>
                    <td>{it.call_id ? <Link className="underline" href="/calls">{it.call_id}</Link> : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      ))}
    </div>
  );
}
```

- [ ] **Step 3: 构建 + 提交**

```bash
cd apps/web && npx tsc --noEmit && npm run build
git add apps/web
git commit -m "feat(web): 外呼战役页——新建/启停/进度表/来源通话跳转"
```

---

### Task 13: E2E + 文档 + 全量门禁

**Files:**
- Create: `scripts/e2e_campaign.py`
- Modify: `docs/RUNTIME_TOPOLOGY.md`、`docs/REPO_MAP.md`（新表/新端点/新脚本/新页面各一行）
- Test: 实机 E2E（需运行栈）

**Interfaces:**
- Consumes: 全部前序任务；`scripts/e2e_real_customer.py` 的 `tts_pcm`/进房姿势（import 复用）
- Produces: `python scripts/e2e_campaign.py`（退出码 0=PASS）

- [ ] **Step 1: 写 E2E**

结构（照 `scripts/e2e_interpret.py` 的 run_one/主流程骨架）：

```python
#!/usr/bin/env python3
"""外呼战役 E2E(mock 档全链路):3 对象——1 接通走完话术/1 无人接/1 接通即挂。

断言:串行自动下一通按序发生、终态正确、接通轮 captured 后名册自动入册、
campaign done。话术剧本=接通客户台词含报 WA 号码句(触发捕获)。
"""
```

要点：
1. 前置检查：`GET /health`、`/api/settings`（sip.mode 期望 mock）、三 worker 端口（8081-8083）；栈未起或非 mock 档直接退出非 0。
2. 建 3 对象（display_name 前缀 `E2E-CAMP-`——吃心跳豁免前缀族），phone `+8529xxxxxx1/2/3`；绑定测试话术（查 `templates` 表 acc-001 现有 zh 六步话术 id；没有则建最小 2 步模板：step1 身份+step2 收 WhatsApp）。
3. `POST /api/campaigns`（object_ids×3，scenarios：obj1=answer（script 台词含「我WhatsApp係六四三二零一一一」）、obj2=no_answer、obj3=reject）→ start。
4. 轮询 `GET /api/campaigns/{id}`（2s 间隔，超时 240s）：记录 items 状态迁移序列；断言任意时刻至多 1 个 item 处于 dialing/in_call（串行）。
5. 终态断言：items 状态 = {answer 轮 done、no_answer 轮 no_answer、reject 轮 rejected}；campaign status=done。
6. 名册断言：`GET /api/roster` 含 obj1 的 number=64320111 且 channel=whatsapp；`POST handled` 后 call 的 whatsapp_status=handled。
7. 话音真度：answer 轮 mock 客户出真声——读 mock-callee.log 或 turns 表断言 customer 轮转写非空（`GET /api/calls/{id}/turns`）。
8. 清理：删测试对象/campaign（现有 DELETE 端点），总结 `PASS/FAIL` + 每腿耗时。

- [ ] **Step 2: 实机跑通（需 bok 栈）**

```bash
# 主机栈在 va-deadair worktree 起（用户日常姿势）;本 worktree 代码需先把 agent/CP 指过来:
# 简化路径:直接在 va-campaign 起栈(PYTHONPATH 指本 worktree):
cd /Users/halo/Documents/bok/va-campaign && python tools/bok.py serve   # 首次先 down
python scripts/e2e_campaign.py
```

（跑前 `ps aux | grep agent_runtime` 必须 0——A/B 殭尸 worker 铁律；E2E 错峰跑，勿与真实通话/探针抢 GPU。）

- [ ] **Step 3: 文档**

`docs/RUNTIME_TOPOLOGY.md`：加「外呼战役（mock 档）」小节（CP campaign loop 5s / mock callee 子进程 / dial-result 链路 / BOK_SIP_MODE）；`docs/REPO_MAP.md`：新文件行（dialer.py/campaign.py/mock_callee.py/e2e_campaign.py/roster 页/campaigns 页）+ 新表行。

- [ ] **Step 4: 全量门禁**

```bash
cd /Users/halo/Documents/bok/va-campaign
/Users/halo/Documents/bok/voice-assistant/.venv312/bin/python -m pytest -q   # 全绿
/Users/halo/Documents/bok/voice-assistant/.venv312/bin/python -m compileall -q apps packages services tools scripts
cd apps/web && npx tsc --noEmit && npm run build
```

- [ ] **Step 5: 提交**

```bash
git add scripts/e2e_campaign.py docs
git commit -m "test(e2e): 外呼战役全链路 E2E——串行自动下一通/名册入册/终态三态断言+文档"
```

---

## Task 依赖图

```
Task1(roster repo) ─→ Task2(channel 入册) ─→ Task3(roster API) ─→ Task4(roster 页)
Task5(dialer) ─→ Task6(mock callee) ─→ Task7(agent 外呼+dial-result)
Task8(SIP settings; 依赖 Task5 resolve_dial_mode 已在 T5 内含)
Task9(campaign repo) ─→ Task10(campaign loop; 依赖 T7 dial-result) ─→ Task11(campaign API) ─→ Task12(campaigns 页)
全部 ─→ Task13(E2E+文档)
```

并行机会：Wave1（T1-T4）与 Wave2（T5-T8）无互相依赖，可两组 subagent 并行；T9 可与 T5-T8 并行；T10 起串行。
