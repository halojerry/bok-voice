"""意图候选挖掘探针（`scripts/probe_intent_mine.py`）纯函数测试。

只测**离线可判定**的部分：SSRF guard / 抽样去重 / 宽容 JSON 解析 / 候选清洗合并 /
**去重合并（P2.1 下轮）** / **平台意图钉死** / **graph doc 组装 + 生产严格校验对齐** /
覆盖回放（与生产 `flow_graph.normalize_graph_text` 同语义）/ prompt 约束。
LLM 调用与真库读取不在单测范围（探针实跑覆盖）。
"""
from __future__ import annotations

import json
import re
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


def test_build_report_carries_merge_pin_alignment_and_lists():
    """报告必须带上下轮迭代的四块新面：合并台账 / 平台钉死 / 图对齐 / 两清单。"""
    samples = [{"text": "拼多多买嘅。", "count": 4}, {"text": "我要投诉。", "count": 3}]
    cands, _ = pim.pin_platform_intent(
        [{"id": "complaint_formal", "name": "正式投诉", "keywords": ["投诉", "服务", "正式"]}])
    cov = pim.coverage_replay(samples, cands)
    kh = pim.keyword_hit_counts(samples, cands)
    ready, review = pim.classify_candidates(cands, coverage=cov, keyword_hits=kh)
    align = pim.validate_candidates_graph(cands)
    rep = pim.build_report(template_id="t1", template_calls=2, rows=["x"], samples=samples,
                           candidates=cands, coverage=cov, model="m",
                           thresholds={"min_intents": 8, "min_coverage": 0.7}, batch_log=[],
                           merge={"before": 3, "after": 2}, platform_pin={"id": "platform_identify"},
                           alignment=align, import_ready=ready, needs_review=review)
    assert rep["merge"] == {"before": 3, "after": 2}
    assert rep["platform_pin"]["id"] == "platform_identify"
    assert rep["graph_alignment"]["ok"] is True
    assert rep["graph_alignment"]["errors"] == []
    assert rep["graph_alignment"]["catchall"] is True
    assert "doc" not in rep["graph_alignment"]  # graph doc 体积大，报告只留对齐结论
    # 钉死意图覆盖住 4 条平台原话（拼多多），投诉意图仍需人审/或直接导入均可，但两表
    # 必须把全部候选各归一处、不漏不重。
    ids = {r["id"] for r in rep["import_ready"]} | {r["id"] for r in rep["needs_review"]}
    assert ids == {c["id"] for c in cands}


# --------------------------------------------------------------------------- #
# 去重合并（P2.1 下轮迭代）
# --------------------------------------------------------------------------- #

def test_ids_are_synonymous_is_conservative():
    assert pim.ids_are_synonymous("session_affirm", "session_confirm")
    assert pim.ids_are_synonymous("logistics_trace", "logistics_track")
    assert pim.ids_are_synonymous("followup_push", "follow_up_push")  # 仅差下划线
    # 同 id 不是「合并证据」（那是 merge_candidates 的并集路径）
    assert not pim.ids_are_synonymous("session_affirm", "session_affirm")
    # 同一主体但关系不同 / 语义近邻 → 绝不并（宁少勿滥）
    assert not pim.ids_are_synonymous("logistics_trace", "logistics_delay")
    assert not pim.ids_are_synonymous("session_doubt", "identity_question")
    assert not pim.ids_are_synonymous("compensation_ask", "refund_ask")
    assert not pim.ids_are_synonymous("contact_whatsapp", "contact_add")
    assert not pim.ids_are_synonymous("session_doubt", "session_close")
    assert not pim.ids_are_synonymous("greeting", "hello")  # 单词 id 证据不足


