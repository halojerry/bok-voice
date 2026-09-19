# QA 匹配优先级（Phase 3.1）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `qa_entries.priority`（小者先，默认 10）让运营显式指定「这条必须赢」；全默认时命中结果与今逐字节相同，`BOK_QA_PRIORITY=0` 一键回纯分数档。

**Architecture:** 一列数据（DB/CP 透传）+ 一处排序键（`QaIndex.match` 胜者选择）+ 表单一个数字框 + 探针对照腿。不动阈值、不动打分公式、不动 by_id/graph play 路径。设计契约见 [spec §1](../specs/2026-09-18-flow-graph-phase3.md)。

**Tech Stack:** Python 3.12 (SQLAlchemy/FastAPI) / TS+React / pytest + node:test。

## Global Constraints

- **零变化铁律**：全默认优先级（全部=10）时，胜者与今逐字节同——含平分吃 created_at（最老先）。单测钉死。
- kill-switch：`os.environ.get("BOK_QA_PRIORITY", "1") == "1"`（全仓同款读法）。
- DB 迁移只落 `deps.build_engine()` 幂等段 `_ensure_column`；方言可移植门禁 `tests/test_db_portability.py`。
- priority 域 [0,1000]，越界钳制不报错（CP 端钳）；与 graph `DEFAULT_PRIORITY=10` 同约定（小者先）。
- 术语门禁：唯一合法拼写 `cantonese`；门禁扫全部跟踪文件。
- Conventional commits scope `qa-priority`；一逻辑变更一提交。
- 每任务收尾：该任务测试绿 + `python -m compileall -q apps packages`（web 任务 `npx tsc --noEmit`）。
- 执行环境：工作树 `/Users/halo/Documents/bok/voice-assistant-worktrees/flow-graph-phase3`，分支 `flow-graph-phase3`，venv 已在主树 `.venv312` 惯例下由 bootstrap 建。

---

### Task 1: DB 层 — `priority` 列 + 迁移 + 透传

**Files:**
- Modify: `packages/business-db/bok_voice_business_db/models.py`（`QaEntry`，`cluster_head_id` 列注释之后）
- Modify: `apps/control-plane/control_plane/deps.py`（幂等段，`cluster_head_id`/`graph_json` 两行之后追加）
- Modify: `packages/business-db/bok_voice_business_db/repository.py`（SQL `create_qa_entry` 构造 / `update_qa_entry` allowed 集；InMemory 同名两法——`priority` 是 kwargs 可传字段，照 `lang` 姿势）
- Modify: `scripts/.p0_supabase_schema.sql`（重跑 `scripts/dump_postgres_ddl.py`）+ `scripts/smoke_postgres.py` `_MIGRATION_COLUMNS`
- Test: `tests/test_qa_priority_field.py`

**Interfaces:**
- Produces: `QaEntry.priority: int`（默认 10）经 `_qa_to_dict` 进全部 QA API 响应；create/update（SQL+InMemory）接受 `priority` 键。

- [ ] **Step 1: 失败测试**（fixture 照 `tests/test_qa_cluster_field.py` 的 `_sql_repo` 姿势，**记得 env save/restore**——Task 2 家族旧教训）

```python
"""priority 数据层:迁移补列 / create+update 透传 / 默认 10（spec 2026-09-18 §1）。"""
from __future__ import annotations

import os

os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-priority")


def test_migration_adds_priority(tmp_path):
    saved = os.environ.get("DATABASE_URL")
    try:
        os.environ["DATABASE_URL"] = f"sqlite:///{tmp_path}/prio.db"
        from sqlalchemy.orm import sessionmaker

        from control_plane import deps
        from bok_voice_business_db.repository import SqlAlchemyBusinessRepository

        engine = deps.build_engine()
        assert deps.build_engine() is not None  # 幂等二跑
        repo = SqlAlchemyBusinessRepository(sessionmaker(bind=engine, expire_on_commit=False)())
        import sqlalchemy as sa

        with engine.connect() as conn:
            cols = {c["name"] for c in sa.inspect(conn).get_columns("qa_entries")}
        assert "priority" in cols
    finally:
        if saved is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = saved


def test_create_update_roundtrip_and_default():
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    e = repo.create_qa_entry({"account_id": "acc-001", "question_text": "q", "answer_text": "a"})
    assert e["priority"] == 10  # 默认
    repo.update_qa_entry(e["id"], {"priority": 1})
    assert repo.get_qa_entry(e["id"])["priority"] == 1
```

