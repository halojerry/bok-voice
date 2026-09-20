"""probe_branch_action.py 离线纯函数自检（pytest 面；真栈跑腿由探针本体承担）。

钉住面（不经真栈、不碰 CP/agent.log）：
- 打点正则族：jump_noop 不被 jump 前缀误吃、canned hit/miss 归类、推进行三族。
- evaluate_leg 六腿判据的正/反例（含假绿闸：absence 判据无观测不成立 PASS）。
- 触发语铁律（≥10 字无逗号）与模板分支契约（真 flow.parse_step_ref/
  parse_branch_action/match_step_branch 离线复验——条件命中面与近失转写容忍）。
- 物化目标闭合：resp 原文经真 parse_branch_action 剥标记渲染后 == 逐字比对目标。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "probe_branch_action", _ROOT / "scripts" / "probe_branch_action.py")
pba = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("livekit", type(sys)("livekit"))  # 探针顶层 import livekit.rtc
sys.modules.setdefault("livekit.rtc", type(sys)("rtc"))
_SPEC.loader.exec_module(pba)


# ---- 打点解析 -----------------------------------------------------------------
def test_parse_jump_noop_not_eaten_by_jump_prefix():
    events = pba.parse_branch_events([
        "BRANCH_ACTION jump step=5",
        "BRANCH_ACTION jump_noop step=5",
        "BRANCH_CANNED hit step=2 branch_len=13 (call c1)",
        "BRANCH_CANNED miss step=2 branch_len=13 (call c1)",
        "无关日志行",
    ])
    assert [e["kind"] for e in events] == ["jump", "jump_noop", "hit", "miss"]
    assert events[2]["source"] == "canned" and events[3]["source"] == "canned"
    assert events[0]["source"] == "action"
    assert events[2]["step"] == "2"


def test_advance_lines_catches_three_families_only():
    lines = [
        "[flow] rule=auto step=3 (call c1)",
        "[flow] rule=confirm step=2 (call c1)",
        "[flow] judge(bg)=confirm step=3 (call c1)",
        "BRANCH_ACTION hold step=2",
        "[flow] defer-ack (call c1)",
        "[flow] say-step verbatim step=2",
    ]
    assert len(pba.advance_lines(lines)) == 3


def test_evaluate_hold_advance_missing_window_is_false():
    assert pba.evaluate_hold_advance({"trigger": []}) is False
    assert pba.evaluate_hold_advance(
        {"trigger": ["BRANCH_ACTION hold step=2"], "verify": ["[flow] rule=auto step=3"]}
    ) is False
    assert pba.evaluate_hold_advance(
        {"trigger": ["BRANCH_ACTION hold step=2"], "verify": ["TTS_CACHE hit=1"]}
    ) is True


# ---- 判据正/反例（每腿至少一正一反） -------------------------------------------
def _ev(expected: int = 2, *, log: bool = True) -> dict:
    marks = [100 * (i + 1) for i in range(expected + 1)]
    return pba.probe_evidence(log_exists=log, marks=marks, expected_rounds=expected)


def test_canned_leg_positive():
    res = pba.evaluate_leg(
        leg="canned", evidence=_ev(),
        events_by_window={"trigger": pba.parse_branch_events(
            ["BRANCH_CANNED hit step=2 branch_len=13"])},
        turns=[
            {"role": "user", "transcript": pba.TRIGGERS["canned"], "provider": ""},
            {"role": "assistant", "provider": "branch-canned", "gen": "script",
             "template_step": 2, "transcript": pba.CANNED_RESP_TEXT},
        ],
        materialized={pba.CANNED_RESP_RAW: "ok"})
    assert res["pass"] is True


def test_canned_leg_fails_without_materialization_or_exact_text():
    common = dict(
        evidence=_ev(),
        events_by_window={"trigger": pba.parse_branch_events(
            ["BRANCH_CANNED hit step=2 branch_len=13"])},
        turns=[{"role": "assistant", "provider": "branch-canned", "gen": "script",
                "template_step": 2, "transcript": pba.CANNED_RESP_TEXT}])
    assert pba.evaluate_leg(leg="canned", materialized={pba.CANNED_RESP_RAW: "missing"},
                            **common)["pass"] is False
    bad_turns = [common["turns"][0],
                 {**common["turns"][0], "transcript": pba.CANNED_RESP_TEXT + "啊"}]
    assert pba.evaluate_leg(leg="canned", materialized={pba.CANNED_RESP_RAW: "ok"},
                            turns=bad_turns, **{k: v for k, v in common.items()
                                                if k != "turns"})["pass"] is False


def test_refuse_leg_positive_and_negatives():
    turns = [
        {"role": "user", "transcript": pba.TRIGGERS["refuse"], "provider": ""},
        {"role": "assistant", "provider": "branch-refuse", "gen": "script",
         "template_step": 2, "transcript": pba.REFUSE_RESP_TEXT},
    ]
    ok = pba.evaluate_leg(
        leg="refuse", evidence=_ev(),
        events_by_window={"trigger": pba.parse_branch_events(
            ["BRANCH_ACTION refuse step=2 text_len=20"])},
        turns=turns, call_row={"status": "ended", "assist_status": ""},
        refuse_end_elapsed_s=14.5)
    assert ok["pass"] is True
    # gen=llm → 收线前有 LLM 生成
    llm_turns = [turns[0], {**turns[1], "gen": "llm"}]
    assert pba.evaluate_leg(leg="refuse", evidence=_ev(),
                            events_by_window={"trigger": pba.parse_branch_events(
                                ["BRANCH_ACTION refuse step=2 text_len=20"])},
                            turns=llm_turns, call_row={"status": "ended"},
                            refuse_end_elapsed_s=14.5)["pass"] is False
    # 未收线 / 收线超窗 / 收线后还有 LLM 轮
    assert pba.evaluate_leg(leg="refuse", evidence=_ev(),
                            events_by_window={"trigger": pba.parse_branch_events(
                                ["BRANCH_ACTION refuse step=2 text_len=20"])},
                            turns=turns, call_row={"status": "active"},
                            refuse_end_elapsed_s=14.5)["pass"] is False
    assert pba.evaluate_leg(leg="refuse", evidence=_ev(),
                            events_by_window={"trigger": pba.parse_branch_events(
                                ["BRANCH_ACTION refuse step=2 text_len=20"])},
                            turns=turns, call_row={"status": "ended"},
                            refuse_end_elapsed_s=45.0)["pass"] is False
    assert pba.evaluate_leg(leg="refuse", evidence=_ev(),
                            events_by_window={"trigger": pba.parse_branch_events(
                                ["BRANCH_ACTION refuse step=2 text_len=20"])},
                            turns=turns + [{"role": "assistant", "provider": "",
                                            "gen": "llm", "template_step": 2,
                                            "transcript": "还在吗"}],
                            call_row={"status": "ended"},
                            refuse_end_elapsed_s=14.5)["pass"] is False


def test_handoff_leg_positive_and_negatives():
    turns = [
        {"role": "user", "transcript": pba.TRIGGERS["handoff"], "provider": ""},
        {"role": "assistant", "provider": "branch-notify", "gen": "llm",
         "template_step": 2, "transcript": "好的帮您转接"},
        {"role": "user", "transcript": pba.NEUTRAL_TEXT, "provider": ""},
        {"role": "assistant", "provider": "", "gen": "llm",
         "template_step": 3, "transcript": "好的您说"},
    ]
    ok = pba.evaluate_leg(
        leg="handoff", evidence=_ev(expected=3),
        events_by_window={"trigger": pba.parse_branch_events(
            ["BRANCH_ACTION handoff step=2"])},
        turns=turns, call_row={"assist_status": "notified"},
        answered_by_window={"neutral": True})
    assert ok["pass"] is True
    assert pba.evaluate_leg(leg="handoff", evidence=_ev(expected=3),
                            events_by_window={"trigger": pba.parse_branch_events(
                                ["BRANCH_ACTION handoff step=2"])},
                            turns=turns, call_row={"assist_status": ""},
                            answered_by_window={"neutral": True})["pass"] is False
    assert pba.evaluate_leg(leg="handoff", evidence=_ev(expected=3),
                            events_by_window={"trigger": pba.parse_branch_events(
                                ["BRANCH_ACTION handoff step=2"])},
                            turns=turns, call_row={"assist_status": "notified"},
                            answered_by_window={"neutral": False})["pass"] is False


def test_jump_leg_positive_and_noop_move_negative():
    jump_line = "BRANCH_ACTION jump step=5"
    noop_line = "BRANCH_ACTION jump_noop step=5"
    turns = [
        {"role": "assistant", "provider": "branch-jump", "gen": "llm",
         "template_step": 5, "transcript": "x"},
        {"role": "assistant", "provider": "", "gen": "llm",
         "template_step": 5, "transcript": "y"},
    ]
    ok = pba.evaluate_leg(leg="jump", evidence=_ev(expected=3),
                          events_by_window={"trigger": pba.parse_branch_events([jump_line]),
                                            "noop": pba.parse_branch_events([noop_line])},
                          turns=turns)
    assert ok["pass"] is True
    bad = pba.evaluate_leg(leg="jump", evidence=_ev(expected=3),
                           events_by_window={"trigger": pba.parse_branch_events([jump_line]),
                                             "noop": pba.parse_branch_events(
                                                 [jump_line, noop_line])},
                           turns=turns)
    assert bad["pass"] is False


def test_hold_leg_positive_and_negatives():
    turns = [
        {"role": "user", "transcript": pba.TRIGGERS["hold"], "provider": ""},
        {"role": "assistant", "provider": "branch-canned", "gen": "script",
         "template_step": 2, "transcript": pba.HOLD_RESP_TEXT},
        {"role": "user", "transcript": pba.VERIFY_TEXT, "provider": ""},
        {"role": "assistant", "provider": "", "gen": "llm",
         "template_step": 2, "transcript": "好的那我继续给您说明"},
    ]
    base = dict(evidence=_ev(expected=3),
                events_by_window={"trigger": pba.parse_branch_events(
                    ["BRANCH_ACTION hold step=2"])},
                turns=turns,
                answered_by_window={"trigger": True, "verify": True},
                materialized={pba.HOLD_RESP_RAW: "ok"})
    assert pba.evaluate_leg(leg="hold", hold_advance_ok=True, **base)["pass"] is True
    assert pba.evaluate_leg(leg="hold", hold_advance_ok=False, **base)["pass"] is False
    # 验证轮步号漂走（推进了）
    drifted = turns[:3] + [{"role": "assistant", "provider": "", "gen": "llm",
                            "template_step": 3, "transcript": "下一步"}]
    assert pba.evaluate_leg(leg="hold", hold_advance_ok=True,
                            turns=drifted,
                            events_by_window=base["events_by_window"],
                            evidence=base["evidence"],
                            answered_by_window=base["answered_by_window"],
                            materialized=base["materialized"])["pass"] is False


def test_kill_leg_positive_and_fake_green_gates():
    miss = "BRANCH_CANNED miss step=2 branch_len=13 (call c1)"
    turns = [{"role": "assistant", "provider": "", "gen": "llm",
              "template_step": 2, "transcript": "按流程答"}]
    ok = pba.evaluate_leg(leg="kill", evidence=_ev(),
                          events_by_window={"trigger": pba.parse_branch_events([miss])},
                          turns=turns)
    assert ok["pass"] is True and ok["checks"]["killswitch_no_canned_hit"] is True
    # miss 允许、hit 不允许
    assert pba.evaluate_leg(leg="kill", evidence=_ev(),
                            events_by_window={"trigger": pba.parse_branch_events(
                                ["BRANCH_CANNED hit step=2 branch_len=13"])},
                            turns=turns)["pass"] is False
    # 假绿闸：日志缺失 / marks 塌陷 / 零 turns
    assert pba.evaluate_leg(leg="kill", evidence=_ev(log=False),
                            events_by_window={"trigger": pba.parse_branch_events([miss])},
                            turns=turns)["pass"] is False
    assert pba.evaluate_leg(leg="kill",
                            evidence=pba.probe_evidence(log_exists=True, marks=[100],
                                                        expected_rounds=2),
                            events_by_window={"trigger": pba.parse_branch_events([miss])},
                            turns=turns)["pass"] is False
    assert pba.evaluate_leg(leg="kill", evidence=_ev(),
                            events_by_window={"trigger": pba.parse_branch_events([miss])},
                            turns=[])["pass"] is False


# ---- 契约面（真 flow 解析器离线复验） ------------------------------------------
def test_template_contract_with_real_parser():
    flow = pytest.importorskip("agent_runtime.flow")
    parts2 = flow.parse_step_ref(str(pba.PROBE_STEPS[1]["ref"]))
    parts5 = flow.parse_step_ref(str(pba.PROBE_STEPS[4]["ref"]))
    assert len(parts2.branches) == 5
    assert [flow.parse_branch_action(r)[0] for _c, r in parts2.branches] == \
        ["refuse", "handoff", "jump", "hold", ""]
    assert flow.parse_branch_action(parts2.branches[2][1])[1] == 5
    assert flow.parse_branch_action(parts5.branches[0][1])[:2] == ("jump", 5)


def test_sentences_meet_shape_rule():
    texts = [pba.WARMUP_TEXT, *pba.TRIGGERS.values(), pba.NOOP_TEXT,
             pba.NEUTRAL_TEXT, pba.VERIFY_TEXT]
    for t in texts:
        assert len(t) >= 10, t
        assert "," not in t and "，" not in t, t


def test_materialize_targets_close_with_real_parser():
    flow = pytest.importorskip("agent_runtime.flow")
    for raw, want in ((pba.CANNED_RESP_RAW, pba.CANNED_RESP_TEXT),
                      (pba.HOLD_RESP_RAW, pba.HOLD_RESP_TEXT),
                      ("【收线】" + pba.REFUSE_RESP_TEXT, pba.REFUSE_RESP_TEXT)):
        got = flow.render_template_text(flow.parse_branch_action(raw)[2], {})
        assert got == want


def test_trigger_matching_contract_with_real_matcher():
    flow = pytest.importorskip("agent_runtime.flow")
    parts = flow.parse_step_ref(str(pba.PROBE_STEPS[1]["ref"]))
    expect = {
        "canned": ("查运单号是多少", ""),
        "refuse": ("说打错电话了", "refuse"),
        "handoff": ("要找真人客服", "handoff"),
        "jump": ("说包裹几时送到", "jump"),
        "hold": ("说等等先", "hold"),
    }
    for leg, (want_cond, want_act) in expect.items():
        text = pba.TRIGGERS[leg]
        verdict = flow.decide_advance(text, facts=None)
        m = flow.match_step_branch(parts, text, verdict)
        assert m is not None, (leg, text, verdict)
        assert m[0] == want_cond, (leg, m[0])
        assert flow.parse_branch_action(m[1])[0] == want_act, (leg, m[1])


def test_leg_rounds_tables_consistent():
    assert set(pba.LEG_ROUNDS) == {"canned", "refuse", "handoff", "jump", "hold", "kill"}
    for leg, rounds in pba.LEG_ROUNDS.items():
        assert rounds[0][0] == "warmup", leg  # 分支步在 step2，必须先推进
        assert rounds[1][0] == "trigger", leg
    assert pba.LEG_ROUNDS["kill"][1][1] == pba.TRIGGERS["jump"]