def test_label_from_id_falls_back_through_synonyms_and_empty_on_unknown():
    assert pim.label_from_id("identity_verify") == "身份核实"
    assert pim.label_from_id("follow_up_push") == "跟进催促"  # push→hurry→催促
    assert pim.label_from_id("logistics_trace") == "物流查询"
    assert pim.label_from_id("wat_ever") == ""  # 译不出=不猜


def test_resolve_name_drift_renames_only_proven_conflict():
    cands = [
        {"id": "session_affirm", "name": "确认应承", "keywords": ["可以", "好啊", "系啊"]},
        {"id": "session_confirm", "name": "确认应承", "keywords": ["可以", "接受呀", "系啊"]},
        {"id": "identity_verify", "name": "确认应承", "keywords": ["证明", "真名", "唔系"]},
    ]
    out, log = pim.resolve_name_drift(cands)
    names = {c["id"]: c["name"] for c in out}
    assert names["identity_verify"] == "身份核实"          # 以 id 为准重命名
    assert names["session_affirm"] == names["session_confirm"] == "确认应承"
    assert log == [{"id": "identity_verify", "old_name": "确认应承",
                    "new_name": "身份核实", "reason": "name_id_drift"}]
    assert cands[2]["name"] == "确认应承"  # 输入未被就地修改


def test_resolve_name_drift_does_not_split_same_name_group():
    """name 与 id 标签沾边就不是漂移：别把本该按同名合并的意图劈开。"""
    cands = [
        {"id": "follow_up_push", "name": "催促跟进", "keywords": ["几时", "跟进", "快帮"]},
        {"id": "session_hurry", "name": "催促跟进", "keywords": ["点解", "咁耐", "未到"]},
    ]
    out, log = pim.resolve_name_drift(cands)
    assert log == []
    assert [c["name"] for c in out] == ["催促跟进", "催促跟进"]


def test_resolve_name_drift_keeps_group_when_no_evidence_at_all():
    """整组标签都与 name 无公共字符（无证据判真主）→ 整组不动。"""
    cands = [
        {"id": "alpha_beta", "name": "确认应承", "keywords": ["可以", "好啊", "系啊"]},
        {"id": "gamma_delta", "name": "确认应承", "keywords": ["收到", "得啦", "系啊"]},
    ]
    out, log = pim.resolve_name_drift(cands)
    assert log == []
    assert [c["name"] for c in out] == ["确认应承", "确认应承"]


def test_resolve_name_drift_marks_untranslatable_with_id_suffix():
    """译不出的 id 也要禁止它沿用别人的名字（否则会被同名规则错并）。"""
    cands = [
        {"id": "weird_thing", "name": "确认应承", "keywords": ["可以", "好啊", "系啊"]},
        {"id": "session_affirm", "name": "确认应承", "keywords": ["可以", "收到", "系啊"]},
    ]
    _out, log = pim.resolve_name_drift(cands)
    assert log == [{"id": "weird_thing", "old_name": "确认应承",
                    "new_name": "确认应承（weird_thing）", "reason": "name_id_drift"}]


def test_dedup_candidates_merges_name_equal_and_sums_hits():
    cands = [
        {"id": "session_affirm", "name": "确认应承", "keywords": ["可以", "好啊"], "hits": 23},
        {"id": "session_confirm", "name": "确认应承", "keywords": ["可以", "接受呀"], "hits": 23},
    ]
    out, log = pim.dedup_candidates(cands)
    assert [c["id"] for c in out] == ["session_affirm"]
    assert out[0]["keywords"] == ["可以", "好啊", "接受呀"]  # 保序并集去重
    assert out[0]["hits"] == 46                              # 命中数相加
    assert out[0]["merged_from"] == ["session_affirm", "session_confirm"]
    assert log["before"] == 2 and log["after"] == 1 and log["merged_away"] == 1
    group = log["merges"][0]
    assert group["from"] == ["session_affirm", "session_confirm"]
    assert "name_equal" in group["reasons"]
    assert group["pre_merge_hits"] == {"session_affirm": 23, "session_confirm": 23}
    assert cands[1]["hits"] == 23  # 输入未被就地修改


