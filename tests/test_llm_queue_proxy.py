""":1235 优先级队列代理（services/llm-mlx/queue_proxy.py，2026-09-26 根治 mlx
解码争用）单测。

根因：mlx_lm server ThreadingHTTPServer 生成路径零锁，并发请求 GPU 时间片
互抢（一次三重派发排空 10 请求各等 44-73s）；挂断 settle 的 Summarizer（3-4s）
正压下一通首轮。修法=代理占 :1235 单并发排队、agent 回复带 X-Bok-Lane: reply
插队；mlx 挪 :1239（bok.py _start_llm 接线，BOK_LLM_QUEUE_PROXY=0 回旧拓扑）。

三层：
- LaneGate 纯逻辑（reply 插队/车道内 FIFO/槽语义/stats）；
- ASGI 级端到端（桩上游记录起止时刻：后台长流在场时 reply 先于后台第二条
  被服务——插队行为整链实证，非只看门）；
- 源扫描（bok.py 拓扑接线、agent 车道头注入在场）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

# fastapi/httpx 导入必须在模块级：文件头有 `from __future__ import annotations`
# （PEP 563），stub 路由的 `request: Request` 成字符串注解，FastAPI 用模块 globals
# 解析 get_type_hints——函数内局部导入会令参数绑定静默失败（实测整链快败无队列日志）。
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "bok_llm_queue_proxy", _ROOT / "services" / "llm-mlx" / "queue_proxy.py")
qp = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("bok_llm_queue_proxy", qp)
_SPEC.loader.exec_module(qp)


# ---- LaneGate 纯逻辑 --------------------------------------------------------
def test_lane_gate_reply_jumps_bg_queue():
    """槽被占时,后到的 reply 先于先到的 bg 被服务（插队=根治点的行为面）。"""
    gate = qp.LaneGate(1)
    done: list[str] = []

    async def hold(lane: str, tag: str):
        async with gate.acquire(lane):
            done.append(tag)

    async def run():
        order: list[str] = []
        async with gate.acquire("bg") as first:  # 占槽的长后台任务
            assert first.waited == 0.0
            bg2 = asyncio.create_task(hold("bg", "bg2"))
            reply = asyncio.create_task(hold("reply", "reply"))
            await asyncio.sleep(0.05)  # 两个等待者都排进队
            order.append("release")
            gate.release()  # 弹 reply（优先）
            await asyncio.wait_for(reply, 1)
            gate.release()  # 弹 bg2
            await asyncio.wait_for(bg2, 1)
            gate.release()
            return order + done

    assert asyncio.run(run()) == ["release", "reply", "bg2"]
    assert gate.stats()["reply_served"] == 1
    assert gate.stats()["bg_served"] == 2


def test_lane_gate_fifo_within_lane_and_free_pass():
    """车道内先到先服务;槽空闲时 acquire 立即过（零等待快路径）。"""
    gate = qp.LaneGate(1)

    async def run():
        async with gate.acquire("reply"):
            r2 = asyncio.create_task(_hold(gate, "reply"))
            r3 = asyncio.create_task(_hold(gate, "reply"))
            await asyncio.sleep(0.03)
            gate.release()
            await asyncio.wait_for(r2, 1)
            gate.release()
            await asyncio.wait_for(r3, 1)
            gate.release()
        # 空槽直过（零等待快路径）
        async with gate.acquire("bg") as free:
            return free.waited

    async def _hold(g, lane):
        async with g.acquire(lane):
            await asyncio.sleep(0)

    assert asyncio.run(run()) == 0.0


def test_lane_gate_stats_shape():
    gate = qp.LaneGate(2)
    s = gate.stats()
    assert s["active"] == 0 and s["queued_reply"] == 0 and s["queued_bg"] == 0


# ---- ASGI 级端到端（桩上游 + 代理 app） -------------------------------------
def test_proxy_reply_priority_end_to_end():
    """后台长流在场 + 后台第二条排队时,repy 请求先被上游服务（整链插队实证）。"""
    served: list[dict] = []

    stub = FastAPI()

    @stub.post("/v1/chat/completions")
    async def _gen(request: Request):  # noqa: ANN202
        lane = request.headers.get("x-bok-lane", "bg")
        rec = {"start": asyncio.get_running_loop().time(), "lane": lane}
        served.append(rec)

        async def stream():
            for _ in range(3):
                await asyncio.sleep(0.08)
                yield b"data: chunk\n\n"
            rec["end"] = asyncio.get_running_loop().time()

        return StreamingResponse(stream(), media_type="text/event-stream")

    proxy = qp.app
    upstream_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=stub), base_url="http://stub")
    qp._CLIENT = upstream_client
    try:
        async def call(lane: str | None):
            headers = {"X-Bok-Lane": lane} if lane else {}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://px") as c:
                r = await c.post("/v1/chat/completions", json={"stream": True}, headers=headers)
                body = b"".join([part async for part in r.aiter_bytes()])
                return r.status_code, len(body), r.headers.get("x-bok-queue-waited-ms")

        async def scenario():
            bg1 = asyncio.create_task(call(None))       # 后台长流先占槽
            await asyncio.sleep(0.02)
            bg2 = asyncio.create_task(call(None))       # 后台第二条排队
            await asyncio.sleep(0.02)
            reply = asyncio.create_task(call("reply"))  # 回复后到 → 插队
            return await asyncio.gather(bg1, bg2, reply)

        results = asyncio.run(scenario())
        assert all(r[0] == 200 and r[1] > 0 for r in results)
        # 上游受理顺序（单槽串行）：bg1(占槽) → reply(插队) → bg2
        lanes = [s["lane"] for s in served]
        assert lanes == ["bg", "reply", "bg"], f"受理顺序错: {lanes}"
        # 串行铁证：每条的开始不早于上一条的结束（单并发,无交错）
        assert served[1]["start"] >= served[0]["end"] - 0.005
        assert served[2]["start"] >= served[1]["end"] - 0.005
        # bg2 等得比 reply 久（插队的量化面）
        waited = {"bg2": float(results[1][2]), "reply": float(results[2][2])}
        assert waited["bg2"] > waited["reply"], f"插队无效: {waited}"
    finally:
        qp._CLIENT = None


def test_proxy_passthrough_not_queued():
    """非生成端点（/v1/models）直通：即便生成槽被占也即刻应答。"""
    stub = FastAPI()

    @stub.get("/v1/models")
    async def _models():
        return JSONResponse({"data": [{"id": "stub"}]})

    @stub.post("/v1/chat/completions")
    async def _gen():  # noqa: ANN202
        async def stream():
            for _ in range(2):
                await asyncio.sleep(0.08)
                yield b"x"
        return StreamingResponse(stream(), media_type="text/event-stream")

    proxy = qp.app
    qp._CLIENT = httpx.AsyncClient(transport=httpx.ASGITransport(app=stub), base_url="http://stub")
    try:
        async def scenario():
            gen = asyncio.create_task(_post_gen())
            await asyncio.sleep(0.03)  # gen 已占槽
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://px") as c:
                r = await c.get("/v1/models")
                models_ok = r.status_code == 200 and r.json()["data"][0]["id"] == "stub"
            await asyncio.wait_for(gen, 5)
            return models_ok

        async def _post_gen():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://px") as c:
                r = await c.post("/v1/chat/completions", json={"stream": True})
                b"".join([p async for p in r.aiter_bytes()])

        assert asyncio.run(scenario()) is True
    finally:
        qp._CLIENT = None


# ---- 源扫描（拓扑接线 + 车道头注入） ----------------------------------------
def test_bok_wiring_and_agent_lane_header_pinned():
    """bok.py 队列拓扑接线 + agent X-Bok-Lane 注入 + cache 档 6GB 缺省在源码钉住。"""
    bok_src = (_ROOT / "tools" / "bok.py").read_text(encoding="utf-8")
    assert "BOK_LLM_QUEUE_PROXY" in bok_src, "kill-switch 在场"
    assert '"1239"' in bok_src, "mlx 内部端口接线在场"
    assert "queue_proxy.py" in bok_src, "代理启动接线在场"
    assert "llm-proxy.pid" in bok_src, "代理 pidfile（down 清扫收编）在场"
    assert '"6GB"' in bok_src, "cache 缺省档在场"

    agent_src = (_ROOT / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py").read_text(encoding="utf-8")
    assert '"X-Bok-Lane", "reply"' in agent_src, "agent reply 车道头注入在场"
