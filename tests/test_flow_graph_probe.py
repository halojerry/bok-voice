"""话术图探针纯函数（2026-09-18）：日志打点解析 / 图轮归属 / 主判据裁决 / 观测前提。

探针本体 `scripts/probe_flow_graph.py` 要真栈（CP+LiveKit+模型），这里只钉它的
离线可判部分——判据正反例、`play_miss` 只记信息位、kill-switch 腿语义，以及
review R1 的**观测前提**（absent 判据不许在没真观测时空过成 PASS）。
"""

from __future__ import annotations

import json
import math
import struct
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


def _ev(*, log: bool = True, marks: list[int] | None = None, expected: int = 2) -> dict:
    """诚实路径默认：2 轮 → 3 个 mark，每轮窗口都有新字节。"""
    return pfg.probe_evidence(
        log_exists=log, marks=[100, 200, 300] if marks is None else marks,
        expected_rounds=expected,
    )


_EV_OK = _ev()


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
        evidence=_EV_OK,
    )
    assert verdict["pass"] is True
    assert verdict["info"]["play_miss"] == 1
    assert verdict["checks"]["evidence_ok"] is True


def test_graph_on_leg_fails_without_jump_or_with_nontrigger_noise():
    no_jump = pfg.evaluate_leg(
        expect_off=False, target_step=4,
        trigger_events=[], nontrigger_events=[], play_events=[],
        turns=_turns("", 2),
        evidence=_EV_OK,
    )
    assert no_jump["pass"] is False
    assert no_jump["checks"]["jump_logged"] is False
    noisy = pfg.evaluate_leg(
        expect_off=False, target_step=4,
        trigger_events=[_JUMP],
        nontrigger_events=[{"kind": "jump_noop", "binding": "bnd_7e8f9a0b"}],
        play_events=[], turns=_turns("graph-jump", 4),
        evidence=_EV_OK,
    )
    assert noisy["pass"] is False
    assert noisy["checks"]["nontrigger_silent"] is False


def test_killswitch_leg_requires_zero_graph_traces():
    clean = pfg.evaluate_leg(
        expect_off=True, target_step=4,
        trigger_events=[], nontrigger_events=[], play_events=[],
        turns=[{"role": "assistant", "provider": "", "gen": "llm",
                "template_step": 2, "transcript": "好的"}],
        evidence=_EV_OK,
    )
    assert clean["pass"] is True
    dirty = pfg.evaluate_leg(
        expect_off=True, target_step=4,
        trigger_events=[_JUMP], nontrigger_events=[], play_events=[],
        turns=_turns("graph-jump", 4),
        evidence=_EV_OK,
    )
    assert dirty["pass"] is False
    assert dirty["checks"]["killswitch_no_logs"] is False
    assert dirty["checks"]["killswitch_no_graph_turns"] is False


# ---------------------------------------------------------------------------
# review R1：absence-based 判据的观测前提（无观测不成 PASS —— 防假绿）
# ---------------------------------------------------------------------------
def test_probe_evidence_honest_and_dishonest_shapes():
    ok = pfg.probe_evidence(log_exists=True, marks=[100, 200, 300], expected_rounds=2)
    assert ok["ok"] is True and ok["rounds_complete"] and ok["grew_each_round"]
    # 日志缺失（零可观测）
    assert pfg.probe_evidence(log_exists=False, marks=[100, 200, 300],
                              expected_rounds=2)["ok"] is False
    # marks 塌成一个（一轮未观测：中途异常/提前退出）
    assert pfg.probe_evidence(log_exists=True, marks=[100], expected_rounds=2)["ok"] is False
    # 中途异常：少一个 mark
    assert pfg.probe_evidence(log_exists=True, marks=[100, 200], expected_rounds=2)["ok"] is False
    # 某轮窗口零字节（该轮没真跑起来）→ 该窗口「零命中」不成立
    assert pfg.probe_evidence(log_exists=True, marks=[100, 100, 300],
                              expected_rounds=2)["ok"] is False
    # 空 marks（连起点都没有）
    assert pfg.probe_evidence(log_exists=True, marks=[], expected_rounds=2)["ok"] is False


def test_killswitch_leg_cannot_pass_without_evidence():
    """③ 假绿形状：日志缺失/无观测时「全程零 FLOW_GRAPH」不许判 PASS。"""
    for broken in (
        _ev(log=False, marks=[100, 200, 300]),
        _ev(marks=[100]),                # marks 塌成一个
        _ev(marks=[100, 200]),           # 中途异常少 mark
    ):
        verdict = pfg.evaluate_leg(
            expect_off=True, target_step=4,
            trigger_events=[], nontrigger_events=[], play_events=[],
            turns=[], evidence=broken,
        )
        assert verdict["pass"] is False, broken
        assert verdict["checks"]["evidence_ok"] is False
        assert verdict["checks"]["killswitch_no_logs"] is False
        assert verdict["checks"]["killswitch_no_graph_turns"] is False


