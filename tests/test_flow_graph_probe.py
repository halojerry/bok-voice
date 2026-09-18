"""话术图探针纯函数（2026-09-18）：日志打点解析 / 图轮归属 / 主判据裁决。

探针本体 `scripts/probe_flow_graph.py` 要真栈（CP+LiveKit+模型），这里只钉它的
离线可判部分——判据正反例、`play_miss` 只记信息位、kill-switch 腿语义。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import probe_flow_graph as pfg  # noqa: E402


def _turns(provider: str, step: int) -> list[dict]:
    return [
        {"role": "user", "transcript": "我要投诉", "provider": ""},
        {"role": "assistant", "transcript": "好的", "provider": provider,
         "gen": "llm", "template_step": step},
    ]


_JUMP = {"kind": "jump", "binding": "bnd_7e8f9a0b", "step": "4"}


def test_parse_graph_events_kinds_and_kv():
    events = pfg.parse_graph_events([
        "2026-09-18 10:00:00,000 [INFO] FLOW_GRAPH jump binding=bnd_7e8f9a0b step=4",
        "FLOW_GRAPH play_miss binding=bnd_c1d2e3f4 qa=qa-1",
        "FLOW_GRAPH jump_noop binding=bnd_7e8f9a0b step=4",
        "[flow] rule=auto step=2 (call c1)",  # 非图行不受影响
    ])
    assert events == [
        {"kind": "jump", "binding": "bnd_7e8f9a0b", "step": "4"},
        {"kind": "play_miss", "binding": "bnd_c1d2e3f4", "qa": "qa-1"},
        {"kind": "jump_noop", "binding": "bnd_7e8f9a0b", "step": "4"},
    ]


def test_graph_turn_rows_only_graph_providers():
    rows = pfg.graph_turn_rows([
        {"role": "user", "provider": "", "template_step": 1, "transcript": "我要投诉"},
        {"role": "assistant", "provider": "graph-jump", "template_step": 4,
         "gen": "llm", "transcript": "好的"},
        {"role": "assistant", "provider": "flow-say", "template_step": 5,
         "gen": "script", "transcript": "通知"},
        {"role": "assistant", "provider": "graph-play", "template_step": 5,
         "gen": "qa_fastpath", "transcript": "罐头"},
    ])
    assert [r["provider"] for r in rows] == ["graph-jump", "graph-play"]
    assert rows[0]["template_step"] == 4


def test_transcript_has_keyword_ignores_mechanism_rows():
    assert pfg.transcript_has_keyword(
        [{"role": "user", "transcript": "我要投诉", "provider": ""}],
        pfg.TRIGGER_KEYWORDS,
    )
    # 机制行（storm-listen/starve-ack）不是客户口，不能当作触发证据
    assert not pfg.transcript_has_keyword(
        [{"role": "user", "transcript": "我要投诉", "provider": "storm-listen"}],
        pfg.TRIGGER_KEYWORDS,
    )


def test_graph_on_leg_passes_with_play_miss_info():
    verdict = pfg.evaluate_leg(
        expect_off=False, target_step=4,
        trigger_events=[_JUMP], nontrigger_events=[],
        play_events=[{"kind": "play_miss", "binding": "bnd_c1d2e3f4", "qa": "qa-1"}],
        turns=_turns("graph-jump", 4),
    )
    assert verdict["pass"] is True
    assert verdict["info"]["play_miss"] == 1


def test_graph_on_leg_fails_without_jump_or_with_nontrigger_noise():
    no_jump = pfg.evaluate_leg(
        expect_off=False, target_step=4,
        trigger_events=[], nontrigger_events=[], play_events=[],
        turns=_turns("", 2),
    )
    assert no_jump["pass"] is False
    assert no_jump["checks"]["jump_logged"] is False
    noisy = pfg.evaluate_leg(
        expect_off=False, target_step=4,
        trigger_events=[_JUMP],
        nontrigger_events=[{"kind": "jump_noop", "binding": "bnd_7e8f9a0b"}],
        play_events=[], turns=_turns("graph-jump", 4),
    )
    assert noisy["pass"] is False
    assert noisy["checks"]["nontrigger_silent"] is False


def test_killswitch_leg_requires_zero_graph_traces():
    clean = pfg.evaluate_leg(
        expect_off=True, target_step=4,
        trigger_events=[], nontrigger_events=[], play_events=[],
        turns=[{"role": "assistant", "provider": "", "gen": "llm",
                "template_step": 2, "transcript": "好的"}],
    )
    assert clean["pass"] is True
    dirty = pfg.evaluate_leg(
        expect_off=True, target_step=4,
        trigger_events=[_JUMP], nontrigger_events=[], play_events=[],
        turns=_turns("graph-jump", 4),
    )
    assert dirty["pass"] is False
    assert dirty["checks"]["killswitch_no_logs"] is False
    assert dirty["checks"]["killswitch_no_graph_turns"] is False


def test_build_graph_json_contract():
    import json

    with_qa = json.loads(pfg.build_graph_json("qa-1"))
    assert with_qa["version"] == 1
    assert [i["label"] for i in with_qa["intents"]] == ["投诉", "退款"]
    actions = {b["action"] for b in with_qa["bindings"]}
    assert actions == {"jump_step", "play_qa"}
    # 无 QA 条目 → play 腿不挂（探针信息位跳过，而非造一条悬空 qa_id）
    no_qa = json.loads(pfg.build_graph_json(""))
    assert [b["action"] for b in no_qa["bindings"]] == ["jump_step"]
    assert "我要投诉" in pfg.build_graph_json("")  # 触发词覆盖主说法
