"""编排梳理波 S1（2026-10-02）单测合集——P0/P1/P2/P3 逐项钉死。

覆盖：
- P1 双 tts 样本：Relay 层基座监视器排空，单样本走内芯真锚（真 provider 流
  + 假内芯跑完整轮，断言 metrics 恰一条且 ttfb=真锚非近零）；
- P1 late-answer 双写：登记 `relieve=False` + item 侧账本去重（同文不双记）；
- P2 judge 让路链：等待帽 4s+env、floor1/idle3、用户话中窗放行、capped→skip
  收窄（仅 agent==thinking 才跳）、_link 钩子恒注册（nudge 门摘除）；
- P2 watchdog hold 撑高删除；
- P3 pause-ack 直记账本。
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

import pytest  # noqa: E402
from livekit.agents import APIConnectOptions, tts  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
TTS_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "tts_cache.py").read_text(encoding="utf-8")


def _seg(src: str, start: str, end: str) -> str:
    a = src.index(start)
    b = src.index(end, a)
    return src[a:b]


def _relay_body() -> str:
    return _seg(TTS_SRC, "class _RelaySynthesizeStream", "class CachedTTS")


# ============================================================ P1 · 双 tts 样本


class _FakeInnerTTS(tts.TTS):
    """假内芯 provider：流祇延迟 latency_s 才出音频（真锚 ttfb=该延迟）。"""

    def __init__(self, latency_s: float = 0.3, sample_rate: int = 24000):
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True, aligned_transcript=False),
            sample_rate=sample_rate,
            num_channels=1,
        )
        self.latency_s = latency_s
        self.raised: list = []

    def synthesize(self, text: str, *, conn_options=None):  # pragma: no cover - 不走
        raise NotImplementedError

    def stream(self, *, conn_options=None):
        return _FakeInnerStream(self)

    def prewarm(self) -> None:  # pragma: no cover - 空实现
        pass


class _FakeInnerStream(tts.SynthesizeStream):
    """真 SynthesizeStream 基座（含基座监视器 emit）+ 首送点 _mark_started。

    模拟 999f903 后的 provider 流形态：[首段文本交给 provider] 时标锚，
    latency_s 后出首帧——基座监视器据真锚算出真 ttfb。
    """

    def __init__(self, inner_tts: _FakeInnerTTS):
        super().__init__(tts=inner_tts, conn_options=APIConnectOptions(max_retry=0))
        self._inner_tts = inner_tts

    def push_text(self, token: str) -> None:
        self._mark_started()  # 官方范本：首段文本交 provider（ttfb 真锚）
        super().push_text(token)

    async def _run(self, output_emitter) -> None:
        output_emitter.initialize(
            request_id="fake-inner-req",
            sample_rate=24000,
            num_channels=1,
            mime_type="audio/pcm",
            stream=True,
        )
        output_emitter.start_segment(segment_id="fake-seg")
        async for _tok in self._input_ch:  # 等 end_input 关流
            pass
        await asyncio.sleep(self._inner_tts.latency_s)
        output_emitter.push(b"\x00\x10" * 2400)  # 0.1s @24k mono s16le
        output_emitter.flush()


def _count_stream_metrics(provider, text: str = "您好，這是一條測試話術。") -> tuple[list, int]:
    seen: list = []
    provider.on("metrics_collected", lambda m: seen.append(m))

    async def _run() -> int:
        stream = provider.stream()
        stream.push_text(text)
        stream.end_input()
        frames = 0
        async for _ev in stream:
            frames += 1
        await stream.aclose()
        return frames

    frames = asyncio.run(_run())
    return seen, frames


def test_relay_emits_single_real_sample(tmp_path):
    """CachedTTS.stream()：内芯真样本恰一条（220-700ms 带内），Relay 退化样本消失。"""
    from agent_runtime.tts_cache import CachedTTS, TtsAudioCache

    inner = _FakeInnerTTS(latency_s=0.3)
    provider = CachedTTS(wrapped=inner, cache=TtsAudioCache(tmp_path))
    seen, frames = _count_stream_metrics(provider)
    assert frames >= 1, "音频帧必须照常转发"
    assert len(seen) == 1, f"每轮恰一条样本（旧双样本=真锚+近零）: {[getattr(m, 'ttfb', None) for m in seen]}"
    assert 0.2 <= seen[0].ttfb <= 0.8, f"单样本必须走内芯真锚: ttfb={seen[0].ttfb}"


def test_first_audio_wrapper_emits_single_real_sample():
    """_FirstAudioTTS 同一条 Relay 层：单样本语义一致（裸 provider 兜底链）。"""
    from agent_runtime.tts_cache import wrap_first_audio_tts

    inner = _FakeInnerTTS(latency_s=0.3)
    provider = wrap_first_audio_tts(inner, None)
    from agent_runtime.tts_cache import _FirstAudioTTS

    assert isinstance(provider, _FirstAudioTTS)
    seen, frames = _count_stream_metrics(provider)
    assert frames >= 1
    assert len(seen) == 1
    assert 0.2 <= seen[0].ttfb <= 0.8


def test_relay_monitor_drained_and_mark_started_kept():
    """源级 pin：Relay 覆写监视器=排空不 emit；_mark_started 保留（framework 锚）。"""
    body = _relay_body()
    assert "async def _metrics_monitor_task(self, event_aiter) -> None:" in body
    assert "async for _ in event_aiter" in body
    assert 'self.emit("metrics_collected"' not in body, "Relay 层不得再 emit 自有退化样本"
    assert "self._mark_started()" in body, "framework USERDATA_TTS_STARTED_TIME 锚点保留"


# ============================================================ P1 · late-answer 双写


def _late_answer_body() -> str:
    return _seg(AGENT_SRC, "async def _late_answer_say", "_set_lacb = getattr(_raw_llm")


def test_late_answer_register_relieve_false():
    """登记侧 relieve=False（先例=qa-fastpath）：抵销单点归 _report_assistant_turn。

    三态之一「抵销一次」：登记不让抵销（本 pin），交付后 report 路按 gen=llm
    抵一次（test_stall_ladder 已钉 report 路判据）——旧登记侧再抵=双扣。
    """
    body = _late_answer_body()
    assert '_register_reply_lane(lane="late-answer", gen="llm", text=text, relieve=False)' in body
    # 串行注册（relieve 参数在 gen/text 之后）不构成第二写法
    assert '_register_reply_lane(lane="late-answer", gen="llm", text=text)' not in body
    # 抵销唯一存活点：_report_assistant_turn 的实答判据（q a-fastpath 同款）
    rep = _seg(AGENT_SRC, "async def _report_assistant_turn", "t_begin = time.monotonic()")
    assert 'if gen in ("llm", "qa_fastpath"):' in rep
    assert "flow_ctrl.relieve_stall_streak()" in rep


def test_late_answer_item_path_ledger_dedup():
    """item 回调记账必须让开登记时点已记的同条（ticket 带文=chokepoint 已入账）。"""
    # 登记函数：ticket 车道（含 text）在登记时点 record_reply
    reg = _seg(AGENT_SRC, "def _register_reply_lane(", "async def _ledger_ack_line")
    assert "context_state.record_reply(_clean_transcript(strip_voice_style(text)), gen)" in reg
    # item 消费点：票据带文 → 置「本 item 已入账」旗
    item = _seg(AGENT_SRC, "def _on_conversation_item(", "async def _report_assistant_turn")
    assert '_last_item_registered["v"] = bool(_ticket is not None and _ticket.text)' in item
    # 账本写入点：旗起 = 跳过第二次 record（防跨轮账本双条）
    ctx = _seg(AGENT_SRC, "def _on_item_for_context(", "if not _assistant_ack:")
    assert (
        'if _last_item_gen["v"] == "llm" and not _last_item_registered["v"]:' in ctx
    ), "item 侧 record_reply 必须受登记旗门控"
    assert 'context_state.record_reply(_clean_transcript(guarded), "llm")' in ctx


def test_ledger_single_entry_semantics():
    """真 ContextState 语义演示：登记一次 + item 侧让开 = 账本单条（无重复）。"""
    from agent_runtime.providers.livekit_plugins import ContextState

    ctx = ContextState(account_id="acc-001")
    text = "您的包裹昨天已经到达驿站，请凭取件码领取。"
    ctx.record_reply(text, "llm")          # 登记时点（chokepoint）
    # item 侧受 _last_item_registered 门控跳过 —— 不再 record_reply
    assert ctx.reply_ledger() == [text], "同文双记会污染跨轮复读账本（重复条目）"


# ============================================================ P2 · judge 让路链


def test_judge_reply_wait_cap_default_4s(monkeypatch):
    from agent_runtime import agent as agent_mod

    assert agent_mod._JUDGE_REPLY_WAIT_S == 4.0
    assert "_JUDGE_REPLY_WAIT_S = 4.0" in AGENT_SRC
    # 新键未进 _FORWARD_ENV=prod 死门 → 不引入（overlay 姿态用已转发 FLOW_JUDGE_* 调）
    assert "BOK_JUDGE_REPLY_WAIT_S" not in AGENT_SRC


def test_judge_reply_wait_constant_call_time_resolved(monkeypatch, capsys):
    """调用时解析：patch 模块常量即改实际等待（守 15s→4s 的档位可测性）。"""
    from agent_runtime import agent as agent_mod

    monkeypatch.setattr(agent_mod, "_JUDGE_REPLY_WAIT_S", 0.15)

    async def run():
        ev = asyncio.Event()  # 永不置位
        return await agent_mod._await_reply_done(ev)

    waited = asyncio.run(run())
    assert waited >= 140.0, f"应按 patched 帽等: {waited}"
    assert "FLOW_JUDGE deferred reply_ms=" in capsys.readouterr().out


def test_judge_yield_defaults_relaxed(monkeypatch):
    monkeypatch.delenv("FLOW_JUDGE_DELAY", raising=False)
    monkeypatch.delenv("FLOW_JUDGE_IDLE_CAP", raising=False)
    from agent_runtime.agent import _judge_yield_env

    assert _judge_yield_env() == (1.0, 3.0)
    monkeypatch.setenv("FLOW_JUDGE_DELAY", "3")
    monkeypatch.setenv("FLOW_JUDGE_IDLE_CAP", "6")
    assert _judge_yield_env() == (3.0, 6.0), "env 覆盖=饥荒 overlay 恢复旧闸语义"


def test_wait_link_idle_allows_user_speech_window():
    """允许用户话中窗直接跑（分端点后只剩 GPU 错峰，不再等客户停嘴）。"""
    from agent_runtime.agent import _wait_link_idle

    link = {"agent": "listening", "user": "speaking"}
    assert asyncio.run(_wait_link_idle(link, floor_s=0, cap_s=0.3)) == "idle"
    link2 = {"agent": "speaking", "user": "speaking"}
    assert asyncio.run(_wait_link_idle(link2, floor_s=0, cap_s=0.3)) == "idle"


def test_capped_skip_narrowed_to_explicit_thinking(monkeypatch):
    """capped→skip 收窄：仅链路明示生成中才跳；状态未知/缺席=放行(fail-open)。"""
    from agent_runtime.agent import _judge_capped_should_skip

    monkeypatch.delenv("BOK_JUDGE_CAPPED_SKIP", raising=False)
    assert _judge_capped_should_skip("idle", {"agent": "thinking"}) is False
    assert _judge_capped_should_skip("capped", {"agent": "thinking"}) is True
    assert _judge_capped_should_skip("capped", {"agent": ""}) is False, "未知=不跳(旧恒 busy 饿死病灶)"
    assert _judge_capped_should_skip("capped", {"agent": "listening"}) is False
    monkeypatch.setenv("BOK_JUDGE_CAPPED_SKIP", "0")
    assert _judge_capped_should_skip("capped", {"agent": "thinking"}) is False


def test_judge_link_hooks_registered_unconditionally():
    """_link 钩子恒注册（nudge 门摘除）——test-object 通话 nudge 关=闸恒 busy 的病根。"""
    src = AGENT_SRC
    assert 'session.on("agent_state_changed", _on_agent_state)' in src
    assert 'session.on("user_state_changed", _on_user_state)' in src
    # 不在 `if nudge_max > 0:` 门里（恒注册,副作用函数内自守）
    gate = re.search(
        r"if nudge_max > 0:\n\s+session\.on\(\"agent_state_changed\", _on_agent_state\)",
        src,
    )
    assert gate is None, "_link 钩子必须与 nudge 解耦恒注册"
    # 两处 capped 判定都走收窄纯函数
    assert src.count("_judge_capped_should_skip(") >= 3  # 定义 1 + 两路 judge


# ============================================================ P2 · watchdog hold


def test_watchdog_extend_hold_read_removed():
    body = _seg(AGENT_SRC, "def _extend_response_watchdog", "_judge_inflight: dict")
    assert "_filler.hold_if_playing" not in body, "hold 撑高已死（垫话让路 hold=0），读取必须删除"
    assert "ext = _response_watchdog_filler_ext_s() if extra_s is None else extra_s" in body
    assert "_filler.reshot_firing()" in body, "reshot 复位语义保留"


# ============================================================ P3 · pause-ack 账本


def test_pause_ack_ledger_direct_write():
    body = _seg(AGENT_SRC, 'lane="pause-ack",', "elif not paused and agent.paused:")
    assert '_ledger_ack_line("pause-ack", _pa_line)' in body
    # notify 车道仍不建票据（语音走 off-band 音轨，不进聊天史）
    assert "notify=True" in body
