#!/usr/bin/env python3
"""0913 实机验收·幽灵 job 探针(C2)。

复现 call-6bd59b40 形态:通话 ended 后向该房间再派 agent job(等价于旧版
operator 重连重建房触发的 dispatch)→ 断言:
①entrypoint 打 GHOST_JOB_REJECTED 且不产生任何幽灵轮/开场白;
②/api/token 对 ended A 线通话拒签(409,重连源头闸)。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "control-plane"))

CP = "http://127.0.0.1:8000"


async def main() -> int:
    from livekit import api as lkapi

    lk = lkapi.LiveKitAPI()  # env LIVEKIT_URL/API_KEY/SECRET(serve 已注入进程 env?显式给)
    ok = True
    with httpx.Client(timeout=10) as c:
        obj = c.post(f"{CP}/api/objects?account_id=acc-001", json={
            "display_name": f"幽灵-验收-{int(time.time())}", "language": "zh",
        }).json()
        persona = c.post(f"{CP}/api/personas?account_id=acc-001", json={
            "name": "验收客服", "language": "zh"}).json()
        call = c.post(f"{CP}/api/calls", json={
            "account_id": "acc-001", "object_id": obj["id"], "persona_id": persona["id"],
            "mode": "live", "direction": "webrtc", "language": "zh"}).json()
        room = call["id"]
        # ② token 先验证活跃通话可签
        r_live = c.post(f"{CP}/api/token", json={"call_id": room, "account_id": "acc-001"})
        print(f"token(active)={r_live.status_code}")
        ok &= r_live.status_code in (200, 201)
        # 结束通话
        c.post(f"{CP}/api/supervisor/{room}/end")
        # ② ended 后拒签
        r_end = c.post(f"{CP}/api/token", json={"call_id": room, "account_id": "acc-001"})
        print(f"token(ended)={r_end.status_code} detail={r_end.json().get('detail','')}")
        ok &= r_end.status_code == 409
        turns_before = c.get(f"{CP}/api/calls/{room}/turns").json()
        n_before = len(turns_before) if isinstance(turns_before, list) else 0
    # ① 对 ended 房间直接派 job(幽灵路径的真实触发形态)
    dispatch = await lk.agent_dispatch.create_dispatch(
        lkapi.CreateAgentDispatchRequest(agent_name="bok-voice", room=room, metadata=json.dumps({"call_id": room}))
    )
    print(f"dispatch={dispatch.id} room={room}(ended)— 等 8s 看 agent 拒接…")
    await asyncio.sleep(8)
    await lk.aclose()
    log = Path.home() / "Library/Application Support/BokVoice/logs/agent.log"
    tail = ""
    try:
        tail = log.read_text(errors="replace")[-20000:]
    except Exception:
        pass
    rejected = "GHOST_JOB_REJECTED" in tail and room in tail
    print(f"①GHOST_JOB_REJECTED logged={rejected}")
    ok &= rejected
    with httpx.Client(timeout=10) as c:
        turns_after = c.get(f"{CP}/api/calls/{room}/turns").json()
        n_after = len(turns_after) if isinstance(turns_after, list) else 0
    no_ghost = n_after == n_before
    print(f"turns before={n_before} after={n_after} no_ghost={no_ghost}")
    ok &= no_ghost
    print("GHOST_PROBE_" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