def test_dedup_candidates_id_synonym_takes_lexicographically_smaller_id():
    cands = [
        {"id": "logistics_track", "name": "查询物流单号", "keywords": ["单号", "发我", "寻查"], "hits": 5},
        {"id": "logistics_trace", "name": "查询物流", "keywords": ["物流", "边度", "寄出"], "hits": 40},
    ]
    out, log = pim.dedup_candidates(cands)
    assert [c["id"] for c in out] == ["logistics_trace"]
    assert out[0]["name"] == "查询物流"          # id 的最小者带出名字
    assert out[0]["hits"] == 45
    assert log["merges"][0]["reasons"] == ["id_synonym"]


def test_dedup_candidates_keyword_overlap_needs_high_jaccard():
    same = [
        {"id": "pickup_pickup", "name": "自提/去仓", "keywords": ["去仓", "攞件", "去藏龙"], "hits": 3},
        {"id": "session_contact", "name": "联系方式/藏龙", "keywords": ["去藏龙", "去仓", "攞件"], "hits": 3},
    ]
    out, log = pim.dedup_candidates(same)
    assert [c["id"] for c in out] == ["pickup_pickup"]      # Jaccard=1.0 → 并
    assert log["merges"][0]["reasons"] == ["keyword_overlap"]
    weak = [
        {"id": "a_one", "name": "甲", "keywords": ["投诉", "服务", "正式"], "hits": 3},
        {"id": "b_two", "name": "乙", "keywords": ["投诉", "退款", "进度"], "hits": 3},
    ]
    assert len(pim.dedup_candidates(weak)[0]) == 2          # 只共 1 词 → 不并


def test_dedup_candidates_keeps_related_but_distinct_intents():
    cands = [
        {"id": "logistics_delay", "name": "查询物流/迟了", "keywords": ["迟咗", "几耐", "礼拜"], "hits": 12},
        {"id": "logistics_trace", "name": "查询物流", "keywords": ["单号", "发我", "寻查"], "hits": 40},
        {"id": "product_detail", "name": "商品细节", "keywords": ["几多钱", "黑边", "三日"], "hits": 12},
    ]
    out, log = pim.dedup_candidates(cands)
    assert [c["id"] for c in out] == ["logistics_delay", "logistics_trace", "product_detail"]
    assert log["merged_away"] == 0 and log["merges"] == []


def test_dedup_candidates_caps_unioned_keywords():
    cands = [
        {"id": "i_a", "name": "甲", "keywords": [f"甲{i}" for i in range(20)], "hits": 1},
        {"id": "i_a_dup", "name": "甲", "keywords": [f"乙{i}" for i in range(20)], "hits": 1},
    ]
    out, _log = pim.dedup_candidates(cands)
    assert len(out[0]["keywords"]) == pim.CANDIDATE_MAX_KEYWORDS == 32
    assert out[0]["hits"] == 2


def test_mine_candidates_multi_pass_merges_ids_and_feeds_dedup(monkeypatch):
    """多遍挖掘：两遍各自打 LLM，同 id 并集、异名同义 id 留给 dedup 合。"""
    replies = [
        '[{"id":"session_affirm","name":"确认应承","keywords":["可以","好啊","系啊"],"example":[]}]',
        '[{"id":"session_confirm","name":"确认应承","keywords":["系啊","接受呀","好啊"],"example":[]}]',
    ]
    calls = {"n": 0}

    def fake_chat(messages, *, model, max_tokens=4096, timeout=300):
        calls["n"] += 1
        return replies[min(calls["n"], len(replies)) - 1]

    monkeypatch.setattr(pim, "chat", fake_chat)
    raw, log, per_pass = pim.mine_candidates(
        [{"text": "可以啊。", "count": 2}], model="m", batch_size=10, passes=2)
    assert calls["n"] == 2 and len(per_pass) == 2
    assert [c["id"] for c in per_pass[0]] == ["session_affirm"]
    assert [c["id"] for c in per_pass[1]] == ["session_confirm"]
    assert [c["id"] for c in raw] == ["session_affirm", "session_confirm"]
    assert len(log) == 2 and all(line.startswith("pass ") for line in log)
    merged, merge_log = pim.dedup_candidates(raw)
    assert [c["id"] for c in merged] == ["session_affirm"]   # 跨遍同义 id 被并
    assert merge_log["merged_away"] == 1


