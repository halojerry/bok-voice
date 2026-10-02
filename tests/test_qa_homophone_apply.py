"""同音归一应用钩子(语义召回面,2026-09-25)单测——stub embedder,零网络。

qa_gate.QaSemanticIndex 构造注入 ``homophones=``(沉淀引擎经 CP 学到的同音
对子,形状 ``{"wrong","right","support"}`` 列或元组列):``match``/``rank``
的查询文本在 ``normalize_question`` 之后按对子归一再 embed——词条素材向量
不动。本文件钉:

- ``_homophone_pairs``:CP 端点形状(dict/元组列)归一,坏项静默丢弃;
- match:对子在场+env 开 → 同音查询命中正字词条(玩具向量编码:归一后文本
  相等 → 同向量);对子空/None → miss(旧行为);``BOK_QA_HOMOPHONE=0`` →
  有对子也 miss(kill-switch);策略函数 import 失败/调用失败 → 不炸、行为
  =无对子;
- rank:同款一例;
- 词面 ``QaIndex.match`` 零漂移:有无对子状态/env 拨动,同查询同分数;
  词面索引不收 homophones 参数(TypeError);
- build:对子只透传查询侧,素材缓存键零变化(同目录对子前后零重复 embed)。
"""

from __future__ import annotations

import asyncio
import math
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime import qa_gate  # noqa: E402
from agent_runtime.intent_semantic import SEMANTIC_VECTOR_CACHE  # noqa: E402
from agent_runtime.qa_gate import (  # noqa: E402
    QaIndex,
    QaSemanticIndex,
    _homophone_pairs,
)

from bok_voice_core.qa_text import normalize_question  # noqa: E402

_RIGHT_Q = "赔偿标准是什么"  # 词条正字问法
_WRONG_Q = "裴偿标准是什么"  # 客户同音错字问法(裴↔赔,golden 实测 0% 命中族)


class _StubClient:
    """鸭型 embedder:文本→向量查表(玩具编码:归一后文本相等→同向量);
    表外文本回正交默认向量(与主向量余弦 0=结构性 miss)。"""

    def __init__(self, vectors: dict | None = None):
        self.dead = False
        self.last_reason = ""
        self.calls = 0
        self._vectors = {k: [float(x) for x in v] for k, v in (vectors or {}).items()}

    async def embed(self, texts, *, timeout_s=None):
        self.calls += 1
        return [self._vectors.get(t, [0.0, 1.0, 0.0]) for t in texts]


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1e-9
    return [x / n for x in v]


def _entry(eid: str = "e1", q: str = _RIGHT_Q) -> dict:
    return {"id": eid, "question_text": q, "answer_text": f"ans-{eid}", "scope": "global"}


def _mk_index(pairs: list | None = None) -> tuple[_StubClient, QaSemanticIndex]:
    """直构语义索引(不经 build):素材向量=正字问法单位向量(词条侧不动的形状)。"""
    vec = _unit([1.0, 0.0, 0.0])
    client = _StubClient({_RIGHT_Q: vec})
    e = _entry()
    idx = QaSemanticIndex(client, [(e, normalize_question(_RIGHT_Q), vec)], homophones=pairs)
    return client, idx


@pytest.fixture(autouse=True)
def _clean_env_and_cache(monkeypatch):
    monkeypatch.delenv("BOK_QA_HOMOPHONE", raising=False)
    monkeypatch.delenv("BOK_QA_SEM_THRESHOLD", raising=False)
    SEMANTIC_VECTOR_CACHE.clear()
    yield
    SEMANTIC_VECTOR_CACHE.clear()


def test_env_accessor_defaults_and_kill(monkeypatch):
    assert qa_gate.qa_homophone_enabled() is True  # 默认开
    monkeypatch.setenv("BOK_QA_HOMOPHONE", "0")
    assert qa_gate.qa_homophone_enabled() is False
    monkeypatch.setenv("BOK_QA_HOMOPHONE", "1")
    assert qa_gate.qa_homophone_enabled() is True


def test_homophone_pairs_shapes():
    raw = [
        {"wrong": "裴", "right": "赔", "support": 3},  # CP dict 全键(support 忽略)
        ("畀", "俾"),  # 元组
        ["冇", "冇得"],  # 列表二元
        ("a", "b", "c"),  # 多余项忽略
        {"wrong": "x"},  # 缺 right → 丢
        {"wrong": "", "right": "y"},  # 空 wrong → 丢
        {"wrong": "same", "right": "same"},  # 自反 → 丢
        {"wrong": 1, "right": "y"},  # 非字符串 → 丢
        ["solo"],  # 长度<2 → 丢
        "junk",  # 形状不明 → 丢
        None,  # None 项 → 丢
    ]
    assert _homophone_pairs(raw) == [("裴", "赔"), ("畀", "俾"), ("冇", "冇得"), ("a", "b")]
    assert _homophone_pairs(None) == []
    assert _homophone_pairs([]) == []


def test_match_pairs_present_hit_right_entry():
    _client, idx = _mk_index(pairs=[{"wrong": "裴", "right": "赔"}])
    entry, score, reason = asyncio.run(idx.match(_WRONG_Q))
    assert entry is not None and entry["id"] == "e1"
    assert score >= 0.80 and reason == ""


def test_match_no_pairs_is_old_behavior():
    _client, idx = _mk_index(pairs=None)
    entry, top, reason = asyncio.run(idx.match(_WRONG_Q))
    assert entry is None and reason == ""
    assert top < 0.80  # 表外查询=正交默认向量,余弦 0(旧行为)


