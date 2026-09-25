"""快答库语义补位车道(W3b 解锁,2026-09-24)单测——stub embedder,零网络。

qa_gate.QaSemanticIndex:词面 0.90 未中轮的本地 embedding 释义档(阈值 0.80,
词面恒绝对优先,胜者走词面同款折组/轮换/出场链)。本文件钉:

- env 访问器默认/钳制/kill;
- build:问法去重、快照缓存零重复 embed、任一批次失败 → None、空素材 → None;
- match:阈值/幸存过滤/最高余弦胜者/平分插入序/降级闩零调用/reason 传播;
- QaIndex.team_head 折组出口(语义胜者→簇内首个幸存成员);
- agent.py 接线面(源序:词面 except 之后、match0 打点之前;词面命中轮结构性
  不触语义——`_qa_entry is None` 守卫)。
"""

from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime import qa_gate  # noqa: E402
from agent_runtime.intent_semantic import SEMANTIC_VECTOR_CACHE  # noqa: E402
from agent_runtime.qa_gate import QaIndex, QaSemanticIndex  # noqa: E402


class _StubClient:
    """鸭型 embedder:文本→向量查表;fail=True 恒 None(理由落 last_reason)。"""

    def __init__(self, vectors: dict | None = None, *, fail: bool = False):
        self.dead = False
        self.last_reason = ""
        self.calls = 0
        self._vectors = {k: [float(x) for x in v] for k, v in (vectors or {}).items()}
        self._fail = fail

    async def embed(self, texts, *, timeout_s=None):
        self.calls += 1
        if self._fail:
            self.last_reason = "error"
            return None
        return [self._vectors.get(t, [0.0, 1.0, 0.0]) for t in texts]


def _entry(eid, q, lang="", scope="global", step_index=-1, cluster=""):
    e = {"id": eid, "question_text": q, "answer_text": f"ans-{eid}", "scope": scope}
    if lang:
        e["lang"] = lang
    if scope == "step":
        e["step_index"] = step_index
    if cluster:
        e["cluster_head_id"] = cluster
    return e


def _unit(v):
    n = math.sqrt(sum(x * x for x in v)) or 1e-9
    return [x / n for x in v]


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    monkeypatch.delenv("BOK_QA_SEMANTIC", raising=False)
    monkeypatch.delenv("BOK_QA_SEM_THRESHOLD", raising=False)
    SEMANTIC_VECTOR_CACHE.clear()
    yield
    SEMANTIC_VECTOR_CACHE.clear()


def test_env_accessors_defaults_and_clamp(monkeypatch):
    assert qa_gate.qa_semantic_enabled() is True
    assert qa_gate.qa_semantic_threshold() == 0.80
    assert qa_gate.qa_semantic_base_url() == "http://127.0.0.1:8789"
    assert qa_gate.qa_semantic_timeout_s() == 0.4
    monkeypatch.setenv("BOK_QA_SEMANTIC", "0")
    assert qa_gate.qa_semantic_enabled() is False
    monkeypatch.setenv("BOK_QA_SEM_THRESHOLD", "9")
    assert qa_gate.qa_semantic_threshold() == 1.0
    monkeypatch.setenv("BOK_QA_SEM_THRESHOLD", "abc")
    assert qa_gate.qa_semantic_threshold() == 0.80


def _base_entries():
    return [
        _entry("e1", "怎么申请退款"),
        _entry("e2", "快递多久能到"),
        _entry("e3", "怎么联系人工客服"),
    ]


def _base_vectors():
    # 三个问法各占正交基附近;查询向量与 e1 高余弦(≈0.95)、与 e2/e3 低。
    return {
        "怎么申请退款": _unit([1.0, 0.05, 0.0]),
        "快递多久能到": _unit([0.0, 1.0, 0.05]),
        "怎么联系人工客服": _unit([0.05, 0.0, 1.0]),
    }


def test_build_dedups_and_caches(monkeypatch):
    entries = _base_entries() + [_entry("e4", "怎么申请退款")]  # 同问法重复条目
    client = _StubClient(_base_vectors())
    idx = asyncio.run(QaSemanticIndex.build(client, entries))
    assert idx is not None and len(idx) == 3  # 去重后 3 条
    assert client.calls == 1  # 单批(≤64)一次 embed
    # 快照缓存:同目录再建 → 零 embed
    client2 = _StubClient(_base_vectors())
    idx2 = asyncio.run(QaSemanticIndex.build(client2, entries))
    assert idx2 is not None and client2.calls == 0


def test_build_failure_and_empty_return_none():
    assert asyncio.run(QaSemanticIndex.build(_StubClient(fail=True), _base_entries())) is None
    assert asyncio.run(QaSemanticIndex.build(_StubClient(), [])) is None
    assert asyncio.run(QaSemanticIndex.build(_StubClient(), [_entry("x", "  ")])) is None


def test_match_hits_paraphrase_above_threshold():
    client = _StubClient(_base_vectors())
    idx = asyncio.run(QaSemanticIndex.build(client, _base_entries()))
    # 改写问法(词面 0.90 必未中)与 e1 余弦 ≈0.995 → 释义档命中。
    qv = _unit([0.99, 0.10, 0.05])
    client._vectors["我想把钱退回来怎么弄"] = qv
    entry, score, reason = asyncio.run(idx.match("我想把钱退回来怎么弄"))
    assert entry is not None and entry["id"] == "e1"
    assert score >= 0.80 and reason == ""


