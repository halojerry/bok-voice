"""SIP 外播拨号薄层（spec 2026-09-12 Wave2）。

统一四态出口，双后端:
- real:官方 livekit.api CreateSIPParticipant(wait_until_answered=True),
  SipCallError 按 SIP 码映射(486/603 拒接、408/480 无人接、5xx trunk 故障)。
- mock:CP 派生 scripts/mock_callee.py 子进程(真 TTS 客户语音)进房,
  agent wait_for_participant;超时=no_answer、进房后 1.5s 内离房且零音频=rejected。

官方铁律:USER_UNAVAILABLE/SIP_TRUNK_FAILURE(即 no_answer/failed)RoomIO 不自动收,
调用方必须 ctx.shutdown()。ANSWERED 才准 session.start(开场白时序=接通后)。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Mapping

OUT_ANSWERED = "answered"
OUT_NO_ANSWER = "no_answer"
OUT_REJECTED = "rejected"
OUT_FAILED = "failed"

_REJECT_CODES = (486, 603)
_NO_ANSWER_CODES = (408, 480)


@dataclass
class DialOutcome:
    status: str
    participant_identity: str = ""
    detail: str = ""


def map_sip_status_code(code: int) -> str:
    if code in _REJECT_CODES:
        return OUT_REJECTED
    if code in _NO_ANSWER_CODES:
        return OUT_NO_ANSWER
    return OUT_FAILED


def resolve_dial_mode(env: Mapping[str, str],
                      settings: Mapping[str, Any] | None) -> str:
    """解析拨号后端:env kill-switch 优先于 settings DB,兜底 mock。

    语义钉死:env `BOK_SIP_MODE` **有值即为显式覆盖**——合法值(mock/real)直接用;
    非法值视为「显式配错」直接回落 mock,不再读 settings(防止误配 env 被 settings
    静默否决)。settings 只在 env 缺省/为空时生效。
    """
    raw = str(env.get("BOK_SIP_MODE", "") or "").strip().lower()
    if raw:
        return raw if raw in ("mock", "real") else "mock"
    mode = str(((settings or {}).get("sip") or {}).get("mode") or "").strip().lower()
    return mode if mode in ("mock", "real") else "mock"


async def dial_outbound(ctx, *, number: str, mode: str, cp_base: str, call_id: str,
                        scenario: str = "", language: str = "",
                        script: list[str] | None = None,
                        ringing_timeout_s: float = 30.0, trunk_id: str = "",
                        speak_interval_s: float = 0.0) -> DialOutcome:
    # 振铃窗口硬上限 80s（spec §3：protobuf Duration 端拒绝/截断超窗值，且真 SIP 侧
    # 同一振铃窗最多 ~80s）。settings/编排给什么都在入口钳死，负值一律归 0（=不等振铃）。
    ringing_timeout_s = min(max(0.0, float(ringing_timeout_s)), 80.0)
    if mode == "real":
        return await _dial_real(ctx, number=number, trunk_id=trunk_id,
                                ringing_timeout_s=ringing_timeout_s)
    return await _dial_mock(ctx, number=number, cp_base=cp_base, call_id=call_id,
                            scenario=scenario, language=language, script=script,
                            ringing_timeout_s=ringing_timeout_s,
                            speak_interval_s=speak_interval_s)


async def _dial_real(ctx, *, number: str, trunk_id: str, ringing_timeout_s: float) -> DialOutcome:
    from datetime import timedelta

    from livekit.api import CreateSIPParticipantRequest
    identity = f"sip-{number}"
    try:
        await ctx.api.sip.create_sip_participant(
            CreateSIPParticipantRequest(
                sip_trunk_id=trunk_id, sip_call_to=number,
                room_name=ctx.room.name, participant_identity=identity,
                wait_until_answered=True,
                # 注意:protobuf Duration 字段只收 timedelta,传 float 会在构造期
                # TypeError 并被下面的 broad except 吞成 failed(实测 1.8.0)。
                ringing_timeout=timedelta(seconds=max(0.0, float(ringing_timeout_s))),
            )
        )
    except Exception as exc:  # livekit.api.SipCallError —— 属性防御式读取
        # SipCallError.sip_status_code 由 metadata 派生(None=sdk 未带码) → 兜 0=failed。
        code = int(getattr(exc, "sip_status_code", 0) or 0)
        return DialOutcome(status=map_sip_status_code(code), participant_identity=identity,
                           detail=f"{type(exc).__name__}: {exc}")
    try:
        await _wait_participant(ctx, identity, 10.0)
    except (TimeoutError, asyncio.TimeoutError):
        # 竞态:T5 审查遗留——CreateSIPParticipant(wait_until_answered) 返回后 participant
        # 已被 dismantle/未落地时 wait_for_participant 会超时。统一四态出口铁律:
        # 任何异常不得逸出 dial_outbound,超时按 FAILED(振铃已过、人未在房=接线落地失败)。
        return DialOutcome(status=OUT_FAILED, participant_identity=identity,
                           detail="participant not in room after answer")
    return DialOutcome(status=OUT_ANSWERED, participant_identity=identity)


async def _wait_participant(ctx, identity: str, timeout_s: float):
    """wait_for_participant 兼容包装:1.8 若无 timeout kwarg 则退 asyncio.wait_for。"""
    try:
        return await ctx.wait_for_participant(identity=identity, timeout=timeout_s)
    except TypeError:
        return await asyncio.wait_for(ctx.wait_for_participant(identity=identity), timeout_s)


async def _dial_mock(ctx, *, number: str, cp_base: str, call_id: str, scenario: str,
                     language: str, script: list[str] | None,
                     ringing_timeout_s: float,
                     speak_interval_s: float = 0.0) -> DialOutcome:
    import aiohttp

    identity = f"sip-mock-{number}"
    payload = {
        "room": ctx.room.name, "number": number, "identity": identity,
        "scenario": scenario or "answer", "language": language or "cantonese",
        "script": script or [], "ring_delay_s": 3.0,
        "ringing_window_s": ringing_timeout_s,
    }
    # 句间隔只在调用方显式给了正数时下发——缺省沿用子进程自带的 6s（演练/手起
    # 调试零行为变化；E2E 用 campaign dial 块把它对齐到 AI 步进）。
    if float(speak_interval_s or 0) > 0:
        payload["speak_interval_s"] = float(speak_interval_s)
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{cp_base}/api/sip/mock/callee", json=payload) as resp:
            if resp.status != 200:
                return DialOutcome(status=OUT_FAILED, detail=f"mock spawn http {resp.status}")
    try:
        await _wait_participant(ctx, identity, ringing_timeout_s + 5)
    except (TimeoutError, asyncio.TimeoutError):
        return DialOutcome(status=OUT_NO_ANSWER, participant_identity=identity,
                           detail="mock callee never joined")
    # 接通前离房判定(reject 剧本):1.5s 窗内该 participant 离开 → 拒接语义
    left = asyncio.Event()

    def _on_disc(p) -> None:
        if getattr(p, "identity", "") == identity:
            left.set()

    ctx.room.on("participant_disconnected", _on_disc)
    try:
        try:
            await asyncio.wait_for(left.wait(), timeout=1.5)
            return DialOutcome(status=OUT_REJECTED, participant_identity=identity,
                               detail="callee left before audio")
        except asyncio.TimeoutError:
            pass
    finally:
        ctx.room.off("participant_disconnected", _on_disc)
    return DialOutcome(status=OUT_ANSWERED, participant_identity=identity)
