"""同音三期（语料内自聚类挖掘，2026-09-25）离线单测——零 LLM 零 embed 零真库。

面：
- 纯函数 ``mine_homophones_corpus``：金族（裴/赔）孪生句出对、support 跨
  miss 问法累计去重、方向铁律（matched 侧恒为 right / miss↔miss 对不出）、
  异音孪生（陪/赔同音出、背/赔异音不出）、插入删除（长度不等）不出、
  距离 2（两处差异）不出、长度分桶（等长桶内才比对）、count 门与归一剥标点；
- CP 引擎接线：三期 corpus 独有对子 UPSERT source='corpus'；三路合并同
  (wrong,right) support 取大且 source 保持默认 'auto'（同 key 恰写一次）；
  matched 语料构造器（归一对齐去重/miss 剔除/账号过滤）；策略层旧版（无
  ``mine_homophones_corpus``）→ 静默跳过零 error。

造数姿势与 test_qa_digest_engine / test_qa_vectorq 同款：DATABASE_URL=""
强制内存仓 + 注入 repo/audit/live_count 直调 run_digest_once；LLM 桩=junk
决策串；二期语义腿整体桩化（``_mine_semantic_homophones`` → ([], "")），
三期是本轮被测腿。引擎世界的词面设计：命中侧问法必须是**词条词面的
子串/超串**（``_is_miss`` 的「沾」判据）才会落进 matched 语料，而 miss 侧
因一字之差不沾——这正是「命中侧给方向」的真实来源。
"""

from __future__ import annotations

import asyncio
import os
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
from bok_voice_core.qa_digest_policy import (  # noqa: E402
    mine_homophones,
    mine_homophones_corpus,
)

from control_plane import qa_cluster as qa_cluster_mod  # noqa: E402
from control_plane import qa_digest as qd  # noqa: E402

_SEQ = iter(range(300_000, 400_000))
_PW = "Passw0rd!"
_ACC = "acc-001"
_BASE = datetime(2026, 9, 20, 8, 0, 0, tzinfo=timezone.utc)


def _ts(minute: int) -> str:
    """固定基准+分钟偏移的 naive UTC ISO（固定宽度，字典序=时间序）。"""
    return (_BASE + timedelta(minutes=minute)).replace(tzinfo=None).isoformat()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """引擎模块态跨测试隔离：内存表/存储绑定/计划缓存/env 全清。"""
    for env in ("BOK_QA_AUTO_DIGEST", "BOK_QA_DIGEST_INTERVAL_S", "BOK_CP_PUBLIC_URL",
                "MLX_LLM_BASE_URL", "BOK_QA_CLUSTER_MODEL", "BOK_EMBED_BASE_URL"):
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


# ---- 造数（与 test_qa_vectorq 同款姿势） ----


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
            cid = f"call-corpus-{next(_SEQ)}"
            repo.create_call(SessionManifest(
                session_id=cid, account_id=account, object_id=obj_id, persona_id="",
                mode=CallMode.LIVE, direction="outbound", language=lang, providers={},
            ))
            repo.update_call(cid, created_at=_ts(next(_SEQ) % 50_000))
            repo.create_turn(_turn(cid, "user", question, speaker="customer", lang=lang))
            repo.create_turn(_turn(cid, "assistant", answer, speaker="agent_ai", lang=lang))
            cids.append(cid)
    return cids


def _entry(repo, question, answer, *, lang="zh", enabled=True):
    return repo.create_qa_entry({
        "question_text": question,
        "answer_text": answer,
        "lang": lang,
        "account_id": _ACC,
        "enabled": enabled,
        "source": "curated",
    })


def _stub_llm_junk(monkeypatch, n: int):
    """n 行候选全判 junk（零采纳，词条面保持原样）；models 发现桩化（零网络）。"""
    decisions = "[" + ",".join(
        '{"i":%d,"decision":"junk","target":"","note":"无关"}' % i for i in range(n)
    ) + "]"
    monkeypatch.setattr(qa_cluster_mod, "_llm_chat", lambda *a, **kw: decisions)
    monkeypatch.setattr(qa_cluster_mod, "_discover_model", lambda base_url: "/models/fake-Qwen3-4B")


def _stub_semantic_off(monkeypatch):
    """二期语义腿整体桩化（零 embed 零网络），聚焦三期被测腿。"""
    monkeypatch.setattr(qd, "_mine_semantic_homophones", lambda *a, **kw: ([], ""))