def test_match_miss_below_threshold_returns_top():
    client = _StubClient(_base_vectors())
    idx = asyncio.run(QaSemanticIndex.build(client, _base_entries()))
    client._vectors["今天天气怎么样"] = _unit([0.5, 0.5, 0.7])  # 与三问法都低余弦
    entry, top, reason = asyncio.run(idx.match("今天天气怎么样"))
    assert entry is None and reason == ""
    assert 0.0 < top < 0.80  # top=全场最高分(诊断语义,同词面档)


def test_match_survivors_filter_lang_and_step():
    entries = [
        _entry("zh1", "怎么申请退款", lang="zh"),
        _entry("yue1", "点样申请退款", lang="cantonese"),
    ]
    vecs = {
        "怎么申请退款": _unit([1.0, 0.0, 0.0]),
        "点样申请退款": _unit([1.0, 0.02, 0.0]),  # 与查询几乎同向
    }
    client = _StubClient(vecs)
    idx = asyncio.run(QaSemanticIndex.build(client, entries))
    client._vectors["我想把钱退回来怎么弄"] = _unit([1.0, 0.01, 0.0])
    entry, _s, _r = asyncio.run(idx.match("我想把钱退回来怎么弄", lang="zh"))
    assert entry["id"] == "zh1"  # cantonese 条目被滤
    entry, _s, _r = asyncio.run(idx.match("我想把钱退回来怎么弄", lang="cantonese"))
    assert entry["id"] == "yue1"


def test_match_tie_breaks_by_insertion_order():
    entries = [_entry("a", "问法甲"), _entry("b", "问法乙")]
    same = _unit([1.0, 0.0, 0.0])
    client = _StubClient({"问法甲": same, "问法乙": same})
    idx = asyncio.run(QaSemanticIndex.build(client, entries))
    client._vectors["任意问"] = same
    entry, _s, _r = asyncio.run(idx.match("任意问"))
    assert entry["id"] == "a"  # 平分吃插入序


def test_match_degraded_client_zero_calls_and_reasons():
    client = _StubClient(_base_vectors())
    idx = asyncio.run(QaSemanticIndex.build(client, _base_entries()))
    client.dead = True
    calls_before = client.calls
    entry, score, reason = asyncio.run(idx.match("怎么申请退款"))
    assert entry is None and score == 0.0 and reason == "no_embedder"
    assert client.calls == calls_before  # 降级闩:零 embed
    client.dead = False
    client._fail = True
    entry, _s, reason = asyncio.run(idx.match("怎么申请退款"))
    assert entry is None and reason == "error"


def test_match_empty_text_short_circuits():
    client = _StubClient(_base_vectors())
    idx = asyncio.run(QaSemanticIndex.build(client, _base_entries()))
    calls_after_build = client.calls
    entry, _s, reason = asyncio.run(idx.match("   "))
    assert entry is None and reason == "empty"
    assert client.calls == calls_after_build  # 空文本零 embed


def test_lexical_index_team_head_folds_semantic_winner():
    # 语义胜者是变体 → 簇内首个幸存成员代表出场(与词面 match 同款判据)。
    head = _entry("head1", "怎么申请退款")
    variant = _entry("var1", "我想把钱退回来怎么弄", cluster="head1")
    qidx = QaIndex([head, variant])
    folded = qidx.team_head(variant, lang="zh", step_index=None)
    assert folded is not None and folded["id"] == "head1"
    # 整簇(粤语 head+粤语变体)在本语 zh 轮全滤光 → None(裸胜者由调用方兜)。
    head_yue = _entry("head2", "点样退款", lang="cantonese")
    var_yue = _entry("var2", "点样攞返啲钱", lang="cantonese", cluster="head2")
    qidx2 = QaIndex([head_yue, var_yue])
    assert qidx2.team_head(var_yue, lang="zh", step_index=None) is None  # 整簇滤光
    assert qidx.team_head(head, lang="zh", step_index=None) is None  # head 无簇


def test_forward_env_registers_qa_semantic_keys():
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))
    import tools.bok as bok  # noqa: E402

    for key in ("BOK_QA_SEMANTIC", "BOK_QA_SEM_THRESHOLD", "BOK_QA_SEM_BASE_URL", "BOK_QA_SEM_TIMEOUT_MS"):
        assert key in bok._FORWARD_ENV, key


def test_agent_wiring_semantic_sits_between_lexical_miss_and_bump():
    """源序钉死(agent.py 静态断言,镜像 test_intent_semantic_wiring 姿势):
    词面 match/except 之后 → 语义补位(`_qa_entry is None` 守卫) → match0/hit
    打点。守卫在=词面命中轮结构性零语义调用。"""
    src = (Path(__file__).resolve().parents[1] / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(
        encoding="utf-8"
    )
    i_match = src.index("_qa_entry, _qa_score = _qa_index.match(")
    i_sem = src.index("if _qa_entry is None and _qa_sem is not None:")
    i_bump = src.index('_qa_bump("match0" if _qa_entry is None else "hit")')
    assert i_match < i_sem < i_bump
    # 词面恒绝对优先:语义块守卫吃的是词面 None。
    guard = src[i_sem : i_sem + 60]
    assert "_qa_entry is None" in guard
    # 装配面:语义索引构建在 QaIndex 之后、且受 qa_semantic_enabled 门控。
    i_build_qa = src.index("_qa_index = QaIndex(_qa_rows)")
    i_build_sem = src.index("await QaSemanticIndex.build(")
    i_gate = src.index("if _qa_fastpath_on and qa_semantic_enabled():")
    assert i_build_qa < i_build_sem
    assert i_gate < i_build_sem
