"""I3 话面档探针腿（`--jump-speech`）纯函数测试。

判据=跳步轮（provider=graph-jump）回复**词面**：含本步事实词（expect，全含）
且不含被跳步问句词（forbid，任一命中即 FAIL）。修复前实弹基线 call-790fd558
（原样复排第 2/3 步台词）在本判据下 FAIL——判据可分辨，唔係恒绿面。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import probe_flow_graph as pfg  # noqa: E402


def _ev(expected: int = 1) -> dict:
    return pfg.probe_evidence(
        log_exists=True, marks=[100, 200][: expected + 1], expected_rounds=expected)


def _turn(transcript: str) -> list[dict]:
    return [{"role": "assistant", "provider": "graph-jump", "gen": "llm",
             "template_step": pfg.TARGET_STEP, "transcript": transcript}]


_BASE = dict(expect_off=False, target_step=pfg.TARGET_STEP, jump_speech=True,
             trigger_events=[{"kind": "jump", "binding": "bnd_7e8f9a0b", "step": "4"}],
             nontrigger_events=[], play_events=[], evidence=_ev())


def test_jump_speech_positive_and_negative_paths():
    kw = dict(expect_words=["赔付"], forbid_words=["哪个平台", "需要跟您确认"])
    # 正例：讲本步事实、不碰被跳步
    assert pfg.evaluate_leg(turns=_turn("明白，您这单如果确认丢件，会按平台规则赔付。"), **kw, **_BASE)["pass"]
    # 790fd558 形态：原样复排被跳步台词 → FAIL（判据可分辨的锚）
    assert not pfg.evaluate_leg(
        turns=_turn("我这边有一件快递需要跟您确认一下。请问在哪个平台购买？"),
        **kw, **_BASE)["pass"]
    # 缺本步事实词（没讲到位）
    assert not pfg.evaluate_leg(turns=_turn("好的，我帮您登记。"), **kw, **_BASE)["pass"]
    # 无跳步轮（图没触发）
    assert not pfg.evaluate_leg(turns=[], **kw, **_BASE)["pass"]
    # f13c3c06 形态：先讲对本步、尾句回头补问（「平台购买」）→ forbid 命中 FAIL
    assert not pfg.evaluate_leg(
        turns=_turn("明白，您这单如果确认丢件，会按平台规则赔付。\n那您是在哪个平台买的？"),
        **kw, **_BASE)["pass"]


def test_jump_speech_evidence_gate():
    """absence 判据（forbid 不命中）无观测不成 PASS——日志缺失时恒 FAIL。"""
    bad_ev = pfg.probe_evidence(log_exists=False, marks=[100, 200], expected_rounds=1)
    v = pfg.evaluate_leg(
        expect_off=False, target_step=pfg.TARGET_STEP, jump_speech=True,
        expect_words=["赔付"], forbid_words=["哪个平台"],
        trigger_events=[], nontrigger_events=[], play_events=[],
        turns=_turn("会按平台规则赔付。"), evidence=bad_ev)
    assert not v["pass"] and not v["checks"]["evidence_ok"]


def test_plan_rounds_jump_speech_and_default_zero_change():
    kw = dict(trigger_text="我要投诉", nontrigger_text="好的好的", play_text="我要退款",
              after_text="我知道了，你说")
    assert pfg.plan_rounds(then_jump=None, has_qa=True, jump_speech=True,
                           play_round=True, **kw) == [("trigger", "我要投诉")]
    # 缺省档逐字节同旧（零变化铁律）
    assert pfg.plan_rounds(then_jump=None, has_qa=True, play_round=True, **kw) == [
        ("trigger", "我要投诉"), ("nontrigger", "好的好的"), ("play", "我要退款")]
