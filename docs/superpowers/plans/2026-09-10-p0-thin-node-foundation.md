# P0 薄节点地基拆分 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 spec（`docs/superpowers/specs/2026-09-10-thin-node-saas-design.md`）的 P0：数据层具备 Postgres 可移植性、turns 升级为分析账本、节点注册/心跳最小版、node-agent 守护雏形、web 运行时配置注入（节点托管 UI 地基）、部署脚本草版、CUDA 基线探针。

**Architecture:** CP 仍是 FastAPI，但 `DATABASE_URL` 可指向 Supabase Postgres（开发/测试默认不变）；新增 `orgs`/`nodes` 表立 org 缝；node-agent（`tools/node_agent.py`）复用 bok.py 编排函数并加心跳循环；web 静态导出改为运行时读 `window.__BOK_CONFIG__`，同一份产物云端出管理台、节点出坐席工作台。

**Tech Stack:** SQLAlchemy（已有）、FastAPI TestClient、stdlib urllib（node-agent 零新依赖）、Next.js 静态导出。

## Global Constraints

- 术语门禁：粤语规范值全时空唯一拼写（小写 cantonese）；新文件/改动不得出现旧拼写字面量——包括本计划文档自身（`tests/test_cantonese_terminology.py` 扫描跟踪文件，曾发生计划文档自踩）
- `livekit-agents>=1.8.0,<1.9` 不动；本轮零音频链路改动（PERCEIVED_MS 北极星不回归）
- 开发/测试栈默认不回归：`DATABASE_URL` 未设 = in-memory repo（`control_plane/deps.py:245`），SQLite 仍是 dev 默认
- 改完 Python 必跑 `python -m compileall -q apps packages services tools scripts`
- web 改动必跑 `cd apps/web && npx tsc --noEmit && npm run build`
- 改 turns 路径后必跑 `scripts/load_cp_concurrency.py`（turns 并发 30/30 回归，AGENTS.md 规约）
- Conventional commits，一个逻辑改动一个提交
- 新表/新列一律走幂等迁移（`build_engine()` 的 `_ensure_column` 模式，`apps/control-plane/control_plane/deps.py:40-47`）

---

### Task 1: Org/Node 模型 + Postgres 方言兼容门禁

**Files:**
- Modify: `packages/business-db/bok_voice_business_db/models.py`（文件末尾追加两个模型）
- Create: `tests/test_db_portability.py`

**Interfaces:**
- Produces: `Org`、`Node` ORM 模型（Task 3 的 `NodeStore` 依赖）；`Base.metadata.tables` 含 `orgs`/`nodes`
- Consumes: 既有 `Base`/`Mapped`/`mapped_column`/`utcnow`（models.py 头部已 import）

- [ ] **Step 1: 写失败测试**

Create `tests/test_db_portability.py`:

```python
"""方言兼容门禁（spec 2026-09-10-thin-node-saas §5）：全部 ORM 表必须能对
sqlite 与 postgresql 两种方言编译 DDL。P0 起业务库要能搬 Supabase Postgres，
任何 sqlite 专有类型/语法都会在这道门禁上红。无服务器、纯编译，可进 CI。
"""

from __future__ import annotations

from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql, sqlite

from bok_voice_business_db.models import Base

EXPECTED_TABLES = {
    "accounts", "persona_profiles", "object_profiles", "conversation_templates",
    "object_topics", "call_sessions", "turns", "settlements", "global_insights",
    "global_settings", "audit_events", "conversation_template_revisions",
    "usage_records", "qa_entries", "orgs", "nodes",
}


def test_all_tables_declared():
    assert set(Base.metadata.tables.keys()) >= EXPECTED_TABLES


def test_ddl_compiles_on_sqlite_and_postgres():
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        for table in Base.metadata.tables.values():
            CreateTable(table).compile(dialect=dialect)  # 不抛即过
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/pytest tests/test_db_portability.py -v`
Expected: FAIL —— `EXPECTED_TABLES` 里 `orgs`/`nodes` 不存在（`test_all_tables_declared` 红）

- [ ] **Step 3: 实现两个模型**

在 `packages/business-db/bok_voice_business_db/models.py` 末尾（`QaEntry` 类之后）追加：

```python
class Org(Base):
    """租户（P0 骨架：身份体系 P1 落地，先立 org 缝）。"""
    __tablename__ = "orgs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/suspended
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Node(Base):
    """部署节点（客户机房 GPU 盒）：注册时签发 node_token，只存 sha256。"""
    __tablename__ = "nodes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    token_hash: Mapped[str] = mapped_column(String(128), default="")
    platform: Mapped[str] = mapped_column(String(32), default="")  # cuda-win / mac-mlx
    version: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="offline")  # online/offline/revoked
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    last_seen_at: Mapped[object] = mapped_column(DateTime, default=None, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
```