def test_mine_candidates_survives_single_batch_llm_error(monkeypatch):
    calls = {"n": 0}

    def flaky_chat(messages, *, model, max_tokens=4096, timeout=300):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("boom")
        return '[{"id":"i_a","name":"甲","keywords":["甲一","甲二","甲三"],"example":[]}]'

    monkeypatch.setattr(pim, "chat", flaky_chat)
    raw, log, _per_pass = pim.mine_candidates(
        [{"text": "甲一", "count": 1}, {"text": "甲二", "count": 1}],
        model="m", batch_size=1, passes=1)
    assert [c["id"] for c in raw] == ["i_a"]
    assert any("LLM error" in line for line in log)


# --------------------------------------------------------------------------- #
# 平台意图钉死
# --------------------------------------------------------------------------- #

def test_pin_platform_intent_always_present_and_first():
    cands = [{"id": "logistics_trace", "name": "查询物流",
              "keywords": ["单号", "发我", "寻查"], "hits": 40}]
    out, log = pim.pin_platform_intent(cands)
    assert out[0]["id"] == pim.PLATFORM_INTENT_ID == "platform_identify"
    assert out[0]["pinned"] is True
    assert out[0]["keywords"] == list(pim.PLATFORM_INTENT_KEYWORDS)
    assert out[0]["name"] == pim.PLATFORM_INTENT_NAME
    assert [c["id"] for c in out][1:] == ["logistics_trace"]  # 其余候选零改动
    assert log["created"] is True and log["absorbed"] == [] and log["replaced"] is None


def test_pin_platform_intent_absorbs_other_platform_ids_without_keyword_pollution():
    cands = [
        {"id": "platform_confirm", "name": "确认平台/站",
         "keywords": ["点站", "呢度", "喺这里"], "hits": 2},
        {"id": "contact_add", "name": "加联系方式",
         "keywords": ["加你", "WhatsApp", "得唔得"], "hits": 12},
    ]
    out, log = pim.pin_platform_intent(cands)
    assert [c["id"] for c in out] == ["platform_identify", "contact_add"]
    assert out[0]["keywords"] == list(pim.PLATFORM_INTENT_KEYWORDS)  # 指示词不并入
    assert log["absorbed"] == [{"id": "platform_confirm", "name": "确认平台/站",
                                "keywords_dropped": ["点站", "呢度", "喺这里"], "hits": 2}]


def test_pin_platform_intent_replaces_llm_made_same_id():
    cands = [{"id": "platform_identify", "name": "平台",
              "keywords": ["拼多", "买嘅", "平台"], "hits": 3}]
    out, log = pim.pin_platform_intent(cands)
    assert len(out) == 1
    assert out[0]["keywords"] == list(pim.PLATFORM_INTENT_KEYWORDS)
    assert log["created"] is False
    assert log["replaced"]["keywords_dropped"] == ["拼多", "买嘅", "平台"]


