"""3.4 意图引擎接线钉死（review F1/F5 回归锚）。

闭包接线（消费时序/调度结构/单飞时序）离线起不了真栈——源级锚同
test_flow_graph_then_jump.py 姿势：文本切片钉结构，改接线形态即红。
"""
from __future__ import annotations

from pathlib import Path

_SRC = (
    Path(__file__).resolve().parents[1] / "apps" / "agent" / "agent_runtime" / "agent.py"
).read_text(encoding="utf-8")


def test_scheduler_pinned_to_graph_block_tail_with_user_text_gate():
    """F1(+P2.2 兜底腿):调度调用必须钉在图块尾部、以 `user_text` 为闸、唯一入口。

    合并形态（P2.2 复核修 + 三臂闭包化）：`elif user_text:` 承接「常规命中已
    派发」的 else 支、内层 `if not _gregular_hit:` 显式留痕兜底命中同档撒网。
    旧形态 `else:` 与图激活块平级——空转写轮（user_text 为空令图块整体跳过）会
    漏进调度：白烧一次 9B 之外，挂上的 pending 在下一轮无话语支撑地触发绑定
    （say/收线/图关各路径 `_intent_judge_candidates` 门已覆盖，唯 user_text 唔喺
    门参数里，故结构上钉死）。`_gregular_hit = _gbinding is not None`（兜底只
    暂存 `_gcatchall_stash`，唔算常规命中）：兜底命中仍要撒网，否则图里一挂 "*"
    就令判据层永久饿死。判据命中的**优先权不变**：下一轮照旧由 pick_graph_action
    先于兜底消费。位置必须在常规动作派发**之后**：判据任务在调度点捕获
    `flow_ctrl.current`（store 守卫 `flow_ctrl.current != step_at`），常规 jump
    换步后再调度才落得进下一轮（兜底派发按 P2.2 让位 QA 快路、QA 未接才执行，
    闭包本体不变）。
    """
    head = _SRC.index("elif user_text:")
    call = _SRC.index("_maybe_schedule_intent_judge(user_text)")
    assert head < call
    between = _SRC[head:call]
    # 同一分支体内：中间不得再出现分支头/函数定义（出现=调用被挪进别的结构）
    for forbidden in ("\n            elif ", "\n            else:", "\n    def ", "\n    async def "):
        assert forbidden not in between, forbidden
    assert "if not _gregular_hit:" in between  # 内层卫兵(兜底命中同档撒网的显式留痕)
    assert _SRC.count("_maybe_schedule_intent_judge(user_text)") == 1  # 唯一调度入口
    # 「常规命中」定义(P2.2;复核修后兜底=暂存,常规命中即 _gbinding 非空);
    # 定义先于闸门,常规派发在闸门之前(三臂已抽 _gdispatch 闭包)
    defined = _SRC.index("_gregular_hit = _gbinding is not None")
    dispatch_def = _SRC.index("async def _gdispatch(")
    dispatch = _SRC.index("await _gdispatch(_gbinding, False)")
    assert defined < dispatch < head
    # 跳步臂在闭包体内(执行序=dispatch 调用点) → 常规 jump 换步发生在调度前
    assert "flow_ctrl.jump_to(_gtarget)" in _SRC[dispatch_def:dispatch]


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


# ---- Laya 决策旁路接线（2026-09-26，docs/LAYA-EVAL.md 第一落位 intent judge）----
# 闭包接线离线起不了真栈（同上三钉的姿势）：文本切片钉结构，改接线形态即红。
# 行为面（hit/abstain/None/off 四路）的 mock 客户端测试在 tests/test_laya_judge.py
# 的 pick_intent_laya 段——那是模块级可测入口，闭包只做打点与命中透传。


