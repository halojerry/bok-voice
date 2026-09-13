from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.dialer import (
    OUT_ANSWERED, OUT_FAILED, OUT_NO_ANSWER, OUT_REJECTED,
    DialOutcome, map_sip_status_code, resolve_dial_mode,
)
from agent_runtime.dialer import _dial_mock, _dial_real, _wait_participant
from agent_runtime import dialer as _dialer_mod


def test_dial_outbound_clamps_ringing_timeout(monkeypatch):
    """入口硬钳 [0, 80]（spec §3：protobuf Duration 端上限 80s）。

    运营/编排给超窗值一律砍到 80，负值归 0（=不等振铃）；钳制发生在后端分派前。
    """
    seen: list[float] = []

    async def _fake_mock(ctx, *, number, cp_base, call_id, scenario, language,
                         script, ringing_timeout_s, speak_interval_s=0.0):
        seen.append(ringing_timeout_s)
        return DialOutcome(status=OUT_ANSWERED)

    monkeypatch.setattr(_dialer_mod, "_dial_mock", _fake_mock, raising=True)
    for given, expected in ((30.0, 30.0), (999.0, 80.0), (-5.0, 0.0), (80.0, 80.0)):
        asyncio.run(_dialer_mod.dial_outbound(
            object(), number="123", mode="mock", cp_base="http://cp", call_id="c1",
            ringing_timeout_s=given))
        assert seen[-1] == expected, f"given={given}"


def test_map_sip_status_code():
    assert map_sip_status_code(486) == OUT_REJECTED
    assert map_sip_status_code(603) == OUT_REJECTED
    assert map_sip_status_code(408) == OUT_NO_ANSWER
    assert map_sip_status_code(480) == OUT_NO_ANSWER
    assert map_sip_status_code(500) == OUT_FAILED
    assert map_sip_status_code(0) == OUT_FAILED


def test_resolve_dial_mode():
    assert resolve_dial_mode({"BOK_SIP_MODE": "real"}, {}) == "real"
    assert resolve_dial_mode({}, {"sip": {"mode": "real"}}) == "real"
    assert resolve_dial_mode({}, {}) == "mock"
    assert resolve_dial_mode({"BOK_SIP_MODE": "bogus"}, {"sip": {"mode": "real"}}) == "mock"
    assert resolve_dial_mode({}, {"sip": {}}) == "mock"


def test_resolve_dial_mode_normalizes_and_falls_back():
    # 大小写/空白归一
    assert resolve_dial_mode({"BOK_SIP_MODE": " REAL "}, {}) == "real"
    assert resolve_dial_mode({"BOK_SIP_MODE": "Mock"}, {}) == "mock"
    # env 有值但空串 = 未设置 → 回落 settings
    assert resolve_dial_mode({"BOK_SIP_MODE": "  "}, {"sip": {"mode": "real"}}) == "real"
    # settings 段整体缺失 / 非法值 → mock
    assert resolve_dial_mode({}, None) == "mock"
    assert resolve_dial_mode({}, {"sip": None}) == "mock"
    assert resolve_dial_mode({}, {"sip": {"mode": "bogus"}}) == "mock"


# ---- _wait_participant 兼容包装（livekit-agents 1.8 无 timeout kwarg） ----

class _CtxNoTimeout:
    """复刻 1.8 真实签名：无 timeout kwarg → 必须走 asyncio.wait_for 回退。"""

    async def wait_for_participant(self, identity=None, kind=None):
        await asyncio.sleep(0.01)
        return f"joined:{identity}"


class _CtxWithTimeout:
    async def wait_for_participant(self, identity=None, kind=None, timeout=None):
        return f"joined:{identity}:{timeout}"


class _CtxNeverJoins:
    async def wait_for_participant(self, identity=None, kind=None):
        await asyncio.sleep(30)


def test_wait_participant_falls_back_without_timeout_kwarg():
    got = asyncio.run(_wait_participant(_CtxNoTimeout(), "sip-x", 1.0))
    assert got == "joined:sip-x"


def test_wait_participant_uses_timeout_kwarg_when_supported():
    got = asyncio.run(_wait_participant(_CtxWithTimeout(), "sip-x", 2.0))
    assert got == "joined:sip-x:2.0"


def test_wait_participant_raises_on_timeout():
    try:
        asyncio.run(_wait_participant(_CtxNeverJoins(), "sip-x", 0.05))
    except (TimeoutError, asyncio.TimeoutError):
        return
    raise AssertionError("expected TimeoutError")


# ---- real 后端：SipCallError → 四态映射（SDK 真对象，非 stub） ----

class _SipApi:
    def __init__(self, exc: Exception | None) -> None:
        self._exc = exc
        self.requests: list[object] = []

    async def create_sip_participant(self, req):
        self.requests.append(req)
        if self._exc is not None:
            raise self._exc
        return None


class _Api:
    def __init__(self, exc: Exception | None) -> None:
        self.sip = _SipApi(exc)


class _Room:
    name = "room-1"


class _RealCtx:
    def __init__(self, exc: Exception | None) -> None:
        self.api = _Api(exc)
        self.room = _Room()

    async def wait_for_participant(self, identity=None, kind=None):
        return f"joined:{identity}"


def _sip_error(code: int):
    """构造 SDK 真 SipCallError（sip_status_code 由 metadata 派生）。"""
    from livekit.api import SipCallError
    return SipCallError(
        str(code), "busy", status=400,
        metadata={"sip_status_code": str(code), "sip_status": "busy"},
    )


