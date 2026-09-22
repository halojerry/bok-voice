"""P2.2 兜底接线源级 pin:agent.py graph 块 fallback + provider + 观测行(照 I1 装配面惯例)。

真栈入口在 entrypoint 闭包内(离线起不了真栈),故用源码切片钉结构:改接线形态
(兜底提前到 judge 归因之前/挪出四道闸/漏 provider 标记/漏日志/**兜底抢在 QA 快路
之前派发**)即红。纯逻辑面见 tests/test_flow_graph_catchall.py。

**复核修（2026-09-21）后的 precedence 铁律**：REFUSE>DEFER>say>graph 常规>QA>
**catch-all**>LLM——兜底只接「常规意图与 QA 都没接住」的轮；字面命中的罐头
（~50ms 即答）绝不可被 "*" 的 jump/notify 抢走（QA 饿死=层间不串通复刻）。
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = (_ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
_FLOW_SRC = (_ROOT / "apps" / "agent" / "agent_runtime" / "flow.py").read_text(encoding="utf-8")

_FALLBACK = "_gcatchall_stash = flow_ctrl.pick_catchall_binding()"
_DISPATCH = "await _gdispatch(_gbinding, False)"  # 常规命中派发(闭包化后)
_CATCHALL_DISPATCH = "await _gdispatch(_gcatchall_stash, True)"


def _graph_block() -> str:
    """graph 块切片:四道闸起 → 常规派发前(兜底 pick 必须整段在里面)。"""
    start = _SRC.index('os.environ.get("BOK_FLOW_GRAPH", "1") == "1"')
    end = _SRC.index(_DISPATCH, start)
    return _SRC[start:end]


def test_agent_catchall_fallback_inside_graph_gates():
    """兜底 **pick** 在四道闸之内(BOK_FLOW_GRAPH / 有意图 / 非 closing / 有话语):
    移出去 = 空转写轮/收线轮/图 kill-switch 关时照跳,precedence 铁律破。
    """
    block = _graph_block()
    assert _FALLBACK in block
    # kill-switch:兜底整段随 BOK_FLOW_GRAPH(无新 env)
    assert _SRC.index('os.environ.get("BOK_FLOW_GRAPH", "1") == "1"') < _SRC.index(_FALLBACK)
    assert "BOK_FLOW_GRAPH_CATCHALL" not in _SRC  # 无新开关
    # 初始化点(后置派发段要读它)
    assert "_gcatchall_stash = None" in _SRC[: _SRC.index(_FALLBACK)]


def test_agent_catchall_only_after_regular_miss():
    """顺序铁律:常规 pick_graph_action → judge 归因 → 兜底 pick(暂存)。"""
    pick = _SRC.index("_gbinding = pick_graph_action(")
    assert "judge_pending_expired" in _SRC[pick : _SRC.index(_FALLBACK)]
    assert _SRC.index("judge_pending_fired") < _SRC.index(_FALLBACK)
    assert _SRC.index(_FALLBACK) < _SRC.index(_DISPATCH)
    # 兜底只在常规裁决空手时才问(`if _gbinding is None:` 卫兵在场)
    seg = _SRC[_SRC.index(_FALLBACK) - 200 : _SRC.index(_FALLBACK)]
    assert "if _gbinding is None:" in seg
    # 常规路仍在(没被兜底替换),且兜底排在它之后
    assert pick < _SRC.index(_FALLBACK)


def test_agent_catchall_dispatch_after_qa_fastpath():
    """**复核修主钉**:兜底派发点必须在 QA 快路之后——QA 命中即 StopResponse,
    永远轮不到兜底;字面罐头先答,"*" 只接 QA 没接住的轮。
    """
    qa_block = _SRC.index("Q→A 检索快路")
    catchall_at = _SRC.index(_CATCHALL_DISPATCH)
    assert qa_block < catchall_at
    # 兜底派发点之后紧跟 LLM 路收尾(_filler.arm)——它就是 LLM 前最后一道闸
    assert catchall_at < _SRC.index("_filler.arm()")
    # 兜底观测行在派发点旁(动作本体可见)
    assert '"FLOW_GRAPH catchall binding=' in _SRC[catchall_at - 400 : catchall_at + 200]


def test_agent_catchall_log_line_shape():
    """观测行与既有 FLOW_GRAPH 族同风格:`catchall binding=<id> action=<...>`。"""
    assert '"FLOW_GRAPH catchall binding=' in _SRC
    assert "action={_gcatchall_stash.action}" in _SRC
    # 与既有 jump/play 族同档(便于 grep 归因),不退化成 print 别的前缀
    assert "FLOW_GRAPH judge_pending_expired" in _SRC


def test_agent_catchall_provider_marker_on_all_three_arms():
    """三臂 provider 全带兜底标记(动作本体见 catchall 日志;jump/notify/play 各一)。"""
    assert '"graph-catchall" if _from_catchall else "graph-jump"' in _SRC
    assert '"graph-catchall" if _from_catchall else "graph-notify"' in _SRC
    assert 'provider="graph-catchall" if _from_catchall else "graph-play"' in _SRC


def test_agent_catchall_keeps_existing_action_dispatch_untouched():
    """三臂既有语义零变化:notify 仍不 StopResponse、play 仍 raise、jump 仍记账。"""
    jump = _SRC.index('if _b.action == "jump_step":')
    notify = _SRC.index("elif _b.action == ACTION_NOTIFY_HUMAN:")
    play = _SRC.index("else:  # play_qa:", notify)
    assert jump < notify < play
    assert "flow_ctrl.graph_fired.add(_b.id)" in _SRC[jump:notify]
    assert "raise StopResponse()" not in _SRC[notify:play]      # 打铃不抢话
    assert "raise StopResponse()" in _SRC[play : play + 6000]   # 罐头播完压掉 LLM
    # 判据调度闸从「仅 `_gbinding is None`」放宽成「非常规命中」(兜底命中同档撒网),
    # 唯一入口仍在;细节见 tests/test_intent_judge_wiring.py 的 F1 锚。
    assert "elif user_text:" not in _SRC
    assert "if user_text and not _gregular_hit:" in _SRC
    assert "_maybe_schedule_intent_judge(user_text)" in _SRC


def test_flow_controller_delegates_to_pure_function():
    """flow.py 是求值接缝(逻辑在 core 纯函数):装配 fired 账本 + closing 冻结。"""
    assert "pick_catchall_action(" in _FLOW_SRC
    assert "fired=self.graph_fired" in _FLOW_SRC
    seg = _FLOW_SRC[_FLOW_SRC.index("def pick_catchall_binding(") : _FLOW_SRC.index("def note_turn_outcome(")]
    assert "if self.closing:" in seg and "return None" in seg
    assert "step_1based=" in seg
