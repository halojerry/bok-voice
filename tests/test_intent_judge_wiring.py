"""3.4 意图引擎接线钉死（review F1/F5 回归锚）。

闭包接线（消费时序/调度结构/单飞时序）离线起不了真栈——源级锚同
test_flow_graph_then_jump.py 姿势：文本切片钉结构，改接线形态即红。
"""
from __future__ import annotations

from pathlib import Path

_SRC = (
    Path(__file__).resolve().parents[1] / "apps" / "agent" / "agent_runtime" / "agent.py"
).read_text(encoding="utf-8")


def test_scheduler_pinned_to_user_text_branch():
    """F1:调度调用必须钉在 `elif user_text:` 分支体内（图块真求值过 + 话语非空）。

    旧形态 `else:` 与图激活块平级——空转写轮（user_text 为空令图块整体跳过）会
    漏进调度：白烧一次 9B 之外，挂上的 pending 在下一轮无话语支撑地触发绑定。
    """
    head = _SRC.index("elif user_text:")
    call = _SRC.index("_maybe_schedule_intent_judge(user_text)")
    between = _SRC[head:call]
    # 同一分支体内：中间不得再出现分支头/函数定义（出现=调用被挪进别的结构）
    for forbidden in ("\n            elif ", "\n            else:", "\n    def ", "\n    async def "):
        assert forbidden not in between, forbidden
    assert _SRC.count("_maybe_schedule_intent_judge(user_text)") == 1  # 唯一调度入口


def test_consume_pop_precedes_pick_and_pairs_kill_switch():
    """消费点时序：pending 先取即清（TTL）再进 pick；judge_hit 折入处带
    BOK_FLOW_GRAPH_JUDGE 配对（F5：=0 时 pending 照清但不再喂 hit，无半开态）。"""
    pop_read = _SRC.index('_gjudge_pending.get("intent", "")')
    pop_clear = _SRC.index('_gjudge_pending["intent"] = ""')
    hit_arg = _SRC.index("judge_hit=(")
    assert pop_read < pop_clear < hit_arg  # 先取→即清→喂 pick
    pair_block = _SRC[hit_arg : hit_arg + 400]
    assert "BOK_FLOW_GRAPH_JUDGE" in pair_block  # 消费位 kill-switch 配对


def test_inflight_flag_precedes_spawn():
    """单飞时序：置位必须先于起任务（反窗口：任务先起、置位失败=永久卡死 inflight）。"""
    set_flag = _SRC.index('_gjudge_inflight["on"] = True')
    spawn = _SRC.index("_spawn_report(_background_intent_judge")
    assert set_flag < spawn
    # finally 清位：起跌路径（含 import 失败/CancelledError）都唔可以漏
    fin = _SRC.index("async def _background_intent_judge")
    fin_seg = _SRC[fin : _SRC.index("def _maybe_schedule_intent_judge", fin)]
    assert fin_seg.count('finally:') == 1
    assert '_gjudge_inflight["on"] = False' in fin_seg.rsplit("finally:", 1)[1]
