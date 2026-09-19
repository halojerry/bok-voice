# QA 画布 + 罐头状态面 + 惜客通导入器 实现计划（Phase 1）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** /qa 页新增「列表|画布」双视图（React Flow 画布：话术步骤脊柱 + QA 卫星簇 + 簇边/步骤边可编辑）、罐头物化状态面（试听缓存回放 + 单条/批量补料）、惜客通 `tbl_ai_knowledge.json` 导入器。

**Architecture:** 唯一 schema 变更=`qa_entries.cluster_head_id`（deps 幂等补列+删除级联）。画布边全部由现有字段派生、PATCH 直写，qa_gate/agent 零改动。罐头状态由 `pregen_tts.py --qa-status`（与物化同一条代码路径算 key，零重复）输出 JSON，CP 子进程调用+TTL 缓存；试听=读 `{key}.pcm` 包 WAV 回放（零云费）。导入器走 HTTP（镜像 mine_qa.py），dry-run 默认。

**Tech Stack:** React 19 / Next 16 静态导出、`@xyflow/react` v12（懒加载）、FastAPI + SQLAlchemy（sqlite/postgres 双方言）、Node `node --test`（TS 单文件转译模式）、pytest。

**Spec:** `docs/superpowers/specs/2026-09-17-qa-canvas-design.md`（rev2）

## Global Constraints

- 运行时零改动：`qa_gate`、agent 装配、TTS 物化行为不变；`cluster_head_id` 不进缓存键/匹配面。
- 粤语规范值唯一拼写 `cantonese`（`tests/test_cantonese_terminology.py` 全仓门禁，新代码禁出现旧拼写）。
- DB 迁移只在 `control_plane/deps.py build_engine()` 幂等段（`_ensure_column`，inspector 探测）；新列禁 sqlite 专有语法（`tests/test_db_portability.py`）。
- 改表后必须重跑 `python scripts/dump_postgres_ddl.py` 更新 `scripts/.p0_supabase_schema.sql`。
- Python PEP8 + `from __future__ import annotations` + 类型注解；改完跑 `python -m compileall -q apps packages services tools scripts`。
- web 改动门禁：`cd apps/web && npx tsc --noEmit && npm run build && npm test`。
- CP key/密钥分离与鉴权闸沿用现状：数据面 `_gate_page(request,"qa")`，烧云配额操作（现场合成预览、pregen 触发）`require_role(request,"admin","root")`。
- 惜客通外部字面量（`Answer2`/`AfterAnswerSceneId` 等驼峰键）是外部系统真字面量，照抄不改名。
- 提交：conventional commits，一任务一提交。

---

### Task 1: `cluster_head_id` 数据层（models + 迁移 + 仓库双后端 + schemas）

**Files:**
- Modify: `packages/business-db/bok_voice_business_db/models.py`（QaEntry，~line 356 `source` 列后）
- Modify: `apps/control-plane/control_plane/deps.py`（build_engine 幂等补列段，~line 90 起）
- Modify: `packages/business-db/bok_voice_business_db/repository.py`（SqlAlchemyBusinessRepository `_qa_to_dict`/`create_qa_entry`/`update_qa_entry`/`delete_qa_entry`；InMemoryBusinessRepository 同名四处）
- Modify: `apps/control-plane/control_plane/schemas.py`（`QaEntryCreate`/`QaEntryPatch`，~line 336-362）
- Test: `tests/test_qa_cluster_field.py`

**Interfaces:**
- Produces: `QaEntry.cluster_head_id: str`（默认 `""`）；`list_qa_entries`/`create_qa_entry`/`update_qa_entry` 返回 dict 含 `cluster_head_id` 键；`delete_qa_entry(id)` 级联清引用。后续 Task 6/8 依赖 `api.patchQa(id, {cluster_head_id})` 与行字段 `cluster_head_id`。

- [ ] **Step 1: 写失败测试**

```python
"""cluster_head_id 数据层：迁移补列 / patch 透传 / 删除级联清引用（spec §4.1）。"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-owner")


def _sql_repo(tmpdir: str):
    """tmp sqlite + build_engine 迁移后建 SQL 仓库（幂等二跑=幂等性回归）。"""
    os.environ["DATABASE_URL"] = f"sqlite:///{tmpdir}/qa.db"
    from control_plane import deps
    from bok_voice_business_db.repository import SqlAlchemyBusinessRepository

    engine = deps.build_engine()
    assert engine is not None
    assert deps.build_engine() is not None  # 二跑不炸=幂等
    return SqlAlchemyBusinessRepository(engine)


def test_migration_adds_cluster_head_id(tmp_path):
    repo = _sql_repo(str(tmp_path))
    import sqlalchemy as sa

    with repo.engine.connect() as conn:  # type: ignore[attr-defined]
        cols = {c["name"] for c in sa.inspect(conn).get_columns("qa_entries")}
    assert "cluster_head_id" in cols


def test_create_patch_dict_roundtrip(tmp_path):
    repo = _sql_repo(str(tmp_path))
    head = repo.create_qa_entry({"question_text": "q1", "answer_text": "a", "account_id": "acc-001"})
    row = repo.create_qa_entry(
        {"question_text": "q2", "answer_text": "a", "account_id": "acc-001",
         "cluster_head_id": head["id"]}
    )
    assert row["cluster_head_id"] == head["id"]
    assert repo.list_qa_entries("acc-001")[0]["cluster_head_id"] == ""  # dict 序列化含键
    patched = repo.update_qa_entry(row["id"], {"cluster_head_id": ""})  # 断簇=写空串
    assert patched is not None and patched["cluster_head_id"] == ""


def test_delete_head_cascades_children(tmp_path):
    repo = _sql_repo(str(tmp_path))
    head = repo.create_qa_entry({"question_text": "h", "answer_text": "a", "account_id": "acc-001"})
    child = repo.create_qa_entry(
        {"question_text": "c", "answer_text": "a", "account_id": "acc-001",
         "cluster_head_id": head["id"]}
    )
    assert repo.delete_qa_entry(head["id"]) is True
    assert repo.get_qa_entry(child["id"])["cluster_head_id"] == ""


def test_in_memory_repo_parity(monkeypatch):
    os.environ["DATABASE_URL"] = ""  # 强制内存仓库
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    head = repo.create_qa_entry({"question_text": "h", "answer_text": "a", "account_id": "acc-001"})
    child = repo.create_qa_entry(
        {"question_text": "c", "answer_text": "a", "account_id": "acc-001",
         "cluster_head_id": head["id"]}
    )
    assert child["cluster_head_id"] == head["id"]
    assert repo.delete_qa_entry(head["id"]) is True
    assert repo.get_qa_entry(child["id"])["cluster_head_id"] == ""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_qa_cluster_field.py -v`
Expected: FAIL（`cluster_head_id` 键缺失 / 列不存在）

- [ ] **Step 3: 最小实现**

`models.py` QaEntry（`source` 列后加一行）:

```python
    source: Mapped[str] = mapped_column(String(16), default="curated")
    # 同义簇(spec 2026-09-17 Phase1):非空=本条是指向条目的变体(一层星形)。
    # 纯展示/组织字段——qa_gate 匹配/罐头 key 均不读它。
    cluster_head_id: Mapped[str] = mapped_column(String(64), default="")
```

`deps.py` build_engine 幂等段（现有 `_ensure_column` 调用序列尾部追加）:

```python
                _ensure_column(
                    conn,
                    "qa_entries",
                    "cluster_head_id",
                    "cluster_head_id VARCHAR(64) DEFAULT ''",
                )
```

`repository.py` SqlAlchemyBusinessRepository（四处）:

```python
    def _qa_to_dict(self, row) -> dict:
        return {
            # ...现有键不动...
            "source": row.source,
            "cluster_head_id": getattr(row, "cluster_head_id", "") or "",
            "template_id": row.template_id,
            "created_at": row.created_at.isoformat() if row.created_at else "",
        }
```

`create_qa_entry` 构造参数追加 `cluster_head_id=data.get("cluster_head_id") or "",`；
`update_qa_entry` 追加：

```python
        if "cluster_head_id" in patch and patch["cluster_head_id"] is not None:
            row.cluster_head_id = str(patch["cluster_head_id"])
```

`delete_qa_entry`（级联清引用，spec §8）:

```python
    def delete_qa_entry(self, entry_id: str) -> bool:
        row = self.session.get(models.QaEntry, entry_id)
        if row is None:
            return False
        self.session.delete(row)
        # 级联:head 删除后变体的簇指针清空(防孤儿引用,spec §4.1)。
        self.session.execute(
            sa_update(models.QaEntry)
            .where(models.QaEntry.cluster_head_id == entry_id)
            .values(cluster_head_id="")
        )
        self.session.commit()
        return True
```