- [ ] **Step 2: 红** — Run: `.venv312/bin/python -m pytest tests/test_qa_priority_field.py -v` → FAIL（列缺/键缺）
- [ ] **Step 3: 实现** — models 加列（注释：`# 匹配优先级(Phase 3.1):阈值过关者中小者先;默认 10=与 graph DEFAULT_PRIORITY 同约定,全默认零变化。`）；deps 加 `_ensure_column(conn, "qa_entries", "priority", "priority INTEGER NOT NULL DEFAULT 10")`；repository 四处透传；重跑 DDL 脚本 + `_MIGRATION_COLUMNS` 加行
- [ ] **Step 4: 绿 + 门禁** — pytest（新文件 + `test_db_portability.py` + `test_template_graph_field.py`）+ compileall
- [ ] **Step 5: Commit** — `feat(qa-priority): priority column — idempotent migration + repo passthrough`

---

### Task 2: CP API — schema + 钳制

**Files:**
- Modify: `apps/control-plane/control_plane/schemas.py`（`QaEntryCreate.priority: int = 10` / `QaEntryPatch.priority: Optional[int] = None`）
- Modify: `apps/control-plane/control_plane/main.py`（create/patch 两端点：落库前 `priority = max(0, min(int(priority or 10), 1000))`，钳制不报错）
- Test: `tests/test_qa_priority_api.py`

**Interfaces:**
- Consumes: Task 1 透传。
- Produces: `POST/PATCH /api/qa-entries` 接受 `priority`；越界钳到 [0,1000]；缺省 10。fixture 照 `tests/test_qa_canned_status.py` `_client_and_repo`。

- [ ] **Step 1: 失败测试**

```python
"""priority API:透传 / 钳制 / 缺省 10。"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-prio-api")


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    return TestClient(app).__enter__(), repo


def test_create_clamps_and_defaults(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    ok = client.post("/api/qa-entries", json={"question_text": "q", "answer_text": "a"})
    assert ok.status_code == 200 and ok.json()["priority"] == 10
    hi = client.post("/api/qa-entries", json={"question_text": "q2", "answer_text": "a", "priority": 9999})
    assert hi.json()["priority"] == 1000
    lo = client.post("/api/qa-entries", json={"question_text": "q3", "answer_text": "a", "priority": -5})
    assert lo.json()["priority"] == 0
    pid = ok.json()["id"]
    patch = client.patch(f"/api/qa-entries/{pid}", json={"priority": 1})
    assert patch.json()["priority"] == 1
```

- [ ] **Step 2: 红** — FAIL（pydantic 忽略未知键）
- [ ] **Step 3: 实现** — schemas 两字段；两端点钳制（一处 3 行 helper `_clamp_priority(v) -> int`）
- [ ] **Step 4: 绿** — pytest（新文件+Task 1 文件）+ compileall
- [ ] **Step 5: Commit** — `feat(qa-priority): qa-entries API accepts clamped priority`

---

### Task 3: 运行时 — `QaIndex.match` 胜者键（核心）

**Files:**
- Modify: `apps/agent/agent_runtime/qa_gate.py`（`match()` 胜者选择 + 模块级 `qa_priority_enabled()`）
- Test: `tests/test_qa_gate.py`（追加；现有 9 个用例零改动全绿=零变化基线的一部分）

**Interfaces:**
- Consumes: entry dict 的 `priority` 键（缺省按 10——旧 CP 响应/旧库未迁移时的宽容面）。
- Produces: 阈值过关者按 `(priority asc, score desc, 插入序)` 取胜者；`BOK_QA_PRIORITY=0` 回纯分数档（现状键 = `score desc, 插入序`）。

- [ ] **Step 1: 失败测试**（追加到 `tests/test_qa_gate.py`）

