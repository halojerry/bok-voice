"""学习/账本卫生四修单测(2026-09-27)。

覆盖:
- TASK 1 ``qa_text.mine_qa_pairs`` / ``is_content_reply``:垫话/兜底降级行不再是
  mine 的答案,真答案不再被 seen 丢弃;
- TASK 2 ``gap_mining.compute_coverage``:降级 provider 行单列 degraded 桶、不进
  内容快路分母;
- TASK 3 ``summarize.Summarizer._render_transcript``:跳过账本噪声行,B 线放行;
- TASK 4 ``scripts/pregen_tts.py`` 源级 pin:steps_json 必须取详情冻结 overlay。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# ---- TASK 1:mine_qa_pairs 答案必须是内容回复 ----


def test_is_content_reply_classification():
    from bok_voice_core.qa_text import is_content_reply

    assert is_content_reply({"role": "assistant", "text": "x", "gen": "llm"})
    assert not is_content_reply({"role": "assistant", "text": "x", "gen": "filler"})
    assert not is_content_reply({"role": "assistant", "text": "x", "gen": "interrupted"})
    assert not is_content_reply(
        {"role": "assistant", "text": "x", "gen": "script", "provider": "watchdog-ack"}
    )
    assert not is_content_reply(
        {"role": "assistant", "text": "x", "gen": "script", "provider": "late-answer"}
    )
    # 字段缺省 → 内容回复(旧调用/旧数据保守);B 线同传轮放行。
    assert is_content_reply({"role": "assistant", "text": "x"})
    assert is_content_reply({"role": "assistant", "text": "x", "line": "b", "gen": "filler"})


def test_mine_qa_pairs_filler_between_yields_real_answer():
    from bok_voice_core.qa_text import mine_qa_pairs

    convs = [
        [
            {"role": "user", "text": "你们几点发货", "lang": "zh"},
            {"role": "assistant", "text": "嗯，我看下。", "lang": "zh", "gen": "filler"},
            {"role": "assistant", "text": "今天下单当天发出。", "lang": "zh", "gen": "llm"},
        ]
    ]
    out = mine_qa_pairs(convs, min_calls=1, limit=10)
    assert len(out) == 1
    assert out[0]["question"] == "你们几点发货"
    assert out[0]["answer"] == "今天下单当天发出。"  # 真答案,不是垫话
    assert out[0]["calls"] == 1


def test_mine_qa_pairs_skips_ack_provider_answer():
    from bok_voice_core.qa_text import mine_qa_pairs

    convs = [
        [
            {"role": "user", "text": "我的快递到哪了", "lang": "zh"},
            {"role": "assistant", "text": "稍等，我帮你查。", "lang": "zh",
             "gen": "script", "provider": "watchdog-ack"},
            {"role": "assistant", "text": "已到深圳中转仓。", "lang": "zh", "gen": "llm"},
        ]
    ]
    out = mine_qa_pairs(convs, min_calls=1, limit=10)
    assert len(out) == 1
    assert out[0]["answer"] == "已到深圳中转仓。"


def test_mine_qa_pairs_only_filler_after_yields_nothing():
    from bok_voice_core.qa_text import mine_qa_pairs

    convs = [
        [
            {"role": "user", "text": "你们周末上班吗", "lang": "zh"},
            {"role": "assistant", "text": "让我看看。", "lang": "zh", "gen": "filler"},
        ]
    ]
    assert mine_qa_pairs(convs, min_calls=1, limit=10) == []


def test_mine_qa_pairs_question_not_consumed_by_filler():
    """旧行为:垫话占据相邻位即标 seen,随后真答案被当复读丢弃(221 条实证)。"""
    from bok_voice_core.qa_text import mine_qa_pairs

    convs = [
        [
            {"role": "user", "text": "可不可以退货", "lang": "zh"},
            {"role": "assistant", "text": "係，等我睇睇。", "lang": "zh", "gen": "filler"},
            # 客户复问同一问法 → 这次有真内容回复
            {"role": "user", "text": "可不可以退货", "lang": "zh"},
            {"role": "assistant", "text": "七天无理由退货。", "lang": "zh", "gen": "llm"},
        ]
    ]
    out = mine_qa_pairs(convs, min_calls=1, limit=10)
    assert len(out) == 1
    assert out[0]["answer"] == "七天无理由退货。"
    assert out[0]["calls"] == 1


def test_mine_qa_pairs_legacy_dicts_without_gen_still_adjacent():
    """无 gen/provider 的旧调用(当前 iter_call_conversations 形态)行为不变。"""
    from bok_voice_core.qa_text import mine_qa_pairs

    convs = [
        [
            {"role": "user", "text": "运费多少", "lang": "zh"},
            {"role": "assistant", "text": "首公斤二十。", "lang": "zh"},
        ]
    ]
    out = mine_qa_pairs(convs, min_calls=1, limit=10)
    assert len(out) == 1
    assert out[0]["answer"] == "首公斤二十。"


# ---- TASK 2:coverage 降级行单列 degraded、不进内容快路分母 ----


def test_compute_coverage_excludes_degradation_and_exposes_bucket():
    from control_plane import gap_mining as gm

    rows = [
        {"gen": "script", "provider": "flow-say"},          # 内容快路(直念步)
        {"gen": "qa_fastpath", "provider": "qa-fastpath"},  # 内容快路(QA 罐头)
        {"gen": "script", "provider": "watchdog-ack"},      # 降级
        {"gen": "script", "provider": "late-answer"},       # 降级
        {"gen": "llm", "provider": ""},                     # 内容 LLM
        {"gen": "llm", "provider": "branch-notify"},        # 降级(通知人工副作用行)
        {"gen": "filler", "provider": ""},                  # 非回复
    ]
    cov = gm.compute_coverage(rows)
    # 分母=内容回复轮(7 行 - filler - 3 降级)
    assert cov["turns"] == 3
    assert cov["fastpath"] == 2 and cov["llm"] == 1
    assert cov["degraded"] == 3
    assert cov["fastpath_ratio"] == round(2 / 3, 4)
    assert cov["degraded_ratio"] == round(3 / 6, 4)
    # 来源细分仍覆盖全部回复轮(含降级行),保持向后兼容
    assert cov["by_gen"] == {"script": 3, "qa_fastpath": 1, "llm": 2}
    assert cov["by_provider"]["watchdog-ack"] == 1
    assert cov["by_provider"]["late-answer"] == 1
    assert cov["by_provider"]["branch-notify"] == 1


def test_compute_coverage_zero_and_no_degradation_unchanged():
    from control_plane import gap_mining as gm

    # 无降级行:旧口径逐字节不变(turns=回复轮,ratio=fast/turns)
    rows = [
        {"gen": "script", "provider": "flow-say"},
        {"gen": "qa_fastpath", "provider": "graph-play"},
        {"gen": "llm", "provider": ""},
    ]
    cov = gm.compute_coverage(rows)
    assert cov["turns"] == 3 and cov["fastpath"] == 2 and cov["degraded"] == 0
    assert cov["fastpath_ratio"] == round(2 / 3, 4)
    # 分母为 0 → 0.0(不 ZeroDivisionError)
    empty = gm.compute_coverage([{"gen": "filler", "provider": ""}])
    assert empty["turns"] == 0 and empty["degraded"] == 0
    assert empty["fastpath_ratio"] == 0.0 and empty["degraded_ratio"] == 0.0


def test_gaps_gate_still_keys_on_llm_gen():
    """gaps 口径未动:降级行不因新桶改变 LLM 判定入口(is_llm_gen 仍是唯一源)。"""
    from control_plane import gap_mining as gm

    assert gm.is_llm_gen("llm")
    assert not gm.is_llm_gen("script")
    assert not gm.is_llm_gen("qa_fastpath")
    assert gm.is_degraded_provider("watchdog-ack")
    assert gm.is_degraded_provider("branch-notify")
    assert not gm.is_degraded_provider("flow-say")
    assert not gm.is_degraded_provider("")


# ---- TASK 3:_render_transcript 跳账本噪声,B 线放行 ----


class _TTurn:
    def __init__(self, role, transcript, *, gen="", provider="", line="a"):
        self.role = role
        self.transcript = transcript
        self.gen = gen
        self.provider = provider
        self.line = line


def test_render_transcript_skips_filler_and_ack_rows_keeps_b_line():
    from control_plane.summarize import Summarizer

    turns = [
        _TTurn("user", "你好"),
        _TTurn("assistant", "嗯，我看下。", gen="filler"),
        _TTurn("assistant", "稍等，我帮你查。", gen="script", provider="watchdog-ack"),
        _TTurn("assistant", "今天下单当天发出。", gen="llm"),
        _TTurn("assistant", "【译文】hello", gen="filler", line="b"),  # B 线放行
    ]
    out = Summarizer._render_transcript(turns)
    assert "嗯，我看下。" not in out
    assert "稍等，我帮你查。" not in out
    assert "今天下单当天发出。" in out
    assert "【译文】hello" in out


def test_render_transcript_legacy_objects_without_gen_still_render():
    from control_plane.summarize import Summarizer

    turns = [_TTurn("user", "你好"), _TTurn("assistant", "我哋嘅產品主打防水。")]
    out = Summarizer._render_transcript(turns)
    assert "你好" in out and "我哋嘅產品主打防水。" in out


# ---- TASK 4:pregen_tts 读详情冻结 overlay,不在列表载荷上解 steps_json ----


def test_pregen_tts_reads_frozen_template_detail_not_list_payload():
    src = (ROOT / "scripts" / "pregen_tts.py").read_text(encoding="utf-8")
    # 机器通道自报头(与运行时 agent ControlPlaneClient 同款)+ 通道参数
    assert "X-Bok-Channel" in src and '"agent"' in src
    assert "channel=True" in src
    # 逐条详情端点(冻结 overlay),而非裸列表 steps_json
    assert re.search(r"/api/templates/\{quote\(tid\)\}", src), "详情端点须逐条拉(冻结 overlay)"
    # _fetch_cp 里列表行必须换详情行再物化
    assert "_template_detail_rows(base, token" in src, "列表行须换详情冻结行"
    # 旧直读列表载荷形态不得残留:列表拉取后必须经 _template_detail_rows 包裹
    assert "_template_detail_rows(base, token, _cp_get(" in src
