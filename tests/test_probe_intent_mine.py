"""意图候选挖掘探针（`scripts/probe_intent_mine.py`）纯函数测试。

只测**离线可判定**的部分：SSRF guard / 抽样去重 / 宽容 JSON 解析 / 候选清洗合并 /
覆盖回放（与生产 `flow_graph.normalize_graph_text` 同语义）/ prompt 约束。LLM 调用与
真库读取不在单测范围（探针实跑覆盖）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import probe_intent_mine as pim  # noqa: E402


# --------------------------------------------------------------------------- #
# guard / 归一化
# --------------------------------------------------------------------------- #

def test_guard_allows_loopback_rejects_remote():
    assert pim.guard("http://127.0.0.1:1237/v1/models").startswith("http://127.0.0.1")
    assert pim.guard("https://localhost:1237/x").startswith("https://localhost")
    for bad in ("http://evil.example.com/x", "ftp://127.0.0.1/x",
                "http://192.168.1.9:1237/x", "file:///etc/passwd"):
        with pytest.raises(ValueError):
            pim.guard(bad)


def test_normalize_text_matches_production_semantics():
    """覆盖回放必须与 flow_graph 同语义：双侧剥标点/空格 + casefold。"""
    from bok_voice_core.flow_graph import normalize_graph_text

    for raw in ("我要。投诉！", "我 要 投 诉", "REFUND PLZ", "拼多多嘅",
                " 你 好 ， 世 界 ", ""):
        assert pim.normalize_text(raw) == normalize_graph_text(raw)
    assert pim.normalize_text("我要。投诉！") == "我要投诉"


# --------------------------------------------------------------------------- #
# 抽样
# --------------------------------------------------------------------------- #

def test_dedup_samples_counts_filters_and_orders():
    rows = ["退款", "退款", "我要退款。", "我要退款", "啊", "哦", "  ", "物流到哪了"]
    out = pim.dedup_samples(rows, min_chars=3, limit=10)
    # 「退款」2 字被 min_chars=3 过滤；「我要退款。」与「我要退款」归一化同键=2 次
    assert out[0] == {"text": "我要退款。", "count": 2}
    assert {"text": "物流到哪了", "count": 1} in out
    assert all(len(s["text"].strip()) >= 3 for s in out)
    assert all(s["text"].strip() not in ("啊", "哦") for s in out)


def test_dedup_samples_limit_caps_at_max():
    rows = [f"这是第{i}条不一样的话" for i in range(500)]
    assert len(pim.dedup_samples(rows, limit=9999)) == pim.MAX_SAMPLES


# --------------------------------------------------------------------------- #
# 宽容解析
# --------------------------------------------------------------------------- #

def test_parse_candidates_from_fenced_preamble_array():
    raw = """好的，结果如下：