def test_match_empty_pairs_is_old_behavior():
    _client, idx = _mk_index(pairs=[])
    entry, _top, _reason = asyncio.run(idx.match(_WRONG_Q))
    assert entry is None


def test_match_killswitch_zero_bypasses_pairs(monkeypatch):
    _client, idx = _mk_index(pairs=[("裴", "赔")])
    monkeypatch.setenv("BOK_QA_HOMOPHONE", "0")
    entry, _top, reason = asyncio.run(idx.match(_WRONG_Q))
    assert entry is None and reason == ""  # 有对子也旁路,行为逐字节同旧


def test_match_import_failure_degrades(monkeypatch):
    """策略包缺席(常态部署形态):sys.modules 置 None → import 失败 → 原查询。"""
    _client, idx = _mk_index(pairs=[("裴", "赔")])
    monkeypatch.setitem(sys.modules, "bok_voice_core.qa_digest_policy", None)
    entry, _top, reason = asyncio.run(idx.match(_WRONG_Q))
    assert entry is None and reason == ""  # 不炸,行为=无对子


def test_match_policy_raise_degrades(monkeypatch):
    """策略函数调用期异常:假模块 apply_homophones 直接 raise → 兜原查询。"""
    _client, idx = _mk_index(pairs=[("裴", "赔")])
    fake = types.ModuleType("bok_voice_core.qa_digest_policy")

    def _boom(q, pairs):  # noqa: ANN001, ARG001
        raise RuntimeError("policy exploded")

    fake.apply_homophones = _boom
    monkeypatch.setitem(sys.modules, "bok_voice_core.qa_digest_policy", fake)
    entry, _top, reason = asyncio.run(idx.match(_WRONG_Q))
    assert entry is None and reason == ""


def test_match_non_string_and_empty_policy_output_degrades(monkeypatch):
    """策略返回非字符串/空串:当失败兜原查询(绝不把空文本喂进 embed)。"""
    _client, idx = _mk_index(pairs=[("裴", "赔")])
    fake = types.ModuleType("bok_voice_core.qa_digest_policy")
    fake.apply_homophones = lambda q, pairs: ""  # type: ignore[method-assign]
    monkeypatch.setitem(sys.modules, "bok_voice_core.qa_digest_policy", fake)
    entry, _top, reason = asyncio.run(idx.match(_WRONG_Q))
    assert entry is None and reason == ""
    fake2 = types.ModuleType("bok_voice_core.qa_digest_policy")
    fake2.apply_homophones = lambda q, pairs: 123  # type: ignore[assignment,method-assign]
    monkeypatch.setitem(sys.modules, "bok_voice_core.qa_digest_policy", fake2)
    entry, _top, reason = asyncio.run(idx.match(_WRONG_Q))
    assert entry is None and reason == ""


def test_rank_pairs_present_hit_and_absent_miss():
    _client, idx = _mk_index(pairs=[("裴", "赔")])
    rows = asyncio.run(idx.rank(_WRONG_Q))
    assert rows and rows[0][1] == "e1" and rows[0][0] >= 0.60  # 召回地板默认 0.60
    _client2, idx2 = _mk_index(pairs=None)
    assert asyncio.run(idx2.rank(_WRONG_Q)) == []  # 无对子:正交零分,地板滤光


def test_build_forwards_pairs_and_cache_key_untouched():
    client1 = _StubClient({_RIGHT_Q: _unit([1.0, 0.0, 0.0])})
    idx1 = asyncio.run(QaSemanticIndex.build(client1, [_entry()]))
    assert idx1 is not None and idx1._homophones == []
    client2 = _StubClient({})  # 空表:若对子进缓存键必重 embed,零调用=键未变
    idx2 = asyncio.run(
        QaSemanticIndex.build(client2, [_entry()], homophones=[{"wrong": "裴", "right": "赔"}])
    )
    assert idx2 is not None and client2.calls == 0  # 素材向量/缓存键零漂移
    assert idx2._homophones == [("裴", "赔")]  # 对子只落查询侧


def test_lexical_match_zero_drift(monkeypatch):
    """词面 0.90 快道零漂移:同查询同分数,与对子状态/env 拨动完全无关。"""
    qidx = QaIndex([_entry()])
    s_default = qidx.match(_WRONG_Q)[1]
    monkeypatch.setenv("BOK_QA_HOMOPHONE", "0")
    s_off = qidx.match(_WRONG_Q)[1]
    monkeypatch.setenv("BOK_QA_HOMOPHONE", "1")
    s_on = qidx.match(_WRONG_Q)[1]
    assert s_default == s_off == s_on
    with pytest.raises(TypeError):  # 词面索引不收对子参数(契约面)
        QaIndex([_entry()], homophones=[("裴", "赔")])  # type: ignore[call-arg]


def test_lexical_rank_zero_drift(monkeypatch):
    """词面召回 rank 同款零漂移(词面召回的对子收益归语义面管)。"""
    qidx = QaIndex([_entry()])
    rows_default = qidx.rank(_WRONG_Q)
    monkeypatch.setenv("BOK_QA_HOMOPHONE", "0")
    rows_off = qidx.rank(_WRONG_Q)
    monkeypatch.setenv("BOK_QA_HOMOPHONE", "1")
    rows_on = qidx.rank(_WRONG_Q)
    assert rows_default == rows_off == rows_on
