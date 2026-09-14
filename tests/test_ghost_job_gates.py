"""C2 幽灵 job 三道闸(2026-09-13,call-6bd59b40 实证):

挂断后 operator 页 livekit 全量重连 → CP /api/token 旧版照签新 token →
重连重建房 → LiveKit 再派 job → agent 对无人房重放开场白 + turns 幽灵轮
+ session_report 被幽灵 job 覆盖 + 旧进程 kill -30。

三闸:①agent entrypoint 装配时 status=ended 拒接(GHOST_JOB_REJECTED,
CP 不可达保守放行——entrypoint 闭包,代码位次+compileall 保证);②CP
session-report 对 ended 且已有 report 的 call 409 拒绝覆盖(首个 report
在 ended 后仍收——agent 收尾先 ended 后上报的顺序);③CP /api/token 对
ended 通话拒签(掐断重连链的源头;web tokenSource.custom 续签前同款
预检双保险)。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # in-memory repo
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

from fastapi.testclient import TestClient  # noqa: E402

from control_plane.main import app  # noqa: E402


def _mk_call(client: TestClient) -> str:
    created = client.post(
        "/api/calls",
        json={"account_id": "acc-001", "object_id": "obj-1", "persona_id": "p-1", "mode": "simulation"},
    ).json()
    return str(created["id"])


# ---- 闸3:token 拒签 ended 通话 ----


def test_token_refused_for_ended_call():
    with TestClient(app) as client:
        call_id = _mk_call(client)
        # 活跃通话照签(官方 TokenSource 契约)
        ok = client.post("/api/token", json={"call_id": call_id, "account_id": "acc-001"})
        assert ok.status_code in (200, 201) and "participantToken" in ok.json()
        # 结束后拒签(幽灵重连源头)
        ended = client.post(f"/api/supervisor/{call_id}/end")
        assert ended.status_code == 200
        refused = client.post("/api/token", json={"call_id": call_id, "account_id": "acc-001"})
        assert refused.status_code == 409
        assert "ended" in refused.json()["detail"]
        # 未知房间(CP 查无此通话)保守放行——新建流/竞态不可误伤
        fresh = client.post("/api/token", json={"room_name": "call-never-exists", "account_id": "acc-001"})
        assert fresh.status_code in (200, 201)
        # B 线 interpret 不拦:0912 定案「ended 后仍签发,终态不翻」契约保持
        itp = client.post(
            "/api/calls",
            json={"account_id": "acc-001", "object_id": "obj-1", "persona_id": "p-1",
                  "mode": "simulation", "kind": "interpret", "target_lang": "en"},
        ).json()
        client.post("/api/token", json={"account_id": "acc-001", "call_id": itp["id"], "role": "me"})
        client.post(f"/api/calls/{itp['id']}/hangup")
        resp = client.post("/api/token", json={"account_id": "acc-001", "call_id": itp["id"], "role": "other"})
        assert resp.status_code in (200, 201)
        assert client.get(f"/api/calls/{itp['id']}").json()["status"] == "ended"


# ---- 闸2:session-report 幽灵覆盖防护 ----


def test_session_report_ghost_overwrite_rejected():
    with TestClient(app) as client:
        call_id = _mk_call(client)
        client.post(f"/api/supervisor/{call_id}/end")
        # ended 后的首个 report 仍收(agent 收尾顺序:先 ended 后上报)
        first = client.post(f"/api/calls/{call_id}/session-report", json={"usage": {"llm": 1}})
        assert first.status_code == 200 and first.json()["stored"] is True
        # 幽灵 job 的第二份 → 409 拒绝覆盖
        ghost = client.post(f"/api/calls/{call_id}/session-report", json={"usage": {"llm": 999}})
        assert ghost.status_code == 409
        # 真数据未被覆盖
        row = client.get(f"/api/calls/{call_id}").json()
        assert '"llm": 1' in str(row.get("session_report") or "")
        # 活跃通话不受影响
        live_id = _mk_call(client)
        r = client.post(f"/api/calls/{live_id}/session-report", json={"usage": {"llm": 2}})
        assert r.status_code == 200
