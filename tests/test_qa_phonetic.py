"""粤语音系补位层单测:纯函数对齐 + QaIndex 第二段集成 + kill-switch/阈值/语言闸。

案例全部来自 2026-09-22 真库验证(873 粤语真实客户轮 × 35 粤语词条):同音
替换(陪→赔)、跨方言书写(普通话字面 vs 粤语词条)、ASR 碎片化+插语气词。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))
sys.path.insert(0, str(ROOT / "packages" / "core"))

from agent_runtime.qa_gate import QaIndex, qa_phonetic_enabled, qa_phonetic_threshold  # noqa: E402
from bok_voice_core.qa_phonetic import (  # noqa: E402
    DEFAULT_THRESHOLD,
    align_score,
    text_to_syllables,
)


def _cantonese_entry(eid: str, question: str, **extra) -> dict:
    entry = {
        "id": eid,
        "question_text": question,
        "lang": "cantonese",
        "scope": "global",
        "step_index": -1,
        "cluster_head_id": "",
    }
    entry.update(extra)
    return entry


# ---- 纯函数 ----

def test_text_to_syllables_basic_and_non_hanzi_skip():
    syls = text_to_syllables("咩快遞")
    assert len(syls) == 3
    # 数字/英文/标点/空格落 None → 跳过(碎裂空格天然免疫)
    assert len(text_to_syllables("打 错电话")) == 4
    assert text_to_syllables("852-1234") == []


def test_align_score_homophone_recovery_vs_unrelated():
    # 真库实证对:客户「那你要怎么赔给我呢」 vs 词条「要点样赔」(跨方言书写)
    kw = text_to_syllables("要点样赔")
    utt_hit = text_to_syllables("那你要怎么赔给我呢")
    utt_miss = text_to_syllables("今日天气几好")
    assert align_score(kw, utt_hit) >= DEFAULT_THRESHOLD
    assert align_score(kw, utt_miss) < 0.6


def test_align_score_candidate_expansion_bridges_multireading():
    # 「怎么赔给我」跨方言桥靠 怹 的异读 dim2 桥接 點(关候选展开即掉线,验证阶段实证)
    kw = text_to_syllables("点样赔俾我")
    utt = text_to_syllables("點樣賠給我")
    assert align_score(kw, utt) >= DEFAULT_THRESHOLD


# ---- QaIndex 集成 ----

def test_phonetic_second_stage_hits_on_literal_miss(monkeypatch):
    monkeypatch.delenv("BOK_QA_PHONETIC", raising=False)
    monkeypatch.delenv("BOK_QA_PHONETIC_THRESHOLD", raising=False)
    index = QaIndex([_cantonese_entry("q1", "要点样赔"), _cantonese_entry("q2", "咩快遞")])
    entry, score = index.match("那你要怎么赔给我呢", lang="cantonese")
    assert entry is not None and entry["id"] == "q1"
    assert score >= DEFAULT_THRESHOLD


def test_phonetic_recovers_homophone_and_fragment(monkeypatch):
    monkeypatch.delenv("BOK_QA_PHONETIC", raising=False)
    index = QaIndex([_cantonese_entry("q1", "要点样赔"), _cantonese_entry("q2", "咩快遞")])
    # 陪/赔 同音替换(真库实证 0.838,命中的是「赔」词条)
    entry, _score = index.match("你要怎么陪我呢", lang="cantonese")
    assert entry is not None and entry["id"] == "q1"
    # 碎片化+插语气词(真库实证 0.850 族)
    entry2, _score2 = index.match("什么快递。係啊", lang="cantonese")
    assert entry2 is not None and entry2["id"] == "q2"


def test_phonetic_kill_switch_off_is_byte_old_behavior(monkeypatch):
    monkeypatch.setenv("BOK_QA_PHONETIC", "0")
    assert qa_phonetic_enabled() is False
    index = QaIndex([_cantonese_entry("q1", "要点样赔")])
    assert index._phon == []  # noqa: SLF001 - kill 档不建音系索引
    entry, _score = index.match("那你要怎么赔给我呢", lang="cantonese")
    assert entry is None  # 字面 miss 即终局(旧行为)


def test_phonetic_threshold_env_gate(monkeypatch):
    monkeypatch.delenv("BOK_QA_PHONETIC", raising=False)
    monkeypatch.setenv("BOK_QA_PHONETIC_THRESHOLD", "0.99")
    assert qa_phonetic_threshold() == 0.99
    index = QaIndex([_cantonese_entry("q1", "要点样赔")])
    entry, _score = index.match("那你要怎么赔给我呢", lang="cantonese")
    assert entry is None
    # 坏值宽容回默认
    monkeypatch.setenv("BOK_QA_PHONETIC_THRESHOLD", "not-a-number")
    assert qa_phonetic_threshold() == DEFAULT_THRESHOLD


def test_phonetic_cantonese_only_zh_never_triggers(monkeypatch):
    monkeypatch.delenv("BOK_QA_PHONETIC", raising=False)
    index = QaIndex([_cantonese_entry("q1", "要点样赔")])
    entry, _score = index.match("那你要怎么赔给我呢", lang="zh")
    assert entry is None  # zh 通话不做音系匹配(zh 线验证 NO-GO,无此档)


def test_literal_hit_still_wins_over_phonetic(monkeypatch):
    monkeypatch.delenv("BOK_QA_PHONETIC", raising=False)
    index = QaIndex([_cantonese_entry("q1", "要点样赔")])
    entry, score = index.match("要点样赔", lang="cantonese")
    assert entry is not None and entry["id"] == "q1"
    assert score >= 0.90  # 字面直中,不走音系分


def test_phonetic_winner_resolves_to_cluster_head(monkeypatch):
    """音系胜者是变体 → 折组代表出场(与字面档同一条 _team_head 路,C1 同判据)。"""
    monkeypatch.delenv("BOK_QA_PHONETIC", raising=False)
    head = _cantonese_entry("head1", "要点样赔")
    variant = _cantonese_entry("var1", "怎么赔给我", cluster_head_id="head1")
    index = QaIndex([head, variant])
    entry, score = index.match("那你要怎么赔给我呢", lang="cantonese")
    assert entry is not None
    assert entry["id"] == "head1"  # 变体胜出 → head 代表出场
    assert score >= DEFAULT_THRESHOLD


def test_phonetic_respects_step_scope(monkeypatch):
    monkeypatch.delenv("BOK_QA_PHONETIC", raising=False)
    scoped = _cantonese_entry("q1", "要点样赔", scope="step", step_index=3)
    index = QaIndex([scoped])
    # 本轮无步骤上下文 → 条目被滤出,音系层不接盘(绝不比 match 命中面宽)
    entry, _score = index.match("那你要怎么赔给我呢", lang="cantonese")
    assert entry is None
    entry2, _s2 = index.match("那你要怎么赔给我呢", lang="cantonese", step_index=3)
    assert entry2 is not None and entry2["id"] == "q1"


def test_no_cantonese_entries_no_phonetic_index(monkeypatch):
    monkeypatch.delenv("BOK_QA_PHONETIC", raising=False)
    zh_only = _cantonese_entry("z1", "要怎么赔")
    zh_only["lang"] = "zh"
    index = QaIndex([zh_only])
    assert index._phon == []  # noqa: SLF001 - 无粤语条目不建音系索引(零成本)
    entry, _score = index.match("那你要怎么赔给我呢", lang="cantonese")
    assert entry is None  # 语言过滤先行,音系层无从接盘