（`DateTime` 若头部未 import 则补进 `from sqlalchemy import ...` 行；`Mapped[object]` 写法保持与文件内既有 nullable 风格一致，若文件里已有 `Mapped[datetime | None]` 用法则照用。）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv312/bin/pytest tests/test_db_portability.py -v`
Expected: PASS（2 个测试全绿；若 postgres 编译报某列类型不兼容，修该列类型后重跑）

- [ ] **Step 5: 门禁 + 提交**

```bash
python -m compileall -q apps packages services tools scripts
git add packages/business-db/bok_voice_business_db/models.py tests/test_db_portability.py
git commit -m "feat(db): orgs/nodes 表模型+Postgres 方言兼容门禁——薄节点 P0 地基(spec §5)"
```

---

### Task 2: turns 分析账本新列（ORM + 幂等迁移 + repo 双实现 + HTTP 端点）

**Files:**
- Modify: `packages/business-db/bok_voice_business_db/models.py:118-129`（Turn 类）
- Modify: `apps/control-plane/control_plane/deps.py`（`_ensure_column` 调用块，~:50-105）
- Modify: `packages/core/bok_voice_core/types.py:81`（TurnEvent dataclass）
- Modify: `packages/business-db/bok_voice_business_db/repository.py:92-103`（SQL create_turn）与同文件 get_turns 行序列化处、`:673`（内存 create_turn）
- Modify: `apps/control-plane/control_plane/main.py:857-883`（add_turn 端点）
- Test: `tests/test_turns_ledger.py`

**Interfaces:**
- Produces: `TurnEvent` 新字段 `org_id/line/speaker/gen/template_step/started_ms/ended_ms/perceived_ms`（全部带缺省值）；`POST /api/calls/{call_id}/turns` 接受同名可选 query 参数；GET turns 返回体含全部新字段
- Consumes: Task 1 无依赖，可并行；既有 `_ensure_column` 幂等迁移模式

- [ ] **Step 1: 写失败测试**

Create `tests/test_turns_ledger.py`（照 `tests/test_turn_auditability.py` 的 env/TestClient 惯例）:

```python
"""turns 分析账本（spec 2026-09-10 §6.1）：org_id/line/speaker/gen/template_step/
时间轴/perceived_ms 落库 + 旧调用零破坏（缺省值兜底）+ 幂等迁移补列。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # in-memory repo
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

from fastapi.testclient import TestClient

from control_plane.main import app

NEW_FIELDS = {
    "org_id": "org-demo",
    "line": "b",
    "speaker": "customer",
    "gen": "llm",
    "template_step": 2,
    "started_ms": 1500,
    "ended_ms": 4200,
    "perceived_ms": 1820,
}


def _make_call(client: TestClient) -> str:
    r = client.post(
        "/api/calls",
        json={"account_id": "acc-001", "mode": "simulation", "direction": "webrtc", "language": "cantonese"},
    )
    assert r.status_code in (200, 201)
    return r.json()["id"]


def test_new_ledger_fields_persisted():
    with TestClient(app) as client:
        call_id = _make_call(client)
        r = client.post(
            f"/api/calls/{call_id}/turns",
            params={"role": "user", "transcript": "我個單號係三七七八九零", **NEW_FIELDS},
        )
        assert r.status_code in (200, 201)
        t = client.get(f"/api/calls/{call_id}/turns").json()[-1]
        for key, value in NEW_FIELDS.items():
            assert t[key] == value, f"{key} 未落库: {t}"


def test_old_callers_get_defaults():
    with TestClient(app) as client:
        call_id = _make_call(client)
        r = client.post(
            f"/api/calls/{call_id}/turns",
            params={"role": "assistant", "transcript": "你好"},
        )
        assert r.status_code in (200, 201)
        t = client.get(f"/api/calls/{call_id}/turns").json()[-1]
        assert t["org_id"] == "" and t["line"] == "a" and t["speaker"] == ""
        assert t["perceived_ms"] == 0


def test_migration_adds_ledger_columns(tmp_path, monkeypatch):
    db = tmp_path / "ledger.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    from control_plane.deps import build_engine

    build_engine()
    build_engine()  # 幂等：二启不报错
    import sqlite3

    cols = {row[1] for row in sqlite3.connect(db).execute("PRAGMA table_info(turns)")}
    assert {"org_id", "line", "speaker", "gen", "template_step", "started_ms", "ended_ms", "perceived_ms"} <= cols
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/pytest tests/test_turns_ledger.py -v`
Expected: FAIL —— 新字段 404/KeyError（端点不认识新参数）

- [ ] **Step 3: 实现（四处触点，一次到位）**

① `packages/core/bok_voice_core/types.py` `TurnEvent`（:81）追加带缺省字段：

```python
    org_id: str = ""
    line: str = "a"          # a=客服(A 线) / b=同传(B 线)
    speaker: str = ""        # customer/agent_ai/agent_human | me/other(B 线)
    gen: str = ""            # llm/script/filler/qa_fastpath
    template_step: int = 0
    started_ms: int = 0
    ended_ms: int = 0
    perceived_ms: int = 0
```

② `packages/business-db/bok_voice_business_db/models.py` `Turn` 类（:118-129）追加同名列（`org_id` 带 `index=True`，`template_step` 等用 `Integer`）。

③ `apps/control-plane/control_plane/deps.py` 迁移块（`_ensure_column` 调用序列尾部）追加：

```python
                _ensure_column(conn, "turns", "org_id", "org_id VARCHAR(64) DEFAULT ''")
                _ensure_column(conn, "turns", "line", "line VARCHAR(8) DEFAULT 'a'")
                _ensure_column(conn, "turns", "speaker", "speaker VARCHAR(32) DEFAULT ''")
                _ensure_column(conn, "turns", "gen", "gen VARCHAR(16) DEFAULT ''")
                _ensure_column(conn, "turns", "template_step", "template_step INTEGER DEFAULT 0")
                _ensure_column(conn, "turns", "started_ms", "started_ms INTEGER DEFAULT 0")
                _ensure_column(conn, "turns", "ended_ms", "ended_ms INTEGER DEFAULT 0")
                _ensure_column(conn, "turns", "perceived_ms", "perceived_ms INTEGER DEFAULT 0")
```

④ `packages/business-db/bok_voice_business_db/repository.py`：SQL `create_turn`（:92-103 的 `models.Turn(...)` 构造）补 8 个 `turn.<field>` 传参；同文件 `get_turns` 的行→dict 序列化处同步补列（在该文件内 grep `latency_ms` 找齐全部 Turn 行读取点）；内存版 `create_turn`（:673）TurnEvent 直接携带字段无需改动，但内存 `get_turns` 返回 dict 时确认含新字段（TurnEvent 若以 `asdict` 输出则自动带出）。

⑤ `apps/control-plane/control_plane/main.py` `add_turn`（:857-883）签名追加可选参数并在 `TurnEvent(...)` 构造补传：

```python
    org_id: str = "",
    line: str = "a",
    speaker: str = "",
    gen: str = "",
    template_step: int = 0,
    started_ms: int = 0,
    ended_ms: int = 0,
    perceived_ms: int = 0,
```

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `.venv312/bin/pytest tests/test_turns_ledger.py tests/test_turn_auditability.py tests/test_control_plane.py -v`
Expected: PASS（新账本 + 既有审计/迁移测试全绿）

- [ ] **Step 5: 门禁 + 提交**

```bash
python -m compileall -q apps packages services tools scripts
git add -A packages/core packages/business-db apps/control-plane tests/test_turns_ledger.py
git commit -m "feat(cp): turns 升级分析账本——org_id/line/speaker/gen/话术步/时间轴/perceived_ms(spec §6.1)"
```

---

### Task 3: 节点注册/心跳端点（P0 最小版）

**Files:**
- Create: `apps/control-plane/control_plane/nodes_store.py`
- Modify: `apps/control-plane/control_plane/main.py`（启动装配点旁初始化 NodeStore + 三个端点）
- Test: `tests/test_nodes_registry.py`

**Interfaces:**
- Produces:
  - `POST /api/nodes/register` body `{name, platform, org_id?, version?}` → `{node_id, node_token, heartbeat_interval_s: 60}`
  - `POST /api/nodes/heartbeat`（`Authorization: Bearer <node_token>`）body `{metrics?}` → `{ok: true, commands: []}`（commands P3 填充）
  - `GET /api/nodes` → `[{node_id, name, org_id, platform, version, status, last_seen_at}]`
  - `NodeStore(engine|None)`：`register() -> tuple[str, str]`、`heartbeat(token, metrics) -> bool`、`list_nodes() -> list[dict]`、纯函数 `effective_status(last_seen_at, now, revoked=False) -> str`
- Consumes: Task 1 的 `Node` ORM 模型

- [ ] **Step 1: 写失败测试**

Create `tests/test_nodes_registry.py`:

```python
"""节点注册/心跳 P0 最小版（spec §4.2 L1 前置）：token 只存哈希、心跳刷新
last_seen、离线判定=3× 心跳窗口、engine=None 时退化为内存存储（测试/dev 栈）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from control_plane.nodes_store import NodeStore, effective_status