（文件顶部按现有 import 风格补 `from sqlalchemy import update as sa_update`，若已有 update 别名则复用。）

`InMemoryBusinessRepository` 同步四处：`create_qa_entry` 行 dict 加 `"cluster_head_id": data.get("cluster_head_id") or "",`；`update_qa_entry` 加同款分支；`delete_qa_entry` 删除前遍历 `self.qa_entries.values()` 把 `cluster_head_id == entry_id` 的置空；`_qa` 序列化处（list/get 出口）补键。

`schemas.py`：`QaEntryCreate` 加 `cluster_head_id: str = ""`；`QaEntryPatch` 加 `cluster_head_id: Optional[str] = None`。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_qa_cluster_field.py tests/test_owner_scope.py tests/test_db_portability.py -v`
Expected: 全 PASS（owner_scope 回归=未破坏 B3 语义）

- [ ] **Step 5: 重跑 Supabase DDL 引导件**

Run: `.venv312/bin/python scripts/dump_postgres_ddl.py && git diff --stat scripts/.p0_supabase_schema.sql`
Expected: 产物含 `cluster_head_id` 列且带回环校验输出 OK。（应用侧 Supabase migration 由操作者经 MCP 执行，非本任务阻塞项。）

- [ ] **Step 6: compileall + 提交**

```bash
python -m compileall -q apps packages services tools scripts
git add -A && git commit -m "feat(qa): cluster_head_id column — variant cluster edges data layer"
```

---

### Task 2: `pregen_tts.py --qa-status` / `--entry-id`（罐头状态/补料限定的物化侧）

**Files:**
- Modify: `scripts/pregen_tts.py`（argparse ~line 427-436；`_qa_jobs` 后新增 `_qa_status`；`main_async` 接线）
- Test: `tests/test_pregen_qa_status.py`

**Interfaces:**
- Produces: `pregen_tts.py --qa-status --cp <url>` 输出 stdout JSON `{"qa_status": {entry_id: {"state": "ok"|"missing", "voice": str, "key": str}}}`；`--entry-id <id>`（可重复）只处理指定条目（`--qa` 物化与 `--qa-status` 共用过滤）。Task 3 的 CP 端点消费该 JSON。

- [ ] **Step 1: 写失败测试**

```python
"""--qa-status:与物化同口径算 key,ok/missing 两态;--entry-id 过滤。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in ("apps/agent", "packages/core", "scripts"):
    sp = str(ROOT / p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

from agent_runtime.tts_cache import TtsAudioCache  # noqa: E402
import pregen_tts  # noqa: E402


def _rows():
    return [
        {"id": "qa:ok", "answer_text": "您好，包裹已到驿站。", "lang": "zh", "enabled": True},
        {"id": "qa:miss", "answer_text": "不好意思，请稍等。", "lang": "zh", "enabled": True},
        {"id": "qa:off", "answer_text": "停用条目不计", "lang": "zh", "enabled": False},
        {"id": "qa:empty", "answer_text": "", "lang": "zh", "enabled": True},
    ]


def test_qa_status_ok_and_missing(tmp_path):
    cache = TtsAudioCache(tmp_path)
    rows = _rows()
    # 先用同函数算计划,再把 ok 条目物化进缓存,状态应翻成 ok。
    plan = pregen_tts._qa_status(
        rows, persona_pool=[], lang_personas={"zh": None},
        all_personas=False, tts_cfg={}, voice_mode="single",
        model="speech-2.8-hd", sample_rate=24000, cache=cache,
    )
    assert set(plan) == {"qa:ok", "qa:miss"}  # 停用/空答案不进计划
    miss = plan["qa:miss"]
    assert miss["state"] == "missing" and miss["voice"] and miss["key"]
    cache.store(miss["key"], b"\x00\x00" * 100, text="不好意思，请稍等。",
                voice=miss["voice"], model="speech-2.8-hd", pin=True)
    plan2 = pregen_tts._qa_status(
        rows, persona_pool=[], lang_personas={"zh": None},
        all_personas=False, tts_cfg={}, voice_mode="single",
        model="speech-2.8-hd", sample_rate=24000, cache=cache,
    )
    assert plan2["qa:miss"]["state"] == "ok"
    assert plan2["qa:ok"]["state"] == "missing"  # 未物化的仍 missing


def test_entry_id_filter(tmp_path):
    cache = TtsAudioCache(tmp_path)
    plan = pregen_tts._qa_status(
        _rows(), persona_pool=[], lang_personas={"zh": None},
        all_personas=False, tts_cfg={}, voice_mode="single",
        model="speech-2.8-hd", sample_rate=24000, cache=cache,
        entry_ids={"qa:miss"},
    )
    assert set(plan) == {"qa:miss"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_pregen_qa_status.py -v`
Expected: FAIL（`_qa_status` 不存在）

- [ ] **Step 3: 最小实现**

`scripts/pregen_tts.py`，`_qa_jobs` 之后新增（复用 `_assemble_minimax_voice_map`/`_resolve_voice_map`/`minimax_speed_for`/`TtsAudioCache.key_for`——与 `_materialize` 逐行同口径，spec「零重复」锚点）:

```python
def _qa_status(
    qa_rows: list[dict],
    persona_pool: list[dict],
    lang_personas: dict[str, dict | None],
    *,
    all_personas: bool,
    tts_cfg: dict,
    voice_mode: str,
    model: str,
    sample_rate: int,
    cache: TtsAudioCache,
    entry_ids: set[str] | None = None,
) -> dict[str, dict]:
    """--qa-status 可测核心:与 _materialize 同口径逐条目算 key、查缓存。

    条目状态:任一计划音色版本在缓存=ok,否则 missing。停用/空答案不进结果。
    """
    out: dict[str, dict] = {}
    map_cache: dict[tuple[str, str], dict] = {}
    for e in qa_rows:
        eid = str(e.get("id") or "")
        if not eid or (entry_ids and eid not in entry_ids):
            continue
        if not bool(e.get("enabled", True)):
            continue
        text = str(e.get("answer_text") or "").strip()
        if not text:
            continue
        lang = _normalize_lang((e or {}).get("lang"), default="zh") or "zh"
        voices: list[str] = []
        if all_personas:
            for persona in persona_pool:
                v = _persona_resolved_voice(persona, lang, tts_cfg, voice_mode)
                if v:
                    voices.append(v)
        else:
            persona = lang_personas.get(lang)
            v = _persona_resolved_voice(persona, lang, tts_cfg, voice_mode)
            if v:
                voices.append(v)
        if not voices:
            out[eid] = {"state": "missing", "voice": "", "key": ""}
            continue
        speed = minimax_speed_for(lang)
        state = "missing"
        voice_used = ""
        key_used = ""
        for voice in voices:
            key = cache.key_for(text, voice=voice, model=model, speed=speed, emotion="")
            if cache.get(key) is not None:
                state, voice_used, key_used = "ok", voice, key
                break
            state, voice_used, key_used = "missing", voice_used or voice, key_used or key
        out[eid] = {"state": state, "voice": voice_used, "key": key_used}
    return out
```

argparse（`--qa` 帮助行后追加）:

```python
    ap.add_argument("--qa-status", action="store_true", help="不合成:逐条目输出物化状态 JSON(stdout),供 CP canned-status 端点消费")
    ap.add_argument("--entry-id", action="append", default=[], help="只处理指定 qa 条目 id(可重复;--qa/--qa-status 共用过滤)")
```

`main_async()` 里 `qa_rows = _cp_get(...)` 之后、物化之前接线（`--qa-status` 分支打印后直接 return 0，绝不建 provider）:

```python
    if args.qa_status:
        cache = TtsAudioCache(default_cache_dir(), sample_rate=int(args.sample_rate))
        status = _qa_status(
            qa_rows, persona_pool, lang_personas,
            all_personas=args.all_personas, tts_cfg=tts_cfg, voice_mode=voice_mode,
            model=model, sample_rate=int(args.sample_rate), cache=cache,
            entry_ids=set(args.entry_id) if args.entry_id else None,
        )
        print(json.dumps({"qa_status": status}, ensure_ascii=False))
        return 0
```

（变量名 `tts_cfg`/`voice_mode`/`model`/`sample_rate`/`persona_pool`/`lang_personas` 以 `main_async` 现有局部变量为准对齐——实现时读该函数现文，勿新造第二套解析。`--qa` 物化线同样接入 `--entry-id`：`qa_rows = [r for r in qa_rows if not args.entry_id or r.get("id") in set(args.entry_id)]`。）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_pregen_qa_status.py -v && python -m compileall -q scripts`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add scripts/pregen_tts.py tests/test_pregen_qa_status.py
git commit -m "feat(pregen): --qa-status JSON + --entry-id filter for canned surface"
```

---

### Task 3: CP 罐头端点（canned-status / canned-audio / pregen 触发）

**Files:**
- Modify: `apps/control-plane/control_plane/pregen.py`（新增 spawn 函数与 TTL 缓存）
- Modify: `apps/control-plane/control_plane/main.py`（`/api/qa-entries/{id}/hit` 端点后追加三个端点）
- Test: `tests/test_qa_canned_status.py`

**Interfaces:**
- Consumes: Task 2 的 `--qa-status` stdout JSON。
- Produces:
  - `GET /api/qa/canned-status?account_id=` → `{"available": bool, "statuses": {entry_id: {state, voice, key}}, "generated_at": epoch}`
  - `GET /api/qa/{entry_id}/canned-audio` → `audio/wav`（24kHz mono s16le PCM 包 WAV）或 404
  - `POST /api/qa/pregen` body `{"ids": []}` → `{"status": "queued"|"already_running"|...}`（admin/root）

- [ ] **Step 1: 写失败测试**

```python
"""罐头状态面:spawn 失败降级 available=False / 状态透传 / 缓存音频回放 WAV。"""
from __future__ import annotations

import io
import os
import wave
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-canned")

from agent_runtime.tts_cache import TtsAudioCache  # noqa: E402

FIXTURE = {
    "qa_status": {"qa:x": {"state": "ok", "voice": "v1", "key": "a" * 40}},
}


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    client = TestClient(app).__enter__()
    return client, repo


def test_canned_status_spawn_failure_degrades(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)

    def boom(base_url: str) -> dict:
        raise RuntimeError("spawn fail")

    monkeypatch.setattr("control_plane.pregen.qa_status_json", boom)
    r = client.get("/api/qa/canned-status")
    assert r.status_code == 200 and r.json()["available"] is False


def test_canned_status_passthrough_and_ttl(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    calls = {"n": 0}

    def fake(base_url: str) -> dict:
        calls["n"] += 1
        return FIXTURE

    monkeypatch.setattr("control_plane.pregen.qa_status_json", fake)
    monkeypatch.setattr("control_plane.pregen._STATUS_TTL_S", 60.0, raising=False)
    assert client.get("/api/qa/canned-status").json()["statuses"] == FIXTURE["qa_status"]
    assert client.get("/api/qa/canned-status").json()["statuses"] == FIXTURE["qa_status"]
    assert calls["n"] == 1  # TTL 内只 spawn 一次


def test_canned_audio_wav_replay(monkeypatch, tmp_path):
    client, repo = _client_and_repo(monkeypatch)
    entry = repo.create_qa_entry({"question_text": "q", "answer_text": "您好", "account_id": "acc-001"})
    cache = TtsAudioCache(tmp_path)
    cache.store("b" * 40, b"\x01\x02" * 240, text="您好", voice="v", model="m", pin=True)
    monkeypatch.setattr(
        "control_plane.pregen.qa_status_json",
        lambda base_url: {"qa_status": {entry["id"]: {"state": "ok", "voice": "v", "key": "b" * 40}}},
    )
    monkeypatch.setattr("control_plane.pregen.cache_root", lambda: tmp_path)
    r = client.get(f"/api/qa/{entry['id']}/canned-audio")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(r.content)) as w:
        assert w.getframerate() == 24000 and w.getnchannels() == 1


def test_pregen_trigger_single_flight(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    seen: dict = {}

    def fake_spawn(base_url: str, entry_ids: list[str]) -> dict:
        seen["ids"] = entry_ids
        return {"status": "queued"}

    monkeypatch.setattr("control_plane.pregen.qa_pregen_spawn", fake_spawn)
    r = client.post("/api/qa/pregen", json={"ids": ["qa:1"]})
    assert r.status_code == 200 and r.json()["status"] == "queued"
    assert seen["ids"] == ["qa:1"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_qa_canned_status.py -v`
Expected: FAIL（`qa_status_json`/端点不存在）

- [ ] **Step 3: 最小实现**

`control_plane/pregen.py` 追加：

```python
_STATUS_TTL_S = 60.0
_status_cache: tuple[float, dict | None] = (0.0, None)


def _repo_root_script() -> Path:
    return _repo_root() / "scripts" / "pregen_tts.py"


def qa_status_json(base_url: str) -> dict:
    """spawn pregen_tts.py --qa-status 取状态(子进程=与物化同一条代码路径,零重复)。"""
    script = _repo_root_script()
    if not script.exists():
        return {"available": False}
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "BOK_CP_URL": base_url}
    _bake_ssl_cert_file(env)
    proc = subprocess.run(  # noqa: S603 - 固定脚本+参数,无 shell
        [sys.executable, str(script), "--qa-status", "--cp", base_url],
        cwd=str(_repo_root()), env=env, capture_output=True, text=True, timeout=120,
    )
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            parsed = json.loads(line)
            if "qa_status" in parsed:
                return {"available": True, "qa_status": parsed["qa_status"]}
    return {"available": False}


def qa_canned_status(base_url: str, *, force: bool = False) -> dict:
    """TTL 缓存的罐头状态;spawn 失败降级 available=False(spec §8)。"""
    import time as _time

    now = _time.monotonic()
    if not force and _status_cache[1] is not None and now - _status_cache[0] < _STATUS_TTL_S:
        return _status_cache[1]  # type: ignore[return-value]
    try:
        data = qa_status_json(base_url)
        out = {"available": bool(data.get("available")), "statuses": dict(data.get("qa_status") or {})}
    except Exception:  # noqa: BLE001 - 状态面永不炸端点
        out = {"available": False, "statuses": {}}
    _status_cache = (now, out)  # type: ignore[assignment]
    globals()["_status_cache"] = (now, out)
    return out


def cache_root() -> Path:
    """tts-cache 根目录(canned-audio 读 {key}.pcm 用)。本地形态 CP/agent 同盘。"""
    from agent_runtime_shim import default_cache_dir  # 见下方说明

    return default_cache_dir()


def qa_pregen_spawn(base_url: str, entry_ids: list[str]) -> dict:
    """触发 --qa 物化(可限 ids);与 persona 自动物化共用单飞锁语义。"""
    script = _repo_root_script()
    if not script.exists():
        return {"status": "script_missing"}
    with _SPAWN_LOCK:
        running = _PREGEN_PROCS.get("__qa__")
        if running is not None and running.poll() is None:
            return {"status": "already_running"}
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "BOK_CP_URL": base_url}
        _bake_ssl_cert_file(env)
        cmd = [sys.executable, str(script), "--qa", "--cp", base_url]
        for eid in entry_ids or []:
            cmd += ["--entry-id", str(eid)]
        proc = _spawn_detached(cmd, env, _log_path())
        _PREGEN_PROCS["__qa__"] = proc
    return {"status": "queued", "pid": proc.pid, "log": str(_log_path())}
```

**实现说明（照做，不是占位）**：`cache_root()` 里 agent 包不可从 CP import（依赖方向）。把 `default_cache_dir` 的目录推导逻辑（读 `tts_cache.py:91` 现文：darwin `~/Library/Application Support/BokVoice/tts-cache` 等）**原样抄成一个 6 行函数**放 `control_plane/pregen.py` 顶部并删掉上面的 import shim——目录布局是稳定契约，注释注明「与 agent_runtime.tts_cache.default_cache_dir 同布局，改动须双侧同步」。`_status_cache` 直接用模块级变量重赋值（删掉示例里的重复赋值行，保留 `globals()` 一处即可）。

`main.py` 三个端点（`hit_qa_entry` 后追加；import 区补 `from . import pregen as pregen_mod` 若尚未有）:

```python
@app.get("/api/qa/canned-status")
def qa_canned_status_ep(request: Request, account_id: str = "acc-001") -> dict:
    _gate_page(request, "qa")
    scoped_account(request, account_id)
    out = pregen_mod.qa_canned_status(str(request.base_url).rstrip("/"))
    return {"available": out["available"], "statuses": out["statuses"]}


@app.get("/api/qa/{entry_id}/canned-audio")
def qa_canned_audio(entry_id: str, request: Request) -> Response:
    # 试听=罐头缓存回放,零云费,qa 页面权限即可;404=缺料(前端回退 preview,烧云归 admin)。
    _gate_page(request, "qa")
    deny_cross_account(request, _repo().get_qa_entry(entry_id))
    import re as _re

    out = pregen_mod.qa_canned_status(str(request.base_url).rstrip("/"))
    info = (out.get("statuses") or {}).get(entry_id) or {}
    key = str(info.get("key") or "")
    if info.get("state") != "ok" or not _re.fullmatch(r"[0-9a-f]{40}", key):
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="canned audio not materialized")
    pcm = (pregen_mod.cache_root() / f"{key}.pcm").read_bytes()  # miss → 404
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)
    return Response(content=buf.getvalue(), media_type="audio/wav")


@app.post("/api/qa/pregen")
def qa_pregen_ep(payload: dict, request: Request) -> dict:
    # 烧云配额操作,与 /api/tts/preview 同闸(spec §Global Constraints)。
    require_role(request, "admin", "root")
    ids = [str(x) for x in (payload.get("ids") or [])]
    return pregen_mod.qa_pregen_spawn(str(request.base_url).rstrip("/"), ids)
```

（`io`/`wave` 若未 import 在 main.py 顶部补；`Response` 已在 fastapi import 内。）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_qa_canned_status.py tests/test_owner_scope.py -v && python -m compileall -q apps`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add apps/control-plane/control_plane/pregen.py apps/control-plane/control_plane/main.py tests/test_qa_canned_status.py
git commit -m "feat(cp): qa canned-status / canned-audio(wav replay) / pregen trigger endpoints"
```

---

### Task 4: web 数据面——`api.ts` 扩展 + `lib/qa-canvas.ts` 纯函数

**Files:**
- Modify: `apps/web/lib/api.ts`（`deleteQa` 后追加三方法）
- Create: `apps/web/lib/qa-canvas.ts`
- Test: `apps/web/test/qa-canvas.test.mjs`

**Interfaces:**
- Consumes: Task 3 的三个端点；`GET /api/templates`（`steps_json` 字段，models.py:73）。
- Produces（Task 5/6/7 消费）:
  - `api.qaCannedStatus(accountId): Promise<{available: boolean; statuses: Record<string, {state: "ok"|"missing"; voice: string; key: string}>}>`
  - `api.pregenQa(ids: string[]): Promise<{status: string}>`；`api.cannedAudioUrl(id): string`
  - `parseTemplateSteps(stepsJson: string): FlowStep[]`（`{goal, ref}`）
  - `deriveGraph(rows: QaRow[], steps: FlowStep[], opts: {langFilter: string; positions: Record<string, {x: number; y: number}>}): {qaNodes: CanvasQaNode[]; stepNodes: CanvasStepNode[]; edges: CanvasEdge[]}`
  - `resolveClusterTarget(rows: QaRow[], fromId: string, toId: string): {ok: boolean; headId: string; reason: string}`
  - `LOCAL_POS_KEY(accountId: string, templateId: string): string`

- [ ] **Step 1: 写失败测试**（复用 `apps/web/test/apiBase.test.mjs` 的单文件转译装配——照抄该文件头部 `TMP_OUT`/`execFileSync(tsc …)` 装配段，把入参换成 `lib/qa-canvas.ts`，以下只列测试体）

```js
// 追加到装配段之后：
import assert from "node:assert/strict";
const qa = require(path.join(TMP_OUT, "qa-canvas.js"));

const ROWS = [
  { id: "h", question_text: "怎么查物流", answer_text: "在小程序查", lang: "zh", scope: "global", step_index: -1, cluster_head_id: "", enabled: true, hit_count: 5, created_at: "2026-01-01" },
  { id: "v", question_text: "物流咋查", answer_text: "在小程序查", lang: "zh", scope: "step", step_index: 1, cluster_head_id: "h", enabled: true, hit_count: 1, created_at: "2026-01-02" },
  { id: "s", question_text: "多久到", answer_text: "三天内", lang: "zh", scope: "step", step_index: 0, cluster_head_id: "", enabled: false, hit_count: 0, created_at: "2026-01-03" },
];
const STEPS = [{ goal: "开场", ref: "你好" }, { goal: "通知", ref: "抱歉" }];

test("parseTemplateSteps 解析 steps_json", () => {
  const steps = qa.parseTemplateSteps(JSON.stringify(STEPS));
  assert.equal(steps.length, 2);
  assert.equal(qa.parseTemplateSteps("").length, 0);
});

test("deriveGraph 步骤脊柱+簇边+步骤边", () => {
  const g = qa.deriveGraph(ROWS, STEPS, { langFilter: "all", positions: {} });
  assert.equal(g.stepNodes.filter((n) => n.data.virtual !== true).length, 2);
  const kinds = g.edges.map((e) => e.data.kind).sort();
  assert.deepEqual(kinds, ["cluster", "step", "step"]);
  const cluster = g.edges.find((e) => e.data.kind === "cluster");
  assert.equal(cluster.source, "v"); assert.equal(cluster.target, "h");
  // 布局确定性:同输入两次全同
  assert.deepEqual(qa.deriveGraph(ROWS, STEPS, { langFilter: "all", positions: {} }),
                   qa.deriveGraph(ROWS, STEPS, { langFilter: "all", positions: {} }));
});

test("resolveClusterTarget 星形校验", () => {
  assert.equal(qa.resolveClusterTarget(ROWS, "v", "h").ok, true);       // 变体重挂主条目
  assert.equal(qa.resolveClusterTarget(ROWS, "h", "v").ok, false);      // head 不可挂到自己变体
  assert.equal(qa.resolveClusterTarget(ROWS, "h", "h").ok, false);      // 自连
  assert.equal(qa.resolveClusterTarget(ROWS, "s", "v").headId, "h");    // 目标是变体→重定向其 head
});

test("LOCAL_POS_KEY 形态", () => {
  assert.equal(qa.LOCAL_POS_KEY("acc-001", "tpl-1"), "qa-canvas-pos:acc-001:tpl-1");
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd apps/web && node --test test/qa-canvas.test.mjs`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `lib/qa-canvas.ts` + `api.ts` 三方法**

```ts
// apps/web/lib/qa-canvas.ts
// QA 画布纯函数(spec §4.3):解析 steps、派生节点/边、簇校验、布局契约。
// 布局契约(archify 原则):步骤脊柱=唯一主路径;QA 簇=最近步骤列的星形侧分支;
// 确定性网格(同输入同输出);语义边标签;位置覆盖走 localStorage。

export type QaRow = {
  id: string;
  owner_user_id?: string;
  question_text?: string;
  answer_text?: string;
  lang?: string;
  scope?: string;
  step_index?: number;
  voice_id?: string;
  enabled?: boolean;
  hit_count?: number;
  source?: string;
  cluster_head_id?: string;
  created_at?: string;
};

export type FlowStep = { goal: string; ref: string };
export type Pt = { x: number; y: number };

export type CanvasStepNode = {
  id: string; type: "qaStep";
  position: Pt;
  data: { index: number; goal: string; refFirstLine: string; virtual: boolean };
};
export type CanvasQaNode = {
  id: string; type: "qaEntry";
  position: Pt;
  data: QaRow & { isHead: boolean; canned?: "ok" | "missing" };
};
export type CanvasEdge = {
  id: string;
  source: string; target: string;
  sourceHandle: string | null; targetHandle: string | null;
  data: { kind: "cluster" | "step" };
  animated?: boolean;
  label?: string;
};

export const COL_W = 300;
export const ROW_H = 150;

export function parseTemplateSteps(stepsJson: string): FlowStep[] {
  try {
    const raw = JSON.parse(stepsJson || "[]");
    return Array.isArray(raw) ? raw : [];
  } catch {
    return [];
  }
}

export function LOCAL_POS_KEY(accountId: string, templateId: string): string {
  return `qa-canvas-pos:${accountId}:${templateId}`;
}

/** 列 x 坐标:0=通用/未挂步,1..N=步骤。 */
function colOf(row: QaRow, stepCount: number): number {
  if (String(row.scope ?? "") === "step") {
    const idx = Number(row.step_index ?? -1);
    if (idx >= 0 && idx < stepCount) return idx + 1;
  }
  return 0;
}

export function deriveGraph(
  rows: QaRow[],
  steps: FlowStep[],
  opts: { langFilter: string; positions: Record<string, Pt> },
): { qaNodes: CanvasQaNode[]; stepNodes: CanvasStepNode[]; edges: CanvasEdge[] } {
  const filtered = (rows ?? []).filter(
    (r) => opts.langFilter === "all" || String(r.lang ?? "zh") === opts.langFilter,
  );
  const stepNodes: CanvasStepNode[] = steps.map((s, i) => ({
    id: `step:${i}`,
    type: "qaStep" as const,
    position: { x: 0, y: 40 + i * (ROW_H + 60) },
    data: {
      index: i,
      goal: String(s.goal || ""),
      refFirstLine: String(s.ref || "").split("\n")[0].slice(0, 40),
      virtual: false,
    },
  }));
  // 虚拟「全程通用」节点在列首上方。
  stepNodes.unshift({
    id: "step:global",
    type: "qaStep",
    position: { x: 0, y: 0 },
    data: { index: -1, goal: "全程通用", refFirstLine: "不挂步骤的条目归此列", virtual: true },
  });

  const heads = new Set(filtered.map((r) => String(r.cluster_head_id || "")).filter(Boolean));
  const byCol = new Map<number, QaRow[]>();
  for (const r of filtered) {
    const col = colOf(r, steps.length);
    if (!byCol.has(col)) byCol.set(col, []);
    byCol.get(col)!.push(r);
  }
  const qaNodes: CanvasQaNode[] = [];
  const cursor = new Map<number, number>(); // 列内游标(确定性行序:创建序=输入序)
  for (const r of filtered) {
    const col = colOf(r, steps.length);
    const isHead = heads.has(String(r.id));
    // 星形侧分支:head 行首,变体缩进挂其右下。
    const headId = String(r.cluster_head_id || "");
    const indent = headId ? 120 : 0;
    const rowIdx = cursor.get(col) ?? 0;
    const base = stepNodes.find((n) => n.id === `step:${col === 0 ? "global" : col - 1}`)!.position;
    const pos = opts.positions[String(r.id)] ?? {
      x: base.x + COL_W + indent,
      y: base.y + rowIdx * (ROW_H - 30) + (col === 0 ? 40 : 0),
    };
    cursor.set(col, rowIdx + 1);
    qaNodes.push({
      id: String(r.id),
      type: "qaEntry",
      position: pos,
      data: { ...r, isHead },
    });
  }
  const edges: CanvasEdge[] = [];
  for (const r of filtered) {
    const headId = String(r.cluster_head_id || "");
    if (headId && filtered.some((x) => String(x.id) === headId)) {
      edges.push({
        id: `c:${r.id}:${headId}`,
        source: String(r.id), target: headId,
        sourceHandle: null, targetHandle: null,
        data: { kind: "cluster" },
      });
    }
    if (String(r.scope ?? "") === "step") {
      const idx = Number(r.step_index ?? -1);
      if (idx >= 0 && idx < steps.length) {
        edges.push({
          id: `s:${r.id}:step:${idx}`,
          source: String(r.id), target: `step:${idx}`,
          sourceHandle: null, targetHandle: null,
          data: { kind: "step" },
          animated: false,
          label: `进入第 ${idx + 1} 步`,
        });
      }
    }
  }
  return { qaNodes, stepNodes, edges };
}

/** 连簇校验(spec §4.4):一层星形;目标是变体→重定向其 head;同簇/自连拒绝。 */
export function resolveClusterTarget(
  rows: QaRow[], fromId: string, toId: string,
): { ok: boolean; headId: string; reason: string } {
  if (fromId === toId) return { ok: false, headId: "", reason: "不能连接到自己" };
  const headOf = (id: string): string => {
    const r = rows.find((x) => String(x.id) === id);
    return String(r?.cluster_head_id || "") || id;
  };
  const targetHead = headOf(toId);
  if (targetHead === fromId) return { ok: false, headId: "", reason: "主条目不能挂到自己的变体" };
  if (headOf(fromId) === targetHead && headOf(fromId) !== fromId) {
    return { ok: false, headId: "", reason: "两条目已在同一簇" };
  }
  return { ok: true, headId: targetHead, reason: "" };
}
```

`api.ts`（`deleteQa` 后）:

```ts
  qaCannedStatus: (accountId = "acc-001") =>
    request<{ available: boolean; statuses: Record<string, { state: "ok" | "missing"; voice: string; key: string }> }>(
      `/api/qa/canned-status?account_id=${encodeURIComponent(accountId)}`,
    ),
  pregenQa: (ids: string[]) =>
    request<{ status: string }>("/api/qa/pregen", { method: "POST", body: JSON.stringify({ ids }) }),
  cannedAudioUrl: (id: string) => `${apiBase()}/api/qa/${id}/canned-audio`,
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd apps/web && node --test test/qa-canvas.test.mjs && npx tsc --noEmit`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add apps/web/lib/api.ts apps/web/lib/qa-canvas.ts apps/web/test/qa-canvas.test.mjs
git commit -m "feat(web): qa-canvas pure functions — graph derivation, cluster validation, api surface"
```

---

### Task 5: React Flow 画布组件（只读渲染 + 罐头/权限徽标）

**Files:**
- Modify: `apps/web/package.json`（`npm i @xyflow/react`）
- Create: `apps/web/components/qa-canvas-view.tsx`
- Test: 无新测试文件（交互组件；布局/校验已在 Task 4 纯函数层钉住）。门禁=`tsc --noEmit && npm run build`。

**Interfaces:**
- Consumes: Task 4 的 `deriveGraph/parseTemplateSteps/LOCAL_POS_KEY` 与 `api.listQaAll/listTemplates/qaCannedStatus`。
- Produces: `QaCanvasView`（props 见下方代码）——Task 7 在 page.tsx 引用。

- [ ] **Step 1: 安装依赖**

Run: `cd apps/web && npm i @xyflow/react`
Expected: package.json 出现 `"@xyflow/react": "^12.x"`

- [ ] **Step 2: 实现 `components/qa-canvas-view.tsx`（完整组件）**

```tsx
"use client";

// QA 画布视图(spec §4.3/§4.4,2026-09-17 Phase1)。只读渲染在本文件;
// 连线/断线编辑回调经 props 上抛(page.tsx 持数据与 PATCH)。
// 布局=lib/qa-canvas.deriveGraph(确定性);拖动位置 localStorage;MiniMap 常开。

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Background, Controls, MiniMap, ReactFlow,
  type Edge, type Node, type NodeProps,
} from "@xyflow/react";
import "reactflow-style";
import {
  deriveGraph, LOCAL_POS_KEY, parseTemplateSteps,
  type FlowStep, type Pt, type QaRow,
} from "@/lib/qa-canvas";

export type TemplateRow = { id: string; name?: string; steps_json?: string; language?: string };

const LANG_LABEL: Record<string, string> = { zh: "普", cantonese: "粤", en: "EN" };
const SOURCE_LABEL: Record<string, string> = { curated: "精选", mined: "挖掘", imported: "导入" };

function QaEntryNode({ data }: NodeProps) {
  const d = data as QaRow & { isHead: boolean; canned?: "ok" | "missing"; canEdit: boolean };
  const dim = d.enabled === false;
  return (
    <div
      className={`w-[260px] rounded-lg border bg-white/5 p-3 text-xs ${dim ? "opacity-50" : ""} ${d.isHead ? "border-(--accent)" : "border-(--card-border)"}`}
      title={d.canEdit ? undefined : "共享条目由主管维护"}
    >
      <p className="line-clamp-2 font-medium">{String(d.question_text ?? "(无问法)")}</p>
      <p className="mt-1 line-clamp-1 muted">{String(d.answer_text ?? "")}</p>
      <div className="mt-2 flex flex-wrap items-center gap-1">
        <span className="rounded-sm bg-white/10 px-1 text-[10px]">{LANG_LABEL[String(d.lang ?? "zh")] ?? d.lang}</span>
        <span className="rounded-sm bg-white/10 px-1 text-[10px]">命中 {Number(d.hit_count ?? 0)}</span>
        {d.source && <span className="rounded-sm bg-white/10 px-1 text-[10px]">{SOURCE_LABEL[d.source] ?? d.source}</span>}
        {d.canned === "missing" && <span className="rounded-sm bg-amber-400/20 px-1 text-[10px] text-amber-300">缺料</span>}
        {d.canned === "ok" && <span className="rounded-sm bg-emerald-400/20 px-1 text-[10px] text-emerald-300">罐头✓</span>}
        {d.enabled === false && <span className="rounded-sm bg-white/10 px-1 text-[10px]">停用</span>}
        {!d.canEdit && <span className="rounded-sm bg-white/10 px-1 text-[10px]" title="共享只读">🔒</span>}
      </div>
    </div>
  );
}

function QaStepNode({ data }: NodeProps) {
  const d = data as { index: number; goal: string; refFirstLine: string; virtual: boolean };
  return (
    <div className={`w-[200px] rounded-lg border p-3 text-xs ${d.virtual ? "border-dashed border-(--card-border) muted" : "border-(--accent) bg-(--accent)/5"}`}>
      <p className="font-medium">{d.virtual ? "全程通用" : `第 ${d.index + 1} 步 · ${d.goal}`}</p>
      {!d.virtual && <p className="mt-1 line-clamp-2 muted">{d.refFirstLine}</p>}
    </div>
  );
}

const NODE_TYPES = { qaEntry: QaEntryNode, qaStep: QaStepNode };

export default function QaCanvasView(props: {
  rows: QaRow[];
  templates: TemplateRow[];
  templateId: string;
  onTemplateChange: (id: string) => void;
  canned: Record<string, { state: "ok" | "missing" }>;
  canEditRow: (row: QaRow) => boolean;
  onNodeClick: (row: QaRow) => void;
  onPaneDoubleClick: (pt: { x: number; y: number }) => void;
  onConnectCluster: (fromId: string, toId: string) => void;
  onDisconnect: (edge: { id: string; data?: { kind?: string }; source: string; target: string }) => void;
  onStepConnect: (entryId: string, stepIndex: number) => void;
}) {
  const { rows, templates, templateId, canned } = props;
  const [langFilter, setLangFilter] = useState("all");
  const [positions, setPositions] = useState<Record<string, Pt>>({});
  const accountId = "acc-001"; // 与页面 useAccount 同源,Task 7 接线时由 props 传入替换。

  useEffect(() => {
    try {
      const raw = localStorage.getItem(LOCAL_POS_KEY(accountId, templateId));
      setPositions(raw ? (JSON.parse(raw) as Record<string, Pt>) : {});
    } catch {
      setPositions({});
    }
  }, [accountId, templateId]);

  const steps: FlowStep[] = useMemo(() => {
    const tpl = templates.find((t) => String(t.id) === templateId);
    return parseTemplateSteps(String(tpl?.steps_json ?? ""));
  }, [templates, templateId]);

  const graph = useMemo(
    () => deriveGraph(rows, steps, { langFilter, positions }),
    [rows, steps, langFilter, positions],
  );

  const nodes: Node[] = useMemo(
    () => [
      ...graph.stepNodes.map((n) => ({ ...n, data: { ...n.data } })),
      ...graph.qaNodes.map((n) => ({
        ...n,
        data: { ...n.data, canned: canned[String(n.id)]?.state, canEdit: props.canEditRow(n.data) },
      })),
    ],
    [graph, canned, props],
  );
  const edges: Edge[] = useMemo(
    () =>
      graph.edges.map((e) => ({
        ...e,
        animated: e.data.kind === "step",
        style: e.data.kind === "cluster"
          ? { stroke: "var(--accent)", strokeWidth: 1.5 }
          : { stroke: "#888", strokeDasharray: "4 3" },
        labelStyle: { fontSize: 10 },
      })),
    [graph],
  );

  const onNodeDragStop = useCallback(
    (_: unknown, node: Node) => {
      setPositions((prev) => {
        const next = { ...prev, [node.id]: node.position };
        try {
          localStorage.setItem(LOCAL_POS_KEY(accountId, templateId), JSON.stringify(next));
        } catch { /* 隐私模式丢弃,不阻塞 */ }
        return next;
      });
    },
    [accountId, templateId],
  );

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <select
          className="rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--accent)"
          value={templateId}
          onChange={(e) => props.onTemplateChange(e.target.value)}
        >
          {templates.map((t) => (
            <option key={String(t.id)} value={String(t.id)}>{String(t.name || t.id)}</option>
          ))}
        </select>
        {(["all", "zh", "cantonese", "en"] as const).map((k) => (
          <button
            key={k}
            className={`btn-ghost text-xs ${langFilter === k ? "border-(--accent) text-accent" : "muted"}`}
            onClick={() => setLangFilter(k)}
          >
            {k === "all" ? "全部" : LANG_LABEL[k]}
          </button>
        ))}
        <button
          className="btn-ghost text-xs"
          onClick={() => {
            localStorage.removeItem(LOCAL_POS_KEY(accountId, templateId));
            setPositions({});
          }}
        >
          重置布局
        </button>
      </div>
      <div className="h-[600px] rounded-lg border border-(--card-border)">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={NODE_TYPES}
          fitView
          minZoom={0.2}
          onNodeDragStop={onNodeDragStop}
          onNodeClick={(_, node) => {
            const row = rows.find((r) => String(r.id) === node.id);
            if (row) props.onNodeClick(row);
          }}
          onEdgeClick={(_, edge) => {
            if (confirm("解除这条连线？")) props.onDisconnect(edge as never);
          }}
        >
          <Background gap={24} />
          <Controls />
          <MiniMap pannable zoomable />
        </ReactFlow>
      </div>
    </div>
  );
}
```

实现说明：`import "reactflow-style"` 是占位写法——落地时改为 `import "@xyflow/react/dist/style.css";`（v12 正确路径）。`onPaneDoubleClick`/`onConnectCluster`/`onStepConnect` 在 Task 6 接入 React Flow 的 `onConnect`/`onDoubleClick` 后消费；本任务先完成 props 面与只读渲染，编译通过即可。`accountId` 用 prop 注入替换字面量（Task 7 传入 `useAccount()` 值）。

- [ ] **Step 3: 编译门禁**

Run: `cd apps/web && npx tsc --noEmit && npm run build`
Expected: PASS（组件未被引用也须编译干净；未消费 props 的告警允许）

- [ ] **Step 4: 提交**

```bash
git add apps/web/package.json apps/web/package-lock.json apps/web/components/qa-canvas-view.tsx
git commit -m "feat(web): qa canvas view — react flow spine/satellite read-only render"
```

---

### Task 6: 画布编辑交互（连簇/挂步骤/断线/右键/双击新建/回滚）

**Files:**
- Modify: `apps/web/components/qa-canvas-view.tsx`
- Test: `apps/web/test/qa-canvas.test.mjs`（追加 `resolveClusterTarget` 边界用例，已在 Task 4 覆盖核心；本任务补「错误回滚」纯逻辑）

**Interfaces:**
- Consumes: Task 4 `resolveClusterTarget`；`api.patchQa`；Task 5 组件。
- Produces: 可编辑画布（连线写 `cluster_head_id` / `scope+step_index+template_id`，失败回滚）。

- [ ] **Step 1: 写失败测试（回滚纯逻辑：`revertPatch` 辅助）**

在 `lib/qa-canvas.ts` 追加并测试：

```ts
/** PATCH 失败回滚:返回剔除乐观变更后的行集(spec §8:不保留脏边)。 */
export function revertCluster(rows: QaRow[], childId: string, prevHeadId: string): QaRow[] {
  return rows.map((r) => (String(r.id) === childId ? { ...r, cluster_head_id: prevHeadId } : r));
}
```

测试体（追加到 qa-canvas.test.mjs）:

```js
test("revertCluster 回滚断簇", () => {
  const rows = [{ id: "v", cluster_head_id: "h2" }];
  assert.equal(qa.revertCluster(rows, "v", "h1")[0].cluster_head_id, "h1");
});
```

- [ ] **Step 2: 跑测试确认失败 → 实现 → 通过**

Run: `cd apps/web && node --test test/qa-canvas.test.mjs`（先 FAIL 后 PASS）

- [ ] **Step 3: 组件接编辑回调**

`qa-canvas-view.tsx`：

- `onConnect`（ReactFlow prop）：

```tsx
onConnect={(conn) => {
  if (!conn.source || !conn.target) return;
  const srcIsQa = conn.source.indexOf("step:") !== 0;
  if (!srcIsQa) return; // 只允许从条目拖出
  const target = conn.target;
  if (target.startsWith("step:")) {
    const idx = Number(target.slice(5));
    if (!Number.isNaN(idx)) props.onStepConnect(conn.source, idx === -1 ? -1 : idx);
    return;
  }
  props.onConnectCluster(conn.source, target);
}}
```

- `ReactFlow` 上补 `onNodeContextMenu={(e, node) => { e.preventDefault(); const row = rows.find(...); row && props.onNodeContextMenu?.(row, e); }}`（props 加可选用法，Task 7 提供菜单）与 `onPaneDoubleClick={(e) => props.onPaneDoubleClick({ x: e.clientX, y: e.clientY })}`。
- 条目节点 `QaEntryNode` 外层补拖拽柄渲染条件：`canEdit` 为假时不渲染 `<Handle>`（在节点 JSX 里加 `type:"qaEntry"` 专用 source/target Handle，`isConnectable={d.canEdit}`）。

`page.tsx` 侧处理器（Task 7 一并接线；本任务先把逻辑函数写进 page 或抽 `lib`——保持 page 内联即可）：

```ts
async function connectCluster(fromId: string, toId: string) {
  const verdict = resolveClusterTarget(rows ?? [], fromId, toId);
  if (!verdict.ok) { setErr(verdict.reason); return; }
  const prev = String((rows ?? []).find((r) => String(r.id) === fromId)?.cluster_head_id ?? "");
  setRows((rs) => (rs ?? []).map((r) => (String(r.id) === fromId ? { ...r, cluster_head_id: verdict.headId } : r))); // 乐观
  try {
    await api.patchQa(fromId, { cluster_head_id: verdict.headId });
    setErr("");
  } catch (e) {
    setErr(String(e));
    setRows((rs) => revertCluster(rs ?? [], fromId, prev)); // 回滚,spec §8
    await refresh();
  }
}

async function stepConnect(entryId: string, stepIndex: number) {
  const patch = stepIndex < 0
    ? { scope: "global", step_index: -1, template_id: "" }
    : { scope: "step", step_index: stepIndex, template_id: templateId };
  try { await api.patchQa(entryId, patch); setErr(""); await refresh(); }
  catch (e) { setErr(String(e)); await refresh(); }
}
```

- [ ] **Step 4: 门禁**

Run: `cd apps/web && node --test test/qa-canvas.test.mjs && npx tsc --noEmit`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add apps/web
git commit -m "feat(web): canvas editing — cluster/step connect, disconnect, optimistic rollback"
```

---

### Task 7: 页面接线——「列表|画布」切换 + 试听/补料 UI

**Files:**
- Modify: `apps/web/app/(app)/qa/page.tsx`
- Modify: `apps/web/components/qa-canvas-view.tsx`（ accountId prop 注入）
- Test: 门禁级（`tsc --noEmit && npm run build && npm test`；交互无新纯函数）

**Interfaces:**
- Consumes: Task 3/4/5/6 全部产出。
- Produces: `/qa` 页头「列表 | 画布」分段切换；画布空态/加载态沿用 `LoadingState/EmptyState`。

- [ ] **Step 1: page.tsx 接线**

- 顶部加 `const [view, setView] = useState<"list" | "canvas">("list");`、`const [templates, setTemplates] = useState<TemplateRow[]>([]);`、`const [templateId, setTemplateId] = useState("");`、`const [canned, setCanned] = useState<Record<string, { state: "ok" | "missing" }>>({});`。
- `useEffect` 拉 `api.listTemplates(accountId)`（话务员 403 时静默空表）与 `api.qaCannedStatus(accountId)`（失败静默 `{}`——spec §8 徽标降级）；`accountId` 变化时重拉。
- 页头 tab 区追加视图切换（沿用现有 `btn-ghost` 分段样式）：

```tsx
<div className="flex items-center gap-1">
  {([["list", "列表"], ["canvas", "画布"]] as const).map(([k, label]) => (
    <button key={k} className={`btn-ghost text-xs ${view === k ? "border-(--accent) text-accent" : "muted"}`}
            onClick={() => setView(k)}>{label}</button>
  ))}
</div>
```

- `view === "canvas"` 时左栏渲染 `<QaCanvasView …>`（`next/dynamic` 懒加载：`const QaCanvasView = dynamic(() => import("@/components/qa-canvas-view"), { ssr: false, loading: () => <LoadingState /> });`）并传全量回调（Task 6 处理器 + 试听 + 右键菜单）。
- **试听**（画布侧栏 + 右键菜单共用）：

```ts
function audition(row: QaRow) {
  const audio = new Audio(api.cannedAudioUrl(String(row.id)));
  audio.play().catch(() => {
    // 缺料回退:现场合成烧云配额,仅主管可用(user 见 alert 提示待物化)。
    if (isManager) {
      void api.previewTts({ text: String(row.answer_text ?? ""), language: String(row.lang ?? "zh") });
    } else {
      window.alert("该条目罐头未物化，请联系主管在画布上「重新物化」。");
    }
  });
}
```

- **补料**：画布工具条「一键补料」= `api.pregenQa([])`；右键菜单「重新物化」= `api.pregenQa([row.id])`；均仅 `isManager` 渲染（403 由闸兜底），完成后 `setTimeout(refreshCanned, 3000)` 延迟刷状态。

- [ ] **Step 2: 门禁**

Run: `cd apps/web && npx tsc --noEmit && npm run build && npm test`
Expected: 全 PASS

- [ ] **Step 3: 手工冒烟（运行栈）**

```bash
python tools/bok.py serve   # 已在跑则跳过
# 浏览器 http://127.0.0.1:3000/qa ：切换画布→见步骤脊柱+条目；拖线连簇→列表页可见关系不变；试听出声
```

- [ ] **Step 4: 提交**

```bash
git add apps/web/app/\(app\)/qa/page.tsx apps/web/components/qa-canvas-view.tsx
git commit -m "feat(web): /qa list|canvas toggle with canned audition and materialize actions"
```

---

### Task 8: 惜客通导入器 `scripts/import_xkt_qa.py`

**Files:**
- Create: `scripts/import_xkt_qa.py`
- Test: `tests/test_import_xkt_qa.py`

**Interfaces:**
- Consumes: 惜客通导出 `tbl_ai_knowledge.json`（顶层数组；外部真字面量键 `Question/Answer/Answer2..5/Status/lang 无`）；`POST /api/qa-entries`。
- Produces: CLI `--input <json> --account acc-001 [--lang auto] [--owner ""] [--apply]`；纯函数 `plan_import(rows, existing_questions, lang_mode)` → `(creates: list[dict], report: dict)`。

- [ ] **Step 1: 写失败测试**

```python
"""惜客通导入纯函数:&拆分/簇指针/语言启发/去重/停用保留(spec §6)。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import import_xkt_qa as imp  # noqa: E402


