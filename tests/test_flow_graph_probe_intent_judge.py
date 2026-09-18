"""意图 judge 探针腿（Phase 3.4 Task 3）纯函数测试 —— 离线可判面。

真栈两腿（`--intent-judge` / `--intent-judge --expect-off`）由控制器合并后实跑，这里钉：

- `plan_rounds`：intent-judge 档 =「fuzzy」+「consume」两轮（无 QA 依赖，永不空表）；
  `play_round` 旗标在本档不适用；默认/then-jump 档轮次表零变化。
- `evaluate_leg` intent-judge 分支：硬判据 `judge_scheduled`（fuzzy 窗口）+
  `judge_hit_logged`（全局尾扫）+ `judge_effective`（pending_fired **且** consume
  jump/turn 行双锚）；kill 腿判据面**额外**含 `killswitch_no_judge_logs`（judge
  日志族不在 RE_FLOW_GRAPH，不单独并入会逃过 no_logs）；absence 判据吃 evidence 闸。
- `build_graph_json(judge=)`：默认不带 judge 键（旧腿零变化）；judge 档挂投诉意图。
- `parse_judge_events`：k=v 拆词；非 judge 的 FLOW_GRAPH 行不收。
- `FUZZY_TEXT` 洁净性：不撞图全部触发/play 关键词（腿前提——撞上即关键词路径，
  judge 根本不调度）；不撞规则十族词（DEFER/REFUSE/FAREWELL 会令 fuzzy 轮在图块
  之前被截走/收线冻结，judge_scheduled 结构性打不出来）。

helper `_ev`/`_turns` 与 tests/test_flow_graph_probe_then_jump.py 同款自持。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import probe_flow_graph as pfg  # noqa: E402


def _turns(provider: str, step: int) -> list[dict]:
    return [
        {"role": "user", "transcript": "你们拖了半个月都不处理", "provider": ""},
        {"role": "assistant", "transcript": "好的", "provider": provider,
         "gen": "llm", "template_step": step},
    ]


def _ev(*, log: bool = True, marks: list[int] | None = None, expected: int = 2) -> dict:
    return pfg.probe_evidence(
        log_exists=log, marks=[100, 200, 300] if marks is None else marks,
        expected_rounds=expected,
    )


_EV_OK = _ev()
_KW = dict(trigger_text="我要投诉", nontrigger_text="好的好的", play_text="我要退款",
           after_text="我知道了，你说")

_SCHEDULED = {"kind": "judge_scheduled", "intents": "1", "step": "1"}
_HIT = {"kind": "judge_hit", "intent": "int_1a2b3c4d", "step": "1"}
_FIRED = {"kind": "judge_pending_fired", "binding": "bnd_7e8f9a0b", "step": "1"}
_JUMP = {"kind": "jump", "binding": "bnd_7e8f9a0b", "step": "4"}


# ---------------------------------------------------------------------------
# 轮次表
# ---------------------------------------------------------------------------
def test_plan_rounds_intent_judge_leg():
    assert pfg.plan_rounds(then_jump=None, has_qa=False, intent_judge=True,
                           play_round=True, **_KW) == [
        ("fuzzy", pfg.FUZZY_TEXT), ("consume", "我知道了，你说")]
    # play_round 旗标在本档不适用（无 play 信息位轮可言）
    assert pfg.plan_rounds(then_jump=None, has_qa=True, intent_judge=True,
                           play_round=False, **_KW) == [
        ("fuzzy", pfg.FUZZY_TEXT), ("consume", "我知道了，你说")]
    # 自定义 fuzzy 话术透传
    assert pfg.plan_rounds(then_jump=None, has_qa=False, intent_judge=True,
                           play_round=True, fuzzy_text="气死我了", **_KW)[0] == (
        "fuzzy", "气死我了")


def test_plan_rounds_default_and_then_jump_zero_change():
    """intent_judge 缺省时默认/then-jump 档轮次表逐字节同旧（零变化铁律）。"""
    assert pfg.plan_rounds(then_jump=None, has_qa=True, play_round=True, **_KW) == [
        ("trigger", "我要投诉"), ("nontrigger", "好的好的"), ("play", "我要退款")]
    assert pfg.plan_rounds(then_jump=4, has_qa=True, play_round=True, **_KW) == [
        ("play", "我要退款"), ("after", "我知道了，你说")]


# ---------------------------------------------------------------------------
# evaluate_leg intent-judge 分支
# ---------------------------------------------------------------------------
def test_evaluate_intent_judge_positive_path():
    v = pfg.evaluate_leg(
        expect_off=False, target_step=4, intent_judge=True,
        trigger_events=[], nontrigger_events=[], play_events=[],
        fuzzy_events=[_SCHEDULED], consume_events=[_JUMP],
        judge_events=[_HIT, _FIRED],
        turns=_turns("graph-jump", 4), evidence=_EV_OK)
    assert v["pass"] and v["checks"] == {
        "evidence_ok": True, "judge_scheduled": True,
        "judge_hit_logged": True, "judge_effective": True}
    # turn 行锚单独成立（consume jump 日志缺席时 graph-jump@4 仍救得回 judge_effective）
    v2 = pfg.evaluate_leg(
        expect_off=False, target_step=4, intent_judge=True,
        trigger_events=[], nontrigger_events=[], play_events=[],
        fuzzy_events=[_SCHEDULED], consume_events=[],
        judge_events=[_HIT, _FIRED],
        turns=_turns("graph-jump", 4), evidence=_EV_OK)
    assert v2["pass"]


def test_evaluate_intent_judge_negative_paths():
    base = dict(expect_off=False, target_step=4, intent_judge=True,
                trigger_events=[], nontrigger_events=[], play_events=[],
                turns=_turns("", 2), evidence=_EV_OK)
    # 9B 判 miss（判据不贴合转写）
    assert not pfg.evaluate_leg(fuzzy_events=[_SCHEDULED], consume_events=[],
                                judge_events=[{"kind": "judge_miss", "step": "1"}],
                                **base)["pass"]
    # hit 了但 pending 没被消费（consume 轮缺失/过期）
    assert not pfg.evaluate_leg(fuzzy_events=[_SCHEDULED], consume_events=[],
                                judge_events=[_HIT], **base)["pass"]
    # scheduled 都没打（关键词命中走了同步路径/图未开/引擎没装判据）
    assert not pfg.evaluate_leg(fuzzy_events=[], consume_events=[_JUMP],
                                judge_events=[_HIT, _FIRED], **base)["pass"]
    # fired 了但 consume 侧零 jump 痕迹（jump 分支没跑=诡异路径，双锚逮住）
    assert not pfg.evaluate_leg(fuzzy_events=[_SCHEDULED], consume_events=[],
                                judge_events=[_HIT, _FIRED], **base)["pass"]


def test_evaluate_intent_judge_kill_leg():
    base = dict(expect_off=True, target_step=4, intent_judge=True,
                trigger_events=[], nontrigger_events=[], play_events=[],
                fuzzy_events=[], consume_events=[], turns=[])
    # 正例：零图 + 零 judge 痕迹
    v = pfg.evaluate_leg(judge_events=[], evidence=_EV_OK, **base)
    assert v["pass"] and v["checks"]["killswitch_no_judge_logs"]
    # judge_scheduled 残留被 no_judge_logs 逮到（RE_FLOW_GRAPH 不收 judge 行，
    # 不并入这条断言面 judge 残留会逃过 no_logs）
    v2 = pfg.evaluate_leg(judge_events=[_SCHEDULED], evidence=_EV_OK, **base)
    assert not v2["pass"] and not v2["checks"]["killswitch_no_judge_logs"]
    # 假绿闸：日志缺失 → 零痕迹不成立
    v3 = pfg.evaluate_leg(judge_events=[], evidence=_ev(log=False), **base)
    assert not v3["pass"]


def test_evaluate_default_leg_keys_zero_change():
    """默认档/then-jump 档的判据键集不含 judge 面（intent_judge 缺省零变化）。"""
    v = pfg.evaluate_leg(
        expect_off=False, target_step=4,
        trigger_events=[_JUMP], nontrigger_events=[], play_events=[],
        turns=_turns("graph-jump", 4), evidence=_EV_OK)
    assert set(v["checks"]) == {"evidence_ok", "jump_logged",
                                "trigger_turn_provider", "nontrigger_silent"}


# ---------------------------------------------------------------------------
# build_graph_json / parse_judge_events
# ---------------------------------------------------------------------------
def test_build_graph_json_judge_key_projection():
    plain = json.loads(pfg.build_graph_json("qa-1"))
    tj = json.loads(pfg.build_graph_json("qa-1", then_jump=4))
    assert all("judge" not in i for i in plain["intents"] + tj["intents"])
    doc = json.loads(pfg.build_graph_json("qa-1", judge=True))
    intent = next(i for i in doc["intents"] if i["id"] == "int_1a2b3c4d")
    prompt = intent["judge"]["prompt"]
    assert 1 <= len(prompt) <= 400  # CP 严格校验面同档


def test_parse_judge_events_extraction():
    evs = pfg.parse_judge_events([
        "2026-09-19 10:00:00 [INFO] FLOW_GRAPH judge_scheduled intents=2 step=1 (call c-1)",
        "FLOW_GRAPH judge_hit intent=int_1a2b3c4d step=1 (call c-1)",
        "FLOW_GRAPH judge_skipped reason=stale (call c-1)",
        "FLOW_GRAPH jump binding=bnd_7e8f9a0b step=4",  # 非 judge 行不收
        "噪声行",
    ])
    assert evs == [
        {"kind": "judge_scheduled", "intents": "2", "step": "1"},
        {"kind": "judge_hit", "intent": "int_1a2b3c4d", "step": "1"},
        {"kind": "judge_skipped", "reason": "stale"},
    ]


# ---------------------------------------------------------------------------
# FUZZY_TEXT 洁净性（腿前提）
# ---------------------------------------------------------------------------
def test_fuzzy_text_avoids_keywords_and_rule_families():
    """fuzzy 话术是硬判据的观测前提：撞关键词 → 同步路径先行、judge 根本不调度；
    撞 DEFER/REFUSE/FAREWELL → fuzzy 轮在图块之前被截走/收线冻结。对着
    `agent_runtime.flow` 真 regex 十族 + 图全部触发词逐项钉。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))
    from agent_runtime import flow  # noqa: PLC0415

    text = pfg.FUZZY_TEXT
    for name in ("_CONFIRM_RE", "_QUESTION_RE", "_DEFER_RE", "_REFUSE_RE",
                 "_FAREWELL_RE", "_DENY_RE", "_HANGUP_RE",
                 "_STRONG_AFFIRM_RE", "_CONFIRM_TURN_RE", "_REPEAT_RE"):
        rx = getattr(flow, name)
        assert not rx.search(text), f"{name} 命中了 fuzzy 话术 {text!r}"
    for word in pfg.PLAY_KEYWORDS + pfg.TRIGGER_KEYWORDS:
        assert not re.search(re.escape(word), text, re.IGNORECASE), word