def test_pin_platform_intent_is_idempotent_on_replayed_reports():
    """回放自家旧报告时，表里那条钉死意图不该被记成「替换掉了什么」。"""
    cands = [{"id": pim.PLATFORM_INTENT_ID, "name": pim.PLATFORM_INTENT_NAME,
              "keywords": list(pim.PLATFORM_INTENT_KEYWORDS), "hits": 14, "pinned": True},
             {"id": "logistics_trace", "name": "查询物流", "keywords": ["单号", "发我", "寻查"]}]
    out, log = pim.pin_platform_intent(cands)
    assert [c["id"] for c in out] == [pim.PLATFORM_INTENT_ID, "logistics_trace"]
    assert log["already_pinned"] is True and log["replaced"] is None
    assert out[0]["keywords"] == list(pim.PLATFORM_INTENT_KEYWORDS)


def test_pinned_platform_keywords_cover_real_phrase_variants():
    """钉死词表要盖住上一轮漏掉的真实原话变体（含「拼多」截断形态）。"""
    for text in ("拼多多买嘅。", "拼多多。我。", "京东买嘅。", "拼拼多。多多。",
                 "之前而家去左边。拼多。"):
        assert any(pim.normalize_text(kw) in pim.normalize_text(text)
                   for kw in pim.PLATFORM_INTENT_KEYWORDS), text
    assert "拼多" in pim.PLATFORM_INTENT_KEYWORDS  # 真实转写补词（拼多多的子串）


# --------------------------------------------------------------------------- #
# graph doc 组装 + 生产严格校验对齐
# --------------------------------------------------------------------------- #

def test_suggest_binding_close_jumps_others_notify():
    assert pim.suggest_binding("session_close")["action"] == "jump_step"
    assert pim.suggest_binding("session_farewell", close_step=7)["step"] == 7
    assert pim.suggest_binding("logistics_trace")["action"] == "notify_human"
    assert pim.suggest_binding("complaint_formal")["action"] == "notify_human"
    assert pim.suggest_binding("transfer_human")["action"] == "notify_human"


def test_build_graph_doc_binds_every_intent_and_adds_catchall():
    cands = [
        {"id": "logistics_trace", "name": "查询物流", "keywords": ["单号", "发我", "寻查"]},
        {"id": "session_close", "name": "告别", "keywords": ["拜拜", "得啦", "安好"]},
    ]
    doc = pim.build_graph_doc(cands, close_step=5)
    assert [i["id"] for i in doc["intents"]] == ["logistics_trace", "session_close", "*"]
    assert doc["intents"][2]["keywords"] == []          # 兜底 keywords 必空
    assert len(doc["bindings"]) == 3                     # 每个意图一条 → 无孤儿
    assert all(re.fullmatch(r"bnd_[0-9a-f]{8}", b["id"]) for b in doc["bindings"])
    by_intent = {b["intent"]: b for b in doc["bindings"]}
    assert by_intent["logistics_trace"]["action"] == "notify_human"
    assert by_intent["session_close"]["action"] == "jump_step"
    assert by_intent["session_close"]["step"] == 5
    assert by_intent["*"]["action"] == "notify_human"
    assert by_intent["*"]["once"] is False               # 兜底 once 必 false
    assert pim._binding_id("logistics_trace") == by_intent["logistics_trace"]["id"]  # 确定性


def test_validate_candidates_graph_zero_errors():
    cands, _log = pim.pin_platform_intent(pim.dedup_candidates([
        {"id": "session_affirm", "name": "确认应承", "keywords": ["可以", "好啊", "系啊"], "hits": 23},
        {"id": "session_confirm", "name": "确认应承", "keywords": ["可以", "接受呀", "系啊"], "hits": 23},
        {"id": "session_close", "name": "告别", "keywords": ["拜拜", "得啦", "安好"], "hits": 8},
        {"id": "logistics_trace", "name": "查询物流", "keywords": ["单号", "发我", "寻查"], "hits": 40},
    ])[0])
    res = pim.validate_candidates_graph(cands, close_step=6)
    assert res["ok"] is True and res["errors"] == []
    assert res["intents"] == len(cands) + 1        # + 兜底
    assert res["bindings"] == len(cands) + 1
    assert res["warnings"] == []                   # 候选 id 全是合法 snake_case
    assert res["doc"]["intents"][-1]["id"] == "*"