def _audit_log():
    events: list[dict] = []

    def audit(action, **kw):
        events.append({"action": action, **kw})
        return {"action": action}

    return events, audit


def _run(repo, audit_fn):
    return asyncio.run(qd.run_digest_once(repo=repo, audit=audit_fn, live_count=lambda: 0))


# ---- 纯函数：mine_homophones_corpus ----


def test_corpus_golden_pei_family_yields_pair():
    """金族端到端：miss（未命中）× matched（命中）孪生句 → (裴,赔)。"""
    out = mine_homophones_corpus(
        [{"question": "我想先问下裴几多", "count": 6}],
        [{"question": "我想先问下赔几多", "count": 3}],
    )
    assert out == [{"wrong": "裴", "right": "赔", "support": 1, "example": "我想先问下裴几多"}]


def test_corpus_support_accumulates_across_miss_questions_and_dedupes():
    """support=去重 miss 问法数：第二条 miss 孪生累计；重复问法不重复计。"""
    misses = [
        {"question": "我想先问下裴几多", "count": 6},
        {"question": "我想先问下裴几多", "count": 9},  # 同问法去重
        {"question": "想问下裴几多", "count": 4},      # 第二个支持问法
    ]
    matched = [
        {"question": "我想先问下赔几多", "count": 3},
        {"question": "想问下赔几多", "count": 2},
    ]
    out = mine_homophones_corpus(misses, matched)
    assert out == [{"wrong": "裴", "right": "赔", "support": 2, "example": "我想先问下裴几多"}]


def test_corpus_direction_matched_side_is_right_and_missmiss_not_emitted():
    """方向铁律：matched 侧字恒为 right；miss↔miss 孪生对（两侧都未命中）
    无方向证据，结构性不出。"""
    # matched 侧含「赔」→ right=赔，不是 (赔,裴)
    out = mine_homophones_corpus(
        [{"question": "想问下裴几多", "count": 3}],
        [{"question": "想问下赔几多", "count": 2}],
    )
    assert out[0]["wrong"] == "裴" and out[0]["right"] == "赔"
    # 裴/陪 同音互为孪生，但两侧都是 miss → 零输出
    out = mine_homophones_corpus(
        [
            {"question": "我想问下裴几多", "count": 5},
            {"question": "我想问下陪几多", "count": 4},
        ],
        [{"question": "几时送到", "count": 9}],  # 无任何孪生 matched
    )
    assert out == []


def test_corpus_homophone_vs_heterophone_twin():
    """陪/赔 同 pei 出对；背 bei 异音不出。"""
    out = mine_homophones_corpus(
        [{"question": "想问下陪几多", "count": 2}],
        [{"question": "想问下赔几多", "count": 3}],
    )
    assert out == [{"wrong": "陪", "right": "赔", "support": 1, "example": "想问下陪几多"}]
    assert mine_homophones_corpus(
        [{"question": "想问下背几多", "count": 2}],
        [{"question": "想问下赔几多", "count": 3}],
    ) == []


def test_corpus_insert_delete_length_mismatch_not_emitted():
    """插入/删除（长度不等）不是单字替换：不出对（长度分桶天然挡掉）。"""
    assert mine_homophones_corpus(
        [{"question": "我想问下裴几多钱", "count": 3}],   # 8 字
        [{"question": "我想问下赔几多", "count": 3}],     # 7 字（插入位）
    ) == []
    assert mine_homophones_corpus(
        [{"question": "我想问下裴几多", "count": 3}],     # 7 字（删除位）
        [{"question": "我想问下赔几多钱啊", "count": 3}],  # 9 字
    ) == []


def test_corpus_distance2_not_emitted():
    """距离 2（等长两处差异，两处都是同音对）也整句不出——孪生句只认一处。"""
    assert mine_homophones_corpus(
        [{"question": "钟意裴几多", "count": 3}],
        [{"question": "仲意赔几多", "count": 3}],
    ) == []


def test_corpus_length_bucketing_only_same_length_compared():
    """长度分桶：只有同长桶内的 matched 参与——异长 matched 不影响结果，
    同长 matched 才出对。"""
    misses = [{"question": "问下裴几多", "count": 3}]
    matched = [
        {"question": "我想问下问下赔几多", "count": 9},  # 9 字异长桶（词面相近但不等长）
        {"question": "问下赔几多", "count": 2},          # 5 字同长桶 → 唯一孪生
    ]
    out = mine_homophones_corpus(misses, matched)
    assert out == [{"wrong": "裴", "right": "赔", "support": 1, "example": "问下裴几多"}]
    # 同长但完全同形（距离 0）跳过
    assert mine_homophones_corpus(
        [{"question": "问下赔几多", "count": 3}],
        [{"question": "问下赔几多", "count": 9}],
    ) == []


