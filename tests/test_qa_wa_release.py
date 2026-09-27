"""WA 收号步 question 放行 + 每词条 hit_threshold 消费端(VectorQ)单测。

两件事(2026-09-25,全离线纯函数/stub embedder 零网络):

1. ``qa_exclude_reason`` 的 wa_step_locked 闸放行 question 轮——verdict 归一
   化后为 question(flow.QUESTION)且同轮无 wa_signal 时跳过该闸:收号步客户
   提问(「可以点样赔?」)答罐头与收号不冲突,先播快答再继续收号。其余闸
   (advanced/closing/wa_signal/digits/refuse/verdict)独立判定零改动;
   ``BOK_QA_WA_STEP_QUESTION=0`` 回旧行为(wa_step_locked 对 question 照拦)。
2. ``QaSemanticIndex.match`` 逐条目阈值 ``_effective_thr``:词条
   ``hit_threshold`` 列在场且在 (0,1] 用它,缺席/坏值回全局——列未写=行为
   逐字节同旧(golden adjacent 负样本零漂移);显式 threshold 参数仍是全局
   回退;``rank()`` 召回地板不吃词条列(召回供给与出场阈值是两件事)。
"""

from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))
sys.path.insert(0, str(ROOT / "packages" / "core"))

from agent_runtime import qa_gate  # noqa: E402
from agent_runtime.intent_semantic import SEMANTIC_VECTOR_CACHE  # noqa: E402
from agent_runtime.qa_gate import (  # noqa: E402
    QaSemanticIndex,
    _effective_thr,
    qa_exclude_reason,
)


@pytest.fixture(autouse=True)
def _clean_env_and_cache(monkeypatch):
    """默认档钉死:kill-switch 全部在场关闭态 + 语义向量缓存逐测清空。"""
    monkeypatch.delenv("BOK_QA_WA_STEP_QUESTION", raising=False)
    monkeypatch.delenv("BOK_QA_SEMANTIC", raising=False)
    monkeypatch.delenv("BOK_QA_SEM_THRESHOLD", raising=False)
    SEMANTIC_VECTOR_CACHE.clear()
    yield
    SEMANTIC_VECTOR_CACHE.clear()


# ---- 1. WA 收号步 question 放行(qa_exclude_reason 纯函数) ----

def test_wa_step_question_released():
    """question verdict + 收号步锁定 + 无 WA 信号 → 放行(空 reason)。"""
    assert (
        qa_exclude_reason("可以点样赔?", verdict="question", wa_step_locked=True, wa_captured=False)
        == ""
    )
    # 归一化大小写宽容(flow 常量本就小写;调用面透传防御)
    assert (
        qa_exclude_reason("可以点样赔?", verdict="QUESTION", wa_step_locked=True, wa_captured=False)
        == ""
    )
    # 号码已捕获 → 本就不锁(旧行为零漂移)
    assert (
        qa_exclude_reason("好的", verdict="question", wa_step_locked=True, wa_captured=True) == ""
    )


def test_wa_step_other_verdicts_still_locked():
    """非 question verdict 同条件 → 仍 wa_step_locked(错杀只救提问轮)。"""
    for v in ("", "confirm", "unclear", "objection", "offtopic", "defer", "repeat", "farewell"):
        assert (
            qa_exclude_reason("好的", verdict=v, wa_step_locked=True, wa_captured=False)
            == "wa_step_locked"
        ), f"verdict={v!r} 不应放行"


def test_wa_step_question_with_wa_signal_still_blocked():
    """同轮带 WA 信号 → wa_signal 闸先行(question 放行只作用于锁定闸)。"""
    assert (
        qa_exclude_reason(
            "我WhatsApp係13700000000",
            verdict="question",
            wa_signal="captured",
            wa_step_locked=True,
            wa_captured=False,
        )
        == "wa_signal"
    )


def test_wa_step_question_digits_still_blocked():
    """digits 闸独立:question 放行不豁数字轮(WA 捕获状态机零触碰)。"""
    assert (
        qa_exclude_reason(
            "我個號碼係13700000000", verdict="question", wa_step_locked=True, wa_captured=False
        )
        == "digits"
    )


def test_wa_step_question_refuse_still_blocked():
    """refuse 词面闸独立照走:收号步说不要 → 仍 refuse(绝不让快路吃拒绝轮)。"""
    assert (
        qa_exclude_reason("我唔要啦", verdict="question", wa_step_locked=True, wa_captured=False)
        == "refuse"
    )


