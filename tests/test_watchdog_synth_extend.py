"""在途合成×响应看门狗顺延单测(2026-09-26,call-ec075023)。

根因:LLM 文本 3.2s 完成、TTS bidi 首包 ~4.0s 临界——4s 闸恰在真回复将出声时
force-interrupt 掐掉,改念兜底句+心跳,客户挂线。修法:fire 时刻读
tts_provider.reply_stream_pending_since()(回复流已开、首音频未到=慢非死火)
→ 一次性顺延 BOK_RESPONSE_WATCHDOG_SYNTH_EXT_S(默认 2s)再判;真死火只多等
一窗,兜底路径不变。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.agent import _response_watchdog_synth_ext_s  # noqa: E402
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
    assert 'BOK_RESPONSE_WATCHDOG_SYNTH_EXT_S' in (
        ROOT / "tools" / "bok.py"
    ).read_text(encoding="utf-8"), "新 env 必须进 _FORWARD_ENV(prod 封闭面可达)"
