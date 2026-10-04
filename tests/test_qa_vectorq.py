"""VectorQ 每词条阈值生产端 + 同音挖掘二期（语义锚定）离线单测。

面：
- 列迁移：build_engine 两跑幂等补 ``qa_entries.hit_threshold``（真 SQLite 文件
  + 旧库手工建表升级路径）；
- repo 往返：InMemory/SQL 两实现 hit_threshold 读写（缺省 NULL、显式
  None=回全局、hit_count 计数器不可经 update 改）；
- 纯函数 ``mine_homophones_semantic``：金族（裴/赔）改写叠加出对、改写段
  不成对、异音不成对、sim<floor/缺席键不进、2v2 同音块、错位保守、
  support 去重与确定性排序、sim_floor 参数；
- 引擎一升二禁：repeat_after_play 0.80→0.83→…→0.95 顶格仍犯→禁用；
  当轮刚升不回落；
- 引擎清白回落：被升词条窗口 fired>0 步降 0.02，触底 0.80 写 NULL；
  fired=0 不动；
- 引擎二期接线：桩 embed 向量 → (裴,赔) 入库（support≥2 闸）；embed 不可达
  → 二期跳过记 run error、一期照跑；``_merge_homophone_pairs`` support 取大；
  词条向量进程内缓存命中与问题变更失效。

全离线：embed 一律 monkeypatch 桩（零网络）；LLM 桩=junk 决策串（同
test_qa_digest_engine 姿势）；库=InMemory / SQLite 临时文件。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
# 占位值刻意用重复串（熵 < 3.5）：防密钥门禁 generic-api-key 误报（同族先例）。
os.environ.setdefault("BOK_JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")

ROOT = Path(__file__).resolve().parents[1]
for _p in ("packages/core", "packages/business-db"):
    _path = str(ROOT / _p)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pytest  # noqa: E402

from bok_voice_business_db.repository import (  # noqa: E402
    InMemoryBusinessRepository,
    SqlAlchemyBusinessRepository,
)
from bok_voice_core.qa_digest_policy import (  # noqa: E402
    mine_homophones,
    mine_homophones_semantic,
)

from control_plane import deps as cp_deps  # noqa: E402
from control_plane import qa_cluster as qa_cluster_mod  # noqa: E402
from control_plane import qa_digest as qd  # noqa: E402

_SEQ = iter(range(200_000, 300_000))
_PW = "Passw0rd!"
_ACC = "acc-001"
_BASE = datetime(2026, 9, 20, 8, 0, 0, tzinfo=timezone.utc)


def _ts(minute: int) -> str:
    """固定基准+分钟偏移的 naive UTC ISO（固定宽度，字典序=时间序）。"""
    return (_BASE + timedelta(minutes=minute)).replace(tzinfo=None).isoformat()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """引擎模块态跨测试隔离：内存表/存储绑定/计划缓存/embed 缓存/env 全清。"""
    for env in ("BOK_QA_AUTO_DIGEST", "BOK_QA_DIGEST_INTERVAL_S", "BOK_CP_PUBLIC_URL",
                "MLX_LLM_BASE_URL", "BOK_QA_CLUSTER_MODEL", "BOK_EMBED_BASE_URL"):
        monkeypatch.delenv(env, raising=False)
    qd._MEM_HOMOPHONES.clear()
    qd._MEM_RUNS.clear()
    qd._STORAGE["factory"] = None
    qd.reset_policy_cache()
    qd.reset_embed_cache()
    qa_cluster_mod._plan_cache.clear()
    qa_cluster_mod._RUNNING = False
    yield
    qd._MEM_HOMOPHONES.clear()
    qd._MEM_RUNS.clear()
    qd._STORAGE["factory"] = None
    qd.reset_policy_cache()
    qd.reset_embed_cache()
    qa_cluster_mod._plan_cache.clear()
    qa_cluster_mod._RUNNING = False


# ---- 造数（与 test_qa_digest_engine 同款姿势） ----


def _turn(cid, role, text, *, speaker="", gen="", lang="zh"):
    from bok_voice_core.types import TurnEvent

    n = next(_SEQ)
    return TurnEvent(
        trace_id=cid, call_id=cid, turn_id=f"t{n}", role=role, transcript=text,
        language=lang, created_at=_ts(n), line="a",
        speaker=speaker, gen=gen,
    )


def _seed_calls(repo, plan, *, account=_ACC):
    """plan=[(question, answer, n_calls, lang)] → 真实对象+通话+两轮 turns。"""
    from bok_voice_core.types import CallMode, SessionManifest

    obj_id = repo.create_object(account, {"display_name": "陈大文", "phone": "+85200000001"})["id"]
    cids = []
    for question, answer, n_calls, lang in plan:
        for _ in range(n_calls):
            cid = f"call-vtq-{next(_SEQ)}"
            repo.create_call(SessionManifest(
                session_id=cid, account_id=account, object_id=obj_id, persona_id="",
                mode=CallMode.LIVE, direction="outbound", language=lang, providers={},
            ))
            repo.update_call(cid, created_at=_ts(next(_SEQ) % 50_000))
            repo.create_turn(_turn(cid, "user", question, speaker="customer", lang=lang))
            repo.create_turn(_turn(cid, "assistant", answer, speaker="agent_ai", lang=lang))
            cids.append(cid)
    return cids


def _seed_repeat_call(repo, question, canned, llm_reply):
    """一通「问 → 播快答 → 又问同一条 → 走 LLM」的通话（repeat_after_play 铁证）。"""
    from bok_voice_core.types import CallMode, SessionManifest

    obj_id = repo.create_object(_ACC, {"display_name": "陈大文", "phone": "+85200000001"})["id"]
    cid = f"call-vtq-{next(_SEQ)}"
    repo.create_call(SessionManifest(
        session_id=cid, account_id=_ACC, object_id=obj_id, persona_id="",
        mode=CallMode.LIVE, direction="outbound", language="zh", providers={},
    ))
    repo.update_call(cid, created_at=_ts(next(_SEQ) % 50_000))
    repo.create_turn(_turn(cid, "user", question, speaker="customer"))
    repo.create_turn(_turn(cid, "assistant", canned, speaker="agent_ai", gen="qa_fastpath"))
    repo.create_turn(_turn(cid, "user", question, speaker="customer"))
    repo.create_turn(_turn(cid, "assistant", llm_reply, speaker="agent_ai", gen="llm"))
    return cid


def _seed_healthy_call(repo, question, canned):
    """一通「问 → 快答播出去、没被复问」的通话（清白命中=fired>0 且回落证据）。"""
    from bok_voice_core.types import CallMode, SessionManifest

    obj_id = repo.create_object(_ACC, {"display_name": "陈大文", "phone": "+85200000001"})["id"]
    cid = f"call-vtq-{next(_SEQ)}"
    repo.create_call(SessionManifest(
        session_id=cid, account_id=_ACC, object_id=obj_id, persona_id="",
        mode=CallMode.LIVE, direction="outbound", language="zh", providers={},
    ))
    repo.update_call(cid, created_at=_ts(next(_SEQ) % 50_000))
    repo.create_turn(_turn(cid, "user", question, speaker="customer"))
    repo.create_turn(_turn(cid, "assistant", canned, speaker="agent_ai", gen="qa_fastpath"))
    return cid


def _entry(repo, question, answer, *, lang="zh", created_at="", enabled=True, **extra):
    payload = {
        "question_text": question,
        "answer_text": answer,
        "lang": lang,
        "account_id": _ACC,
        "enabled": enabled,
        "source": "curated",
        "created_at": created_at,
    }
    payload.update(extra)
    return repo.create_qa_entry(payload)


def _stub_llm(monkeypatch, decisions: str):
    """固定决策串；models 发现桩化（零网络）。"""
    monkeypatch.setattr(qa_cluster_mod, "_llm_chat", lambda *a, **kw: decisions)
    monkeypatch.setattr(qa_cluster_mod, "_discover_model", lambda base_url: "/models/fake-Qwen3-4B")


def _stub_embed_off(monkeypatch):
    """embed 不可达桩：任何调用 → None（二期降级路径）。"""
    monkeypatch.setattr(qd, "_embed_vectors", lambda *a, **kw: None)


def _stub_embed_vectors(monkeypatch, vec_map, default=None):
    """向量查表桩：texts 逐条回 vec_map[t]（缺省向量兜底），并计调用文本数。"""
    calls = {"texts": 0}

    def fake(texts, *, base_url="", timeout=0.0):
        out = []
        for t in texts:
            calls["texts"] += 1
            out.append(list(vec_map.get(t, default or [0.0, 0.0, 1.0])))
        return out

    monkeypatch.setattr(qd, "_embed_vectors", fake)
    return calls


def _audit_log():
    events: list[dict] = []

    def audit(action, **kw):
        events.append({"action": action, **kw})
        return {"action": action}

    return events, audit


def _run(repo, audit_fn):
    return asyncio.run(qd.run_digest_once(repo=repo, audit=audit_fn, live_count=lambda: 0))


# ---- 列迁移：两跑幂等 + 旧库升级 ----


def test_hit_threshold_migration_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "vtq-mig.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    cp_deps.build_engine()
    cp_deps.build_engine()  # 二跑：列已在，幂等跳过不报错

    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(qa_entries)")}
        assert "hit_threshold" in cols
    finally:
        conn.close()


def test_hit_threshold_migration_upgrades_legacy_db(tmp_path, monkeypatch):
    """旧库（手工建的无新列 qa_entries）→ build_engine 补列，两跑幂等。"""
    db = tmp_path / "vtq-legacy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE qa_entries (id VARCHAR(64) PRIMARY KEY, question_text TEXT, answer_text TEXT)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    cp_deps.build_engine()
    cp_deps.build_engine()

    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(qa_entries)")}
        assert "hit_threshold" in cols
    finally:
        conn.close()


# ---- repo 往返：InMemory / SQL ----


def test_inmemory_repo_hit_threshold_roundtrip():
    repo = InMemoryBusinessRepository()
    row = repo.create_qa_entry({
        "question_text": "赔几多", "answer_text": "核实后答复。", "lang": "zh", "account_id": _ACC,
    })
    assert row["hit_threshold"] is None, "缺省=NULL（=全局默认档）"
    assert repo.get_qa_entry(row["id"])["hit_threshold"] is None

    repo.update_qa_entry(row["id"], {"hit_threshold": 0.83})
    assert repo.get_qa_entry(row["id"])["hit_threshold"] == pytest.approx(0.83)
    assert repo.list_qa_entries(_ACC)[0]["hit_threshold"] == pytest.approx(0.83)

    repo.update_qa_entry(row["id"], {"hit_threshold": None})
    assert repo.get_qa_entry(row["id"])["hit_threshold"] is None, "显式 None=回全局默认档"

    # hit_count 是计数器（只走 incr，update 白名单外）；hit_threshold 普通字段可写
    repo.incr_qa_hit(row["id"], 3)
    repo.update_qa_entry(row["id"], {"hit_count": 99, "hit_threshold": 0.86})
    got = repo.get_qa_entry(row["id"])
    assert got["hit_count"] == 3, "hit_count 不可经 update 篡改"
    assert got["hit_threshold"] == pytest.approx(0.86)

    # 建单显式带值
    row2 = repo.create_qa_entry({
        "question_text": "几时送到", "answer_text": "两到三日。", "lang": "zh",
        "account_id": _ACC, "hit_threshold": 0.89,
    })
    assert row2["hit_threshold"] == pytest.approx(0.89)


def test_sql_repo_hit_threshold_roundtrip(tmp_path, monkeypatch):
    db = tmp_path / "vtq-sql.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    engine = cp_deps.build_engine()
    from sqlalchemy.orm import sessionmaker

    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    repo = SqlAlchemyBusinessRepository(maker())
    try:
        row = repo.create_qa_entry({
            "question_text": "赔几多", "answer_text": "核实后答复。", "lang": "zh",
            "account_id": _ACC, "hit_threshold": 0.83,
        })
        assert row["hit_threshold"] == pytest.approx(0.83)
        assert repo.get_qa_entry(row["id"])["hit_threshold"] == pytest.approx(0.83)

        bare = repo.create_qa_entry({
            "question_text": "几时送到", "answer_text": "两到三日。", "lang": "zh", "account_id": _ACC,
        })
        assert bare["hit_threshold"] is None, "SQL 建单缺省 NULL"

        repo.update_qa_entry(row["id"], {"hit_threshold": 0.86})
        assert repo.list_qa_entries(_ACC)[0]["hit_threshold"] == pytest.approx(0.86)

        repo.update_qa_entry(row["id"], {"hit_threshold": None})
        assert repo.get_qa_entry(row["id"])["hit_threshold"] is None, "显式 None=回全局"
    finally:
        repo.close()


# ---- 纯函数：mine_homophones_semantic（语义锚定） ----


def test_semantic_golden_pei_family_yields_pair():
    """金族用例：改写+同音叠加（整句距离>1，一期治不动）→ 语义锚定出 (裴,赔)。"""
    out = mine_homophones_semantic(
        [{"question": "我想先问下裴几多", "count": 4}],
        [{"id": "e1", "question_text": "可以点样赔"}],
        {("我想先问下裴几多", "e1"): 0.8},
    )
    assert out == [{"wrong": "裴", "right": "赔", "support": 1, "example": "我想先问下裴几多"}]


def test_semantic_rewrite_segment_yields_no_extra_pairs():
    """改写段（「我想先问下」vs「可以点样」）长段不成对：输出只含 (裴,赔) 一对。"""
    out = mine_homophones_semantic(
        [{"question": "我想先问下裴几多", "count": 4}],
        [{"id": "e1", "question_text": "可以点样赔"}],
        {("我想先问下裴几多", "e1"): 0.8},
    )
    assert {(p["wrong"], p["right"]) for p in out} == {("裴", "赔")}


def test_semantic_diff_pinyin_replacement_not_paired():
    """对齐段异音（钟 zhong / 赔 pei）不成对。"""
    out = mine_homophones_semantic(
        [{"question": "可以点样钟", "count": 3}],
        [{"id": "e1", "question_text": "可以点样赔"}],
        {("可以点样钟", "e1"): 0.9},
    )
    assert out == []


def test_semantic_two_by_two_homophone_block():
    """2v2 同音块（陪尝/赔偿）逐位取对：两对都出。"""
    out = mine_homophones_semantic(
        [{"question": "陪尝几多", "count": 2}],
        [{"id": "e1", "question_text": "赔偿几多"}],
        {("陪尝几多", "e1"): 0.9},
    )
    assert {(p["wrong"], p["right"]) for p in out} == {("陪", "赔"), ("尝", "偿")}


def test_semantic_misaligned_homophone_conservative():
    """错位同音（尝陪 vs 赔偿）：只取对齐到 equal 块的 (尝,偿)，陪/赔 不成对。"""
    out = mine_homophones_semantic(
        [{"question": "尝陪几多", "count": 2}],
        [{"id": "e1", "question_text": "赔偿几多"}],
        {("尝陪几多", "e1"): 0.9},
    )
    assert {(p["wrong"], p["right"]) for p in out} == {("尝", "偿")}


def test_semantic_sim_below_floor_and_missing_key_excluded():
    q = "我想先问下裴几多"
    entries = [{"id": "e1", "question_text": "可以点样赔"}]
    assert mine_homophones_semantic(
        [{"question": q, "count": 3}], entries, {(q, "e1"): 0.74}
    ) == [], "sim<floor 不进"
    assert mine_homophones_semantic([{"question": q, "count": 3}], entries, {}) == [], "缺席键不进"


def test_semantic_sim_floor_param_tightens():
    q = "我想先问下裴几多"
    entries = [{"id": "e1", "question_text": "可以点样赔"}]
    sims = {(q, "e1"): 0.78}
    assert mine_homophones_semantic(
        [{"question": q, "count": 3}], entries, sims
    ), "0.78 ≥ 默认 0.75 → 出对"
    assert mine_homophones_semantic(
        [{"question": q, "count": 3}], entries, sims, sim_floor=0.8
    ) == [], "sim_floor=0.8 收紧 → 不进"


def test_semantic_count1_miss_and_identical_norm_excluded():
    entries = [{"id": "e1", "question_text": "可以点样赔"}]
    assert mine_homophones_semantic(
        [{"question": "我想先问下裴几多", "count": 1}], entries,
        {("我想先问下裴几多", "e1"): 0.9},
    ) == [], "count<2=噪声嫌疑不看"
    assert mine_homophones_semantic(
        [{"question": "可以点样赔", "count": 9}], entries,
        {("可以点样赔", "e1"): 0.99},
    ) == [], "完全同形无替换可言"


def test_semantic_support_dedup_order_and_example():
    """support=去重 miss 归一形数；重复问法不重复计；(-support, wrong, right) 排序。"""
    misses = [
        {"question": "我想问下裴几多", "count": 3},
        {"question": "我想问下裴几多", "count": 9},  # 同问法去重
        {"question": "帮我睇下裴几多呀", "count": 2},
    ]
    entries = [{"id": "e1", "question_text": "可以点样赔"}]
    sims = {("我想问下裴几多", "e1"): 0.9, ("帮我睇下裴几多呀", "e1"): 0.85}
    out = mine_homophones_semantic(misses, entries, sims)
    assert out == [{
        "wrong": "裴", "right": "赔", "support": 2, "example": "我想问下裴几多",
    }]


def test_semantic_empty_inputs():
    assert mine_homophones_semantic([], [], {}) == []
    assert mine_homophones_semantic(
        [{"question": "我想先问下裴几多", "count": 3}], [], {("我想先问下裴几多", "e1"): 0.9}
    ) == []


def test_semantic_phase1_untouched_distance1_still_works():
    """一期 mine_homophones 契约不变：距离=1 纯同音照出（回归护栏）。"""
    out = mine_homophones(
        [{"question": "裴几多", "count": 3}],
        [{"id": "e1", "question_text": "赔几多"}],
    )
    assert out == [{"wrong": "裴", "right": "赔", "support": 1, "example": "裴几多"}]


# ---- 引擎：一升二禁（repeat_after_play → 阈值阶梯，顶格才禁用） ----


def test_vectorq_raise_then_disable_ladder(monkeypatch):
    repo = InMemoryBusinessRepository()
    e = _entry(repo, "幾時送到", "兩到三日。")
    _seed_repeat_call(repo, "幾時送到", "兩到三日。", "大概三日內到。")
    _stub_llm(monkeypatch, '[{"i":0,"decision":"junk","target":"","note":"无关"}]')
    _stub_embed_off(monkeypatch)  # 候选 miss 空（count<2），embed 不该被调；保险桩
    events, audit_fn = _audit_log()

    ladder = [0.83, 0.86, 0.89, 0.92, 0.95]
    for expected in ladder:
        out = _run(repo, audit_fn)
        assert out["skipped"] == ""
        assert out["threshold_raised"] == 1 and out["disabled"] == 0
        assert out["threshold_decayed"] == 0, "当轮刚升过不参与回落"
        row = repo.get_qa_entry(e["id"])
        assert row["enabled"] is True, "未顶格：只升不禁"
        assert row["hit_threshold"] == pytest.approx(expected)
        upd = [x for x in events if x["action"] == "qa_entry.update"]
        assert len(upd) == 1
        assert upd[0]["detail"]["source"] == "auto-digest"
        assert upd[0]["detail"]["reason"] == "repeat_after_play"
        assert upd[0]["detail"]["hit_threshold"]["new"] == pytest.approx(expected)
        events.clear()

    # 顶格 0.95 仍 repeat → 才禁用（一升二禁的「二」）
    out = _run(repo, audit_fn)
    assert out["disabled"] == 1 and out["threshold_raised"] == 0
    row = repo.get_qa_entry(e["id"])
    assert row["enabled"] is False
    assert row["hit_threshold"] == pytest.approx(0.95), "禁用不动阈值（可逆，不删）"
    upd = [x for x in events if x["action"] == "qa_entry.update"]
    assert len(upd) == 1 and upd[0]["detail"]["enabled"] is False
    assert upd[0]["detail"]["hit_threshold"]["old"] == pytest.approx(0.95)

    # 幂等：已禁用行不再体检
    events.clear()
    out = _run(repo, audit_fn)
    assert out["disabled"] == 0 and out["threshold_raised"] == 0
    assert not [x for x in events if x["action"] == "qa_entry.update"]


def test_vectorq_raise_skips_unhealthy_entries_still_gated(monkeypatch):
    """非 repeat 类提案语义不变：never_asked 新词条（龄 0d）仍被龄门拦下。"""
    repo = InMemoryBusinessRepository()
    _seed_calls(repo, [])  # 只立账号，零候选
    _entry(repo, "会收手续费吗", "不收。")  # occ=0 且龄 0d → never_asked 龄门拦下
    _stub_llm(monkeypatch, '[{"i":0,"decision":"junk","target":"","note":"无关"}]')
    _stub_embed_off(monkeypatch)
    events, audit_fn = _audit_log()

    out = _run(repo, audit_fn)
    assert out["disabled"] == 0 and out["threshold_raised"] == 0
    assert repo.list_qa_entries(_ACC)[0]["enabled"] is True
    assert not [x for x in events if x["action"] == "qa_entry.update"]


# ---- 引擎：清白命中回落（步降 0.02，触底写 NULL） ----


def test_vectorq_decay_on_clean_hits_and_null_at_floor(monkeypatch):
    repo = InMemoryBusinessRepository()
    e = _entry(repo, "幾時送到", "兩到三日。")
    repo.update_qa_entry(e["id"], {"hit_threshold": 0.86})
    _seed_healthy_call(repo, "幾時送到", "兩到三日。")
    _stub_llm(monkeypatch, '[{"i":0,"decision":"junk","target":"","note":"无关"}]')
    _stub_embed_off(monkeypatch)
    events, audit_fn = _audit_log()

    # 0.86 → 0.84（fired=1 清白命中）
    out = _run(repo, audit_fn)
    assert out["threshold_decayed"] == 1 and out["threshold_raised"] == 0 and out["disabled"] == 0
    row = repo.get_qa_entry(e["id"])
    assert row["enabled"] is True
    assert row["hit_threshold"] == pytest.approx(0.84)
    upd = [x for x in events if x["action"] == "qa_entry.update"]
    assert len(upd) == 1
    assert upd[0]["detail"]["reason"] == "clean_hit_decay"
    assert upd[0]["detail"]["fired"] == 1
    assert upd[0]["detail"]["hit_threshold"] == {"old": 0.86, "new": 0.84}
    events.clear()

    # 0.81 → 触底（0.79 ≤ 0.80）→ 写 NULL 回全局档
    repo.update_qa_entry(e["id"], {"hit_threshold": 0.81})
    out = _run(repo, audit_fn)
    assert out["threshold_decayed"] == 1
    assert repo.get_qa_entry(e["id"])["hit_threshold"] is None, "回到全局档写 NULL"
    upd = [x for x in events if x["action"] == "qa_entry.update"]
    assert len(upd) == 1 and upd[0]["detail"]["hit_threshold"]["new"] is None

    # fired=0 的被升词条不动（窗口内没人问出清白命中）
    e2 = _entry(repo, "可以上门取件吗", "可以。")  # occ=0 龄 0d → never_asked 龄门拦下
    repo.update_qa_entry(e2["id"], {"hit_threshold": 0.86})
    events.clear()
    out = _run(repo, audit_fn)
    assert out["threshold_decayed"] == 0
    assert repo.get_qa_entry(e2["id"])["hit_threshold"] == pytest.approx(0.86)
    assert not [x for x in events if x["action"] == "qa_entry.update"]


# ---- 引擎：二期语义锚定接线（桩 embed / 不可达降级 / 合并 / 缓存） ----


def test_digest_semantic_homophone_learns_pair(monkeypatch):
    """桩向量：两个 miss 与词条语义同族（cos≈1）→ 二期出 (裴,赔) support=2 入库。"""
    repo = InMemoryBusinessRepository()
    e = _entry(repo, "可以点样赔", "理赔流程如下。")
    _entry(repo, "你们是哪家公司", "我们是集运中转仓。")  # 语义远亲（向量正交）
    _seed_calls(repo, [
        ("我想问下裴几多", "帮您查。", 2, "zh"),
        ("帮我睇下裴几多层收费", "稍等。", 2, "zh"),
    ])
    _stub_llm(monkeypatch, (
        '[{"i":0,"decision":"junk","target":"","note":"无关"},'
        '{"i":1,"decision":"junk","target":"","note":"无关"}]'
    ))
    _stub_embed_vectors(monkeypatch, {
        "我想问下裴几多": [1.0, 0.0],
        "帮我睇下裴几多层收费": [1.0, 0.0],
        "可以点样赔": [1.0, 0.0],
        "你们是哪家公司": [0.0, 1.0],
    })
    events, audit_fn = _audit_log()

    out = _run(repo, audit_fn)

    assert out["homophones"] == 1, "一期词面（距离>1）零贡献，全部来自二期"
    assert not [x for x in out["errors"] if "homophone" in x]
    rows = qd.list_homophones()
    assert len(rows) == 1
    assert rows[0]["wrong"] == "裴" and rows[0]["right"] == "赔"
    assert rows[0]["support"] == 2, "两个去重 miss 问法支持同一对"
    learn = next(x for x in events if x["action"] == "qa.homophone_learn")
    assert learn["detail"]["pairs"] == 1
    assert qd.list_runs()[0]["homophones"] == 1
    assert e["id"], "词条在库（占位断言保持行引用）"


def test_digest_embed_unavailable_degrades_to_phase1(monkeypatch):
    """embed 不可达：二期跳过记 run error，一期词面路径照跑，整轮不炸。"""
    repo = InMemoryBusinessRepository()
    _entry(repo, "点解仲未送到", "帮我查一下。")
    _entry(repo, "仲未送到", "请稍等。")
    _seed_calls(repo, [
        ("我想问下裴几多", "帮您查。", 2, "zh"),   # 二期候选（词面距离>1 一期也治不了）
        ("点解钟未送到", "帮你催一下。", 2, "zh"),  # 一期候选（距离=1 钟/仲同音）
        ("钟未送到", "马上去查。", 2, "zh"),        # 一期第二个支持 miss（support 凑 2）
    ])
    _stub_llm(monkeypatch, (
        '[{"i":0,"decision":"junk","target":"","note":"无关"},'
        '{"i":1,"decision":"junk","target":"","note":"无关"},'
        '{"i":2,"decision":"junk","target":"","note":"无关"}]'
    ))
    _stub_embed_off(monkeypatch)
    events, audit_fn = _audit_log()

    out = _run(repo, audit_fn)

    assert out["homophones"] == 1, "一期（钟/仲 距离=1 同音）照常入库"
    rows = qd.list_homophones()
    assert rows[0]["wrong"] == "钟" and rows[0]["right"] == "仲"
    assert any(
        x.startswith("homophone-semantic(") and "embed unavailable" in x
        for x in out["errors"]
    ), "二期降级记因"
    assert "homophone-semantic" in qd.list_runs()[0]["error"]
    learn = next(x for x in events if x["action"] == "qa.homophone_learn")
    assert learn["detail"]["pairs"] == 1


def test_merge_homophone_pairs_takes_max_support():
    merged = qd._merge_homophone_pairs(
        [{"wrong": "钟", "right": "仲", "support": 2, "example": "a"}],
        [
            {"wrong": "钟", "right": "仲", "support": 4, "example": "b"},
            {"wrong": "裴", "right": "赔", "support": 2, "example": "c"},
        ],
    )
    assert merged == [
        {"wrong": "钟", "right": "仲", "support": 4, "example": "a"},
        {"wrong": "裴", "right": "赔", "support": 2, "example": "c"},
    ], "同对合并 support 取大、example 取先到；(-support, wrong, right) 排序"
    assert qd._merge_homophone_pairs([], [], None) == []


def test_entry_vector_cache_hit_and_invalidation(monkeypatch):
    """词条向量进程内缓存：命中不重复请求；问题文本变更 → 键变 → 重取。"""
    calls = _stub_embed_vectors(monkeypatch, {})
    entries = [
        {"id": "e1", "question_text": "赔几多"},
        {"id": "e2", "question_text": "几时送到"},
    ]
    v1 = qd._entry_vectors(entries)
    assert v1 is not None and len(v1) == 2
    assert calls["texts"] == 2
    v2 = qd._entry_vectors(entries)
    assert v2 is not None and calls["texts"] == 2, "缓存命中零新请求"
    qd._entry_vectors([{"id": "e1", "question_text": "赔几多啊"}])
    assert calls["texts"] == 3, "问题变了键就变 → 重取"
    # 失败值不进缓存：embed 挂了 → None；恢复后能重试成功
    qd.reset_embed_cache()
    monkeypatch.setattr(qd, "_embed_vectors", lambda *a, **k: None)
    assert qd._entry_vectors(entries) is None
    calls2 = _stub_embed_vectors(monkeypatch, {})
    assert qd._entry_vectors(entries) is not None
    assert calls2["texts"] == 2, "失败不留缓存，恢复后全量重取"


def test_embed_base_url_env_override(monkeypatch):
    monkeypatch.delenv("BOK_EMBED_BASE_URL", raising=False)
    assert qd.embed_base_url() == "http://127.0.0.1:8789", "缺省本机 bge 侧车"
    monkeypatch.setenv("BOK_EMBED_BASE_URL", "http://10.0.0.8:8789/")
    assert qd.embed_base_url() == "http://10.0.0.8:8789", "env 覆盖且剥尾斜杠"


def test_cosine_zero_vector_safe():
    assert qd._cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert qd._cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert qd._cosine([0.0, 0.0], [1.0, 1.0]) == 0.0, "零向量按不相似，绝不出 NaN"


def test_semantic_mining_skips_when_policy_lacks_function(monkeypatch):
    """策略层旧版（无 mine_homophones_semantic）→ 静默跳过零 error（并行开发兼容）。"""
    repo = InMemoryBusinessRepository()
    _entry(repo, "可以点样赔", "理赔流程如下。")
    _seed_calls(repo, [("我想问下裴几多", "帮您查。", 2, "zh")])
    _stub_llm(monkeypatch, '[{"i":0,"decision":"junk","target":"","note":"无关"}]')

    class _OldPolicy:
        mine_homophones = staticmethod(mine_homophones)

    pairs, err = qd._mine_semantic_homophones(_OldPolicy(), [], [])
    assert pairs == [] and err == "", "无二期函数 → 空对零因"
