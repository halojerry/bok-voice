"""QA 词库命中率探针纯函数测试(summarize 的 would-hit/miss 计数与排序)。

命中判定与 agent.py 命中块同源:QaIndex.match 实返回 (entry|None, score)
(keyword-only 签名),entry 非 None 即命中——探针与测试用同一判定。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import probe_qa_hit  # noqa: E402


def test_summarize_counts_would_hit_and_misses():
    rows = [{"id": 1, "question_text": "你们几点上班", "answer_text": "九点",
             "lang": "zh", "scope": "global", "step_index": None, "enabled": 1}]
    pairs = [{"question": "你们几点上班", "lang": "zh", "calls": 5},
             {"question": "我想改收货地址", "lang": "zh", "calls": 3}]
    out = probe_qa_hit.summarize(rows, pairs)
    assert out["pairs_total"] == 2
    assert out["would_hit"] == 1
    assert out["would_hit_rate"] == 0.5
    assert out["top_misses"][0]["question"] == "我想改收货地址"


def test_summarize_step_scoped_entry_never_matches_pair_without_step():
    # pair(挖据报告行)不带 step_index → step 作用域条目永不命中,计 miss;
    # 守护「None/无 entry 一律 miss」的判定路径。
    rows = [{"id": 2, "question_text": "几时送到", "answer_text": "明天",
             "lang": "zh", "scope": "step", "step_index": 2, "enabled": 1}]
    pairs = [{"question": "几时送到", "lang": "zh", "calls": 7}]
    out = probe_qa_hit.summarize(rows, pairs)
    assert out["pairs_total"] == 1
    assert out["would_hit"] == 0
    assert out["top_misses"] == [{"question": "几时送到", "calls": 7, "lang": "zh"}]


def test_summarize_empty_pairs_zero_rate():
    rows = [{"id": 3, "question_text": "你们几点上班", "answer_text": "九点",
             "lang": "zh", "scope": "global", "step_index": None, "enabled": 1}]
    out = probe_qa_hit.summarize(rows, [])
    assert out["pairs_total"] == 0
    assert out["would_hit"] == 0
    assert out["would_hit_rate"] == 0.0
    assert out["top_misses"] == []