```json
[{"id":"compensation_ask","name":"询问赔偿","keywords":["赔偿","点样赔","赔钱"],
  "example":["可以点样赔？","我要赔偿"]}]
```"""
    out = pim.parse_intent_candidates(raw)
    assert len(out) == 1
    assert out[0]["id"] == "compensation_ask"
    assert out[0]["keywords"] == ["赔偿", "点样赔", "赔钱"]
    assert out[0]["example"] == ["可以点样赔？", "我要赔偿"]


def test_parse_candidates_repairs_missing_closing_bracket():
    raw = ('[{"id":"logistics_trace","name":"查询物流","keywords":["到边度","物流","单号"],'
           '"example":["物流到哪了"]}')
    out = pim.parse_intent_candidates(raw)
    assert len(out) == 1 and out[0]["id"] == "logistics_trace"


def test_parse_candidates_accepts_object_wrapper():
    raw = '{"intents":[{"id":"whatsapp_contact","name":"加联系方式",' \
          '"keywords":["whatsapp","加你","微信"],"example":["加你WhatsApp得唔得？"]}]}'
    out = pim.parse_intent_candidates(raw)
    assert [c["id"] for c in out] == ["whatsapp_contact"]


def test_parse_candidates_drops_malformed_and_generic():
    raw = """[
      {"id":"ok_intent","name":"好","keywords":["退款","赔钱","要钱"],"example":[]},
      {"id":"询问赔偿","name":"坏 id（中文）","keywords":["退款","赔钱","要钱"],"example":[]},
      {"id":"too_few","name":"关键词不足","keywords":["退款","你"],"example":[]},
      {"id":"dedup_kw","name":"去重后不足","keywords":["你","我","佢","嘅"],"example":[]}
    ]"""
    out = pim.parse_intent_candidates(raw)
    assert [c["id"] for c in out] == ["ok_intent"]


def test_sanitize_keywords_dedups_by_normalized_key():
    assert pim._sanitize_keywords(["退款", "退款", "退 款", "赔"]) == ["退款"]


def test_parse_candidates_salvages_from_truncated_array():
    """9B 长批次实测会被 max_tokens 砍在对象中途：完整对象必须救回来，截断的那条丢弃。"""
    raw = ('[{"id":"a_b","name":"甲","keywords":["退款","赔钱","要钱"],"example":["x"]},'
           '{"id":"c_d","name":"乙","keywords":["物流",')
    assert [c["id"] for c in pim.parse_intent_candidates(raw)] == ["a_b"]
    assert pim._salvage_json_objects(raw) == [
        {"id": "a_b", "name": "甲", "keywords": ["退款", "赔钱", "要钱"], "example": ["x"]}]


def test_parse_candidates_garbage_returns_empty():
    assert pim.parse_intent_candidates("模型今天不想说话") == []
    assert pim.parse_intent_candidates("") == []


# --------------------------------------------------------------------------- #
# 合并 / 覆盖回放
# --------------------------------------------------------------------------- #

def test_merge_candidates_unions_keywords_and_examples():
    a = [{"id": "i_a", "name": "甲", "keywords": ["退款", "赔钱"], "example": ["x"]}]
    b = [{"id": "i_a", "name": "", "keywords": ["赔钱", "退货"], "example": ["y", "z"]},
         {"id": "i_b", "name": "乙", "keywords": ["物流"], "example": []}]
    out = pim.merge_candidates([a, b])
    assert [c["id"] for c in out] == ["i_a", "i_b"]
    assert out[0]["name"] == "甲"
    assert out[0]["keywords"] == ["退款", "赔钱", "退货"]
    assert out[0]["example"] == ["x", "y"]


def test_merge_candidates_caps_merged_keyword_budget():
    """合并并集有界（=生产 MAX_KEYWORDS 32），不是无界膨胀。"""
    batch = [{"id": "i_a", "name": "甲",
              "keywords": [f"词{i}" for i in range(40)], "example": []}]
    out = pim.merge_candidates([batch])
    assert len(out[0]["keywords"]) == pim.CANDIDATE_MAX_KEYWORDS == 32


def test_load_candidates_file_preserves_merged_keywords(tmp_path):
    """回放不能二次砍词：候选级上限(32) ≠ 单批输出契约(8)，否则跑/回放读数不一致。"""
    p = tmp_path / "rep.json"
    p.write_text(json.dumps({
        "model": "9b", "candidates": [{"id": "logistics_trace", "name": "查询物流",
                                       "keywords": [f"kw{i}" for i in range(22)]}],
    }), encoding="utf-8")
    cands, label = pim.load_candidates_file(str(p))
    assert label == "9b"
    assert len(cands[0]["keywords"]) == 22
    assert pim.load_candidates_file.__doc__ is not None


def test_coverage_replay_hits_casefold_punct_and_reports_uncovered():
    samples = [
        {"text": "我要。投诉！", "count": 5},
        {"text": "REFUND", "count": 3},
        {"text": "完全无关的一句话", "count": 9},
    ]
    cands = [
        {"id": "complaint", "name": "投诉", "keywords": ["投诉"], "example": []},
        {"id": "refund_ask", "name": "退款", "keywords": ["refund"], "example": []},
    ]
    cov = pim.coverage_replay(samples, cands)
    assert cov["total"] == 3 and cov["covered"] == 2
    assert cov["per_intent"] == {"complaint": 1, "refund_ask": 1}
    assert pytest.approx(cov["rate"], rel=1e-6) == 2 / 3
    # uncovered 按频次降序：无关句 count=9 排第一
    assert [s["text"] for s in cov["uncovered"]] == ["完全无关的一句话"]


def test_build_mine_prompt_carries_constraints_and_samples():
    prompt = pim.build_mine_prompt([{"text": "可以点样赔？", "count": 7}])
    assert "逐字摘自" in prompt and "不算意图" in prompt
    assert "1. [7次] 可以点样赔？" in prompt


def test_build_report_thresholds_and_pass_flag():
    samples = [{"text": "可以点样赔？", "count": 7}]
    cands = [{"id": "compensation_ask", "name": "询问赔偿",
              "keywords": ["赔", "赔偿", "点样赔"], "example": []}]
    cov = pim.coverage_replay(samples, cands)
    rep = pim.build_report(template_id="t1", template_calls=2, rows=["x"], samples=samples,
                           candidates=cands, coverage=cov, model="m",
                           thresholds={"min_intents": 8, "min_coverage": 0.6}, batch_log=[])
    assert rep["pass"] == {"intents": False, "coverage": True, "overall": False}
    assert rep["candidates"][0]["hits"] == 1
    assert rep["coverage"]["rate"] == 1.0
    # 稳健性：唯一意图被去掉后覆盖率归零（PASS 余量一眼可读）
    assert rep["coverage_sensitivity"]["drop_top_intent"] == {"id": "compensation_ask", "rate": 0.0}
    assert rep["coverage_sensitivity"]["cap_keywords_per_intent"]["limit"] == pim.MAX_KEYWORDS
    assert "samples_dedup_total" not in rep  # 误导字段已删（它曾等于客户轮总数）
