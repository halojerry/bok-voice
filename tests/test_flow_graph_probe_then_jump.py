"""追问链探针腿（Phase 3.3 Task 4）纯函数测试 —— 离线可判面。

真栈两腿（`--then-jump` / `--then-jump --expect-off`）由控制器合并后实跑，这里只钉：

- `plan_rounds`：then-jump 档 =「播」+「跳后」两轮（无 QA 条目/未物化 → 空表=腿跳过）；
  默认档轮次表逐字节同旧。
- `post_jump_step_seen`：跳后步号只看**非** `graph-play` 的转写行（播放轮本体行在跳前
  落库，步号是跳前步，天然不能充当跳后证据；哪怕步号撞上也恒不计入）。
- `evaluate_leg` 的 then-jump 分支：硬判据 `play_logged` + `then_jump_effective`
  （OR 语义=同轮 `FLOW_GRAPH jump … via=then_jump` 日志 · after 轮 `template_step` 命中，
  勘误预检 2：播放轮本体行在跳前落库，跳后步号只能在下一轮看到）；kill 腿判据面不变；
  **默认档（`then_jump=None`）判据集/事件键集一字不差**（零变化铁律）。
- `build_graph_json`：默认调用一个 `then_jump` 键都不带（旧腿/旧断言零变化）。

helper `_ev`/`_turns` 与 `tests/test_flow_graph_probe.py` 同款，在本文件自持（避免跨测试
模块导入的脆弱耦合）。
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
        {"role": "user", "transcript": "我要退款", "provider": ""},
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


# ---------------------------------------------------------------------------
# 轮次表 / 跳后步号归属
# ---------------------------------------------------------------------------
def test_plan_rounds_then_jump_leg_and_default_zero_change():
    kw = dict(trigger_text="我要投诉", nontrigger_text="好的好的", play_text="我要退款",
              after_text="我知道了，你说")
    assert pfg.plan_rounds(then_jump=4, has_qa=True, play_round=True, **kw) == [
        ("play", "我要退款"), ("after", "我知道了，你说")]
    # 无 QA 条目（或未物化）→ 空表：腿跳过必须显式，不许以「没跑出东西」空过成 PASS
    assert pfg.plan_rounds(then_jump=4, has_qa=False, play_round=True, **kw) == []
    # 默认档（无 then_jump）逐字节同旧
    assert pfg.plan_rounds(then_jump=None, has_qa=True, play_round=True, **kw) == [
        ("trigger", "我要投诉"), ("nontrigger", "好的好的"), ("play", "我要退款")]
    assert pfg.plan_rounds(then_jump=None, has_qa=False, play_round=True, **kw) == [
        ("trigger", "我要投诉"), ("nontrigger", "好的好的")]


def test_plan_rounds_then_jump_play_round_flag_is_irrelevant():
    """then-jump 档的「播」轮就是触发轮（无 trigger/nontrigger）——`--no-play-round`
    不得把它关掉，否则本腿零触发语、结构性空跑。"""
    kw = dict(trigger_text="我要投诉", nontrigger_text="好的好的", play_text="我要退款",
              after_text="我知道了，你说")
    assert pfg.plan_rounds(then_jump=4, has_qa=True, play_round=False, **kw) == [
        ("play", "我要退款"), ("after", "我知道了，你说")]


def test_post_jump_step_seen_excludes_play_rows():
    play_row = {"role": "assistant", "provider": "graph-play", "template_step": 2,
                "transcript": "罐头"}
    next_row = {"role": "assistant", "provider": "", "template_step": 4, "transcript": "好的"}
    assert pfg.post_jump_step_seen([play_row, next_row], 4) is True
    assert pfg.post_jump_step_seen([next_row], 4) is True
    # 只有播放轮本体（步号=跳前步）→ 不算；graph-play 行恒不计入（哪怕步号撞上）
    assert pfg.post_jump_step_seen([play_row], 4) is False
    assert pfg.post_jump_step_seen([{**play_row, "template_step": 4}], 4) is False
    # 其它步号不算；空表不算；客户轮不计入
    assert pfg.post_jump_step_seen([next_row], 5) is False
    assert pfg.post_jump_step_seen([], 4) is False
    assert pfg.post_jump_step_seen(
        [{"role": "user", "provider": "", "template_step": 4, "transcript": "退款"}], 4
    ) is False


# ---------------------------------------------------------------------------
# then-jump 腿判据
# ---------------------------------------------------------------------------
def test_evaluate_leg_then_jump_hard_checks():
    play_ms = {"kind": "play", "binding": "bnd_c1d2e3f4", "qa": "qa-1"}
    jmp = {"kind": "jump", "binding": "bnd_c1d2e3f4", "step": "4", "via": "then_jump"}
    kw = dict(expect_off=False, target_step=4, then_jump=4, trigger_events=[],
              nontrigger_events=[], after_events=[], evidence=_EV_OK)

    by_log = pfg.evaluate_leg(play_events=[play_ms, jmp], turns=_turns("graph-play", 2), **kw)
    assert by_log["pass"] is True
    assert set(by_log["checks"]) == {"evidence_ok", "play_logged", "then_jump_effective"}
    assert by_log["checks"]["then_jump_effective"] is True
    assert by_log["info"]["then_jump_logged"] is True and by_log["info"]["next_turn_step"] is False

    by_next = pfg.evaluate_leg(play_events=[play_ms], turns=_turns("", 4), **kw)
    assert by_next["pass"] is True and by_next["info"]["next_turn_step"] is True
    assert by_next["info"]["then_jump_logged"] is False

    # play_miss（罐头未物化）→ play_logged 硬 FAIL：本腿主判据就是「播+跳」
    miss = pfg.evaluate_leg(play_events=[{"kind": "play_miss", "binding": "bnd_c1d2e3f4"}],
                            turns=_turns("graph-play", 2), **kw)
    assert miss["pass"] is False and miss["checks"]["play_logged"] is False

    # 播了但没跳（无日志、下一轮也没新步号）→ then_jump_effective FAIL
    dead = pfg.evaluate_leg(play_events=[play_ms], turns=_turns("graph-play", 2), **kw)
    assert dead["pass"] is False and dead["checks"]["then_jump_effective"] is False

    # 有 jump 但 via 不是 then_jump（jump_step 绑定的图内跳）/步号不符 → 不算链证据
    for wrong in ({"kind": "jump", "binding": "bnd_c1d2e3f4", "step": "4"},
                  {"kind": "jump", "binding": "bnd_c1d2e3f4", "step": "4", "via": "manual"},
                  {"kind": "jump", "binding": "bnd_c1d2e3f4", "step": "3", "via": "then_jump"},
                  {"kind": "jump_noop", "binding": "bnd_c1d2e3f4", "step": "4",
                   "via": "then_jump"}):
        v = pfg.evaluate_leg(play_events=[play_ms, wrong], turns=_turns("graph-play", 2), **kw)
        assert v["checks"]["then_jump_effective"] is False, wrong
        assert v["pass"] is False, wrong

    # 观测面坏（日志缺失）→ 共享前提 evidence_ok FAIL（与旧腿同闸；正向判据照实报命中）
    blind = pfg.evaluate_leg(play_events=[play_ms, jmp], turns=_turns("graph-play", 2),
                             expect_off=False, target_step=4, then_jump=4, trigger_events=[],
                             nontrigger_events=[], after_events=[], evidence=_ev(log=False))
    assert blind["checks"]["evidence_ok"] is False and blind["pass"] is False
    assert blind["checks"]["then_jump_effective"] is True


def test_evaluate_leg_then_jump_kill_leg_and_default_shape():
    # kill 腿判据面不变（仍 absence-based + evidence 前提）
    clean = pfg.evaluate_leg(expect_off=True, target_step=4, then_jump=4,
                             trigger_events=[], nontrigger_events=[], play_events=[],
                             after_events=[], turns=[], evidence=_EV_OK)
    assert clean["pass"] is True
    assert set(clean["checks"]) == {"evidence_ok", "killswitch_no_logs", "killswitch_no_graph_turns"}
    dirty = pfg.evaluate_leg(expect_off=True, target_step=4, then_jump=4,
                             trigger_events=[], nontrigger_events=[],
                             play_events=[{"kind": "play", "binding": "b"}],
                             after_events=[], turns=[], evidence=_EV_OK)
    assert dirty["checks"]["killswitch_no_logs"] is False
    # kill 腿 absence 面必须覆盖 after 窗口（跳后轮冒出图痕迹同属越闸）
    late = pfg.evaluate_leg(expect_off=True, target_step=4, then_jump=4,
                            trigger_events=[], nontrigger_events=[], play_events=[],
                            after_events=[{"kind": "jump", "binding": "b", "step": "4",
                                           "via": "then_jump"}],
                            turns=_turns("graph-play", 4), evidence=_EV_OK)
    assert late["checks"]["killswitch_no_logs"] is False
    assert late["checks"]["killswitch_no_graph_turns"] is False
    # 默认档（then_jump=None）判据面逐字节同旧
    plain = pfg.evaluate_leg(expect_off=False, target_step=4, trigger_events=[_JUMP],
                             nontrigger_events=[], play_events=[],
                             turns=_turns("graph-jump", 4), evidence=_EV_OK)
    assert set(plain["checks"]) == {"evidence_ok", "jump_logged", "trigger_turn_provider",
                                    "nontrigger_silent"}
    assert set(plain["events"]) == {"trigger", "nontrigger", "play"}
    assert "then_jump_logged" not in plain["info"] and "next_turn_step" not in plain["info"]
    # then-jump 档的事件面多一个 after 窗口（信息位逐窗可读）
    tj = pfg.evaluate_leg(expect_off=False, target_step=4, then_jump=4, trigger_events=[],
                          nontrigger_events=[], play_events=[], after_events=[],
                          turns=[], evidence=_EV_OK)
    assert set(tj["events"]) == {"trigger", "nontrigger", "play", "after"}


def test_build_graph_json_then_jump_binding():
    with_tj = json.loads(pfg.build_graph_json("qa-1", then_jump=4))
    play = next(b for b in with_tj["bindings"] if b["action"] == "play_qa")
    assert play["then_jump"] == 4
    # 其余绑定/意图面零变化（链只挂在 play_qa 那条上）
    jump_binding = next(b for b in with_tj["bindings"] if b["action"] == "jump_step")
    assert "then_jump" not in jump_binding and jump_binding["step"] == pfg.TARGET_STEP
    # 默认调用（旧签名）一个 then_jump 键都不带 → 旧腿/旧断言零变化
    assert all("then_jump" not in b for b in json.loads(pfg.build_graph_json("qa-1"))["bindings"])
    # 无 QA 条目时 play_qa 绑定本就不挂，then_jump 不得凭空造出来
    no_qa = json.loads(pfg.build_graph_json("", then_jump=4))
    assert [b["action"] for b in no_qa["bindings"]] == ["jump_step"]


# ---------------------------------------------------------------------------
# 勘误预检 2：after 轮话术必须停在 UNCLEAR（避开规则推进/收线/异议四族词）
# ---------------------------------------------------------------------------
def test_default_after_text_avoids_rule_families():
    """默认 `--after-text` 是硬判据的观测前提（勘误预检 2）：命中 `_CONFIRM_RE`
    单字（好/是/对/嗯/系/係…）→ 规则推进把步号 4→5，after 轮步号判据被规则推进
    污染（本腿硬判据因此取 OR 语义，但默认文本仍须尽量干净）；命中 REFUSE/FAREWELL
    → 收线冻结；命中提问/拖延 → 语义漂移。

    这里对着 `agent_runtime.flow` 的真 regex 逐族钉（只读其正则，不引入 agent.py）。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))
    from agent_runtime import flow  # noqa: PLC0415

    text = pfg.AFTER_TEXT
    for name in ("_CONFIRM_RE", "_QUESTION_RE", "_DEFER_RE", "_REFUSE_RE",
                 "_FAREWELL_RE", "_DENY_RE", "_HANGUP_RE"):
        rx = getattr(flow, name)
        assert not rx.search(text), f"{name} 命中了 after 话术 {text!r}"
    # 也不得撞上图的两个触发意图词（退款/投诉）——撞了 after 轮会再触发一次图动作
    for word in pfg.PLAY_KEYWORDS + pfg.TRIGGER_KEYWORDS:
        assert not re.search(re.escape(word), text, re.IGNORECASE), word