def test_validate_alignment_leg_actually_bites_on_orphan():
    """对齐腿不是空的：把某意图的绑定抽掉，生产校验必须报孤儿意图。"""
    from bok_voice_core.flow_graph import validate_flow_graph

    doc = pim.build_graph_doc([{"id": "a_b", "name": "甲", "keywords": ["甲一", "甲二"]}])
    doc["bindings"] = [b for b in doc["bindings"] if b["intent"] != "a_b"]
    errors = validate_flow_graph(json.dumps(doc, ensure_ascii=False))
    assert any("no enabled binding" in e for e in errors)


def test_write_import_graph_doc_writes_valid_graph_json(tmp_path):
    """落盘的「可直接导入」graph_json 必须能过生产严格校验（运营会拿它去写库）。"""
    from bok_voice_core.flow_graph import validate_flow_graph

    cands, _log = pim.pin_platform_intent([
        {"id": "complaint_formal", "name": "正式投诉", "keywords": ["投诉", "你们", "服务"]},
        {"id": "session_close", "name": "告别结束", "keywords": ["拜拜", "得啦", "安好"]},
    ])
    path = pim.write_import_graph_doc(cands, tmp_path, template_id="t1", close_step=6)
    assert path.name.startswith("graph-import-t1-")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert validate_flow_graph(json.dumps(doc, ensure_ascii=False)) == []
    by_intent = {b["intent"]: b for b in doc["bindings"]}
    assert by_intent["complaint_formal"]["action"] == "notify_human"
    assert by_intent["session_close"]["step"] == 6
    assert by_intent["*"]["once"] is False
    assert doc["intents"][-1]["keywords"] == []


# --------------------------------------------------------------------------- #
# 导入分级（噪声词判据 / 两清单）
# --------------------------------------------------------------------------- #

def test_keyword_is_noisy_flags_digits_particles_and_rare_short_words():
    assert pim.keyword_is_noisy("六四三二")
    assert pim.keyword_is_noisy("嘅")
    assert pim.keyword_is_noisy("底细", hits=1)       # 冷僻短词=抄来的碎片
    assert not pim.keyword_is_noisy("底细", hits=5)   # 同词在全库常见 → 不算噪声
    assert not pim.keyword_is_noisy("拼多多", hits=14)
    assert not pim.keyword_is_noisy("正式投诉", hits=3)


def test_classify_candidates_splits_ready_and_review():
    samples = [
        {"text": "我要投诉你们。", "count": 3},
        {"text": "正式投诉你们。", "count": 2},
        {"text": "你们服务太差。", "count": 2},
        {"text": "拼多多买嘅。", "count": 2},
        {"text": "拼多多买嘢。", "count": 1},
    ]
    cands, _log = pim.pin_platform_intent([
        {"id": "complaint_formal", "name": "正式投诉",
         "keywords": ["投诉", "你们", "服务"], "hits": 7},
        {"id": "dead_intent", "name": "死意图",
         "keywords": ["绝无此句", "也绝无啊", "更绝无嘢"], "hits": 0},
    ])
    cov = pim.coverage_replay(samples, cands)
    hits = pim.keyword_hit_counts(samples, cands)
    ready, review = pim.classify_candidates(cands, coverage=cov, keyword_hits=hits,
                                            renamed_ids=set())
    assert [r["id"] for r in ready] == ["complaint_formal", "platform_identify"]
    assert [r["id"] for r in review] == ["dead_intent"]
    assert "no_coverage_hits" in review[0]["reasons"]
    assert review[0]["hits"] == 0
    # 钉死意图豁免「抄碎片」判据：语料里没命中的品牌词（淘宝/天猫…）不降级它
    pinned_row = next(r for r in ready if r["id"] == "platform_identify")
    assert pinned_row["pinned"] is True and pinned_row["noisy_keywords"] == []
    # 两清单各归一处、不漏不重
    assert {r["id"] for r in ready} | {r["id"] for r in review} == {c["id"] for c in cands}


