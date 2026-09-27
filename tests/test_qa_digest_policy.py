"""qa_digest_policy 离线单测:敏感域判定/分档裁决/同音对挖掘/查询侧归一。

全离线零 IO 零 LLM:策略纯函数的契约钉子。
"""

from bok_voice_core.qa_digest_policy import (
    MIN_FRESH_RECURRENCE,
    MIN_VARIANT_RECURRENCE,
    apply_homophones,
    classify_candidates,
    is_sensitive_answer,
    mine_homophones,
)


# ---- is_sensitive_answer ----


def test_sensitive_hits_money_multiplier_digits_percent():
    assert is_sensitive_answer("赔偿三百蚊") is True  # 关键词 蚊
    assert is_sensitive_answer("最高赔两倍") is True  # 中文数字+倍
    assert is_sensitive_answer("refund 500 dollars") is True  # 英文货币词
    assert is_sensitive_answer("赔偿300元") is True  # 数字+单位紧邻
    assert is_sensitive_answer("赔付上限 200 块") is True
    assert is_sensitive_answer(" pro-forma ¥88 ") is True  # 货币符号
    assert is_sensitive_answer("单号823744016384") is True  # ≥4 位数字串
    assert is_sensitive_answer("赔付比例百分之三十") is True
    assert is_sensitive_answer("成功率 95%") is True


def test_sensitive_non_hits():
    assert is_sensitive_answer("我们星期一上门") is False
    assert is_sensitive_answer("好的没问题") is False
    assert is_sensitive_answer("稍等我帮你查一下物流") is False
    assert is_sensitive_answer("") is False


# ---- classify_candidates ----


def _cand(**kw) -> dict:
    base = {
        "question": "点样查物流进度",
        "lang": "zh",
        "count": 5,
        "cluster_verdict": "fresh",
        "target_entry_id": None,
        "sample_answer": "稍等我帮你查一下物流进度",
        "sample_lang": "zh",
    }
    base.update(kw)
    return base


def test_threshold_constants():
    assert MIN_VARIANT_RECURRENCE == 2
    assert MIN_FRESH_RECURRENCE == 3


def test_classify_auto_variant():
    c = _cand(cluster_verdict="variant", target_entry_id="qa-1", count=2)
    r = classify_candidates([c])
    assert r["auto_variant"] == [c]
    assert r["auto_fresh"] == []
    assert r["pending"] == []
    assert r["skipped"] == []


def test_classify_variant_no_target():
    c = _cand(cluster_verdict="variant", target_entry_id=None, count=9)
    r = classify_candidates([c])
    assert r["skipped"] == [(c, "variant_no_target")]


def test_classify_variant_low_count():
    c = _cand(cluster_verdict="variant", target_entry_id="qa-1", count=1)
    r = classify_candidates([c])
    assert r["skipped"] == [(c, "variant_low_count")]


def test_classify_auto_fresh():
    c = _cand(count=3)
    r = classify_candidates([c])
    assert r["auto_fresh"] == [c]
    assert r["auto_variant"] == []
    assert r["pending"] == []
    assert r["skipped"] == []


def test_classify_fresh_sensitive_goes_pending():
    c = _cand(question="赔偿可以拿几多", count=3, sample_answer="最高可以赔两倍")
    r = classify_candidates([c])
    assert r["pending"] == [c]
    assert r["auto_fresh"] == []


def test_classify_fresh_lang_mismatch():
    c = _cand(count=3, sample_answer="等我帮你查下喇", sample_lang="cantonese")
    r = classify_candidates([c])
    assert r["skipped"] == [(c, "lang_mismatch")]


def test_classify_fresh_low_count_and_empty_answer():
    r = classify_candidates(
        [
            _cand(count=2),
            _cand(count=4, sample_answer="   "),
        ]
    )
    assert [(x, reason) for x, reason in r["skipped"]] == [
        (r["skipped"][0][0], "fresh_low_count"),
        (r["skipped"][1][0], "empty_answer"),
    ]