def test_laya_block_sits_after_semantic_before_dispatch():
    """插入点钉死：语义车道之后、动作派发之前。

    合并形态：三臂抽 `_gdispatch` 闭包（定义在块首），本块与语义块都在图块内、
    闭包的**调用点**（常规派发 `await _gdispatch(...)`）之前。早了会抢关键词/
    9B pending/语义（旁路只准兜模糊轮）；晚了（`elif user_text:` 里）赶不上当轮
    三臂消费——「当轮即视为图命中」是本功能的存在意义。
    """
    sem = _SRC.index("if _gbinding is None and _intent_sem is not None:")
    laya = _SRC.index("if _gbinding is None and laya_judge_enabled():")
    dispatch = _SRC.index("await _gdispatch(_gbinding, False)")
    assert sem < laya < dispatch


def test_laya_hit_feeds_pick_judge_hit_same_gate():
    """命中消费路径钉死：只换「命中从哪来」——laya answer 经 pick_graph_action
    (judge_hit=) 入裁决（同权同守卫，不烧 once 不改胜者语义），消费三臂一行不改。"""
    laya = _SRC.index("if _gbinding is None and laya_judge_enabled():")
    seg = _SRC[laya : _SRC.index("await _gdispatch(_gbinding, False)", laya)]
    assert "_gbinding = pick_graph_action(" in seg
    assert 'judge_hit=str(_laya_ans.get("choice") or "")' in seg
    # enabled 闸在最外层（与 _gbinding is None 同一 if 行）：默认 "0"=零调用零日志
    assert "_gbinding is None and laya_judge_enabled()" in seg
    # 命中观测行（LAYA-EVAL 对账）
    assert "FLOW_GRAPH judge_hit source=laya" in seg


def test_laya_pick_closure_wraps_module_and_logs_once_per_call():
    """闭包壳纪律：判定装配在 pick_intent_laya（模块级可测）；off/unavailable
    每通一次（per-call 旗挂 agent 实例）；off 行仅「当通曾开」后可能出现——
    默认闸关=零日志。"""
    start = _SRC.index("async def _laya_intent_pick")
    seg = _SRC[start : _SRC.index("class PausableAgent", start)]
    # 闸在最外层（零调用零日志的结构性保证）
    assert "if not laya_judge_enabled():" in seg
    # off 行的 once 旗带 ever_on 前置（默认关=零日志）
    ever = seg.index('if flags["ever_on"] and not flags["off"]:')
    off_log = seg.index('LAYA_JUDGE verdict=off')
    assert ever < off_log
    # unavailable 的 once 旗
    unavail_flag = seg.index('if not flags["unavail"]:')
    unavail_log = seg.index("LAYA_JUDGE verdict=unavailable")
    assert unavail_flag < unavail_log
    # 判定装配只经模块入口（state/instructions 组装不在闭包里重写一份）
    assert "pick_intent_laya(" in seg
    assert "build_intent_state(" not in seg and "build_intent_instructions(" not in seg


def test_laya_fallback_keeps_9b_scheduler_sole_entry():
    """回落路钉死：abstain/None 后必落 `elif user_text:` 的既有 9B 调度——laya
    块不新增任何 _maybe_schedule_intent_judge 调用点（唯一调度入口计数不变）。"""
    assert _SRC.count("_maybe_schedule_intent_judge(user_text)") == 1
    laya = _SRC.index("if _gbinding is None and laya_judge_enabled():")
    seg = _SRC[laya : _SRC.index("elif user_text:", laya)]
    assert "_maybe_schedule_intent_judge" not in seg


def test_laya_history_source_pinned_to_turn_ctx():
    """state 历史来源钉死：turn_ctx.items + exclude=new_message（原话单独置头，
    不进 history——build_intent_state 的客户原话置头纪律依赖这一点）。"""
    laya = _SRC.index("if _gbinding is None and laya_judge_enabled():")
    seg = _SRC[laya : laya + 1200]
    assert 'getattr(turn_ctx, "items", None)' in seg
    assert "exclude=new_message" in seg