def test_corpus_count_gate_and_normalize_strips_punctuation():
    """miss count<2 不看（噪声嫌疑）；标点归一剥掉后照样比对。"""
    assert mine_homophones_corpus(
        [{"question": "裴几多", "count": 1}],
        [{"question": "赔几多", "count": 9}],
    ) == [], "count<2=噪声嫌疑不看（matched 侧不设门槛）"
    out = mine_homophones_corpus(
        [{"question": "裴几多？", "count": 3}],
        [{"question": "赔几多", "count": 2}],
    )
    assert out == [{"wrong": "裴", "right": "赔", "support": 1, "example": "裴几多？"}]


def test_corpus_empty_and_malformed_inputs():
    assert mine_homophones_corpus([], []) == []
    assert mine_homophones_corpus(
        [{"question": "裴几多", "count": 3}], []
    ) == [], "matched 空 → 无方向证据 → 零输出"
    assert mine_homophones_corpus(
        [None, "bad", {"count": 5}, {"question": "裴几多", "count": 2}],
        [None, {"question": "赔几多", "count": 1}],
    ) == [{"wrong": "裴", "right": "赔", "support": 1, "example": "裴几多"}]


def test_corpus_does_not_touch_phase1_contract():
    """回归护栏：一期 mine_homophones 契约不变（距离=1 纯同音照出）。"""
    assert mine_homophones(
        [{"question": "裴几多", "count": 3}],
        [{"id": "e1", "question_text": "赔几多"}],
    ) == [{"wrong": "裴", "right": "赔", "support": 1, "example": "裴几多"}]


# ---- CP 引擎接线 ----


def test_engine_corpus_only_pair_upserts_with_corpus_source(monkeypatch):
    """三期 corpus 独有对子：一期词面零贡献、二期桩空 → UPSERT source='corpus'。

    词面设计：词条「我想问下赔几多少钱」把命中侧问法（前缀子串）标记成
    matched，miss 侧因一字之差不沾词条——方向证据来自语料自身。
    """
    repo = InMemoryBusinessRepository()
    _entry(repo, "我想问下赔几多少钱", "赔付金额如下。")
    _seed_calls(repo, [
        ("我想问下裴几多", "帮您查。", 3, "zh"),   # miss（⊄ 词条）
        ("我想问下赔几多", "帮您查。", 3, "zh"),   # matched（⊂ 词条）
        ("问下裴几多", "帮您查。", 2, "zh"),       # miss（support 第二票）
        ("问下赔几多", "帮您查。", 2, "zh"),       # matched（⊂ 词条）
    ])
    _stub_llm_junk(monkeypatch, 4)
    _stub_semantic_off(monkeypatch)
    events, audit_fn = _audit_log()

    out = _run(repo, audit_fn)

    assert out["homophones"] == 1
    assert not [x for x in out["errors"] if "homophone" in x]
    rows = qd.list_homophones()
    assert len(rows) == 1
    assert rows[0]["wrong"] == "裴" and rows[0]["right"] == "赔"
    assert rows[0]["support"] == 2, "两个去重 miss 问法支持同一对"
    assert rows[0]["source"] == "corpus", "三期独有对子盖 corpus 章"
    learn = next(x for x in events if x["action"] == "qa.homophone_learn")
    assert learn["detail"]["pairs"] == 1
    assert qd.list_runs()[0]["homophones"] == 1


