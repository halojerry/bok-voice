"""W1b 语义车道接线钉死(源码 pin,镜像 test_intent_judge_wiring 惯例)。

闭包接线(语义查询时序/守卫判据计算点/play 臂旁路字面/judge 调度未动)离线
起不了真栈——文本切片钉结构,改接线形态即红。
"""
from __future__ import annotations

from pathlib import Path

_SRC = (
    Path(__file__).resolve().parents[1] / "apps" / "agent" / "agent_runtime" / "agent.py"
).read_text(encoding="utf-8")


def test_semantic_query_sits_after_pick_before_judge_elif():
    """语义调用点:首次 pick(关键词+judge)之后、`elif user_text:` judge 调度之前
    ——语义先挡、judge 兜底(miss 落穿调度,绝不能挡住 judge)。"""
    first_pick = _SRC.index("_gbinding = pick_graph_action(")
    sem_call = _SRC.index("_intent_sem.match(")
    sem_repick = _SRC.index("semantic_hit=_sem.intent_id")
    judge_head = _SRC.index("elif user_text:")
    assert first_pick < sem_call < sem_repick < judge_head
    # 查询门:只在关键词未中(_gbinding is None)且索引在场时跑
    assert "_gbinding is None and _intent_sem is not None" in _SRC
    # judge 调度唯一入口不变(语义 miss 落穿才触发)
    assert _SRC.count("_maybe_schedule_intent_judge(user_text)") == 1


def test_graph_advanced_computed_before_arms_and_pick():
    """`_graph_advanced` 必须在三臂派发之前算(jump 臂置 _flow_step_before=-1,
    臂后算会自触发),也在 play 守卫消费之前。

    合并形态：三臂在 `_gdispatch` 闭包体内（定义序在块首），消费受调用点钉住
    ——计算必须在两处 `await _gdispatch(...)` 调用之前（执行序保证）；play 守卫
    （`if _graph_advanced:` + play_bypass）在同一闭包体内消费同一判据。
    """
    adv = _SRC.index("_graph_advanced = flow_ctrl.current != _flow_step_before")
    first_pick = _SRC.index("_gbinding = pick_graph_action(")
    dispatch = _SRC.index("await _gdispatch(_gbinding, False)")
    catchall_dispatch = _SRC.index("await _gdispatch(_gcatchall_stash, True)")
    assert adv < first_pick < dispatch < catchall_dispatch
    # play 守卫在闭包体内（定义序在前,执行序由上面两处调用点钉住）
    dispatch_def = _SRC.index("async def _gdispatch(")
    guard = _SRC.index("if _graph_advanced:", dispatch_def)
    assert dispatch_def < guard < dispatch
    assert "FLOW_GRAPH play_bypass" in _SRC[guard : guard + 600]
    # 语义查询的 play_allowed 参数消费同一判据
    assert "play_allowed=not _graph_advanced" in _SRC


def test_play_arm_has_advanced_bypass_literals():
    """play 臂守卫字面:`play_bypass` + `reason=advanced`,且在 play 正装逻辑
    (provider="graph-play")之前——守卫在后=形同虚设。"""
    play_arm = _SRC.index('provider="graph-play"')
    seg = _SRC[:play_arm]
    bypass = seg.rindex("play_bypass")
    assert "reason=advanced" in seg[bypass:play_arm]
    guard = seg.rindex("if _graph_advanced:")
    assert guard < bypass
    # 守卫不烧 once:旁路日志行内不得出现 graph_fired 写入(bypass 段落无 add)
    bypass_block = _SRC[bypass : _SRC.index(")", bypass)]
    assert "graph_fired" not in bypass_block


def test_judge_schedule_row_unmoved():
    """judge 调度行仍钉在 `elif user_text:` 分支体内(F1 回归锚,勿被语义块挤动)。"""
    head = _SRC.index("elif user_text:")
    call = _SRC.index("_maybe_schedule_intent_judge(user_text)")
    between = _SRC[head:call]
    for forbidden in ("\n            elif ", "\n            else:", "\n    def ", "\n    async def "):
        assert forbidden not in between
    assert _SRC.count("_maybe_schedule_intent_judge(user_text)") == 1


def test_semantic_assembly_block_mirrors_qa_index_shape():
    """装配块:QaIndex 块之后、意向规则块之前;语义总闸+有图才建;None 打 off 日志。"""
    qa_block = _SRC.index("[agent] qa fastpath on entries=")  # QaIndex 装配段锚
    assembly = _SRC.index("_intent_sem = None")  # 块首(init;except 内同名在后)
    rules_block = _SRC.index("# 意向规则(W4-T2):装配拉一次")
    assert qa_block < assembly < rules_block
    seg = _SRC[assembly : rules_block]
    assert "semantic_enabled()" in seg
    assert "IntentSemanticIndex.build" in seg
    assert "intent semantic off" in seg
