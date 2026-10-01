"""judge 回复让位（W-GATE，2026-09-27）单测——纯 asyncio + 源级 pin，零真栈。

根因：`services/llm-mlx/queue_proxy.py` 单飞门（_CONCURRENCY=1）只有排队优先级
（reply 插队）、**无抢占**——后台 judge 先占槽且中途流式时，回复要等 judge 整条
流（4B 上 1-4s，实测 TTFT 2366-2546ms 离群 + 回复 tps 14-20 塌陷主因）。修法
（agent 侧）：judge 在让路 delay 之后、进 LLM 之前 await「本轮回复已交付」事件
（`_reply_done_event`，turn 钩子开头 clear / `_report_assistant_turn` 与 close
路径 set / 纯 StopResponse 轮直接 set），上限 `_JUDGE_REPLY_WAIT_S = 4.0`
（2026-10-02 复标：judge/reply 已分端点，互斥消失只剩 GPU 错峰——15s 旧帽是
排队时代余数；overlay 姿态用已转发 FLOW_JUDGE_DELAY/IDLE_CAP 恢复旧闸）。

本文件钉三面：
- 行为面（`_await_reply_done`，模块级可测）：回复未交付=不放行；已置位=零等待
  零日志；超时=照旧放行（旧行为兜底）且落 `FLOW_JUDGE deferred reply_ms=`；
- 接线面（源级 pin）：两路 judge 的 await 插在让路之后、LLM 调用之前；调度点
  逐字节不变；事件/常量在场；close/report 置位；
- 审计面：turn 钩子里每条 `raise StopResponse()` 要么有报告车道（say/QA 罐头
  → item 落账 → 事件置位），要么就地 `_reply_done_event.set()`（静默丢弃轮）。
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime import agent as agent_mod  # noqa: E402

_SRC = (
    Path(__file__).resolve().parents[1] / "apps" / "agent" / "agent_runtime" / "agent.py"
).read_text(encoding="utf-8")


def _seg(start: str, end: str) -> str:
    a = _SRC.index(start)
    b = _SRC.index(end, a)
    return _SRC[a:b]


def _hook() -> str:
    return _seg("async def on_user_turn_completed", "async def _try_append_user_message")


# ---- 行为面:_await_reply_done 三态 ------------------------------------------


def test_judge_waits_for_event_before_llm_call(capsys):
    """回复未交付 → fake judge 不进 LLM；`_report_assistant_turn` 侧置位后才开火。"""

    async def run():
        ev = asyncio.Event()
        calls: list[str] = []

        async def judge_then_llm():
            await agent_mod._await_reply_done(ev, timeout_s=1.0)
            calls.append("llm")

        task = asyncio.create_task(judge_then_llm())
        await asyncio.sleep(0.05)
        assert calls == [], "回复未交付前 judge 不准进 LLM"
        ev.set()  # = _report_assistant_turn 的置位
        await asyncio.wait_for(task, 1.0)
        return calls

    assert asyncio.run(run()) == ["llm"]
    out = capsys.readouterr().out
    m = re.search(r"FLOW_JUDGE deferred reply_ms=(\d+)", out)
    assert m, f"真让位必须落一行 deferred: {out!r}"
    assert int(m.group(1)) >= 40
    assert out.count("FLOW_JUDGE deferred reply_ms=") == 1, "一次让位只准一行"


def test_event_already_set_no_wait_no_log(capsys):
    """事件已置位（回复早已交付）=零等待快路径：0.0 返回、不落行。"""

    async def run():
        ev = asyncio.Event()
        ev.set()
        t0 = time.monotonic()
        waited = await agent_mod._await_reply_done(ev, timeout_s=5.0)
        return waited, time.monotonic() - t0

    waited, dt = asyncio.run(run())
    assert waited == 0.0
    assert dt < 0.1
    assert "deferred" not in capsys.readouterr().out


def test_timeout_cap_proceeds_old_behavior(monkeypatch, capsys):
    """超时（回复卡死/永不来）→ 照旧开火（旧行为兜底），落行 reply_ms≈上限。

    上限默认读模块常量（调用时解析）——测试 patch 小值，等同缺省帽语义。
    """
    monkeypatch.setattr(agent_mod, "_JUDGE_REPLY_WAIT_S", 0.15)

    async def run():
        ev = asyncio.Event()  # 永不置位
        t0 = time.monotonic()
        waited = await agent_mod._await_reply_done(ev)  # 不传 timeout → 读解析器
        return waited, time.monotonic() - t0

    waited, dt = asyncio.run(run())
    assert waited >= 140.0, f"应按 patched 上限等: {waited}"
    assert dt < 1.0, "超时后必须继续（唔可以无限等）"
    assert "FLOW_JUDGE deferred reply_ms=" in capsys.readouterr().out


def test_cap_default_4s_constant():
    """4s 缺省常量（2026-10-02 复标：分端点后 15s 排队帽退役）。"""
    assert agent_mod._JUDGE_REPLY_WAIT_S == 4.0
    assert "_JUDGE_REPLY_WAIT_S = 4.0" in _SRC
    assert 'label: str = "FLOW_JUDGE"' in _SRC  # 日志标签缺省=要求格式
    assert "deferred reply_ms={waited_ms}" in _SRC


# ---- 接线面:事件生命周期 + judge 调用时序 ------------------------------------


def test_event_declared_cleared_and_released_on_report():
    """事件唯一实例（per-call 闭包）+ turn 开头 clear + report 置位。"""
    assert _SRC.count("_reply_done_event = asyncio.Event()") == 1
    # turn 钩子开头 clear（本轮回复未交付），且先于 judge 调度点
    hook = _hook()
    clear_pos = hook.index("_reply_done_event.clear()")
    assert clear_pos < 400, "clear 必须在钩子入口（任何分支之前）"
    assert hook.index("_spawn_report(_background_flow_judge(") > clear_pos
    # _report_assistant_turn=全回复车道唯一 chokepoint → 置位唯一在场
    rep = _seg("async def _report_assistant_turn", "async def _async_update_context")
    assert "_reply_done_event.set()" in rep


def test_event_set_on_close_path():
    """会话关闭=回复永不再来 → _on_close 置位（不陪等等待帽）。"""
    close = _seg("def _on_close(ev):", "async def _close():")
    assert "closed.set()" in close and "_reply_done_event.set()" in close


def test_event_set_on_interrupted_speech_path():
    """打断轮=回复车道就此终结（无 item 交付、report 不会来）→ 置位。

    2026-10-01 call-231aa92a 实弹：打断轮事件不置位 → judge 挂满等待帽才
    放行（当时代码帽=15s；现缺省 4s 同病仍在），恰在重生/下一轮回复最需要 :1237 槽的窗口开火（TTFT 35.6s 级联）。
    20 站点审计漏了这条路——本 pin 防再漏。
    """
    watch = _seg("async def _watch() -> None:", "# 池化(2026-09-17 全量 debug P2-A)")
    guard_pos = watch.index('not bool(getattr(handle, "interrupted", False))')
    set_pos = watch.index("_reply_done_event.set()", guard_pos)
    storm_pos = watch.index("_storm[", set_pos)
    assert guard_pos < set_pos < storm_pos, "置位必须在打断判定之后、风暴计数之前"


def test_both_judges_await_event_after_delay_before_llm():
    """两路 judge：await 插在让路 delay 之后、LLM 调用之前；laya 侧不接（旁路）。"""
    flow = _seg("async def _background_flow_judge", "async def _background_intent_judge")
    assert (
        flow.index("_yield_verdict = await _judge_yield()")
        < flow.index("await _await_reply_done(_reply_done_event)")
        < flow.index("_raw = await _llm_judge(")
    )
    intent = _seg("async def _background_intent_judge", "def _maybe_schedule_intent_judge")
    assert (
        intent.index("_yield_verdict = await _judge_yield()")
        < intent.index("await _await_reply_done(")
        < intent.index("_gjtext = await _llm_judge(")
    )
    assert 'label="FLOW_GRAPH judge_deferred"' in intent, "两路日志族分立"
    # 两路共用同一事件；laya 决策旁路（独立 sidecar）不接本闸
    assert _SRC.count("await _await_reply_done(_reply_done_event") == 2
    assert "_await_reply_done" not in _seg("async def _laya_intent_pick", "class PausableAgent")


def test_scheduling_points_unchanged():
    """调度点不变（fire-and-forget 位置/形态逐字节）——只挪 LLM 调用时刻。

    FIX-2(b)(2026-10-01):调用多带一个 garbled=_garbled_band_round 实参
    (garbled 轮 streak 三写点全守,ASR 病不记模型头上)——调度点/位置/单飞不变。
    """
    assert (
        _SRC.count(
            "_spawn_report(_background_flow_judge("
            "_step_at, user_text, turn_key=_turn_key, garbled=_garbled_band_round))"
        )
        == 1
    )
    assert (
        _SRC.count(
            "_spawn_report(_background_intent_judge(flow_ctrl.current, utt, candidates), slow_s=20.0)"
        )
        == 1
    )
    assert _SRC.count("_maybe_schedule_intent_judge(user_text)") == 1


# ---- 审计面:StopResponse 轮必须报告或直接放行 ---------------------------------

# 静默丢弃轮（无 assistant item 可报）的出口标记 → 其后必须有 set。
_SILENT_DROP_MARKERS = (
    "QWEN3_ECHO_SELF_HEARD_DROP",  # 自听回声整轮丢弃
    "QWEN3_HOTWORD_ECHO_DROP",  # 纯热词回声轮
    "ASR_LEAK_SANITIZE dropped",  # 纯热词 dump（空转写）
    "EMPTY_TURN_DROPPED",  # 净化后空轮
    "paused turn logged, flow frozen",  # 暂停冻结轮
    "listening r{_sm_rounds}, quiet until",  # 打断风暴静听轮（ack 轮已由 say 落账）
    "[whatsapp] accumulate",  # WA 号码碎片暂存
    "[digit-accum] stash",  # 通用单号碎片暂存
    "防御性兜底",  # 第二处 paused 兜底（无日志行）
    'print("GARBLED_REASK cap=1"',  # 碎片重问到顶静默丢弃（注释里也提过此串，取 print 形）
)


def test_silent_stopresponse_sites_release_judge():
    """静默丢弃轮无 item 可报 → 每个出口就地 set（不然 judge 白等满等待帽）。"""
    hook = _hook()
    for marker in _SILENT_DROP_MARKERS:
        i = hook.index(marker)
        nxt_set = hook.find("_reply_done_event.set()", i)
        assert nxt_set != -1 and nxt_set - i < 600, f"{marker} 出口缺 _reply_done_event.set()"


_REPORT_CALLS = ("_say_script(", "session.say(", "_qa_canned_say(", "_register_reply_lane(")


def test_every_stopresponse_reports_or_sets_event():
    """钩子内每条 raise StopResponse() 要么有报告车道（item→report→set），要么就地 set。

    报告车道白名单=会产出 assistant item 的出口（say/QA 罐头）；新加静默出口若
    两样都无 → judge 在 :1235 门口白等/错位让位，本 pin 直接红。
    """
    hook = _hook()
    misses: list[int] = []
    pos = 0
    while True:
        i = hook.find("raise StopResponse()", pos)
        if i < 0:
            break
        window = hook[max(0, i - 3000):i]
        if "_reply_done_event.set()" not in window and not any(
            tok in window for tok in _REPORT_CALLS
        ):
            misses.append(i)
        pos = i + 1
    assert not misses, f"未报告也未放行的 StopResponse 出口（字符偏移）: {misses}"
