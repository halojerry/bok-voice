from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mock_callee.py"
_spec = importlib.util.spec_from_file_location("mock_callee", _SCRIPT)
assert _spec and _spec.loader
mock_callee = importlib.util.module_from_spec(_spec)
sys.modules["mock_callee"] = mock_callee
_spec.loader.exec_module(mock_callee)


def test_plan_timeline_answer():
    tl = mock_callee.plan_timeline("answer", ring_delay_s=3.0, lines=2)
    assert tl[0] == ("join", 3.0)
    assert [e for e, _ in tl if e == "speak"] == ["speak", "speak"]
    assert tl[-1][0] == "leave"


def test_plan_timeline_no_answer():
    # no_answer:永不进房(只等响铃窗耗尽后退出)。
    tl = mock_callee.plan_timeline("no_answer", ring_delay_s=3.0, lines=2)
    assert [e for e, _ in tl] == ["exit"]


def test_plan_timeline_reject():
    # reject:进房即走——join ≈ ring_delay,leave ≈ +0.5s(必须落在 agent 1.5s 窗内)。
    tl = mock_callee.plan_timeline("reject", ring_delay_s=3.0, lines=2)
    assert [e for e, _ in tl] == ["join", "leave"]
    assert tl[0][1] == 3.0
    assert 3.0 < tl[1][1] <= 3.0 + 1.5


def test_plan_timeline_hangup_mid():
    tl = mock_callee.plan_timeline("hangup_mid", ring_delay_s=3.0, lines=3)
    assert [e for e, _ in tl].count("speak") == 1
    assert tl[-1][0] == "leave"


def test_plan_timeline_answer_timing_contract():
    # 首句 ~0.8s 起(agent 进房后 1.5s 窗过了才出声,不会被判 reject)。
    tl = mock_callee.plan_timeline("answer", ring_delay_s=1.0, lines=3)
    speaks = [at for e, at in tl if e == "speak"]
    assert len(speaks) == 3
    assert speaks[0] == 1.8
    assert speaks == sorted(speaks)
    assert tl[-1][0] == "leave" and tl[-1][1] > speaks[-1]


def test_plan_timeline_unknown_scenario_defaults_to_answer():
    tl = mock_callee.plan_timeline("bogus", ring_delay_s=2.0, lines=1)
    assert tl[0][0] == "join" and tl[-1][0] == "leave"


def test_plan_timeline_lines_floor():
    # lines<=0 时 answer 至少说一句,避免零台词。
    tl = mock_callee.plan_timeline("answer", ring_delay_s=0.0, lines=0)
    assert [e for e, _ in tl].count("speak") == 1


def test_plan_timeline_negative_ring_delay_clamped():
    tl = mock_callee.plan_timeline("answer", ring_delay_s=-5.0, lines=1)
    assert tl[0] == ("join", 0.0)


def test_parse_args_defaults_and_roundtrip():
    args = mock_callee.parse_args(
        [
            "--url", "ws://127.0.0.1:7880",
            "--token", "jwt",
            "--identity", "sip-mock-10086",
        ]
    )
    assert args.url == "ws://127.0.0.1:7880"
    assert args.token == "jwt"
    assert args.identity == "sip-mock-10086"
    assert args.scenario == "answer"
    assert args.language == "cantonese"
    assert args.script_json == "[]"
    assert args.script() == []
    assert args.ring_delay == 3.0
    assert args.ringing_window == 35.0
    assert args.hangup_after_turns == 0


def test_parse_args_script_json_list():
    args = mock_callee.parse_args(
        [
            "--url", "ws://x", "--token", "t", "--identity", "i",
            "--script-json", '["你好","我個件未到"]',
            "--scenario", "reject", "--language", "en",
            "--ring-delay", "1.5", "--ringing-window", "20",
            "--hangup-after-turns", "2",
        ]
    )
    assert args.script() == ["你好", "我個件未到"]
    assert args.scenario == "reject"
    assert args.language == "en"
    assert args.ring_delay == 1.5
    assert args.ringing_window == 20.0
    assert args.hangup_after_turns == 2


def test_parse_args_script_json_malformed_is_empty():
    args = mock_callee.parse_args(
        ["--url", "ws://x", "--token", "t", "--identity", "i", "--script-json", "not-json"]
    )
    assert args.script() == []


def test_language_normalized_to_three_states():
    # 语言三态 zh/cantonese/en;旧拼写/未知值一律回落 cantonese(B 线规范值)。
    assert mock_callee.normalize_language("zh") == "zh"
    assert mock_callee.normalize_language("cantonese") == "cantonese"
    assert mock_callee.normalize_language("en") == "en"
    assert mock_callee.normalize_language("yue") == "cantonese"
    assert mock_callee.normalize_language("") == "cantonese"


def test_log_event_line_format():
    line = mock_callee.event_line("join", 3.0, "sip-mock-64320111")
    assert line == "MOCK_CALLEE event=join at=3.0 identity=sip-mock-64320111"