def test_wa_step_question_advanced_still_blocked():
    """advanced 闸最先行:question 放行不碰流程推进判定。"""
    assert (
        qa_exclude_reason(
            "可以点样赔?", verdict="question", wa_step_locked=True, wa_captured=False, advanced=True
        )
        == "advanced"
    )


def test_wa_step_question_killswitch_restores_legacy(monkeypatch):
    monkeypatch.setenv("BOK_QA_WA_STEP_QUESTION", "0")
    assert qa_gate.qa_wa_step_question_enabled() is False
    assert (
        qa_exclude_reason("可以点样赔?", verdict="question", wa_step_locked=True, wa_captured=False)
        == "wa_step_locked"
    )


def test_wa_step_question_switch_default_on(monkeypatch):
    monkeypatch.delenv("BOK_QA_WA_STEP_QUESTION", raising=False)
    assert qa_gate.qa_wa_step_question_enabled() is True


# ---- 2. 每词条 hit_threshold 消费端(QaSemanticIndex.match,VectorQ) ----


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1e-9
    return [x / n for x in v]


def _cos(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(y * y for y in b)) or 1e-9
    return num / (na * nb)


class _StubClient:
    """鸭型 embedder:文本→单位向量查表(镜像 test_qa_semantic.py 姿势)。"""

    def __init__(self, vectors: dict[str, list[float]]):
        self.dead = False
        self.last_reason = ""
        self.calls = 0
        self._vectors = vectors

    async def embed(self, texts, *, timeout_s=None):  # noqa: ARG002 - 契约占位
        self.calls += 1
        return [self._vectors.get(t, [0.0, 1.0, 0.0]) for t in texts]


def _entry(eid: str, q: str, **kw) -> dict:
    e = {"id": eid, "question_text": q, "answer_text": f"ans-{eid}", "scope": "global"}
    e.update(kw)
    return e


# 素材向量:e1/e2 近正交;两个查询向量对 e1 的余弦现场算出(低带 ~0.83/
# 高带 ~0.884),对 e2 都远低于全局 0.80——e2 永不干扰胜者选取。
_V_E1 = _unit([1.0, 0.05, 0.0])
_V_E2 = _unit([0.0, 1.0, 0.05])
_Q_LOW = "退款点搞"  # cos(e1) ≈ 0.830
_V_Q_LOW = _unit([0.75, 0.55, 0.10])
_Q_HIGH = "这个退款流程怎样办理"  # cos(e1) ≈ 0.884
_V_Q_HIGH = _unit([0.83, 0.49, 0.05])
_Q_MID = "送货时间能不能快一点"  # cos(e2) ≈ 0.874、cos(e1) ≈ 0.528(带间查询)
_V_Q_MID = _unit([0.50, 0.90, 0.05])
_COS_LOW = _cos(_V_Q_LOW, _V_E1)
_COS_HIGH = _cos(_V_Q_HIGH, _V_E1)
_COS_MID_E1 = _cos(_V_Q_MID, _V_E1)
_COS_MID_E2 = _cos(_V_Q_MID, _V_E2)


def _semantic_idx(entries: list[dict]) -> QaSemanticIndex:
    vectors = {
        "怎么申请退款": _V_E1,
        "快递多久能到": _V_E2,
        _Q_LOW: _V_Q_LOW,
        _Q_HIGH: _V_Q_HIGH,
        _Q_MID: _V_Q_MID,
    }
    return asyncio.run(QaSemanticIndex.build(_StubClient(vectors), entries))


def test_effective_thr_pure_function():
    """纯函数契约:(0,1] 用列值;缺席/None/0/越界/坏值回全局;entry 缺席防御。"""
    g = 0.80
    assert _effective_thr({"hit_threshold": 0.87}, g) == 0.87
    assert _effective_thr({"hit_threshold": 1.0}, g) == 1.0  # 上边界含
    assert _effective_thr({"hit_threshold": 0.001}, g) == 0.001  # 下边界开(>0)
    assert _effective_thr({"hit_threshold": "0.9"}, g) == pytest.approx(0.9)  # 数值串宽容
    assert _effective_thr({}, g) == g  # 键缺席(旧 CP 响应)
    assert _effective_thr({"hit_threshold": None}, g) == g
    assert _effective_thr({"hit_threshold": 0}, g) == g  # 0 不在 (0,1]
    assert _effective_thr({"hit_threshold": 1.5}, g) == g  # 越上界
    assert _effective_thr({"hit_threshold": -0.2}, g) == g  # 越下界
    assert _effective_thr({"hit_threshold": "abc"}, g) == g  # 坏值
    assert _effective_thr(None, g) == g  # 词条本体缺席防御