def test_classify_junk_and_low_count():
    r = classify_candidates([_cand(cluster_verdict="junk", count=9), _cand(count=0)])
    reasons = [reason for _, reason in r["skipped"]]
    assert "junk" in reasons and "low_count" in reasons


def test_classify_unknown_verdict_conservative_skip():
    c = _cand(cluster_verdict="maybe", count=5)
    r = classify_candidates([c])
    assert r["skipped"] == [(c, "unknown_verdict")]


def test_classify_does_not_mutate_input():
    c = _cand()
    classify_candidates([c])
    assert c["count"] == 5 and c["cluster_verdict"] == "fresh"


# ---- mine_homophones ----


def test_mine_dist1_same_pinyin_yields_pair():
    # 整句距离=1 的同音对(裴/赔 同 pei)直接出对;问号归一剥掉
    out = mine_homophones(
        [{"question": "裴几多？", "count": 3}],
        [{"id": "e1", "question_text": "赔几多"}],
    )
    assert out == [{"wrong": "裴", "right": "赔", "support": 1, "example": "裴几多？"}]


def test_mine_dist_gt1_rewrite_overlay_yields_nothing():
    # 「我想先问下裴几多」vs「可以点样赔」:整句距离>1(改写+同音叠加),
    # 挖掘只管纯同音替换,不出对子——那是语义召回的活。
    out = mine_homophones(
        [{"question": "我想先问下裴几多", "count": 4}],
        [{"id": "e1", "question_text": "可以点样赔"}],
    )
    assert out == []


def test_mine_same_pinyin_out_vs_diff_pinyin_not():
    # 距离1 同音(陪/赔 同 pei)出对
    out = mine_homophones(
        [{"question": "陪几多", "count": 2}],
        [{"id": "e1", "question_text": "赔几多"}],
    )
    assert out == [{"wrong": "陪", "right": "赔", "support": 1, "example": "陪几多"}]
    # 距离1 但异音(背 bei / 赔 pei)不出
    out = mine_homophones(
        [{"question": "背几多", "count": 2}],
        [{"id": "e1", "question_text": "赔几多"}],
    )
    assert out == []


def test_mine_exact_match_distance0_not_counted():
    out = mine_homophones(
        [{"question": "赔几多", "count": 9}],
        [{"id": "e1", "question_text": "赔几多"}],
    )
    assert out == []


def test_mine_count1_miss_excluded():
    out = mine_homophones(
        [{"question": "裴几多", "count": 1}],
        [{"id": "e1", "question_text": "赔几多"}],
    )
    assert out == []


def test_mine_support_dedup_and_example_first_original():
    misses = [
        {"question": "裴几多", "count": 5},
        {"question": "裴几多", "count": 3},  # 同问法去重,不重复计 support
        {"question": "裴几多层收费", "count": 2},
    ]
    entries = [
        {"id": "e1", "question_text": "赔几多"},
        {"id": "e2", "question_text": "赔几多层收费"},
    ]
    out = mine_homophones(misses, entries)
    assert len(out) == 1
    assert out[0]["wrong"] == "裴"
    assert out[0]["right"] == "赔"
    assert out[0]["support"] == 2  # 裴几多(去重后 1)+ 裴几多层收费(1)
    assert out[0]["example"] == "裴几多"  # 首个支持 miss 的原话


def test_mine_sorted_by_support_desc():
    misses = [
        {"question": "背面几时寄出", "count": 3},  # 背/赔 异音,不出对
        {"question": "裴几多", "count": 2},
        {"question": "裴几多层收费", "count": 2},
    ]
    entries = [{"id": "e1", "question_text": "赔几多"}, {"id": "e2", "question_text": "赔几多层收费"}]
    out = mine_homophones(misses, entries)
    assert out == [{"wrong": "裴", "right": "赔", "support": 2, "example": "裴几多"}]


