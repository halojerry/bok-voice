"""W2 刀4(2026-10-08):句级提交连发窗——同客快速连发轮不再各自抢答。

病理(架构体检 2026-10-08):interrupted 80/514=15.6%;句级提交把一串话拆成
多轮,AI 开答即被下一句打断(打断在框架 _user_turn_completed_impl 先于本钩子,
拦截点在钩子内拦不住那一下);打断后 TTFT 5.3-6.8s(僵尸 prefill 抢算力残余);
快速 user-user 对(<4s 无 assistant 隔开)16 例。

实现形状(降级保守档,理由见 commit):窗内第二轮不独立触发生成——user 消息
经 _try_append_user_message 补进 chat ctx + StopResponse + 排窗尾合并补答
(generate_reply 一次性应答窗内全部轮);链式连发每轮入口收旧 flush 重排,
补答恒落在最后一轮提交+窗口处。kill-switch BOK_BURST_MERGE_WINDOW_S(默认
3.0,0=关回旧)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "agent"))

from agent_runtime.agent import _burst_merge_window_s  # noqa: E402

# ---- env 解析 ----


def test_window_default_and_kill_switch(monkeypatch):
    monkeypatch.delenv("BOK_BURST_MERGE_WINDOW_S", raising=False)
    assert _burst_merge_window_s() == 3.0
    monkeypatch.setenv("BOK_BURST_MERGE_WINDOW_S", "0")
    assert _burst_merge_window_s() == 0.0  # 0=关
    monkeypatch.setenv("BOK_BURST_MERGE_WINDOW_S", "2.5")
    assert _burst_merge_window_s() == 2.5
    monkeypatch.setenv("BOK_BURST_MERGE_WINDOW_S", "abc")
    assert _burst_merge_window_s() == 3.0  # 坏值回缺省
    monkeypatch.setenv("BOK_BURST_MERGE_WINDOW_S", "-1")
    assert _burst_merge_window_s() == 0.0  # 负值按关


# ---- 源级 pin:hook 装配契约 ----


@pytest.fixture()
def agent_src():
    return (_ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")


def test_hook_entry_rotation_pinned(agent_src):
    """hook 入口:先取上一轮快照、再落本轮戳+answered 复位+收旧 flush——顺序
    契约(快照必须先于状态覆盖,否则 gap 恒 0)。"""
    i_hook = agent_src.index("async def on_user_turn_completed")
    i_prev = agent_src.index('_burst_prev_ts = float(_burst_state["ts"])', i_hook)
    i_snap = agent_src.index('_burst_prev_answered = bool(_burst_state["answered"])', i_prev)
    i_set = agent_src.index('_burst_state["ts"] = time.monotonic()', i_snap)
    i_reset = agent_src.index('_burst_state["answered"] = False', i_set)
    i_cancel = agent_src.index("_cancel_burst_flush()", i_reset)
    assert i_prev < i_snap < i_set < i_reset < i_cancel


def test_burst_condition_terms_pinned(agent_src):
    """连发判定六闸齐:窗口>0、上一轮在场、期间零出声、间隔落窗内、会话活着、
    非 REPEAT(承应铁律豁免)。"""
    assert (
        "_burst_window > 0\n"
        "                and _burst_prev_ts > 0.0\n"
        "                and not _burst_prev_answered\n"
        "                and 0 < _burst_gap <= _burst_window\n"
        "                and not closed.is_set()\n"
        "                and not getattr(self, \"paused\", False)\n"
        "                and str(flow_ctrl.last_verdict or \"\") != REPEAT"
    ) in agent_src


def test_burst_stop_response_shape_pinned(agent_src):
    """本轮处置:user 消息补 ctx(官方姿势)+账本行 provider=burst-merge+饿死账
    归零+排窗尾补答+StopResponse+reply_done set(judge 放行);垫话 arm 在
    连发块之后(连发轮不 arm)。"""
    i_cond = agent_src.index("and str(flow_ctrl.last_verdict or \"\") != REPEAT")
    i_tail = agent_src.index("async def _try_append_user_message", i_cond)
    block = agent_src[i_cond:i_tail]
    assert "await self._try_append_user_message(new_message)" in block
    assert 'provider="burst-merge"' in block
    assert '_starve["n"] = 0' in block
    assert "_arm_burst_flush(_burst_window - _burst_gap)" in block
    i_stop = block.index("raise StopResponse()")
    i_arm = block.index("_filler.arm()")
    assert i_stop < i_arm, "连发轮先 StopResponse,垫话 arm 只属于非连发路径"
    assert "_reply_done_event.set()" in block


def test_burst_check_sits_after_fast_lanes_before_filler(agent_src):
    """位置契约:连发判定在全部快车道(StopResponse 出口)之后、_filler.arm()
    之前——快车道零感知,仅 LLM 抢答被并窗。"""
    i_burst = agent_src.index("句级提交连发窗(2026-10-08)")
    i_filler = agent_src.index("            _filler.arm()", i_burst)
    i_qa = agent_src.index("Q→A 检索快路(PR-3)")
    assert i_qa < i_burst < i_filler


def test_flush_armored_behavior_pinned(agent_src):
    """flush 行为:单飞(入口先收)、speaking 顺延 0.5s 有界、暂停/收线不发、
    开火置 answered+generate_reply、走 _spawn_report 池。"""
    i_arm = agent_src.index("def _arm_burst_flush(delay_s")
    i_report = agent_src.index("async def _report_assistant_turn(", i_arm)
    block = agent_src[i_arm:i_report]
    assert "_cancel_burst_flush()" in block
    assert 'getattr(session, "user_state", "")' in block
    assert '"speaking" and postpone_left > 0' in block
    assert 'getattr(agent, "paused", False)' in block
    assert 'if closed.is_set():\n                return' in block
    assert '_burst_state["answered"] = True' in block
    assert "session.generate_reply()" in block
    assert "_spawn_report(_flush())" in block
    # 单飞入口:hook 入口的 cancel 与 arm 同源
    assert agent_src.count("def _cancel_burst_flush()") == 1


def test_answered_marking_sites_pinned(agent_src):
    """answered 置位恰三处:assistant item 交付(_on_conversation_item)+可闻
    车道登记(_register_reply_lane,notify 早退之后)+flush 开火(补答即交付)。"""
    i_lane = agent_src.index("def _register_reply_lane")
    i_item = agent_src.index("def _on_conversation_item")
    i_spawn = agent_src.index("def _spawn_report")
    lane_block = agent_src[i_lane:i_item]
    item_block = agent_src[i_item:i_spawn]
    assert item_block.count('_burst_state["answered"] = True') == 1
    assert 'A3:assistant 轮出现=上一用户轮已被接住' in item_block
    # notify 车道(无自有出声)不置位——置位行在 notify 早退 return 之后
    i_notify_ret = lane_block.index("return")
    i_mark = lane_block.index('_burst_state["answered"] = True')
    assert i_notify_ret < i_mark
    # 全仓恰三处(第三处=flush 开火,见 test_flush_armored_behavior_pinned)
    assert agent_src.count('_burst_state["answered"] = True') == 3


def test_state_and_env_forward_registered():
    """env 立法:_FORWARD_ENV 有键(读面=agent_runtime,tests/test_forward_env
    同扫);会话级账声明在 entrypoint。"""
    env_src = (_ROOT / "tools" / "bokctl" / "env.py").read_text(encoding="utf-8")
    assert '"BOK_BURST_MERGE_WINDOW_S"' in env_src
    agent_src = (_ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    assert '_burst_state: dict = {"ts": 0.0, "answered": True}' in agent_src
    assert '_burst_flush_task: dict = {"task": None}' in agent_src
    assert 'os.environ.get("BOK_BURST_MERGE_WINDOW_S", "3.0")' in agent_src