def test_dial_real_maps_sip_status_codes():
    for code, expected in ((486, OUT_REJECTED), (603, OUT_REJECTED),
                           (408, OUT_NO_ANSWER), (480, OUT_NO_ANSWER),
                           (500, OUT_FAILED)):
        ctx = _RealCtx(_sip_error(code))
        out = asyncio.run(_dial_real(ctx, number="123", trunk_id="t1",
                                     ringing_timeout_s=30.0))
        assert isinstance(out, DialOutcome)
        assert out.status == expected, f"code={code}"
        assert out.participant_identity == "sip-123"


def test_dial_real_without_sip_metadata_degrades_to_failed():
    from livekit.api import SipCallError
    ctx = _RealCtx(SipCallError("err", "boom", status=500))
    out = asyncio.run(_dial_real(ctx, number="123", trunk_id="t1",
                                 ringing_timeout_s=30.0))
    assert out.status == OUT_FAILED


def test_dial_real_answered():
    ctx = _RealCtx(None)
    out = asyncio.run(_dial_real(ctx, number="123", trunk_id="t1",
                                 ringing_timeout_s=30.0))
    assert out.status == OUT_ANSWERED
    assert out.participant_identity == "sip-123"


class _RealCtxNeverJoins(_RealCtx):
    """CreateSIPParticipant 成功但 participant 落地前被拆/未进房 → wait 超时。"""

    async def wait_for_participant(self, identity=None, kind=None):
        await asyncio.sleep(30)


def test_dial_real_participant_wait_timeout_is_failed(monkeypatch):
    # T5 审查遗留:wait_for_participant 的 TimeoutError 曾逸出 dial_outbound,
    # 违背「统一四态出口」。修复后包成 OUT_FAILED(_wait_participant 走 wait_for 回退,
    # 故把超时压到 0 避免单测真等 10s)。
    from agent_runtime import dialer
    monkeypatch.setattr(dialer, "_wait_participant",
                        lambda ctx, identity, timeout: asyncio.wait_for(
                            ctx.wait_for_participant(identity=identity), 0.05))
    ctx = _RealCtxNeverJoins(None)
    out = asyncio.run(_dial_real(ctx, number="123", trunk_id="t1",
                                 ringing_timeout_s=30.0))
    assert isinstance(out, DialOutcome)
    assert out.status == OUT_FAILED
    assert out.participant_identity == "sip-123"


# ---- mock 后端：spawn HTTP / 进房超时 / 接通前离房 ----

class _MockRoom:
    name = "room-mock"

    def __init__(self) -> None:
        self._handlers: dict[str, list] = {}

    def on(self, ev, cb) -> None:
        self._handlers.setdefault(ev, []).append(cb)

    def off(self, ev, cb) -> None:
        self._handlers.get(ev, []).remove(cb)

    def emit(self, ev, payload) -> None:
        for cb in list(self._handlers.get(ev, [])):
            cb(payload)


class _Callee:
    def __init__(self, identity: str) -> None:
        self.identity = identity


class _MockCtx:
    def __init__(self, joins: bool = True) -> None:
        self.room = _MockRoom()
        self._joins = joins

    async def wait_for_participant(self, identity=None, kind=None):
        if not self._joins:
            await asyncio.sleep(30)
        return _Callee(identity)


class _FakeHttp:
    """替身 aiohttp.ClientSession：post() 返回指定状态码的上下文管理器。"""

    def __init__(self, status: int = 200) -> None:
        self._status = status

    def post(self, *a, **k):
        status = self._status

        class _Resp:
            def __init__(self, status: int) -> None:
                self.status = status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        return _Resp(status)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _run_mock(monkeypatch, *, ctx, status=200, **kw):
    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession",
                        lambda *a, **k: _FakeHttp(status), raising=True)
    return asyncio.run(_dial_mock(
        ctx, number="123", cp_base="http://cp", call_id="c1",
        scenario=kw.pop("scenario", "answer"),
        language=kw.pop("language", "cantonese"),
        script=kw.pop("script", []),
        ringing_timeout_s=kw.pop("ringing_timeout_s", 5.0), **kw))


def test_dial_mock_answered(monkeypatch):
    out = _run_mock(monkeypatch, ctx=_MockCtx(joins=True))
    assert out.status == OUT_ANSWERED
    assert out.participant_identity == "sip-mock-123"


def test_dial_mock_spawn_http_error_is_failed(monkeypatch):
    out = _run_mock(monkeypatch, ctx=_MockCtx(), status=500)
    assert out.status == OUT_FAILED
    assert "500" in out.detail


def test_dial_mock_never_joins_is_no_answer(monkeypatch):
    out = _run_mock(monkeypatch, ctx=_MockCtx(joins=False), ringing_timeout_s=0.1)
    assert out.status == OUT_NO_ANSWER


def test_dial_mock_leaves_before_audio_is_rejected(monkeypatch):
    ctx = _MockCtx(joins=True)

    async def _leave_soon():
        await asyncio.sleep(0.05)
        ctx.room.emit("participant_disconnected", _Callee("sip-mock-123"))

    async def _run():
        task = asyncio.ensure_future(_leave_soon())
        import aiohttp
        monkeypatch.setattr(aiohttp, "ClientSession",
                            lambda *a, **k: _FakeHttp(200), raising=True)
        try:
            return await _dial_mock(
                ctx, number="123", cp_base="http://cp", call_id="c1",
                scenario="reject", language="cantonese", script=[],
                ringing_timeout_s=5.0)
        finally:
            task.cancel()

    out = asyncio.run(_run())
    assert out.status == OUT_REJECTED