```python
def _e(qid: str, q: str, a: str = "ans", **kw):
    return {"id": qid, "question_text": q, "answer_text": a, "lang": "zh", "scope": "global", **kw}


def test_match_priority_decides_among_passers(monkeypatch):
    monkeypatch.delenv("BOK_QA_PRIORITY", raising=False)
    from agent_runtime import qa_gate

    entries = [
        _e("old", "怎么退款", priority=10),          # 老：默认优先级、分数高
        _e("pinned", "退款要怎么弄啊", priority=1),  # 新：低优先级数字、分数略低
    ]
    idx = qa_gate.QaIndex(entries)
    hit, score = idx.match("怎么退款")
    # 全默认（都 10）→ 纯分数档，old 胜（现状不变）
    entries_default = [_e("old", "怎么退款"), _e("pinned", "退款要怎么弄啊")]
    assert qa_gate.QaIndex(entries_default).match("怎么退款")[0]["id"] == "old"
    # pinned=priority 1 → 小者先压过分数差
    assert hit is not None and hit["id"] == "pinned"


def test_match_default_all_identical_to_legacy(monkeypatch):
    monkeypatch.delenv("BOK_QA_PRIORITY", raising=False)
    from agent_runtime import qa_gate

    # 同分平局 → 插入序（created_at 语义）老先——与现状键一致
    entries = [_e("a", "查询物流"), _e("b", "查一下物流")]
    assert qa_gate.QaIndex(entries).match("查询物流")[0]["id"] == "a"


def test_match_priority_killswitch_restores_legacy(monkeypatch):
    monkeypatch.setenv("BOK_QA_PRIORITY", "0")
    from agent_runtime import qa_gate

    entries = [_e("old", "怎么退款", priority=10), _e("pinned", "退款要怎么弄啊", priority=1)]
    assert qa_gate.QaIndex(entries).match("怎么退款")[0]["id"] == "old"


def test_match_priority_missing_key_treated_as_default(monkeypatch):
    monkeypatch.delenv("BOK_QA_PRIORITY", raising=False)
    from agent_runtime import qa_gate

    entries = [{"id": "nokey", "question_text": "怎么退款", "answer_text": "a", "lang": "zh", "scope": "global"},
               _e("pinned", "退款要怎么弄啊", priority=1)]
    assert qa_gate.QaIndex(entries).match("怎么退款")[0]["id"] == "pinned"
```

- [ ] **Step 2: 红** — FAIL（`priority` 键被忽略，pinned 不胜）
- [ ] **Step 3: 实现** — qa_gate.py：

```python
def qa_priority_enabled() -> bool:
    return os.environ.get("BOK_QA_PRIORITY", "1") == "1"
```

`match()` 胜者循环替换为（打分公式**一字不动**，只换胜者键）：

```python
        best: dict | None = None
        best_key: tuple | None = None
        best_score = 0.0
        use_priority = qa_priority_enabled()
        for entry, e_q, e_vec in self._items:
            if lang and str(entry.get("lang") or "") and str(entry["lang"]) != lang:
                continue
            if str(entry.get("scope") or "global") == "step":
                if step_index is None or int(entry.get("step_index") or -1) != int(step_index):
                    continue
            score = 0.6 * _cos(qv, e_vec)
            if q_low in e_q.lower():
                score += 0.4 * (len(q_low) / max(1, len(e_q)))
            key = (_entry_priority(entry), -score) if use_priority else (-score,)
            if best_key is None or key < best_key:
                best, best_key, best_score = entry, key, score
        if best is not None and best_score >= thr:
            return best, best_score
        return None, best_score
```

加模块级 helper（旧数据宽容）：

```python
def _entry_priority(entry: dict) -> int:
    try:
        return max(0, min(int(entry.get("priority", 10) or 10), 1000))
    except (TypeError, ValueError):
        return 10
```

键语义核对：`(prio asc, -score asc)` 严格小于比较——同键平局保留先见者（=插入序=created_at），与现状平局语义一致；kill 档 `(-score,)` 同样先见者保留。`best_score` 仍为胜者分数供阈值判定（注意：非胜者的更高分不再影响 best_score——现状语义本就如此，胜者分数过阈值）。

- [ ] **Step 4: 绿 + 零变化基线** — Run: `.venv312/bin/python -m pytest tests/test_qa_gate.py tests/test_qa_priority_field.py tests/test_qa_priority_api.py tests/test_qa_gate_byid.py -v`（现有 9+1 用例零改动全绿）+ compileall
- [ ] **Step 5: Commit** — `feat(qa-priority): match winner key (priority asc, score desc) + BOK_QA_PRIORITY kill-switch`

