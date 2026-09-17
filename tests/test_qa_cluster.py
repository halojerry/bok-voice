"""tts-mine --cluster 纯函数单测(2026-09-16 罐头带情绪姊妹篇:QA 同义聚类)。

只测无网络部分:LLM 决策解析(_parse_llm_decisions 宽松容错)+ 三列计划
(plan_cluster variant 继承目标答案/保守门)。LLM 调用本身不进单测。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "packages" / "core"))

_spec = importlib.util.spec_from_file_location("mine_qa", ROOT / "scripts" / "mine_qa.py")
mine_qa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mine_qa)


def test_parse_llm_decisions_tolerates_fences_and_garbage():
    fenced = '```json\n[{"i":0,"decision":"variant","target":"qa:a","note":"同义"},{"i":1,"decision":"junk","target":"","note":"寒暄"}]\n```'
    out = mine_qa._parse_llm_decisions(fenced)
    assert out[0] == {"decision": "variant", "target": "qa:a", "note": "同义"}
    assert out[1]["decision"] == "junk"
    # 前后带闲话也能抠出数组
    assert mine_qa._parse_llm_decisions("好的,结果如下:[{\"i\":2,\"decision\":\"new\",\"target\":\"\",\"note\":\"全新\"}] 谢谢")[2]["decision"] == "new"
    # 非法 decision/缺 i/纯垃圾 → 静默丢
    assert mine_qa._parse_llm_decisions('[{"i":3,"decision":"maybe","target":""}]') == {}
    assert mine_qa._parse_llm_decisions(" totally not json ") == {}
    assert mine_qa._parse_llm_decisions("") == {}


def _existing():
    return [
        {
            "id": "qa:zh1",
            "lang": "zh",
            "account_id": "acc-001",
            "question_text": "你们是哪家公司",
            "answer_text": "我们是集运中转仓。",
        },
    ]


def test_plan_cluster_variant_inherits_target_answer_keeps_candidate_wording():
    pairs = [{"question": "你係邊間公司呀", "answer": "我哋係中轉倉", "lang": "zh", "calls": 9}]
    decisions = {0: {"decision": "variant", "target": "qa:zh1", "note": "同义"}}
    variants, fresh, junk = mine_qa.plan_cluster(pairs, _existing(), decisions)
    assert not fresh and not junk
    assert len(variants) == 1
    v = variants[0]
    assert v["question_text"] == "你係邊間公司呀"  # 候选原话=匹配面,LLM 不得改写
    assert v["answer_text"] == "我们是集运中转仓。"  # 答案继承目标词条,防漂移
    assert v["source"] == "mined" and v["enabled"] is True and v["scope"] == "global"
    assert v["account_id"] == "acc-001"


def test_plan_cluster_conservative_gates():
    pairs = [
        {"question": "随便问", "answer": "x", "lang": "zh", "calls": 5},  # target 不存在
        {"question": "哪间公司", "answer": "x", "lang": "en", "calls": 5},  # 语言不匹配
        {"question": "你们是哪家公司!", "answer": "x", "lang": "zh", "calls": 5},  # 归一后与现有重复
        {"question": "新问题呢", "answer": "新答案", "lang": "zh", "calls": 5},  # new → 交回 --sync 闸
        {"question": "嗯嗯", "answer": "啊", "lang": "zh", "calls": 5},  # junk 带原因
        {"question": "没人理我", "answer": "x", "lang": "zh", "calls": 5},  # 无决策
    ]
    decisions = {
        0: {"decision": "variant", "target": "qa:missing", "note": ""},
        1: {"decision": "variant", "target": "qa:zh1", "note": ""},
        2: {"decision": "variant", "target": "qa:zh1", "note": ""},
        3: {"decision": "new", "target": "", "note": ""},
        4: {"decision": "junk", "target": "", "note": "寒暄"},
        # 5 缺席 → no-decision
    }
    variants, fresh, junk = mine_qa.plan_cluster(pairs, _existing(), decisions)
    assert variants == []
    assert [r["question"] for r in fresh] == ["新问题呢"]
    reasons = {r["question"]: reason for r, reason in junk}
    assert reasons["随便问"] == "target-missing"
    assert reasons["哪间公司"] == "lang-mismatch"
    assert reasons["你们是哪家公司!"] == "dup-existing"
    assert reasons["嗯嗯"] == "寒暄"
    assert reasons["没人理我"] == "no-decision"


def test_plan_cluster_q_len_gate_kills_fragment_variants():
    # 4B judge 对「啊」「多多」类碎片误判 variant(2026-09-16 实弹)——字数门处决
    pairs = [
        {"question": "啊", "answer": "x", "lang": "zh", "calls": 5},
        {"question": "多多", "answer": "x", "lang": "zh", "calls": 5},
    ]
    decisions = {
        0: {"decision": "variant", "target": "qa:zh1", "note": ""},
        1: {"decision": "variant", "target": "qa:zh1", "note": ""},
    }
    variants, fresh, junk = mine_qa.plan_cluster(pairs, _existing(), decisions)
    assert variants == []
    assert {reason for _, reason in junk} == {"q-len-out-of-range"}