def test_classify_candidates_downgrades_noisy_ratio_and_marks_renamed():
    samples = [{"text": "前双处理得。好。", "count": 2}]
    cands = [{"id": "noise_intent", "name": "碎片意图",
              "keywords": ["前双", "底细处理", "嘅"], "hits": 2}]
    cov = pim.coverage_replay(samples, cands)
    hits = pim.keyword_hit_counts(samples, cands)
    ready, review = pim.classify_candidates(cands, coverage=cov, keyword_hits=hits,
                                            renamed_ids={"noise_intent"})
    assert ready == []
    assert "noisy_keywords" in review[0]["reasons"]
    assert review[0]["renamed"] is True
    assert review[0]["noisy_keywords"] == ["前双", "底细处理", "嘅"]

    # 长词（≥5 字）只中 1 条不判噪声——否则清单会被推得只剩个案
    long_kw = [{"id": "long_kw", "name": "长词", "keywords": ["前双处理得好全部", "底细处理", "嘅"],
                "hits": 2}]
    ready2, _review2 = pim.classify_candidates(
        long_kw, coverage=pim.coverage_replay(samples, long_kw),
        keyword_hits=pim.keyword_hit_counts(samples, long_kw))
    assert "前双处理得好全部" not in (ready2 + _review2)[0]["noisy_keywords"]


# --------------------------------------------------------------------------- #
# main() 回放端到端（离线：真库→临时库 / 候选文件→去重合并→钉死→图对齐→报告）
# --------------------------------------------------------------------------- #

def _seed_db(path: Path) -> None:
    """造一张最小真库（call_sessions/turns 两表，探针只读这两张）。"""
    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE call_sessions (id TEXT PRIMARY KEY, template_id TEXT);"
        "CREATE TABLE turns (call_id TEXT, line TEXT, speaker TEXT, transcript TEXT);"
        "INSERT INTO call_sessions VALUES ('c1','tpl1');"
    )
    rows = [
        "拼多多买嘅。", "拼多多。", "京东买嘅。", "我要投诉你们。", "正式投诉你们。",
        "可以啊。", "好啊。", "拜拜。", "得啦。", "迟到咗几时送到。",
    ] * 2
    conn.executemany("INSERT INTO turns VALUES ('c1','a','customer',?)",
                     [(t,) for t in rows])
    conn.commit()
    conn.close()


def test_main_replay_merges_two_candidate_files(tmp_path, capsys):
    """两遍挖掘结果（两个候选文件）在 main 里走同一套合并/钉死/对齐/报告管线。"""
    db = tmp_path / "t.db"
    _seed_db(db)
    pass1 = tmp_path / "p1.json"
    pass1.write_text(json.dumps([
        {"id": "session_affirm", "name": "确认应承", "keywords": ["可以", "好啊", "系啊"]},
        {"id": "platform_confirm", "name": "确认平台", "keywords": ["点站", "呢度", "喺这里"]},
    ], ensure_ascii=False), encoding="utf-8")
    pass2 = tmp_path / "p2.json"
    pass2.write_text(json.dumps([
        {"id": "session_confirm", "name": "确认应承", "keywords": ["系啊", "接受呀", "好啊"]},
        {"id": "complaint_formal", "name": "正式投诉", "keywords": ["投诉", "你们", "服务"]},
    ], ensure_ascii=False), encoding="utf-8")

    out_dir = tmp_path / "out"
    rc = pim.main(["--db", str(db), "--template", "tpl1", "--min-intents", "2",
                   "--candidates-file", str(pass1), "--candidates-file", str(pass2),
                   "--out-dir", str(out_dir)])
    assert rc in (0, 1)  # 门槛可能不达（样本太少），报告本身必须写出来
    rep = json.loads(next(out_dir.glob("intent-mine-tpl1-*.json")).read_text(encoding="utf-8"))
    assert rep["template_id"] == "tpl1"
    assert len(rep["passes"]) == 2                      # 两遍各自的读数都在报告里
    assert all("source" in row for row in rep["passes"])
    assert rep["merge"]["before"] == 4                  # 两遍按 id 合并后 4 条
    assert rep["merge"]["after"] == 3                   # 同名同义 id 并成 session_affirm
    assert rep["intent_count"] == 3                     # + 钉死的平台意图
    ids = {c["id"] for c in rep["candidates"]}
    assert ids == {"session_affirm", "complaint_formal", "platform_identify"}
    assert all(c["id"] != "platform_confirm" for c in rep["candidates"])  # 被吸收
    assert rep["graph_alignment"]["ok"] is True and rep["graph_alignment"]["errors"] == []
    assert rep["graph_alignment"]["import_doc_ok"] is True
    assert len(rep["uncovered_top20"]) <= 20
    # 钉死意图盖住平台原话（4 条样本含拼多多/京东）
    pinned = next(c for c in rep["candidates"] if c["id"] == "platform_identify")
    assert pinned["hits"] >= 3
    out = capsys.readouterr().out
    assert "平台钉死" in out and "图对齐" in out


