"""在途合成×响应看门狗顺延单测(2026-09-26,call-ec075023)。

根因:LLM 文本 3.2s 完成、TTS bidi 首包 ~4.0s 临界——4s 闸恰在真回复将出声时
force-interrupt 掐掉,改念兜底句+心跳,客户挂线。修法:fire 时刻读
tts_provider.reply_stream_pending_since()(回复流已开、首音频未到=慢非死火)
→ 一次性顺延 BOK_RESPONSE_WATCHDOG_SYNTH_EXT_S(默认 2s)再判;真死火只多等
一窗,兜底路径不变。

二判据(2026-09-25,LLM 慢窗):74ddbaf 判据只在 TTS 流开后为真——LLM 本体
生成中(TTS 未接)pending 恒 0,jump 腿慢 LLM(tps 6.5-11)触发轮回复 3.9-4.5s
恰过 4s 闸被掐成 watchdog-ack。补 session.agent_state=="thinking"(回复管线
在途且零音频)同旗同窗顺延一次;判据抽成 _watchdog_synth_extend_reason 纯函数
可单测(fire 係 entrypoint 闭包不可直调)。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from _bok_src import bok_source  # noqa: E402
from agent_runtime.agent import (  # noqa: E402
    _response_watchdog_synth_ext_s,
    _watchdog_synth_extend_reason,
)
from agent_runtime.tts_cache import CachedTTS, _FirstAudioTTS  # noqa: E402


# ---- env 解析 ----


def test_env_default_two_seconds(monkeypatch):
    monkeypatch.delenv("BOK_RESPONSE_WATCHDOG_SYNTH_EXT_S", raising=False)
    assert _response_watchdog_synth_ext_s() == 2.0


def test_env_override_and_kill_switch(monkeypatch):
    monkeypatch.setenv("BOK_RESPONSE_WATCHDOG_SYNTH_EXT_S", "3.5")
    assert _response_watchdog_synth_ext_s() == 3.5
    monkeypatch.setenv("BOK_RESPONSE_WATCHDOG_SYNTH_EXT_S", "0")
    assert _response_watchdog_synth_ext_s() == 0.0
    monkeypatch.setenv("BOK_RESPONSE_WATCHDOG_SYNTH_EXT_S", "garbage")
    assert _response_watchdog_synth_ext_s() == 2.0


# ---- reply_stream_pending_since 状态机(duck 实例,只碰实例属性) ----


def _bare(cls) -> object:
    inst = object.__new__(cls)
    inst._first_audio_cbs = []
    inst._reply_stream_started_monotonic = 0.0
    inst._first_audio_monotonic = 0.0
    return inst


def test_pending_zero_when_never_streamed():
    for inst in (_bare(CachedTTS), _bare(_FirstAudioTTS)):
        assert inst.reply_stream_pending_since() == 0.0


def test_pending_positive_after_stream_before_first_audio():
    for inst in (_bare(CachedTTS), _bare(_FirstAudioTTS)):
        inst._reply_stream_started_monotonic = time.monotonic() - 0.5
        pending = inst.reply_stream_pending_since()
        assert pending > 0.0, "流已开未出声 → 在途起点"


def test_pending_cleared_after_first_audio_fires():
    inst = _bare(CachedTTS)
    inst._reply_stream_started_monotonic = time.monotonic() - 0.5
    hits: list = []
    inst.add_first_audio_listener(lambda: hits.append(1))
    inst._fire_first_audio()
    assert hits == [1], "首音频回调链保持原语义"
    assert inst.reply_stream_pending_since() == 0.0, "出声后不再算在途"


def test_pending_new_stream_reopens_window():
    inst = _bare(_FirstAudioTTS)
    inst._reply_stream_started_monotonic = 100.0
    inst._first_audio_monotonic = 200.0
    assert inst.reply_stream_pending_since() == 0.0
    inst._reply_stream_started_monotonic = 300.0  # 下一轮回复流重开
    assert inst.reply_stream_pending_since() == 300.0


# ---- agent 接线源码钉(顺延分支必须在 force-interrupt 之前)----


def _function_body(src: str, marker: str) -> list[str]:
    lines = src.splitlines()
    start = next(i for i, ln in enumerate(lines) if marker in ln)
    indent = len(lines[start]) - len(lines[start].lstrip())
    body = []
    for ln in lines[start + 1:]:
        stripped = ln.strip()
        if stripped and (len(ln) - len(ln.lstrip())) <= indent and stripped.split()[0] in (
            "def", "async", "class", "@",
        ):
            break
        body.append(ln)
    return body


def test_watchdog_fire_extends_before_force_interrupt():
    src = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    body = _function_body(src, "async def _watchdog_fire")
    joined = "\n".join(body)
    assert "reply_stream_pending_since" in joined, "fire 时刻必须读在途合成信号"
    probe = joined.index("reply_stream_pending_since")
    fired = joined.index('_facts["watchdog_fired"]')
    interrupt = joined.index("session.interrupt(force=True)")
    assert probe < fired < interrupt, (
        "顺延判定必须先于真触发计数与 force-interrupt——否则临界真回复照旧被掐"
    )
    assert 'BOK_RESPONSE_WATCHDOG_SYNTH_EXT_S' in bok_source(), (
        "新 env 必须进 _FORWARD_ENV(prod 封闭面可达)")


# ---- 二判据:LLM 生成中(agent_state==thinking)同旗同窗顺延(2026-09-25)----
# 74ddbaf 判据 reply_stream_pending_since()>arm_time 只在 TTS 流开后为真;
# LLM 本体生成中(TTS 未接)pending 恒 0——jump 腿慢 LLM(tps 6.5-11)触发轮
# 回复 3.9-4.5s 恰过 4s 闸被掐成 watchdog-ack。判据抽成模块级纯函数
# _watchdog_synth_extend_reason(同 _watchdog_extend 注入式,fire 闭包不可直测)。


class _FakeSession:
    """duck AgentSession:只暴露 agent_state 属性。"""

    def __init__(self, agent_state: object = "") -> None:
        self.agent_state = agent_state


class _RaisingState:
    """状态口异常(official property 抛错)=按无在途处理。"""

    @property
    def agent_state(self) -> str:
        raise RuntimeError("state port broken")


def _pending_fn(value: object):
    def _fn() -> object:
        if isinstance(value, Exception):
            raise value
        return value

    return _fn


def test_thinking_path_extends_burns_counter():
    session = _FakeSession("thinking")
    assert (
        _watchdog_synth_extend_reason(_pending_fn(0.0), 100.0, 0, session)
        == "thinking"
    ), "pending=0 + thinking(LLM 生成中)=慢非死火 → 顺延"
    # 二次 fire:已延 1 次 → 仍顺延(2026-09-30 真机 B 组:一次性窗差 1-4s 掐真回复)
    assert (
        _watchdog_synth_extend_reason(_pending_fn(0.0), 100.0, 1, session) == "thinking"
    ), "第二窗仍顺延——真机回复首帧叠 hold 落 5-6s,两窗才覆盖"
    # 三次 fire:计数=2 → 封顶(真死火只多等两窗,兜底不变)
    assert (
        _watchdog_synth_extend_reason(_pending_fn(0.0), 100.0, 2, session) == ""
    ), "两窗封顶,真死火落回 force-interrupt"


def test_tts_pending_takes_precedence_over_thinking():
    # 流已开且晚于武装 → TTS 路,即便 agent_state 也是 thinking(互斥先 pending)
    assert (
        _watchdog_synth_extend_reason(_pending_fn(101.0), 100.0, 0, _FakeSession("thinking"))
        == "tts_pending"
    )
    # 流已开但早于武装(上一轮残留窗口)→ 两路都不顺延
    assert (
        _watchdog_synth_extend_reason(_pending_fn(50.0), 100.0, 0, _FakeSession("thinking"))
        == ""
    )


def test_kill_switch_blocks_thinking_path(monkeypatch):
    monkeypatch.setenv("BOK_RESPONSE_WATCHDOG_SYNTH_EXT_S", "0")
    assert _response_watchdog_synth_ext_s() == 0.0
    assert (
        _watchdog_synth_extend_reason(_pending_fn(0.0), 100.0, False, _FakeSession("thinking"))
        == ""
    ), "kill 时 thinking 路同灭 → 直通 force-interrupt"


def test_non_thinking_or_missing_or_raising_state_no_extend():
    for sess in (
        _FakeSession("speaking"),
        _FakeSession("listening"),
        object(),  # 缺 agent_state 属性(duck/fake 旧姿势)
        _RaisingState(),
    ):
        assert (
            _watchdog_synth_extend_reason(_pending_fn(0.0), 100.0, False, sess) == ""
        )


def test_pending_signal_absent_or_error_falls_to_thinking_check():
    # 信号口缺席(callable 判不过)/异常 → 按 0,落到 thinking 判据
    assert (
        _watchdog_synth_extend_reason(None, 100.0, False, _FakeSession("thinking"))
        == "thinking"
    )
    assert (
        _watchdog_synth_extend_reason(
            _pending_fn(RuntimeError("broken")), 100.0, False, _FakeSession("thinking")
        )
        == "thinking"
    )
    assert (
        _watchdog_synth_extend_reason(
            _pending_fn(RuntimeError("broken")), 100.0, False, _FakeSession("speaking")
        )
        == ""
    )


def test_watchdog_fire_reason_branch_before_force_interrupt():
    src = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    body = _function_body(src, "async def _watchdog_fire")
    joined = "\n".join(body)
    assert "_watchdog_synth_extend_reason(" in joined, (
        "fire 必须经统一判据口(TTS 路 + thinking 路,含 kill 挡板)"
    )
    reason = joined.index("_watchdog_synth_extend_reason(")
    fired = joined.index('_facts["watchdog_fired"]')
    interrupt = joined.index("session.interrupt(force=True)")
    assert reason < fired < interrupt, (
        "顺延分支(含 thinking 二判据)必须先于真触发计数与 force-interrupt"
    )
    assert 'reason={_ext_reason}' in joined, "打点须能区分两路归因(tts_pending/thinking)"