def _row(q: str, answer: str = "答", status: int = 1, **kw):
    base = {"Question": q, "Answer": answer, "Status": status, "Id": 1}
    base.update(kw)
    return base


def test_split_variants_and_cluster_pointer():
    creates, report = imp.plan_import(
        [_row("怎么查物流&物流咋查&点样查物流")], existing=set(), lang_mode="auto",
    )
    assert len(creates) == 3
    head = creates[0]
    assert head["cluster_head_id"] == "" and "_head_question" not in head
    assert creates[1]["question_text"] == "物流咋查"
    assert creates[1]["_head_question"] == "怎么查物流"  # apply 阶段解析为主条目真实 id
    assert creates[2]["_head_question"] == "怎么查物流"
    assert report["variants"] == 2 and report["created"] == 3


def test_lang_heuristic():
    creates, _ = imp.plan_import([_row("唔該問下佢&唔該")], existing=set(), lang_mode="auto")
    assert all(c["lang"] == "cantonese" for c in creates)
    creates2, _ = imp.plan_import([_row("你好&请问")], existing=set(), lang_mode="auto")
    assert all(c["lang"] == "zh" for c in creates2)


def test_lang_override_and_disabled_and_dedup():
    creates, report = imp.plan_import(
        [_row("你好", status=0)],
        existing={"你好"}, lang_mode="en",
    )
    assert creates == [] and report["skipped_dup"] == 1  # 去重优先
    creates2, report2 = imp.plan_import([_row("新问法", status=0)], existing=set(), lang_mode="en")
    assert creates2[0]["lang"] == "en" and creates2[0]["enabled"] is False
    assert report2["disabled"] == 1