def test_match_per_entry_threshold_blocks_mid_cos():
    """hit_threshold=0.87:cos≈0.83 的查询不出场(top 诊断返回同旧语义)。"""
    assert 0.80 < _COS_LOW < 0.87, "测试向量余弦须落在 (全局, 0.87) 带"
    idx = _semantic_idx([_entry("e1", "怎么申请退款", hit_threshold=0.87), _entry("e2", "快递多久能到")])
    entry, top, reason = asyncio.run(idx.match(_Q_LOW))
    assert entry is None and reason == ""
    assert top == pytest.approx(_COS_LOW, abs=1e-6)  # top=全场最高余弦(诊断语义零改动)


def test_match_per_entry_threshold_high_cos_still_passes():
    """同一条目 hit_threshold=0.87:cos≈0.884 的查询照常出场(该条自己的门)。"""
    assert _COS_HIGH >= 0.87, "测试向量余弦须落在 0.87 之上"
    idx = _semantic_idx([_entry("e1", "怎么申请退款", hit_threshold=0.87), _entry("e2", "快递多久能到")])
    entry, score, reason = asyncio.run(idx.match(_Q_HIGH))
    assert entry is not None and entry["id"] == "e1" and reason == ""
    assert score == pytest.approx(_COS_HIGH, abs=1e-6)


def test_match_column_absent_or_bad_falls_back_to_global():
    """列缺席/0/1.5/None/坏值 → 全局 0.80:cos≈0.83 中等查询照常出场(同旧)。"""
    for bad in (None, 0, 0.0, 1.5, -1, "abc", ""):
        entries = [_entry("e1", "怎么申请退款", hit_threshold=bad), _entry("e2", "快递多久能到")]
        idx = _semantic_idx(entries)
        entry, _top, reason = asyncio.run(idx.match(_Q_LOW))
        assert entry is not None and entry["id"] == "e1" and reason == "", f"bad={bad!r}"
    # 键整体缺席(旧 CP 响应)= 行为逐字节同旧
    idx = _semantic_idx([_entry("e1", "怎么申请退款"), _entry("e2", "快递多久能到")])
    entry, _top, _reason = asyncio.run(idx.match(_Q_LOW))
    assert entry is not None and entry["id"] == "e1"


def test_match_explicit_threshold_param_is_global_fallback():
    """显式 threshold 参数=全局回退;词条列在其上逐条生效(压过更高全局)。"""
    entries = [
        _entry("e1", "怎么申请退款", hit_threshold=0.5),
        _entry("e2", "快递多久能到"),  # 无列 → 吃显式全局 0.95
    ]
    idx = _semantic_idx(entries)
    # cos(e1)≈0.83 ≥ 词条门 0.5 → e1 出场;显式全局 0.95 只拦无列的 e2
    # (cos≈0.59)。若词条列不生效,e1 也会被 0.95 拦下 → 结果应为 (None, top)。
    entry, score, _reason = asyncio.run(idx.match(_Q_LOW, threshold=0.95))
    assert entry is not None and entry["id"] == "e1"
    assert score == pytest.approx(_COS_LOW, abs=1e-6)


def test_match_best_is_highest_cosine_among_per_entry_passers():
    """胜者=最高余弦的通过者:低门条目(cos 0.528 ≥ 自家门 0.5,但 < 全局 0.80)
    参战不得抢走高余弦条目(cos 0.874 ≥ 全局)的出场位。"""
    assert 0.5 <= _COS_MID_E1 < 0.80 and _COS_MID_E2 >= 0.80, "测试向量余弦须落在两档带"
    entries = [
        _entry("e1", "怎么申请退款", hit_threshold=0.5),
        _entry("e2", "快递多久能到"),  # 无列 → 全局 0.80
    ]
    idx = _semantic_idx(entries)
    entry, score, _reason = asyncio.run(idx.match(_Q_MID))
    assert entry is not None and entry["id"] == "e2"
    assert score == pytest.approx(_COS_MID_E2, abs=1e-6)


def test_semantic_rank_ignores_entry_threshold():
    """rank() 召回地板不吃词条列:hit_threshold=1.0(出场永不放行)照常召回。"""
    idx = _semantic_idx([_entry("e1", "怎么申请退款", hit_threshold=1.0), _entry("e2", "快递多久能到")])
    # 出场面:1.0 门 → cos≈0.83/0.884 全被拦,match 恒 None
    entry, _top, reason = asyncio.run(idx.match(_Q_HIGH))
    assert entry is None and reason == ""
    # 召回面:floor 0.60 与词条列无关,e1 照常出线
    rows = asyncio.run(idx.rank(_Q_HIGH, k=3, floor=0.60))
    assert "e1" in [eid for _s, eid in rows]
