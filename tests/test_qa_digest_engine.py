"""QA 自沉淀引擎（CP 闲时循环，2026-09-25）离线单测——零 LLM 零真库。

面：策略联动（真策略层 bok_voice_core.qa_digest_policy 在场联跑；LLM 桩=
monkeypatch runner 的 _llm_chat/_discover_model，照 test_qa_cluster_cp 同款）/
LLM 不可用（请求链路桩抛 ConnectError → ClusterError → 聚类跳过但 drift/同音
照跑）/ 策略层缺席降级（monkeypatch 引擎的惰性解析点，不玩 sys.modules——真
模块已合并后 import 机制撞属性缓存不可靠）/ 敏感 fresh 落 pending / drift 三类
自动禁用+never_asked 龄门+幂等 / 同音 UPSERT support 取大 / 闲时闸 busy 零动作 /
env 默认关 digest_loop 空转 / build_engine 两跑幂等建表 / SQL 存储面回环 /
GET /api/stats/qa-digest 管理面闸。

造数姿势与 test_qa_drift.py 同款：DATABASE_URL="" 强制内存仓 + 注入 repo/
audit/live_count 直调 run_digest_once（不经 HTTP、不反查 main）。同音世界按真
策略判据设计：编辑距离=1、单字差异、pinyin 同音（钟/仲 zhong）。
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

from bok_voice_business_db.repository import InMemoryBusinessRepository  # noqa: E402
from bok_voice_core.qa_text import normalize_question  # noqa: E402

from control_plane import deps as cp_deps  # noqa: E402
from control_plane import pregen as pregen_mod  # noqa: E402
from control_plane import qa_cluster as qa_cluster_mod  # noqa: E402
from control_plane import qa_digest as qd  # noqa: E402

_SEQ = iter(range(1, 100_000))
_PW = "Passw0rd!"
_ACC = "acc-001"
_BASE = datetime(2026, 9, 20, 8, 0, 0, tzinfo=timezone.utc)


def _ts(minute: int) -> str:
    """固定基准+分钟偏移的 naive UTC ISO（固定宽度，字典序=时间序）。"""
    return (_BASE + timedelta(minutes=minute)).replace(tzinfo=None).isoformat()


def _days_ago_iso(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None).isoformat()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """引擎模块态跨测试隔离：内存表/存储绑定/计划缓存/env 全清。"""
    for env in ("BOK_QA_AUTO_DIGEST", "BOK_QA_DIGEST_INTERVAL_S", "BOK_CP_PUBLIC_URL",
                "MLX_LLM_BASE_URL", "BOK_QA_CLUSTER_MODEL"):
        monkeypatch.delenv(env, raising=False)
    qd._MEM_HOMOPHONES.clear()
    qd._MEM_RUNS.clear()
    qd._STORAGE["factory"] = None
    qd.reset_policy_cache()
    qa_cluster_mod._plan_cache.clear()
    qa_cluster_mod._RUNNING = False
    yield
    qd._MEM_HOMOPHONES.clear()
    qd._MEM_RUNS.clear()
    qd._STORAGE["factory"] = None
    qd.reset_policy_cache()
    qa_cluster_mod._plan_cache.clear()
    qa_cluster_mod._RUNNING = False


# ---- 造数 ----


def _turn(cid, role, text, *, speaker="", gen="", lang="zh"):
    from bok_voice_core.types import TurnEvent

    n = next(_SEQ)
    return TurnEvent(
        trace_id=cid, call_id=cid, turn_id=f"t{n}", role=role, transcript=text,
        language=lang, created_at=_ts(n), line="a",
        speaker=speaker, gen=gen,
    )


def _seed_calls(repo, plan, *, stamp=None):
    """plan=[(question, answer, n_calls, lang)] → 真实对象+通话+两轮 turns。

    对象名避开 clean-testdata 前缀族；通话 created_at 显式补齐（内存仓
    create_call 不写 created_at，水位过滤按字典序比较需要它）。stamp 可指定
    通话时间戳分钟偏移（大偏移=把通话顶到水位之后，供幂等二跑复 mining）。
    """
    from bok_voice_core.types import CallMode, SessionManifest

    obj_id = repo.create_object(_ACC, {"display_name": "陈大文", "phone": "+85200000001"})["id"]
    cids = []
    for question, answer, n_calls, lang in plan:
        for _ in range(n_calls):
            cid = f"call-digest-{next(_SEQ)}"
            repo.create_call(SessionManifest(
                session_id=cid, account_id=_ACC, object_id=obj_id, persona_id="",
                mode=CallMode.LIVE, direction="outbound", language=lang, providers={},
            ))
            repo.update_call(cid, created_at=_ts(stamp if stamp is not None else next(_SEQ) % 50_000))
            repo.create_turn(_turn(cid, "user", question, speaker="customer", lang=lang))
            repo.create_turn(_turn(cid, "assistant", answer, speaker="agent_ai", lang=lang))
            cids.append(cid)
    return cids


def _entry(repo, question, answer, *, lang="zh", created_at="", enabled=True, source="curated"):
    return repo.create_qa_entry({
        "question_text": question,
        "answer_text": answer,
        "lang": lang,
        "account_id": _ACC,
        "enabled": enabled,
        "source": source,
        "created_at": created_at,
    })


def _stub_llm(monkeypatch, decisions: str):
    """固定决策串；models 发现桩化（零网络）。"""
    monkeypatch.setattr(qa_cluster_mod, "_llm_chat", lambda *a, **kw: decisions)
    monkeypatch.setattr(qa_cluster_mod, "_discover_model", lambda base_url: "/models/fake-Qwen3-4B")


def _audit_log():
    events: list[dict] = []

    def audit(action, **kw):
        events.append({"action": action, **kw})
        return {"action": action}

    return events, audit


# ---- 策略联动：variant/fresh 采纳 + 幂等二跑 + 审计 + pregen ----


def test_digest_adopts_variant_fresh_and_is_idempotent(monkeypatch):
    repo = InMemoryBusinessRepository()
    target = _entry(repo, "你们是哪家公司", "我们是集运中转仓。")
    # stamp=90_000 分钟（≈62 天后）把通话顶到首轮水位之后——二跑才会重新 mining，
    # 幂等断言考的是「同问法已在库」双闸，而非水位把数据挡掉。
    _seed_calls(repo, [
        ("请问你们是哪间公司的呀？", "我们是集运中转仓。", 6, "zh"),          # → variant
        ("点解仲未到我件货", "我帮你催一下。", 3, "zh"),                      # → junk
        ("how long does shipping take", "three to five days.", 3, "en"),      # → auto_fresh
    ], stamp=90_000)
    # zh 批行序（-calls, question）：i0=请问…、i1=点解…；en 批无词条 → fresh 旁路零 LLM
    _stub_llm(monkeypatch, (
        '[{"i":0,"decision":"variant","target":"%s","note":"同义"},'
        '{"i":1,"decision":"junk","target":"","note":"无关"}]' % target["id"]
    ))
    events, audit_fn = _audit_log()
    pregen_calls: list[list[str]] = []

    def fake_pregen(base_url, ids):
        pregen_calls.append(list(ids))
        return {"status": "queued"}

    monkeypatch.setattr(pregen_mod, "qa_pregen_spawn", fake_pregen)

    out = asyncio.run(qd.run_digest_once(repo=repo, audit=audit_fn, live_count=lambda: 0))

    # 采纳面：variant 入库字段与 /api/qa/cluster apply 同款 + source 盖章
    assert out["skipped"] == "" and out["adopted_variant"] == 1 and out["adopted_fresh"] == 1
    rows = [e for e in repo.list_qa_entries(_ACC) if e["id"] not in (target["id"],)]
    variant = next(e for e in rows if e["lang"] == "zh")
    assert variant["question_text"] == normalize_question("请问你们是哪间公司的呀？")
    assert variant["answer_text"] == "我们是集运中转仓。", "答案继承目标词条，防漂移"
    assert variant["cluster_head_id"] == target["id"]
    assert variant["source"] == "auto-digest"
    assert variant["account_id"] == _ACC and variant["owner_user_id"] == ""
    assert variant["enabled"] is True and variant["scope"] == "global"
    assert variant["priority"] == 10
    fresh = next(e for e in rows if e["lang"] == "en")
    assert fresh["question_text"] == normalize_question("how long does shipping take")
    assert fresh["answer_text"] == "three to five days."
    assert fresh["source"] == "auto-digest" and fresh["cluster_head_id"] == ""

    # 同音面：本世界的 miss 与词条词面距离远（真策略=编辑距离 1 才出对）→ 零对子
    assert out["homophones"] == 0 and qd.list_homophones() == []
    assert out["pending"] == 0, "非敏感 fresh 直接入库不落 pending"

    # 审计面：逐条 qa_entry.create + qa.auto_adopt 汇总（一次，含两类计数）
    actions = [e["action"] for e in events]
    assert actions.count("qa_entry.create") == 2
    adopt = next(e for e in events if e["action"] == "qa.auto_adopt")
    assert adopt["detail"]["variant"] == 1 and adopt["detail"]["fresh"] == 1
    assert sorted(adopt["detail"]["ids"]) == sorted([variant["id"], fresh["id"]])

    # pregen 面：有采纳才触发，子进程姿势与 POST /api/qa/pregen 同一入口
    assert sorted(pregen_calls[0]) == sorted([variant["id"], fresh["id"]])
    assert out["pregen"] == "queued"

    # run 行：计数落库 + finished_at 成为水位
    runs = qd.list_runs()
    assert len(runs) == 1 and runs[0]["adopted_variant"] == 1 and runs[0]["adopted_fresh"] == 1
    assert runs[0]["pregen"] == 2 and qd.last_watermark() == runs[0]["finished_at"]

    # 幂等二跑：同 question+lang 已在库（find_existing + plan dup-existing 双闸）
    events.clear()
    out2 = asyncio.run(qd.run_digest_once(repo=repo, audit=audit_fn, live_count=lambda: 0))
    assert out2["adopted_variant"] == 0 and out2["adopted_fresh"] == 0
    assert len(repo.list_qa_entries(_ACC)) == 3, "二跑零新增"
    assert not [e for e in events if e["action"] == "qa.auto_adopt"]


def test_digest_sensitive_fresh_goes_pending_not_db(monkeypatch):
    """敏感域 fresh（答案带金额词）→ 策略层落 pending，不入库（宁误禁勿误播）。"""
    repo = InMemoryBusinessRepository()
    _entry(repo, "你们是哪家公司", "我们是集运中转仓。")
    _seed_calls(repo, [
        ("can i get a refund", "you will get 500 dollars back.", 3, "en"),
    ])
    # en 无词条 → fresh 旁路零 LLM；models 发现桩化即可
    monkeypatch.setattr(qa_cluster_mod, "_discover_model", lambda base_url: "/models/fake-4B")
    events, audit_fn = _audit_log()

    out = asyncio.run(qd.run_digest_once(repo=repo, audit=audit_fn, live_count=lambda: 0))

    assert out["adopted_variant"] == 0 and out["adopted_fresh"] == 0
    assert out["pending"] == 1
    assert len(repo.list_qa_entries(_ACC)) == 1, "敏感候选不落库"
    assert not [e for e in events if e["action"].startswith("qa.")]


def test_digest_learns_homophone_pair_by_real_policy(monkeypatch):
    """同音学习（真策略判据）：miss 与词条问法编辑距离=1、单字差异、钟/仲同音。

    两个不同 miss 问法支持同一 (钟→仲) 对 → support=2 过引擎下限 → UPSERT。
    """
    repo = InMemoryBusinessRepository()
    _entry(repo, "点解仲未送到", "帮我查一下。")
    _entry(repo, "仲未送到", "请稍等。")
    _seed_calls(repo, [
        ("点解钟未送到", "帮你催一下。", 2, "zh"),
        ("钟未送到", "马上去查。", 2, "zh"),
    ])
    _stub_llm(monkeypatch, (
        '[{"i":0,"decision":"junk","target":"","note":"无关"},'
        '{"i":1,"decision":"junk","target":"","note":"无关"}]'
    ))
    events, audit_fn = _audit_log()

    out = asyncio.run(qd.run_digest_once(repo=repo, audit=audit_fn, live_count=lambda: 0))

    assert out["adopted_variant"] == 0 and out["adopted_fresh"] == 0
    assert out["homophones"] == 1
    rows = qd.list_homophones()
    assert len(rows) == 1
    assert rows[0]["wrong"] == "钟" and rows[0]["right"] == "仲"
    assert rows[0]["support"] == 2
    learn = next(e for e in events if e["action"] == "qa.homophone_learn")
    assert learn["detail"]["pairs"] == 1
    assert qd.list_runs()[0]["homophones"] == 1


def test_digest_llm_unavailable_skips_adopt_but_drift_and_homophone_run(monkeypatch):
    """LLM 不可用：请求链路抛 ConnectError（monkeypatch 桩，零网络确定性）→
    ClusterError → 聚类采纳跳过，drift/同音步照跑，run 行照落（error 记因）。"""
    import httpx

    repo = InMemoryBusinessRepository()
    # 在库词条让 zh 批消息非空——聚类真的会出 LLM 请求（空词条语言会旁路 LLM）。
    target = _entry(repo, "你们是哪家公司", "我们是集运中转仓。")
    _seed_calls(repo, [("请问你们是哪间公司的呀？", "我们是集运中转仓。", 6, "zh")])
    monkeypatch.setattr(qa_cluster_mod, "_discover_model", lambda base_url: "/models/fake-Qwen3-4B")

    def boom(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(qa_cluster_mod, "_llm_chat", boom)
    events, audit_fn = _audit_log()

    out = asyncio.run(qd.run_digest_once(repo=repo, audit=audit_fn, live_count=lambda: 0))

    assert out["adopted_variant"] == 0 and out["adopted_fresh"] == 0
    assert any(e.startswith("cluster(") and "connection refused" in e for e in out["errors"])
    assert len(repo.list_qa_entries(_ACC)) == 1, "聚类失败零采纳，既有词条不动"
    assert qd.list_runs() and "cluster" in qd.list_runs()[0]["error"]
    # drift/同音步照跑的证据：本轮无 adopt/policy 类错误，词条未被 drift 误伤
    assert not any("adopt" in e or "policy" in e for e in out["errors"])
    assert repo.get_qa_entry(target["id"])["enabled"] is True, "never_asked 但龄 0d → 龄门拦下"


def test_digest_policy_missing_degrades_gracefully(monkeypatch):
    """策略层缺席：④⑥ 跳过记因，②③⑤⑦⑧ 照跑不炸循环。

    monkeypatch 引擎的惰性解析点（不玩 sys.modules——真模块已合并后 import
    机制撞包属性缓存，setitem None 不可靠）。
    """
    monkeypatch.setattr(qd, "policy_module", lambda: None)
    repo = InMemoryBusinessRepository()
    target = _entry(repo, "你们是哪家公司", "我们是集运中转仓。")
    _seed_calls(repo, [("请问你们是哪间公司的呀？", "我们是集运中转仓。", 6, "zh")])
    _stub_llm(monkeypatch, (
        '[{"i":0,"decision":"variant","target":"%s","note":"同义"}]' % target["id"]
    ))
    events, audit_fn = _audit_log()

    out = asyncio.run(qd.run_digest_once(repo=repo, audit=audit_fn, live_count=lambda: 0))

    assert out["adopted_variant"] == 0 and out["homophones"] == 0
    assert any("policy" in e for e in out["errors"])
    assert qd.list_runs(), "策略缺席不阻 run 行落库"


# ---- drift 自动禁用：三类 + 龄门 + 幂等 ----


def test_digest_drift_disables_three_classes_with_age_gate_and_idempotent(monkeypatch):
    repo = InMemoryBusinessRepository()
    _seed_calls(repo, [])  # 有账号（对象/通话在）但零候选问法
    # never_asked 老词条（龄 20d）→ 禁用
    e_old = _entry(repo, "会收手续费吗", "不收。", created_at=_days_ago_iso(20))
    # never_asked 新词条（龄 0d）→ 龄门拦下，不动
    e_young = _entry(repo, "可以上门取件吗", "可以。", created_at=_days_ago_iso(0))
    # digits_bypass（问法带 4 位数字 run，词条是死重）→ 禁用
    e_digits = _entry(repo, "查询单号1234", "请稍等。", created_at=_days_ago_iso(30))
    # repeat_after_play（播了快答客户又问）→ 一升二禁（2026-09-25 VectorQ 化）：
    # 首犯升阈值 0.80→0.83 不禁用，顶格 0.95 仍犯才禁
    e_repeat = _entry(repo, "幾時送到", "兩到三日。", created_at=_days_ago_iso(30))
    # never_fired（occ≥3 全走 LLM）→ 不自动（两成因分不清）
    e_neverfired = _entry(repo, "可以退换货吗", "七日内可以退换。", created_at=_days_ago_iso(30))
    # never_asked 老词条但终身命中过（2026-09-25 实弹补:hit=17 被窗口判死）→ 不自动
    e_lifethit = _entry(repo, "怎么赔给我", "核实后主动联系您办理。", created_at=_days_ago_iso(20))
    repo.incr_qa_hit(e_lifethit["id"], 17)  # hit_count 是计数器字段,走 incr 不走 update

    cid = _seed_calls(repo, [("可以退换货吗", "网上买的可以直接退。", 3, "zh")])[0]
    repo.create_turn(_turn(cid, "user", "幾時送到", speaker="customer"))
    repo.create_turn(_turn(cid, "assistant", "兩到三日。", speaker="agent_ai", gen="qa_fastpath"))
    repo.create_turn(_turn(cid, "user", "幾時送到", speaker="customer"))
    repo.create_turn(_turn(cid, "assistant", "大概三日內到。", speaker="agent_ai", gen="llm"))
    # digit 问法问 3 次全走 LLM（digits 判据优先于 never_fired）
    for _ in range(3):
        repo.create_turn(_turn(cid, "user", "查询单号1234", speaker="customer"))
        repo.create_turn(_turn(cid, "assistant", "帮您查。", speaker="agent_ai", gen="llm"))
    # 老/新词条的问法从未出现（occ=0）

    events, audit_fn = _audit_log()
    _stub_llm(monkeypatch, (
        '[{"i":0,"decision":"junk","target":"","note":"无关"},'
        '{"i":1,"decision":"junk","target":"","note":"无关"}]'
    ))
    out = asyncio.run(qd.run_digest_once(repo=repo, audit=audit_fn, live_count=lambda: 0))

    assert out["disabled"] == 2
    state = {e["id"]: repo.get_qa_entry(e["id"])["enabled"]
             for e in (e_old, e_young, e_digits, e_repeat, e_neverfired, e_lifethit)}
    assert state[e_old["id"]] is False, "never_asked 龄 20d → 禁用"
    assert state[e_young["id"]] is True, "never_asked 龄 0d < 14d → 不自动"
    assert state[e_lifethit["id"]] is True, "never_asked 但终身命中过 → 不自动（窗口 occ=0 ≠ 没用）"
    assert state[e_digits["id"]] is False, "digits_bypass → 禁用"
    assert state[e_repeat["id"]] is True, "repeat_after_play 首犯 → 一升（阈值 0.83）不禁用"
    assert out["threshold_raised"] == 1
    _thr = repo.get_qa_entry(e_repeat["id"])["hit_threshold"]
    assert _thr is not None and float(_thr) == pytest.approx(0.83)
    upd = [e for e in events if e["action"] == "qa_entry.update"]
    disables = [e for e in upd if e["detail"].get("enabled") is False]
    raises = [e for e in upd if "hit_threshold" in e["detail"]]
    assert len(disables) == 2 and len(raises) == 1
    assert all(e["detail"]["source"] == "auto-digest" for e in upd)
    assert raises[0]["detail"]["reason"] == "repeat_after_play"
    assert raises[0]["detail"]["hit_threshold"]["old"] == pytest.approx(0.80)
    assert raises[0]["detail"]["hit_threshold"]["new"] == pytest.approx(0.83)

    # 二跑：已禁用行不再体检（drift 只扫 enabled=True）→ 零新禁用；
    # repeat 词条窗口证据仍在 → 再升 0.83→0.86（一升二禁的「二」腿）。
    events.clear()
    out2 = asyncio.run(qd.run_digest_once(repo=repo, audit=audit_fn, live_count=lambda: 0))
    assert out2["disabled"] == 0
    assert out2["threshold_raised"] == 1
    _thr2 = repo.get_qa_entry(e_repeat["id"])["hit_threshold"]
    assert _thr2 is not None and float(_thr2) == pytest.approx(0.86)
    upd2 = [e for e in events if e["action"] == "qa_entry.update"]
    assert len(upd2) == 1 and "hit_threshold" in upd2[0]["detail"]


def test_entry_age_days_parsing():
    """龄解析：缺失/坏值按 0（never_asked 需要「确定够老」，证据不足保守不动）。"""
    assert qd._entry_age_days({}) == 0.0
    assert qd._entry_age_days({"created_at": "not-a-date"}) == 0.0
    now = datetime.now(timezone.utc)
    row = {"created_at": (now - timedelta(days=15)).replace(tzinfo=None).isoformat()}
    assert qd._entry_age_days(row, now=now) == pytest.approx(15.0)


# ---- 同音 UPSERT（引擎侧闸与 support 取大） ----


def test_upsert_homophones_support_takes_max_and_filters():
    n = qd.upsert_homophones([
        {"wrong": "拼多多啊", "right": "拼多多", "support": 3},
        {"wrong": "拼夕夕", "right": "拼多多", "support": 1},  # < 下限 → 滤
        {"wrong": "拼多多", "right": "拼多多", "support": 9},  # wrong==right → 滤
        {"wrong": "", "right": "拼多多", "support": 9},        # 空 → 滤
    ])
    assert n == 1
    rows = qd.list_homophones()
    assert len(rows) == 1 and rows[0]["support"] == 3 and rows[0]["source"] == "auto"
    # 旧值大 → 保旧；新值大 → 取新
    qd.upsert_homophones([{"wrong": "拼多多啊", "right": "拼多多", "support": 2}])
    assert qd.list_homophones()[0]["support"] == 3
    qd.upsert_homophones([{"wrong": "拼多多啊", "right": "拼多多", "support": 5}], source="auto-digest")
    assert qd.list_homophones()[0]["support"] == 5


# ---- 闲时闸与空库 ----


def test_busy_gate_skips_with_zero_side_effects():
    repo = InMemoryBusinessRepository()
    _entry(repo, "你们是哪家公司", "我们是集运中转仓。")
    _seed_calls(repo, [("请问你们是哪间公司的呀？", "我们是集运中转仓。", 6, "zh")])
    events, audit_fn = _audit_log()

    out = asyncio.run(qd.run_digest_once(repo=repo, audit=audit_fn, live_count=lambda: 2))

    assert out["skipped"] == "busy"
    assert len(repo.list_qa_entries(_ACC)) == 1, "忙时零写入"
    assert qd.list_homophones() == [] and qd.list_runs() == []
    assert qd.last_watermark() == "", "busy 轮不落行不推水位"
    assert events == []


def test_empty_db_skips_without_run_row():
    repo = InMemoryBusinessRepository()
    out = asyncio.run(qd.run_digest_once(repo=repo, audit=lambda *a, **k: {}, live_count=lambda: 0))
    assert out["skipped"] == "empty"
    assert qd.list_runs() == []


# ---- env 默认关：digest_loop 空转 ----


def _spin_loop(seconds: float) -> None:
    async def _spin():
        task = asyncio.get_running_loop().create_task(qd.digest_loop())
        await asyncio.sleep(seconds)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_spin())


def test_digest_loop_env_off_never_runs(monkeypatch):
    calls = {"n": 0}

    async def fake_once(**kw):
        calls["n"] += 1
        return {"skipped": "busy"}

    monkeypatch.setattr(qd, "run_digest_once", fake_once)
    assert qd.digest_enabled() is False, "env 默认关"
    _spin_loop(0.15)
    assert calls["n"] == 0, "env 关：循环空转零 run"


def test_digest_loop_env_on_runs_and_interval_env_parsed(monkeypatch):
    monkeypatch.setenv("BOK_QA_AUTO_DIGEST", "1")
    monkeypatch.setenv("BOK_QA_DIGEST_INTERVAL_S", "0.05")
    assert qd.digest_interval_s() == 0.05
    calls = {"n": 0}

    async def fake_once(**kw):
        calls["n"] += 1
        return {"skipped": "busy"}

    monkeypatch.setattr(qd, "run_digest_once", fake_once)
    _spin_loop(0.18)
    assert calls["n"] >= 1, "env 开：循环真的跑 run"


def test_interval_env_bad_value_falls_back(monkeypatch):
    monkeypatch.setenv("BOK_QA_DIGEST_INTERVAL_S", "abc")
    assert qd.digest_interval_s() == qd.DIGEST_INTERVAL_S
    monkeypatch.setenv("BOK_QA_DIGEST_INTERVAL_S", "0")
    assert qd.digest_interval_s() == qd.DIGEST_INTERVAL_S, "非正值回默认（禁忙转）"


# ---- 迁移：build_engine 两跑幂等建两表 ----


def test_build_engine_creates_qa_digest_tables_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "digest.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    cp_deps.build_engine()
    cp_deps.build_engine()  # 二启：CREATE TABLE IF NOT EXISTS 幂等不报错

    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"qa_homophones", "qa_digest_runs"} <= tables
        homo_cols = [r[1] for r in conn.execute("PRAGMA table_info(qa_homophones)")]
        assert homo_cols == ["wrong", "right", "support", "source", "created_at"]
        run_cols = [r[1] for r in conn.execute("PRAGMA table_info(qa_digest_runs)")]
        assert run_cols == ["id", "started_at", "finished_at", "adopted_variant",
                            "adopted_fresh", "disabled", "homophones", "pregen", "error"]
        # 复合主键 (wrong, "right") 在
        pk = {r[1] for r in conn.execute("PRAGMA table_info(qa_homophones)") if r[5]}
        assert pk == {"wrong", "right"}
    finally:
        conn.close()


def test_sql_storage_roundtrip_on_sqlite(tmp_path, monkeypatch):
    """SQL 存储面：bind factory 后 upsert/run/watermark 走真表（读后写 UPSERT）。"""
    db = tmp_path / "storage.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    engine = cp_deps.build_engine()
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    qd.bind_storage(factory)
    try:
        qd.upsert_homophones([{"wrong": "拼多多啊", "right": "拼多多", "support": 2}])
        qd.upsert_homophones([{"wrong": "拼多多啊", "right": "拼多多", "support": 4}])
        rows = qd.list_homophones()
        assert len(rows) == 1 and rows[0]["support"] == 4, "UPSERT support 取 max"
        qd.insert_run({"started_at": _ts(1), "finished_at": _ts(2), "adopted_variant": 1})
        runs = qd.list_runs()
        assert len(runs) == 1 and runs[0]["adopted_variant"] == 1
        assert qd.last_watermark() == _ts(2)
    finally:
        qd._STORAGE["factory"] = None


# ---- 端点：GET /api/stats/qa-digest 管理面闸 ----


def _make_client(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane import main as cp_main

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr(cp_main, "_repo", lambda: repo)
    client = TestClient(cp_main.app).__enter__()
    return client, repo


def test_stats_qa_digest_endpoint_shape_and_auth_off(monkeypatch):
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    client, _repo = _make_client(monkeypatch)
    qd.insert_run({"started_at": _ts(1), "finished_at": _ts(2), "adopted_variant": 2})
    qd.upsert_homophones([{"wrong": "拼多多啊", "right": "拼多多", "support": 2}])
    try:
        r = client.get("/api/stats/qa-digest")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enabled"] is False, "env 未设=引擎关"
        assert isinstance(body["policy"], bool), "策略在场与否如实上报"
        assert isinstance(body["interval_s"], float)
        assert len(body["runs"]) == 1 and body["runs"][0]["adopted_variant"] == 2
        assert len(body["homophones"]) == 1 and body["homophones"][0]["wrong"] == "拼多多啊"
    finally:
        client.__exit__(None, None, None)


def test_stats_qa_digest_management_gate(monkeypatch):
    """管理面闸：user 403；admin（未下发裁定的存量默认集）200。"""
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")
    client, repo = _make_client(monkeypatch)
    from control_plane.auth import hash_password

    repo.create_user(username="peon", password_hash=hash_password(_PW), role="user",
                     org_id="org-t", account_id=_ACC)
    repo.create_user(username="boss", password_hash=hash_password(_PW), role="admin",
                     org_id="org-t", account_id=_ACC)
    try:
        r = client.post("/api/auth/login", json={"username": "peon", "password": _PW})
        peon = {"Authorization": f"Bearer {r.json()['token']}"}
        r = client.post("/api/auth/login", json={"username": "boss", "password": _PW})
        boss = {"Authorization": f"Bearer {r.json()['token']}"}
        assert client.get("/api/stats/qa-digest", headers=peon).status_code == 403
        assert client.get("/api/stats/qa-digest", headers=boss).status_code == 200
    finally:
        client.__exit__(None, None, None)