def test_multi_answers_counted_not_imported():
    row = _row("问法一")
    row["Answer2"] = "备选答案"
    creates, report = imp.plan_import([row], existing=set(), lang_mode="zh")
    assert len(creates) == 1
    assert report["alt_answers_seen"] == 1
```

（`cluster_head_id` 在 plan 阶段指向的是**待建主条目**——实现用「创建顺序约定：主条目先建，变体 `cluster_head_id` 填主条目入库后返回的真实 id」，即 `--apply` 阶段二段式：先 POST 主条目拿 id，再 POST 变体。`plan_import` 产出的变体行带 `"_head_question": 主问法`，apply 函数解析成真实 id。测试按此断言修正：`creates[1]["_head_question"] == "怎么查物流"`。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_import_xkt_qa.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现导入器（镜像 `scripts/mine_qa.py` 的 `_cp_request`/argparse/`--apply` 惯例）**

```python
"""惜客通 tbl_ai_knowledge.json → Bok qa_entries 导入器(spec §6,2026-09-17)。

用法:
  python scripts/import_xkt_qa.py --input tbl_ai_knowledge.json [--account acc-001]
      [--lang auto|zh|cantonese|en] [--owner ""] [--apply]
默认 dry-run 打印计划;--apply 走 POST /api/qa-entries(source=imported),
主条目先建、变体回填 cluster_head_id。AfterAnswer*/Priority/Trigger* 等
外部字段不搬,dry-run 报告列示——步骤挂载留给画布拖线。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

_CANTO_MARKS = re.compile(r"[唔該係嘅咗哋啲冇乜嚟]")

# 惜客通未搬运字段前缀(外部真字面量,报告列示用)。
_SKIPPED_PREFIXES = ("AfterAnswer", "Priority", "Trigger", "Interrupt", "LabelId", "Intention", "Force")


def normalize_q(text: str) -> str:
    return re.sub(r"[\s，。？！、,.\?!]+", "", str(text or "")).strip()


def detect_lang(question: str) -> str:
    return "cantonese" if _CANTO_MARKS.search(question or "") else "zh"


def plan_import(
    rows: list[dict],
    existing: set[str],
    lang_mode: str,
) -> tuple[list[dict], dict]:
    """dry-run 可测核心。creates 变体行带 _head_question(apply 时解析为真实 id)。"""
    creates: list[dict] = []
    report = {"created": 0, "variants": 0, "skipped_dup": 0, "disabled": 0,
              "alt_answers_seen": 0, "invalid": 0, "skipped_fields": set()}
    for row in rows or []:
        raw_q = str(row.get("Question") or "").strip()
        answer = str(row.get("Answer") or "").strip()
        if not raw_q or not answer:
            report["invalid"] += 1
            continue
        variants = [v.strip() for v in raw_q.split("&") if v.strip()]
        if not variants:
            report["invalid"] += 1
            continue
        head_q = variants[0]
        if normalize_q(head_q) in existing:
            report["skipped_dup"] += 1
            continue
        enabled = int(row.get("Status") or 1) == 1
        base = {
            "answer_text": answer,
            "lang": lang_mode if lang_mode != "auto" else detect_lang(head_q),
            "scope": "global",
            "step_index": -1,
            "voice_id": "",
            "source": "imported",
            "enabled": enabled,
        }
        if not enabled:
            report["disabled"] += 1
        creates.append({"question_text": head_q, "cluster_head_id": "", **base})
        report["created"] += 1
        for v in variants[1:]:
            if normalize_q(v) in existing:
                report["skipped_dup"] += 1
                continue
            creates.append({**base, "question_text": v, "cluster_head_id": None,
                            "_head_question": head_q})
            report["variants"] += 1
        alt = sum(1 for k in row.keys() if re.fullmatch(r"Answer[2-5]", k) and str(row.get(k) or "").strip())
        report["alt_answers_seen"] += alt
        for k in row.keys():
            if k.startswith(_SKIPPED_PREFIXES):
                report["skipped_fields"].add(k)
    return creates, report


def _cp_request(base: str, path: str, token: str, *, method: str = "GET", payload: dict | None = None) -> object:
    req = urllib.request.Request(f"{base.rstrip('/')}{path}", method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    with urllib.request.urlopen(req, data=data, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--account", default="acc-001")
    ap.add_argument("--cp", default=os.environ.get("BOK_CP_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--lang", default="auto", choices=["auto", "zh", "cantonese", "en"])
    ap.add_argument("--owner", default="")
    ap.add_argument("--apply", action="store_true", help="缺省 dry-run")
    args = ap.parse_args()

    rows = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        print("输入必须是 JSON 数组(惜客通 tbl_ai_knowledge.json)")
        return 2
    token = os.environ.get("BOK_CP_TOKEN", "")
    existing_rows = _cp_request(args.cp, f"/api/qa-entries?account_id={args.account}", token) or []
    existing = {normalize_q(str(e.get("question_text") or "")) for e in existing_rows}
    creates, report = plan_import(rows, existing, args.lang)
    for c in creates:
        c.setdefault("account_id", args.account)
        c.setdefault("owner_user_id", args.owner)
    print(f"计划:新建 {report['created']}(含停用 {report['disabled']}) + 变体 {report['variants']}"
          f" | 去重跳过 {report['skipped_dup']} | 非法行 {report['invalid']}"
          f" | 备选答案计数(不搬) {report['alt_answers_seen']}")
    if report["skipped_fields"]:
        print(f"未搬字段: {sorted(report['skipped_fields'])}")
    if not args.apply:
        for c in creates[:20]:
            print(f"  [{'头' if not c.get('_head_question') else '变体'}] {c['lang']} {c['question_text']}")
        print("dry-run 结束(--apply 落地)")
        return 0
    head_ids: dict[str, str] = {}
    done = 0
    for c in creates:
        head_q = c.pop("_head_question", None)
        if head_q:
            c["cluster_head_id"] = head_ids.get(head_q, "")
        row = _cp_request(args.cp, "/api/qa-entries", token, method="POST", payload=c)
        if not head_q:
            head_ids[str(c["question_text"])] = str(row.get("id"))
        done += 1
    print(f"已入库 {done} 条。下一步: python scripts/pregen_tts.py --qa 物化罐头；步骤挂载请在画布拖线。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

（测试与实现的 `report["variants"]`/`skipped_fields` 为 `set` 时注意 pytest 断言不改集合——报告打印处 `sorted()`。）

- [ ] **Step 4: 跑测试确认通过 + 真实包 dry-run**

Run: `.venv312/bin/python -m pytest tests/test_import_xkt_qa.py -v && .venv312/bin/python scripts/import_xkt_qa.py --input /tmp/xkt-export/tbl_ai_knowledge.json 2>/dev/null || echo "(无栈环境时 dry-run 需运行中的 CP,跳过为可接受)"`
Expected: pytest PASS

- [ ] **Step 5: 提交**

```bash
git add scripts/import_xkt_qa.py tests/test_import_xkt_qa.py
git commit -m "feat(import): xkt tbl_ai_knowledge importer — & variants, cluster pointer, dry-run default"
```

---

### Task 9: 收尾——文档、全量门禁

**Files:**
- Modify: `docs/REPO_MAP.md`（新文件条目：`apps/web/components/qa-canvas-view.tsx`、`apps/web/lib/qa-canvas.ts`、`scripts/import_xkt_qa.py`）
- Test: 全量门禁

- [ ] **Step 1: REPO_MAP 补条目**（照现有条目格式，一行一文件职责）

- [ ] **Step 2: 全量门禁**

```bash
python -m compileall -q apps packages services tools scripts
.venv312/bin/python -m pytest tests/ -x -q
cd services/realtime-translation && npm ci && npm test && cd ../..
cd apps/web && npx tsc --noEmit && npm run build && npm test && cd ../..
```
Expected: 全绿（cargo test 不涉及——无 Rust 改动；verify_bundle 非 merge 必需，打包含再跑）

- [ ] **Step 3: 提交**

```bash
git add docs/REPO_MAP.md
git commit -m "docs(repo-map): qa canvas / canned surface / xkt importer file entries"
```

---

## Self-Review 记录

1. **Spec 覆盖**：§4.1→Task1；§4.2/4.3→Task4/5/7；§4.4→Task5/6/7；§5→Task2/3/7（试听改为「缓存回放优先、preview 仅 admin 回退」——比 spec 初稿更省配额，已在 §3 事实核查与本计划中一致）；§6→Task8；§7 API 表→Task1/3；§8→Task6 回滚+Task3 降级；§9 测试→各任务 + Task9；§10 借鉴→布局契约(Task4)/MiniMap(Task5)/试听(Task7)/罐头面(Task3)；§11 交付顺序=任务序。
2. **占位符扫描**：Task3 `cache_root()` 的 import shim 已改为「照抄 default_cache_dir 目录推导 6 行」的明确指令；Task5 样式 import 已给正确路径；无 TBD。
3. **类型一致性**：`cluster_head_id`（Task1→4→6→8）；`qa_status` JSON 形态 `{state,voice,key}`（Task2→3→4）；`resolveClusterTarget/revertCluster/deriveGraph` 签名（Task4→5/6）；`QaCanvasView` props（Task5→7）已对齐。