def test_engine_shared_pair_merges_max_support_and_keeps_auto_source(monkeypatch):
    """同 (wrong,right) 一期+三期双路命中：support 取大（1 vs 2 → 2）、
    source 保持默认 'auto'（一/二期已产出的对子不被 corpus 改章）、
    UPSERT 恰写一次（written=1 不翻倍）。"""
    repo = InMemoryBusinessRepository()
    _entry(repo, "仲未送到", "帮你催一下。")
    _seed_calls(repo, [
        ("仲未送到啊", "帮您催。", 2, "zh"),   # matched（词条 ⊂ 之）
        ("仲未送到呀", "帮您催。", 2, "zh"),   # matched（词条 ⊂ 之）
        ("钟未送到", "帮你催。", 2, "zh"),     # miss：一期路（距离=1 vs 词条）support 1
        ("钟未送到啊", "帮您催。", 3, "zh"),   # miss：三期路（孪生仲未送到啊）
        ("钟未送到呀", "帮您催。", 2, "zh"),   # miss：三期路（孪生仲未送到呀）
    ])
    _stub_llm_junk(monkeypatch, 5)
    _stub_semantic_off(monkeypatch)
    events, audit_fn = _audit_log()

    out = _run(repo, audit_fn)

    assert out["homophones"] == 1, "同 key 恰写一次，written 不翻倍"
    rows = qd.list_homophones()
    assert len(rows) == 1
    assert rows[0]["wrong"] == "钟" and rows[0]["right"] == "仲"
    assert rows[0]["support"] == 2, "三路合并 support 取大（一期 1 / 三期 2 → 2）"
    assert rows[0]["source"] == "auto", "一/二期已产出的对子保持默认章"
    learn = next(x for x in events if x["action"] == "qa.homophone_learn")
    assert learn["detail"]["pairs"] == 1


def test_engine_below_support_floor_not_upserted(monkeypatch):
    """三期对子 support<2（引擎入库下限）不上表——单问法孪生只够记疑不够铸。"""
    repo = InMemoryBusinessRepository()
    _entry(repo, "我想问下赔几多少钱", "赔付金额如下。")
    _seed_calls(repo, [
        ("我想问下裴几多", "帮您查。", 3, "zh"),  # 唯一 miss 问法 → corpus support=1
        ("我想问下赔几多", "帮您查。", 3, "zh"),  # matched
    ])
    _stub_llm_junk(monkeypatch, 2)
    _stub_semantic_off(monkeypatch)
    events, audit_fn = _audit_log()

    out = _run(repo, audit_fn)

    assert out["homophones"] == 0
    assert qd.list_homophones() == []
    assert not [x for x in events if x["action"] == "qa.homophone_learn"]


def test_matched_corpus_rows_builder_alignment_dedupe_and_account_filter():
    """matched 语料构造器：归一对齐去重、miss 归一形剔除、他账号不收。"""
    candidates = [
        {"account_id": _ACC, "question": "赔几多", "calls": 5},
        {"account_id": _ACC, "question": "赔几多", "calls": 3},    # 同归一形去重（保首行）
        {"account_id": _ACC, "question": "裴几多", "calls": 3},    # miss 侧剔除
        {"account_id": "acc-009", "question": "赔几多层收费", "calls": 2},  # 他账号不收
        {"account_id": _ACC, "question": "几时送到", "calls": 2},
    ]
    misses = [{"question": "裴几多", "count": 3}]
    assert qd._matched_corpus_rows(candidates, _ACC, misses) == [
        {"question": "赔几多", "count": 5},
        {"question": "几时送到", "count": 2},
    ]
    assert qd._matched_corpus_rows([], _ACC, []) == []


def test_corpus_mining_skips_when_policy_lacks_function():
    """策略层旧版（无 mine_homophones_corpus）→ 静默跳过零 error（并行开发兼容）。"""

    class _OldPolicy:
        mine_homophones = staticmethod(mine_homophones)

    pairs, err = qd._mine_corpus_homophones(_OldPolicy(), [], [])
    assert pairs == [] and err == "", "无三期函数 → 空对零因"
    # 策略层异常同样降级为跳过不炸轮
    class _BoomPolicy:
        @staticmethod
        def mine_homophones_corpus(*a, **kw):
            raise RuntimeError("boom")

    pairs, err = qd._mine_corpus_homophones(_BoomPolicy(), [{"question": "裴几多", "count": 2}], [])
    assert pairs == [] and "boom" in err


def test_engine_policy_module_missing_whole_leg_skips(monkeypatch):
    """策略层整模块缺席：⑥ 整腿跳过记因，三期接线不引入新炸点。"""
    monkeypatch.setattr(qd, "policy_module", lambda: None)
    repo = InMemoryBusinessRepository()
    _entry(repo, "我想问下赔几多少钱", "赔付金额如下。")
    _seed_calls(repo, [
        ("我想问下裴几多", "帮您查。", 3, "zh"),
        ("我想问下赔几多", "帮您查。", 3, "zh"),
    ])
    _stub_llm_junk(monkeypatch, 2)
    events, audit_fn = _audit_log()

    out = _run(repo, audit_fn)

    assert out["homophones"] == 0
    assert any("policy" in x for x in out["errors"])
    assert qd.list_runs(), "策略缺席不阻 run 行落库"