def test_dedup_candidates_collapses_duplicate_ids_without_upstream_merge():
    """本函数单独调用（不经 `merge_candidates`）也必须产出无重复 id 的候选表——
    否则组出来的 graph doc 会撞生产 `validate_flow_graph` 的 id duplicated 硬门。"""
    from bok_voice_core.flow_graph import validate_flow_graph

    cands = [
        {"id": "contact_whatsapp", "name": "加 WhatsApp", "keywords": ["号码", "几多", "打嚟"], "hits": 20},
        {"id": "contact_whatsapp", "name": "加 WhatsApp", "keywords": ["号码", "零一七", "会见"], "hits": 7},
    ]
    out, log = pim.dedup_candidates(cands)
    assert [c["id"] for c in out] == ["contact_whatsapp"]
    assert out[0]["keywords"] == ["号码", "几多", "打嚟", "零一七", "会见"]
    assert out[0]["hits"] == 27
    assert log["merges"][0]["reasons"] == ["same_id"]
    assert validate_flow_graph(json.dumps(pim.build_graph_doc(out), ensure_ascii=False)) == []


def test_pin_platform_intent_created_flag_distinguishes_new_from_idempotent():
    """`created` 必须区分「新建」与「幂等重放已有钉死意图」（曾把重放记成新建）。"""
    out, log = pim.pin_platform_intent([
        {"id": "logistics_trace", "name": "查询物流", "keywords": ["单号", "发我", "寻查"]}])
    assert log["created"] is True and log["already_pinned"] is False
    _out2, log2 = pim.pin_platform_intent(out)
    assert log2["created"] is False and log2["already_pinned"] is True


def test_candidate_load_strips_self_rename_suffix(tmp_path):
    """回放自家旧报告：上轮漂移改名留下的 `名字（id）` 后缀要剥掉（重放幂等）。"""
    p = tmp_path / "old.json"
    p.write_text(json.dumps([
        {"id": "session_greet", "name": "打招呼问候（session_greet）",
         "keywords": ["你好", "各位", "早晨"]},
        {"id": "no_suffix", "name": "别的（someone_else）", "keywords": ["甲说", "乙说", "丙说"]},
    ], ensure_ascii=False), encoding="utf-8")
    cands, _label = pim.load_candidates_file(str(p))
    by_id = {c["id"]: c["name"] for c in cands}
    assert by_id["session_greet"] == "打招呼问候"          # 自家后缀剥掉
    assert by_id["no_suffix"] == "别的（someone_else）"    # 不是自家 id 的后缀不动
