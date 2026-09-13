"""同传挂断/结算修复回归（2026-09-11 三路 subagent 审计落地）。

覆盖四个审计定案的 P0/P1：
1. /api/token 不得把终态通话复活回 ACTIVE——09-10 call-a9511563 实证：hangup 200 后
   +18ms 一发断线重连 token 把 ENDED 拉回 ACTIVE，「挂断不结算」主根因。
2. hangup 不得同步阻塞在 LiveKit 断房上——LiveKit 短暂故障时每发 3s、7 连发
   （实测 3012-3023ms/发），断房应后台化（DB 已置 ENDED，断房 best-effort）。
3. webhook participant_left 对 kind=interpret 房不得补派 A 线 bok-voice——
   interp worker 离房被误判 A 线崩溃，往已结束同传房补派形成 5min 周期殭尸循环。
4. reaper 判「房里有人」只数真人——纯 agent 房（殭尸被补派进去的）按空房回收。
"""
import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

from fastapi.testclient import TestClient

from control_plane.main import app


def _wait_until(pred, what: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


def _lkapi_dummy() -> MagicMock:
    api = MagicMock(aclose=AsyncMock())
    api.room.list_participants = AsyncMock(return_value=SimpleNamespace(participants=[]))
    api.agent_dispatch.list_dispatch = AsyncMock(return_value=[])
    return api


def _create_interpret_call(client: TestClient) -> dict:
    return client.post(
        "/api/calls",
        json={
            "account_id": "acc-001",
            "object_id": "",
            "kind": "interpret",
            "mode": "live",
            "direction": "interpret",
            "language": "zh",
            "target_lang": "en",
        },
    ).json()


def test_token_does_not_revive_terminated_call(monkeypatch):
    """终态（ended/failed）通话再取 token：签发照常 200，但 status 不得回 ACTIVE。"""
    from control_plane import main as m

    async def _noop_disconnect(room_name: str) -> None:
        return None

    monkeypatch.setattr(m, "_disconnect_livekit_room", _noop_disconnect)
    with TestClient(m.app) as client:
        created = _create_interpret_call(client)
        room = created["id"]
        assert created["status"] == "ringing"

        # 正常推进不被误伤：首枚 token 把 ringing → active。
        assert client.post("/api/token", json={"account_id": "acc-001", "call_id": room, "role": "me"}).status_code in (200, 201)
        assert client.get(f"/api/calls/{room}").json()["status"] == "active"

        # 挂断 → ended。
        assert client.post(f"/api/calls/{room}/hangup").json()["status"] == "ended"

        # 断线重连的客户端再取 token（09-10 +18ms 复活现场）：仍签发，但终态不翻。
        resp = client.post("/api/token", json={"account_id": "acc-001", "call_id": room, "role": "other"})
        assert resp.status_code in (200, 201)
        assert client.get(f"/api/calls/{room}").json()["status"] == "ended"

        # failed 终态同样不被复活。
        m._repo().update_call(room, status="failed")
        assert client.post("/api/token", json={"account_id": "acc-001", "call_id": room, "role": "me"}).status_code in (200, 201)
        assert client.get(f"/api/calls/{room}").json()["status"] == "failed"


def test_hangup_returns_before_slow_room_delete(monkeypatch):
    """LiveKit 断房慢/故障时 hangup 必须立即返回（断房后台化），且断房最终仍执行。"""
    from control_plane import main as m

    disconnect = AsyncMock(return_value=None)

    async def _slow(room_name: str) -> None:
        await asyncio.sleep(1.0)

    disconnect.side_effect = _slow
    monkeypatch.setattr(m, "_disconnect_livekit_room", disconnect)
    with TestClient(m.app) as client:
        created = _create_interpret_call(client)
        room = created["id"]
        t0 = time.monotonic()
        resp = client.post(f"/api/calls/{room}/hangup")
        dt = time.monotonic() - t0
        assert resp.status_code == 200
        assert resp.json()["status"] == "ended"
        assert dt < 0.5, f"hangup 在断房上阻塞了 {dt:.2f}s"
        # 后台断房最终执行（TestClient 上下文内等完，防上下文退出取消任务）。
        _wait_until(lambda: disconnect.await_count == 1, "background room delete")


def test_webhook_skips_redispatch_for_interpret_rooms(monkeypatch):
    """kind=interpret 房的 agent 离房 ≠ A 线崩溃——不补派 bok-voice（殭尸循环根因）。"""
    from control_plane import main as m

    lkapi = _lkapi_dummy()
    lkapi.agent_dispatch.create_dispatch = AsyncMock()
    monkeypatch.setattr(m, "_lkapi_client", lambda: lkapi)
    with TestClient(m.app) as client:
        room = _create_interpret_call(client)["id"]
        resp = client.post(
            "/api/webhook/livekit",
            json={"event": "participant_left", "room": {"name": room}, "participant": {"identity": "agent-AJ_test"}},
        )
        assert resp.json().get("handled") is False
        # 留出后台误派窗口再断言（若回归，create_task 会在毫秒级就 create）。
        time.sleep(0.3)
    lkapi.agent_dispatch.create_dispatch.assert_not_awaited()


def test_room_with_only_agents_counts_as_empty():
    """reaper 的「房里有人」只数真人：纯 agent 房按空房回收，殭尸房可被兜底收割。"""
    from control_plane import main as m

    def _p(identity: str) -> SimpleNamespace:
        return SimpleNamespace(identity=identity)

    assert m._has_human_participants([_p("bok-voice"), _p("agent-AJ_1"), _p("agent-AJ_2")]) is False
    assert m._has_human_participants([_p("me-call-x"), _p("agent-AJ_1")]) is True
    assert m._has_human_participants([_p("other-call-y")]) is True
    assert m._has_human_participants([]) is False