def test_nontrigger_silent_cannot_pass_vacuously_without_evidence():
    """② 假绿形状：中途异常留下空窗口 → `nontrigger_silent` 不许空过。"""
    verdict = pfg.evaluate_leg(
        expect_off=False, target_step=4,
        trigger_events=[_JUMP], nontrigger_events=[],
        play_events=[], turns=_turns("graph-jump", 4),
        evidence=_ev(marks=[100, 200]),   # 只跑完 1 轮，非触发轮根本没观测
    )
    assert verdict["checks"]["nontrigger_silent"] is False
    assert verdict["pass"] is False
    # 正向判据仍按真观测算：同场景下 jump 日志确实看到了 → 该条仍 PASS（定位清晰）
    assert verdict["checks"]["jump_logged"] is True


def test_positive_checks_need_no_evidence_precondition():
    """正向判据（真看到事件）不受 evidence 闸影响——观测面坏了也照实报命中。"""
    verdict = pfg.evaluate_leg(
        expect_off=False, target_step=4,
        trigger_events=[_JUMP], nontrigger_events=[],
        play_events=[], turns=_turns("graph-jump", 4),
        evidence=_ev(log=False),
    )
    assert verdict["checks"]["evidence_ok"] is False
    assert verdict["checks"]["jump_logged"] is True
    assert verdict["checks"]["trigger_turn_provider"] is True
    assert verdict["pass"] is False


def test_build_graph_json_contract():
    with_qa = json.loads(pfg.build_graph_json("qa-1"))
    assert with_qa["version"] == 1
    assert [i["label"] for i in with_qa["intents"]] == ["投诉", "退款"]
    actions = {b["action"] for b in with_qa["bindings"]}
    assert actions == {"jump_step", "play_qa"}
    # 无 QA 条目 → play 腿不挂（探针信息位跳过，而非造一条悬空 qa_id）
    no_qa = json.loads(pfg.build_graph_json(""))
    assert [b["action"] for b in no_qa["bindings"]] == ["jump_step"]
    assert "我要投诉" in pfg.build_graph_json("")  # 触发词覆盖主说法


# ---------------------------------------------------------------------------
# 起推前「播完」判据的纯函数（2026-09-18 实弹 pacing 修复）
# ---------------------------------------------------------------------------
def _tone(seconds: float, *, amp: int = 3000, sr: int = 16000) -> bytes:
    n = int(sr * seconds)
    return b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * 440 * i / sr)))
                    for i in range(n))


def _silence(seconds: float, *, sr: int = 16000) -> bytes:
    return b"\x00\x00" * int(sr * seconds)


def test_trailing_silence_s_real_seconds():
    """末尾静音按**真实秒**计（640B=20ms @16k/16bit）——阈值语义无 2× 歧义。"""
    assert round(pfg.trailing_silence_s(_tone(1.0) + _silence(0.5)), 2) == 0.5
    assert round(pfg.trailing_silence_s(_silence(2.0)), 2) == 2.0
    # 全程语音 → 0；空缓冲 → 0（「还没出声」不许当成「已安静」）
    assert pfg.trailing_silence_s(_tone(1.0)) == 0.0
    assert pfg.trailing_silence_s(b"") == 0.0


def test_speech_seconds_is_incremental():
    pcm = _tone(1.0) + _silence(1.0)
    speech, processed = pfg.speech_seconds(pcm, 0)
    assert round(speech, 2) == 1.0
    assert processed == len(pcm)
    # 增量：无新帧 → 0 新增（不重复计费已扫段）
    again, processed2 = pfg.speech_seconds(pcm, processed)
    assert again == 0.0 and processed2 == processed
    # 续加新语音照常计
    more, _ = pfg.speech_seconds(pcm + _tone(0.5), processed)
    assert round(more, 2) == 0.5


def test_wait_playout_end_stops_at_tail_silence_not_frame_growth():
    """实弹形状回归：音轨**空闲仍持续推帧**（纯静音，实测 ~2× 实时）——
    旧版用「字节数不再增长」判停嘴结构性不可达，每轮空烧满超时。
    现判据=末尾静音窗（+语音下限），帧一直长也照样停。"""
    import asyncio

    async def _run() -> tuple[float, bool]:
        buf = bytearray(_tone(1.0))
        stop = False

        async def feed() -> None:
            while not stop:
                await asyncio.sleep(0.02)
                buf.extend(_silence(0.05))  # 空闲仍推纯静音帧
        feeder = asyncio.get_running_loop().create_task(feed())
        try:
            return await pfg.wait_playout_end(buf, quiet_s=0.3, min_speech_s=0.5,
                                              timeout_s=8.0)
        finally:
            stop = True
            feeder.cancel()

    waited, ok = asyncio.run(_run())
    assert ok is True, f"应靠末尾静音停住（实等 {waited:.2f}s）"
    assert waited < 5.0, f"不该烧满超时（实等 {waited:.2f}s）"


def test_wait_playout_end_empty_buffer_does_not_return_early():
    """空缓冲（还没出声）→ 末尾静音 0，必须等满超时而不是秒过。"""
    import asyncio

    waited, ok = asyncio.run(
        pfg.wait_playout_end(bytearray(), quiet_s=0.3, min_speech_s=0.5, timeout_s=1.0)
    )
    assert ok is False
    assert waited >= 0.9, f"空缓冲不许秒过（实等 {waited:.2f}s）"