def test_mine_empty_inputs():
    assert mine_homophones([], []) == []
    assert mine_homophones([{"question": "裴几多", "count": 3}], []) == []


# ---- apply_homophones ----


def test_apply_multi_pair_replacement_and_no_match():
    pairs = [
        {"wrong": "裴", "right": "赔", "support": 3},
        {"wrong": "崔", "right": "催", "support": 2},
    ]
    assert apply_homophones("裴几多层收费", pairs) == "赔几多层收费"
    assert apply_homophones("崔一下物流", pairs) == "催一下物流"
    assert apply_homophones("没有这些字啊", pairs) == "没有这些字啊"
    assert apply_homophones("", pairs) == ""


def test_apply_conflict_takes_highest_support():
    pairs = [
        {"wrong": "裴", "right": "陪", "support": 1},
        {"wrong": "裴", "right": "赔", "support": 4},
    ]
    assert apply_homophones("裴几多", pairs) == "赔几多"


def test_apply_conflict_tie_keeps_first():
    pairs = [
        {"wrong": "裴", "right": "陪", "support": 2},
        {"wrong": "裴", "right": "赔", "support": 2},
    ]
    assert apply_homophones("裴几多", pairs) == "陪几多"


def test_apply_tuple_form_and_dict_wins_over_tuple():
    assert apply_homophones("裴几多", [("裴", "赔")]) == "赔几多"
    # 元组形态无计数,恒让位于带 support 的 dict 对
    pairs = [("裴", "陪"), {"wrong": "裴", "right": "赔", "support": 2}]
    assert apply_homophones("裴几多", pairs) == "赔几多"


def test_apply_no_chain_replacement():
    # 单遍替换:替换结果不参与再替换
    pairs = [{"wrong": "裴", "right": "赔", "support": 1}, {"wrong": "赔", "right": "陪", "support": 9}]
    assert apply_homophones("裴一下", pairs) == "赔一下"


def test_apply_empty_pairs_identity():
    assert apply_homophones("裴几多", []) == "裴几多"


# ---- 问句性门 + 赔付比例敏感档（2026-09-25 首轮实弹补丁） ---------------------

def test_looks_like_question_gate():
    from bok_voice_core.qa_digest_policy import looks_like_question
    # 真问题（含种子词条问法族）
    for q in ("怎么联系我", "怎么把钱给我", "怎么截图发给你", "上門檢測使唔使錢",
              "我想投訴吗", "幾時送到", "How do I get my compensation?"):
        assert looks_like_question(q), q
    # 真栈首轮漏过的垃圾（应承/碎片/身份句/陈述句）
    for q in ("嗯", "多多", "啊拼多多", "你好是我", "你好我是连先生",
              "好的淘宝京东单号", "上一单就是淘宝买的", "在拼多多买的东西还没有到货"):
        assert not looks_like_question(q), q


def test_looks_like_question_spaceless_en():
    """挖掘管线 normalize_question 剥空白——英文问句到策略层是无空格形态。"""
    from bok_voice_core.qa_digest_policy import looks_like_question
    assert looks_like_question("canigetarefund")
    assert looks_like_question("howlongdoesittake")
    assert not looks_like_question("thankyouverymuch")


def test_classify_not_question_skipped():
    out = classify_candidates([
        _cand(question="嗯", count=6, sample_answer="好的，马上帮您查。"),
        _cand(question="啊拼多多", cluster_verdict="variant", target_entry_id="qa:xx", count=4),
        _cand(),  # 基线问题照过
    ])
    assert [c["question"] for c in out["auto_fresh"]] == ["点样查物流进度"]
    assert all(reason == "not_question" for _c, reason in out["skipped"])


def test_comp_ratio_sensitive():
    for t in ("这单我全责，一赔二给您，不用您贴钱。", "最高一赔三", "免贴运费"):
        assert is_sensitive_answer(t), t
    assert not is_sensitive_answer("稍后会有专员通过微信联系您")