---

### Task 4: web — 编辑表单 + 列表徽标

**Files:**
- Modify: `apps/web/app/(app)/qa/page.tsx`（条目编辑模态 form 增 `priority` number 输入（默认 10、min 0 max 1000）；create/patch body 带键；列表行与画布 qaEntry 节点在 `priority != 10` 时显示小徽标 `P{n}`——数据已随 list 响应携带，零新拉取）
- Test: 手工冒烟 + `npx tsc --noEmit && npm test && npm run build`（45 基线）

**Interfaces:**
- Consumes: Task 1/2 的 API 字段。
- Produces: 运营可设优先级；徽标只在非默认时出现（默认不添噪音）。

- [ ] **Step 1: 实现** — form state 加 `priority: string`（受控数字输入，提交时 `parseInt` 钳 [0,1000]、NaN→10）；PATCH body 加 `priority`；列表/画布徽标 `<span>` 条件渲染
- [ ] **Step 2: 验证** — tsc + npm test + build 全净；dev 冒烟：改一条 priority=1 → 刷新仍在
- [ ] **Step 3: Commit** — `feat(qa-priority): web editor field + non-default badge`

---

### Task 5: 探针对照腿 + 文档

**Files:**
- Modify: `scripts/probe_qa_hit.py`（增 `--priority-duel` 离线腿）
- Modify: `AGENTS.md`（QA 快路「罐头音三件套」条目族加一句优先级语义 + kill-switch）
- Test: `tests/test_probe_qa_priority.py`（纯函数腿）

**Interfaces:**
- Produces: `--priority-duel`：合成两组条目（同问法、分数交错、priority 相反）→ 报 legacy vs priority 两档胜者对照表 + kill 腿断言回退；exit code 主判据=三断言全过。

- [ ] **Step 1: 失败测试** — 纯函数 `duel_verdicts(rows, query) -> {"legacy": id, "priority": id, "kill": id}`（组装 QaIndex ×2（env 开/关）跑 match）断言 pinned/old/old
- [ ] **Step 2: 红 → Step 3: 实现** — probe 加 `duel()`（不建栈、不拉 CP，纯本地条目）+ CLI 参数 + 报告打印；AGENTS.md 加半句（「priority 小者先（默认 10 全默认零变化），`BOK_QA_PRIORITY=0` 回纯分数档——probe_qa_hit --priority-duel 为验收」）
- [ ] **Step 4: 绿 + 全量** — 新测试 + `python -m compileall -q scripts`
- [ ] **Step 5: Commit** — `feat(qa-priority): probe duel leg + AGENTS note`

---

### Task 6: 全量验收 + PR

- [ ] `.venv312/bin/python -m pytest tests/ -q`（1461 基线 + 新增全绿）
- [ ] `python -m compileall -q apps packages services tools scripts`；web tsc+test+build；B 线 npm test
- [ ] `.venv312/bin/python scripts/probe_qa_hit.py --priority-duel`（真跑）
- [ ] Supabase MCP apply：`ALTER TABLE public.qa_entries ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 10;`（migration 名 `p31_qa_priority`）+ information_schema 回验（controller 步）
- [ ] push → PR（base main）→ CI 绿 → 按 PR 合并

---

## Self-Review

- Spec §1 覆盖：列/钳制→T1/T2；排序键+kill→T3；表单/徽标→T4；探针→T5；验收含 Supabase→T6。零变化铁律有专测（`test_match_default_all_identical_to_legacy` + 现有 9 用例零改动）。无占位符。类型一致：`_entry_priority`/`qa_priority_enabled`/`duel_verdicts` 各任务间签名一致。
- **执行勘误（2026-09-18 inline 执行实录）**：Task 3 Step 3 的胜者循环初版是「选完再过阈」——实测打脸（低分高优先级条目抢走胜者帽后双双落 None）。正确形状=**阈值先行**（`score < thr` 直接 continue，优先级只在过关者中排）+ `top_score` 分离保留未过关时的最高分诊断返回（旧档 `(None, best)` 语义）。夹具同步改诚实形状：同问法平分用优先级破局、垃圾问法（不过阈）永不出线、kill 档回插入序。