def test_register_returns_plaintext_token_once_and_stores_hash():
    store = NodeStore(None)
    node_id, token = store.register(name="n1", platform="cuda-win", org_id="org-1")
    assert node_id and token
    assert store._rows[node_id]["token_hash"] != token  # 落库的是哈希
    assert len(store._rows[node_id]["token_hash"]) == 64  # sha256 hex


def test_heartbeat_authenticates_by_token():
    store = NodeStore(None)
    node_id, token = store.register(name="n1", platform="cuda-win", org_id="")
    assert store.heartbeat(token, metrics={"gpu": 0.4}) is True
    assert store.heartbeat("bad-token", metrics={}) is False


def test_effective_status_offline_after_window():
    now = datetime.now(timezone.utc)
    assert effective_status(None, now) == "offline"
    assert effective_status(now - timedelta(seconds=120), now) == "online"
    assert effective_status(now - timedelta(seconds=181), now) == "offline"


def test_list_nodes_shape():
    store = NodeStore(None)
    node_id, token = store.register(name="edge-1", platform="mac-mlx", org_id="org-1")
    store.heartbeat(token, metrics={})
    rows = store.list_nodes()
    assert rows[0]["node_id"] == node_id
    assert rows[0]["status"] == "online"
    assert "token_hash" not in rows[0]  # 永不出参
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/pytest tests/test_nodes_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: control_plane.nodes_store`

- [ ] **Step 3: 实现 NodeStore + 三个端点**

Create `apps/control-plane/control_plane/nodes_store.py`:

```python
"""节点注册表（P0 最小版，spec §4.2）：engine 有则走 SQL(nodes 表)、无则内存
dict（dev/tests，与 build_repository 的双模一致）。token 明文只在注册响应出现
一次，库内恒为 sha256。commands 通道 P3 填充（L1/L2/L3），P0 恒返回空表。"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

HEARTBEAT_INTERVAL_S = 60
ONLINE_WINDOW_S = HEARTBEAT_INTERVAL_S * 3


def effective_status(last_seen_at, now, revoked: bool = False) -> str:
    if revoked:
        return "revoked"
    if last_seen_at is None:
        return "offline"
    return "online" if (now - last_seen_at).total_seconds() <= ONLINE_WINDOW_S else "offline"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NodeStore:
    def __init__(self, engine) -> None:
        self._engine = engine
        self._rows: dict[str, dict] = {}  # 内存模式（engine=None）
        self._session_factory = None
        if engine is not None:
            from sqlalchemy.orm import sessionmaker

            self._session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def register(self, name: str, platform: str, org_id: str = "", version: str = "") -> tuple[str, str]:
        node_id = f"node-{secrets.token_hex(6)}"
        token = secrets.token_urlsafe(32)
        row = {
            "node_id": node_id, "org_id": org_id, "name": name, "platform": platform,
            "version": version, "token_hash": _hash(token), "metrics_json": "{}",
            "last_seen_at": _utcnow(), "revoked": False,
        }
        if self._session_factory is None:
            self._rows[node_id] = row
        else:
            from bok_voice_business_db import models

            with self._session_factory() as session:
                session.add(models.Node(
                    id=node_id, org_id=org_id, name=name, platform=platform,
                    version=version, token_hash=row["token_hash"], status="online",
                    last_seen_at=row["last_seen_at"],
                ))
                session.commit()
        return node_id, token

    def heartbeat(self, token: str, metrics: dict | None = None) -> bool:
        token_hash = _hash(token)
        import json

        if self._session_factory is None:
            for row in self._rows.values():
                if row["token_hash"] == token_hash and not row["revoked"]:
                    row["last_seen_at"] = _utcnow()
                    row["metrics_json"] = json.dumps(metrics or {})
                    return True
            return False
        from sqlalchemy import update

        from bok_voice_business_db import models

        with self._session_factory() as session:
            result = session.execute(
                update(models.Node)
                .where(models.Node.token_hash == token_hash, models.Node.status != "revoked")
                .values(last_seen_at=_utcnow(), metrics_json=json.dumps(metrics or {}))
            )
            session.commit()
            return result.rowcount > 0

    def list_nodes(self) -> list[dict]:
        now = _utcnow()
        if self._session_factory is None:
            rows = list(self._rows.values())
        else:
            from bok_voice_business_db import models

            with self._session_factory() as session:
                rows = [
                    {"node_id": n.id, "org_id": n.org_id, "name": n.name, "platform": n.platform,
                     "version": n.version, "revoked": n.status == "revoked", "last_seen_at": n.last_seen_at}
                    for n in session.query(models.Node).all()
                ]
        return [
            {
                "node_id": r["node_id"], "org_id": r["org_id"], "name": r["name"],
                "platform": r["platform"], "version": r["version"],
                "status": effective_status(r.get("last_seen_at"), now, r.get("revoked", False)),
                "last_seen_at": r["last_seen_at"].isoformat() if r.get("last_seen_at") else None,
            }
            for r in rows
        ]
```

在 `apps/control-plane/control_plane/main.py` 装配点（`_repo()`/engine 初始化旁，grep `build_repository(` 找到唯一调用处）同款双模初始化：

```python
from control_plane.nodes_store import NodeStore

_node_store = NodeStore(_engine)  # 与 build_repository 同一 engine 变量；engine=None 路径同款
```

（以 main.py 实际变量名为准；engine 未设时传 `None`。）

端点（加在 whatsapp 端点附近）：

```python
class NodeRegisterRequest(BaseModel):
    name: str = ""
    platform: str = ""
    org_id: str = ""
    version: str = ""


class NodeHeartbeatRequest(BaseModel):
    metrics: dict = {}


@app.post("/api/nodes/register")
def register_node(req: NodeRegisterRequest) -> dict:
    node_id, token = _node_store.register(
        name=req.name, platform=req.platform, org_id=req.org_id, version=req.version
    )
    return {"node_id": node_id, "node_token": token, "heartbeat_interval_s": HEARTBEAT_INTERVAL_S}


@app.post("/api/nodes/heartbeat")
def node_heartbeat(req: NodeHeartbeatRequest, authorization: str = Header(default="")) -> dict:
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not _node_store.heartbeat(token, req.metrics):
        raise HTTPException(401, "unknown node token")
    return {"ok": True, "commands": []}


@app.get("/api/nodes")
def list_nodes() -> list[dict]:
    return _node_store.list_nodes()
```

（`Header`/`BaseModel`/`HTTPException`/`HEARTBEAT_INTERVAL_S` 按 main.py 既有 import 情况补齐；`HEARTBEAT_INTERVAL_S` 从 `nodes_store` import。）

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `.venv312/bin/pytest tests/test_nodes_registry.py tests/test_control_plane.py -v`
Expected: PASS

- [ ] **Step 5: 门禁 + 提交**

```bash
python -m compileall -q apps packages services tools scripts
git add apps/control-plane tests/test_nodes_registry.py
git commit -m "feat(cp): 节点注册/心跳端点——token哈希存储/3×窗口离线判定/commands通道占位(spec §4.2)"
```

---

### Task 4: node-agent 守护雏形（心跳循环 + 编排复用）

**Files:**
- Create: `tools/node_agent.py`
- Test: `tests/test_node_agent.py`

**Interfaces:**
- Produces:
  - `NodeConfig` dataclass：`cp_url: str`、`node_token: str`、`heartbeat_interval_s: int = 60`、`max_missed: int = 3`
  - `heartbeat_once(cfg: NodeConfig, metrics: dict | None = None) -> tuple[bool, dict]`（`(ok, response_json)`）
  - `should_refuse_jobs(missed: int, max_missed: int) -> bool`
  - `write_ui_config(out_dir: Path, cp_url: str, livekit_url: str) -> Path`（Task 5 的 `runtime-config.js` 消费方）
  - CLI：`python tools/node_agent.py --cp-url URL --node-token TOKEN [--heartbeat-only] [--ui-dir DIR] [--livekit-url URL]`
- Consumes: Task 3 端点契约；`bok.py` 的 `cmd_up/cmd_down`（`import bok` 同目录）

- [ ] **Step 1: 写失败测试**

Create `tests/test_node_agent.py`:

```python
"""node-agent 守护（spec §4.2/§4.3）：心跳一次性调用、失联拒新 job 纯函数、
UI 运行时配置注入文件内容。全部 monkeypatch，不连真实 CP。"""

from __future__ import annotations

import json
from unittest.mock import patch

from node_agent import NodeConfig, heartbeat_once, should_refuse_jobs, write_ui_config


def _cfg(**kw) -> NodeConfig:
    return NodeConfig(cp_url="http://cp.test", node_token="tok-1", **kw)


def test_heartbeat_once_posts_bearer_and_parses_commands():
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        body = json.loads(req.data.decode())
        assert body == {"metrics": {"gpu": 0.5}}

        class R:
            def read(self, n=-1):
                return json.dumps({"ok": True, "commands": [{"type": "noop"}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R()

    with patch("node_agent.urllib.request.urlopen", fake_urlopen):
        ok, resp = heartbeat_once(_cfg(), metrics={"gpu": 0.5})
    assert ok is True
    assert resp["commands"][0]["type"] == "noop"
    assert captured["url"].endswith("/api/nodes/heartbeat")
    assert captured["headers"]["Authorization"] == "Bearer tok-1"


def test_should_refuse_jobs_after_max_missed():
    assert should_refuse_jobs(0, 3) is False
    assert should_refuse_jobs(2, 3) is False
    assert should_refuse_jobs(3, 3) is True


def test_write_ui_config(tmp_path):
    out = write_ui_config(tmp_path, cp_url="https://cp.example.com", livekit_url="ws://10.0.0.5:7880")
    data = out.read_text()
    assert out.name == "runtime-config.js"
    assert "https://cp.example.com" in data and "ws://10.0.0.5:7880" in data
```

（`sys.path`：conftest 若不含 `tools/`，测试头部加 `sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))`——先看 `tests/conftest.py` 是否已可 import `node_agent`，不可则加。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/pytest tests/test_node_agent.py -v`
Expected: FAIL with `ModuleNotFoundError: node_agent`

- [ ] **Step 3: 实现 tools/node_agent.py**

```python
"""node-agent：薄节点守护（spec §4.2/§4.3，bok.py serve 的无头演化）。

P0 职责：①向云 CP 心跳上报（失联 ≥max_missed 置 refuse_jobs 旗标，日志可见；
拒派发的执行端是 livekit load_threshold，P3 接 commands 通道后由指令精确控制）
②可选拉起全栈（复用 bok.cmd_up/cmd_down）③把 cpUrl/livekitUrl 注入 web 产物
（runtime-config.js），使同一份静态导出可作节点本地坐席工作台。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))


@dataclass
class NodeConfig:
    cp_url: str
    node_token: str
    heartbeat_interval_s: int = 60
    max_missed: int = 3


def heartbeat_once(cfg: NodeConfig, metrics: dict | None = None) -> tuple[bool, dict]:
    body = json.dumps({"metrics": metrics or {}}).encode()
    req = urllib.request.Request(
        f"{cfg.cp_url.rstrip('/')}/api/nodes/heartbeat",
        data=body,
        headers={"Authorization": f"Bearer {cfg.node_token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, json.loads(resp.read().decode())
    except Exception as exc:  # 失联不抛——计数交给调用方
        print(f"[node-agent] heartbeat failed: {exc!r}", flush=True)
        return False, {}


def should_refuse_jobs(missed: int, max_missed: int) -> bool:
    return missed >= max_missed


def write_ui_config(out_dir: Path, cp_url: str, livekit_url: str) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "runtime-config.js"
    target.write_text(
        "window.__BOK_CONFIG__ = "
        + json.dumps({"cpUrl": cp_url, "livekitUrl": livekit_url})
        + ";\n"
    )
    return target


def heartbeat_loop(cfg: NodeConfig, stop: threading.Event) -> None:
    missed = 0
    while not stop.wait(cfg.heartbeat_interval_s):
        ok, _ = heartbeat_once(cfg, metrics={"missed": missed})
        missed = 0 if ok else missed + 1
        if should_refuse_jobs(missed, cfg.max_missed):
            print(f"[node-agent] missed={missed} >= {cfg.max_missed}: REFUSE_JOBS (L1)", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Bok 薄节点守护")
    ap.add_argument("--cp-url", required=True)
    ap.add_argument("--node-token", required=True)
    ap.add_argument("--heartbeat-only", action="store_true", help="不拉起全栈，只跑心跳")
    ap.add_argument("--ui-dir", default="", help="web 静态产物目录（提供则写 runtime-config.js）")
    ap.add_argument("--livekit-url", default="ws://127.0.0.1:7880")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args(argv)

    cfg = NodeConfig(cp_url=args.cp_url, node_token=args.node_token, heartbeat_interval_s=args.interval)
    if args.ui_dir:
        target = write_ui_config(Path(args.ui_dir), cfg.cp_url, args.livekit_url)
        print(f"[node-agent] ui config -> {target}", flush=True)

    if args.heartbeat_only:
        stop = threading.Event()
        print(f"[node-agent] heartbeat loop start (interval={cfg.heartbeat_interval_s}s)", flush=True)
        try:
            heartbeat_loop(cfg, stop)
        except KeyboardInterrupt:
            stop.set()
        return 0

    import bok

    bok.cmd_up()
    stop = threading.Event()
    worker = threading.Thread(target=heartbeat_loop, args=(cfg, stop), daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        bok.cmd_down()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 跑测试确认通过 + 手跑冒烟**

Run: `.venv312/bin/pytest tests/test_node_agent.py -v`
Expected: PASS

冒烟（起临时 CP 或跳过）：`.venv312/bin/python tools/node_agent.py --cp-url http://127.0.0.1:8000 --node-token smoke --heartbeat-only --interval 2`（对无 token 的本地 CP 会打 `heartbeat failed` 日志并持续计数——这本身就是 L1 失联路径的可视验收）；`Ctrl+C` 退出。

- [ ] **Step 5: 门禁 + 提交**

```bash
python -m compileall -q tools
git add tools/node_agent.py tests/test_node_agent.py
git commit -m "feat(node): node-agent 守护雏形——心跳循环/L1失联拒job旗标/UI配置注入(spec §4.2)"
```

---

### Task 5: web 运行时配置注入（节点托管 UI 地基）

**Files:**
- Modify: `apps/web/lib/api.ts:2`（构建期常量 → 运行时函数）
- Modify: `apps/web/lib/api.ts` 内全部 `CONTROL_PLANE_URL` 引用点（:27/:40/:45/:120 等）及全仓其他 import 该常量的文件（先 `grep -rn "CONTROL_PLANE_URL" apps/web --include="*.ts" --include="*.tsx"` 列全）
- Create: `apps/web/public/runtime-config.js`
- Modify: `apps/web/app/layout.tsx`（`<body>` 首位加 `<script src="/runtime-config.js" />`）

**Interfaces:**
- Produces: `apiBase(): string` —— 求值顺序 `window.__BOK_CONFIG__.cpUrl` → `process.env.NEXT_PUBLIC_CONTROL_PLANE_URL` → `http://127.0.0.1:8000`；`window.__BOK_CONFIG__` 形状由 Task 4 `write_ui_config` 写入
- Consumes: 无（独立可先行）

- [ ] **Step 1: 写占位 runtime-config.js**

Create `apps/web/public/runtime-config.js`:

```js
// 运行时配置注入口：节点本地托管时由 node-agent 覆写本文件（tools/node_agent.py
// write_ui_config）；云端托管保持空对象（走构建期 NEXT_PUBLIC_* 或默认值）。
window.__BOK_CONFIG__ = window.__BOK_CONFIG__ || {};
```

- [ ] **Step 2: apiBase 函数化并替换引用点**

`apps/web/lib/api.ts:2` 的

```ts
export const CONTROL_PLANE_URL = process.env.NEXT_PUBLIC_CONTROL_PLANE_URL ?? "http://127.0.0.1:8000";
```

改为：

```ts
declare global {
  interface Window {
    __BOK_CONFIG__?: { cpUrl?: string; livekitUrl?: string };
  }
}

// 运行时求值（构建期常量会把地址烤进产物，节点本地托管即失效）：
// 节点注入的 window.__BOK_CONFIG__.cpUrl 优先，云端托管回退构建期 env/默认值。
export function apiBase(): string {
  if (typeof window !== "undefined" && window.__BOK_CONFIG__?.cpUrl) {
    return window.__BOK_CONFIG__.cpUrl;
  }
  return process.env.NEXT_PUBLIC_CONTROL_PLANE_URL ?? "http://127.0.0.1:8000";
}
```

文件内与全仓所有 `CONTROL_PLANE_URL` 引用点改为 `${apiBase()}${path}` 形式（模板字符串内替换常量为函数调用）；其他文件 `import { CONTROL_PLANE_URL }` 改 `import { apiBase }` 并在调用处求值。**逐个过 `grep -rn "CONTROL_PLANE_URL" apps/web --include="*.ts" --include="*.tsx"` 的全部结果，清零后才能进下一步。**

- [ ] **Step 3: layout 挂载脚本**

`apps/web/app/layout.tsx` 的 `<body>` 首个子元素位置加：

```tsx
<script src="/runtime-config.js" />
```

- [ ] **Step 4: 类型与构建门禁 + 产物抽查**

```bash
cd apps/web && npx tsc --noEmit && npm run build
grep -c "runtime-config.js" out/index.html   # ≥1，静态导出含注入口
grep -rl "127.0.0.1:8000" out/_next/static/chunks/ | head -3  # 允许存在（默认值兜底），但产物可被 runtime 覆写
```

Expected: tsc/build 全绿；`out/runtime-config.js` 存在（public 拷贝）。

- [ ] **Step 5: 提交**

```bash
git add apps/web/lib/api.ts apps/web/public/runtime-config.js apps/web/app/layout.tsx
git add -A apps/web  # 引用点替换的其余文件
git commit -m "feat(web): CP 地址运行时注入——window.__BOK_CONFIG__优先于构建期env，同一产物可作节点本地工作台(spec §8)"
```

---

### Task 6: 节点部署脚本草版（install-node.sh + install-node.ps1）

**Files:**
- Create: `scripts/install-node.sh`
- Create: `scripts/install-node.ps1`

**Interfaces:**
- Produces: `--dry-run` 模式输出将执行的计划步骤清单（不落任何盘）；正式模式按序执行：环境体检 → venv/依赖 → 模型下载 → 节点注册（`--cp-url --node-token`）→ UI 配置注入
- Consumes: Task 4 的 `tools/node_agent.py` CLI

- [ ] **Step 1: 写 install-node.sh**

```bash
#!/usr/bin/env bash
# 薄节点部署草版（spec §11.2；P0=手工档，P2 正式化含离线包）。
# 用法: install-node.sh --cp-url URL --node-token TOK [--dry-run] [--skip-models]
set -euo pipefail

CP_URL=""; NODE_TOKEN=""; DRY_RUN=0; SKIP_MODELS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cp-url) CP_URL="$2"; shift 2;;
    --node-token) NODE_TOKEN="$2"; shift 2;;
    --dry-run) DRY_RUN=1; shift;;
    --skip-models) SKIP_MODELS=1; shift;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done
[[ -n "$CP_URL" && -n "$NODE_TOKEN" ]] || { echo "--cp-url/--node-token required"; exit 2; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
steps=()
step() { steps+=("$*"); echo "[plan] $*"; [[ $DRY_RUN -eq 1 ]] || bash -c "$*"; }

step "echo '[1/5] 环境体检: GPU/驱动/磁盘'"
if command -v nvidia-smi >/dev/null 2>&1; then step "nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader"; else echo "[plan] 无 NVIDIA GPU —— mac-mlx 档继续"; fi
step "df -h \"$HOME\" | tail -1"

step "echo '[2/5] Python venv + 依赖'"
step "\"$REPO_ROOT/scripts/bootstrap.sh\""

if [[ $SKIP_MODELS -eq 0 ]]; then
  step "echo '[3/5] 模型下载（幂等续传）'"
  step "\"$REPO_ROOT/.venv312/bin/python\" \"$REPO_ROOT/tools/bok.py\" download"
fi

step "echo '[4/5] 节点注册 + 心跳/UI 配置注入'"
step "\"$REPO_ROOT/.venv312/bin/python\" \"$REPO_ROOT/tools/node_agent.py\" --cp-url \"$CP_URL\" --node-token \"$NODE_TOKEN\" --ui-dir \"$REPO_ROOT/apps/web/out\" --heartbeat-only --interval 1 & sleep 3; kill %1 2>/dev/null || true"

step "echo '[5/5] doctor 终检'"
step "\"$REPO_ROOT/.venv312/bin/python\" \"$REPO_ROOT/tools/bok.py\" doctor || true"

echo "共 ${#steps[@]} 步。$([[ $DRY_RUN -eq 1 ]] && echo '(dry-run 未执行)' || echo '完成。')"
```

- [ ] **Step 2: 写 install-node.ps1（Windows 草版，结构对齐）**

```powershell
# 薄节点部署草版 Windows/CUDA 档（spec §11.2；正式版 P2 出离线包）。
# 用法: .\install-node.ps1 -CpUrl URL -NodeToken TOK [-DryRun] [-SkipModels]
param(
  [Parameter(Mandatory=$true)][string]$CpUrl,
  [Parameter(Mandatory=$true)][string]$NodeToken,
  [switch]$DryRun,
  [switch]$SkipModels
)
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Step($desc, $cmd) {
  Write-Host "[plan] $desc"
  if (-not $DryRun) { Invoke-Expression $cmd }
}

Step "[1/5] 环境体检: GPU/驱动" "nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader"
Step "[2/5] Python venv + 依赖" "& `"$RepoRoot\scripts\bootstrap.sh`""   # P0 草版占位: Windows 走 scripts/bootstrap.ps1（若无则本步提示手工执行 requirements 安装）
if (-not $SkipModels) {
  Step "[3/5] 模型下载" "& `"$RepoRoot\.venv312\Scripts\python.exe`" `"$RepoRoot\tools\bok.py`" download"
}
Step "[4/5] 节点注册+心跳/UI 注入" "& `"$RepoRoot\.venv312\Scripts\python.exe`" `"$RepoRoot\tools\node_agent.py`" --cp-url `"$CpUrl`" --node-token `"$NodeToken`" --ui-dir `"$RepoRoot\apps\web\out`" --heartbeat-only --interval 1"
Step "[5/5] doctor 终检" "& `"$RepoRoot\.venv312\Scripts\python.exe`" `"$RepoRoot\tools\bok.py`" doctor"
Write-Host "完成（DryRun=$DryRun）"
```

（Windows venv/bootstrap 路径按仓库现状校正：若 `scripts/` 无 Windows 引导，Step 2 改为打印手工指引——P0 草版允许，P2 正式化收口。）

- [ ] **Step 3: 语法与 dry-run 验收**

```bash
bash -n scripts/install-node.sh && chmod +x scripts/install-node.sh
./scripts/install-node.sh --cp-url http://x --node-token t --dry-run | tail -8
command -v pwsh >/dev/null && pwsh -NoProfile -Command "\$ErrorActionPreference='Stop'; [System.Management.Automation.Language.Parser]::ParseFile('$PWD/scripts/install-node.ps1', [ref]\$null, [ref]\$err) | Out-Null; \$err | ForEach-Object { \$_.Message }; exit (\$err.Count -gt 0)"
```

Expected: `bash -n` 零输出；dry-run 打出 5 步 `[plan]` 清单且末行 `共 N 步。(dry-run 未执行)`；pwsh 语法零错误（无 pwsh 则本步跳过并在提交信息注明）。

- [ ] **Step 4: 提交**

```bash
git add scripts/install-node.sh scripts/install-node.ps1
git commit -m "feat(node): 部署脚本草版双平台——dry-run计划输出/体检/模型/注册/doctor(spec §11.2 P0档)"
```

---

### Task 7: CUDA 原型基线探针

**Files:**
- Create: `scripts/probe_cuda_baseline.py`

**Interfaces:**
- Produces: `python scripts/probe_cuda_baseline.py --llm http://HOST:1235 --asr http://HOST:8787 --wav tests/fixtures/<任一三语 wav> --rounds 5 --out reports/cuda_baseline_<date>.json`；输出 JSON 含 `llm_ttft_ms` 与 `asr_ms` 的 p50/p90/max 列表。**门禁口径（spec §9）：p90 LLM TTFT 对齐 Mac 基线量级（1.5s 内为达标线，超标即触发 Mac Studio 备选档讨论）**
- Consumes: 既有 llama-server（OpenAI 兼容 `:1235/v1`）与 qwen-asr sidecar（`:8787`）HTTP 契约；请求 model 字段必须用本地模型路径（memory 规约：repo id 会触发 HF hub 解析）

- [ ] **Step 1: 实现探针（工具型任务，先实现后用 dry-run 验证）**

```python
"""CUDA 原型延迟基线探针（spec §9 门禁）：在 CUDA 节点对 llama-server(:1235)
与 qwen-asr(:8787) 跑与 Mac 侧 measure_latency 同口径的 TTFT/ASR 采样，
出 JSON 基线供 CUDA vs Mac Studio 档决策。本机 Mac 上 --dry-run 只校验参数。"""

from __future__ import annotations

import argparse
import base64
import json
import time
import urllib.request
from datetime import date
from pathlib import Path


def _pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))] if s else 0.0


def _post_json(url: str, payload: dict, timeout: float = 60.0) -> tuple[dict, float]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return data, (time.perf_counter() - t0) * 1000


def probe_llm_ttft(base_url: str, model_path: str, rounds: int) -> list[float]:
    """流式首 token 延迟：TTFT 从请求发出计到首个 chunk 到达。"""
    ttfts: list[float] = []
    req_body = json.dumps({
        "model": model_path, "stream": True,
        "messages": [{"role": "user", "content": "用一句粤语回答：而家幾點？"}],
    }).encode()
    for _ in range(rounds):
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/v1/chat/completions", data=req_body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.readline()  # 首个 SSE chunk 到达即 TTFT
        ttfts.append((time.perf_counter() - t0) * 1000)
    return ttfts


def probe_asr(base_url: str, wav: Path, language: str, rounds: int) -> list[float]:
    audio_b64 = base64.b64encode(wav.read_bytes()).decode()
    results: list[float] = []
    for _ in range(rounds):
        _, ms = _post_json(
            f"{base_url.rstrip('/')}/transcribe",
            {"audio": audio_b64, "language": language},
            timeout=120.0,
        )
        results.append(ms)
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", default="http://127.0.0.1:1235")
    ap.add_argument("--asr", default="http://127.0.0.1:8787")
    ap.add_argument("--model-path", required=True, help="本地模型绝对路径（勿用 repo id，防 HF hub 解析）")
    ap.add_argument("--wav", required=True, type=Path)
    ap.add_argument("--language", default="cantonese")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的探针计划")
    ap.add_argument("--out", default=f"reports/cuda_baseline_{date.today().isoformat()}.json")
    args = ap.parse_args()

    if args.dry_run:
        print(f"[plan] LLM TTFT x{args.rounds} -> {args.llm}/v1/chat/completions (model={args.model_path})")
        print(f"[plan] ASR x{args.rounds} -> {args.asr}/transcribe ({args.wav}, {args.language})")
        print(f"[plan] 输出 -> {args.out}")
        return 0

    assert args.wav.exists(), f"wav 不存在: {args.wav}"
    ttfts = probe_llm_ttft(args.llm, args.model_path, args.rounds)
    asrs = probe_asr(args.asr, args.wav, args.language, args.rounds)
    report = {
        "date": date.today().isoformat(),
        "llm": {"ttft_ms": {"p50": _pct(ttfts, 0.5), "p90": _pct(ttfts, 0.9), "max": max(ttfts), "n": len(ttfts)}},
        "asr": {"ms": {"p50": _pct(asrs, 0.5), "p90": _pct(asrs, 0.9), "max": max(asrs), "n": len(asrs)}},
        "raw": {"ttft_ms": ttfts, "asr_ms": asrs},
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report["llm"] | report["asr"], indent=2))
    print(f"基线已写 {out} —— 回填 spec §9 对比 Mac 基线（PERCEIVED_MS 分解: eou+llm+tts）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: dry-run 验收（Mac 上唯一可跑档）**

Run: `.venv312/bin/python scripts/probe_cuda_baseline.py --model-path /tmp/x --wav tests/fixtures/any.wav --dry-run`
Expected: 打出三行 `[plan]`；不访问网络。（真实 CUDA 采样在有 CUDA 环境的机器上执行，结果回填 spec §9——列为 P0 验收遗留项而非本任务阻塞。）

- [ ] **Step 3: 提交**

```bash
python -m compileall -q scripts
git add scripts/probe_cuda_baseline.py
git commit -m "feat(node): CUDA 原型基线探针——TTFT/ASR 采样出 JSON 基线，Mac Studio 备选档决策门(spec §9)"
```

---

### Task 8: turns 并发回归 + 全量门禁冒烟

**Files:** 无新文件（纯验证任务）

**Interfaces:**
- Consumes: Task 2 改过的 turns 路径（AGENTS.md 规约：改 turns/audit/并发必跑）

- [ ] **Step 1: 全量 pytest**

Run: `./scripts/test.sh`
Expected: 全绿（含术语门禁、方言兼容门禁、账本、节点注册）

- [ ] **Step 2: turns 并发回归**

Run: `.venv312/bin/python scripts/load_cp_concurrency.py`（含 turns 并发 30/30）
Expected: 0 丢轮（Task 2 扩列后 uuid 主键路径未动，防回归）

- [ ] **Step 3: 开发栈冒烟**

Run: `python tools/bok.py down; python tools/bok.py serve` → `python tools/bok.py status`、`python tools/bok.py doctor`
Expected: 七项端口 UP；doctor 正常——确认 SQLite dev 栈零回归（Global Constraints 兜底验证）。

- [ ] **Step 4: 无提交（纯验证；若红回到对应任务修）**

---

### Task 9: 拓扑文档与 AGENTS.md 条款

**Files:**
- Modify: `docs/RUNTIME_TOPOLOGY.md`（新增「分发型拓扑（P0）」段）
- Modify: `AGENTS.md`（架构边界段补一条 P0 落地简记）

**Interfaces:** 无代码接口；按仓库规约「ports/paths/data flow 变更必更 RUNTIME_TOPOLOGY」。

- [ ] **Step 1: RUNTIME_TOPOLOGY.md 增段**

在文件「## 1. 组件与端口」之前插入：

```markdown
## 0. 分发型拓扑（P0 起双形态，spec=2026-09-10-thin-node-saas-design.md）

单机形态（本文其余部分描述的 dev/打包形态）不变。分发货形态新增：
- **云 CP**：同一 control-plane 代码，`DATABASE_URL` 指 Supabase Postgres；托管管理台静态 UI
- **节点包**：LiveKit + ASR/LLM sidecar + agent/interp worker + node-agent（tools/node_agent.py，
  心跳 :8000/api/nodes/heartbeat，commands 通道 P3）+ 节点本地托管坐席 UI（runtime-config.js 注入
  cpUrl/livekitUrl，spec §8 纯内网档）
- 新数据列：turns.org_id/line/speaker/gen/template_step/started_ms/ended_ms/perceived_ms（分析账本，spec §6.1）
- 新表：orgs/nodes（org 缝 + 节点注册表，node_token 只存 sha256）
```

- [ ] **Step 2: AGENTS.md 补条款**

在「Architecture Boundaries & Runtime Rules」段「DB 迁移」条目之后追加一条：

```markdown
- **分发型拓扑（P0 起）**：业务库必须保持 SQL 方言可移植（`tests/test_db_portability.py` 门禁，
  新列/新表禁用 sqlite 专有语法）；turns 新轮上报带分析账本字段（speaker/gen/template_step/
  perceived_ms，缺省值兜底旧调用）；节点身份=NodeStore 注册签发 node_token（只存 sha256），
  node-agent 心跳失联 ≥3 次置 REFUSE_JOBS；web CP 地址运行时注入（`window.__BOK_CONFIG__.cpUrl`
  优先于构建期 env，`apps/web/lib/api.ts` `apiBase()` 单点），禁止再把 CP 地址烤成构建期常量。
```

- [ ] **Step 3: 提交**

```bash
git add docs/RUNTIME_TOPOLOGY.md AGENTS.md
git commit -m "docs: 分发型拓扑P0落地简记——方言门禁/分析账本/NodeStore/运行时注入运行规约"
```

---

## P0 验收清单（对照 spec §10 P0 行）

- [ ] 仓库拆分就绪：CP 可用 `DATABASE_URL` 指 Postgres（方言门禁绿）+ 节点可 `node_agent.py --heartbeat-only` 独立心跳
- [ ] turns 新 schema 落地 + 旧调用零破坏 + turns 并发 30/30 回归
- [ ] 节点注册/心跳端点 + token 哈希存储
- [ ] web 产物可被节点托管（runtime-config.js 注入生效）
- [ ] 部署脚本草版双平台 dry-run 通过
- [ ] **CUDA 基线探针在 CUDA 机器实跑，JSON 回填 spec §9**（本计划唯一异地遗留项；未回填前不得向客户承诺 CUDA 档数字）
- [ ] **Supabase 项目开通 + 生产 `DATABASE_URL` 切换 + 线上冒烟**（外部运维项：需 Supabase 账号建项目；代码侧方言门禁已保证可移植，切换后跑一遍 Task 8 Step 3 同款冒烟）

## 明确不在 P0（后续各出计划）

P1 身份/两级管理员/错误上报；P2 发行管线（Nuitka+签名+离线包）；P3 L1-L3/teardown-org/灰度升级；P4 多租户硬化。B 线随节点包自然携带（interp worker + MT server），无独立任务。
