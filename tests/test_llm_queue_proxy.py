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
import re
import sys
import time
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


def test_proxy_gate_holds_until_stream_exhausted():
    """闸门必须持满整条流（2026-09-28 串行化修正回归钉）。

    旧 bug：handler 在 `async with` 内 return StreamingResponse——__aexit__ 在
    响应头发出时放闸，decode 阶段第二条请求与第一条并行「解码」。既有 ASGI 级
    测试对此结构性失明（ASGITransport 会缓冲完整上游响应体，handler 的 send()
    直到上游流结束才返回，门从未提前放）。本测试用真 uvicorn 上游（头部先于
    体到达，与真 mlx_lm server 行为一致）实证：后台长流在场时，reply 的上游
    受理必须等到 bg 流体耗尽之后。"""
    import socket
    import threading
    import time as _time

    import uvicorn

    served: list[dict] = []
    stub = FastAPI()

    @stub.post("/v1/chat/completions")
    async def _gen(request: Request):  # noqa: ANN202
        lane = request.headers.get("x-bok-lane", "bg")
        rec = {"lane": lane, "start": _time.perf_counter(), "end": None}
        served.append(rec)

        async def stream():
            for _ in range(3):
                await asyncio.sleep(0.08)
                yield b"data: chunk\n\n"
            rec["end"] = _time.perf_counter()

        return StreamingResponse(stream(), media_type="text/event-stream")

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(stub, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started

    proxy = qp.app
    upstream_client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")
    qp._CLIENT = upstream_client
    try:
        async def call(lane: str | None):
            headers = {"X-Bok-Lane": lane} if lane else {}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=proxy), base_url="http://px"
            ) as c:
                r = await c.post("/v1/chat/completions", json={"stream": True}, headers=headers)
                body = b"".join([part async for part in r.aiter_bytes()])
                return r.status_code, len(body)

        async def scenario():
            bg1 = asyncio.create_task(call(None))       # 后台长流先占槽
            await asyncio.sleep(0.03)                   # 头已到、体未耗尽的窗口
            reply = asyncio.create_task(call("reply"))  # 闸门必须仍被 bg1 持有
            return await asyncio.gather(bg1, reply)

        results = asyncio.run(scenario())
        assert all(r[0] == 200 and r[1] > 0 for r in results)
        assert [s["lane"] for s in served] == ["bg", "reply"], f"受理顺序错: {served}"
        # 串行铁证（真 HTTP 语义）：reply 的上游受理不早于 bg1 流体耗尽
        assert served[1]["start"] >= served[0]["end"] - 0.005, (
            f"闸门提前释放(头时相): reply_start={served[1]['start']:.3f} "
            f"bg_end={served[0]['end']:.3f}"
        )
    finally:
        qp._CLIENT = None
        server.should_exit = True
        th.join(timeout=5)


# ---- 过闸观测（W-GATE，2026-09-27） ------------------------------------------
def test_gate_line_printed_for_every_generation_request(capsys):
    """每条生成请求过闸打一行 `GATE lane=<lane> waited_ms=<n> active=<n>`。

    W-GATE 观测面：TTFT 分解要按日志归因到「排队多少毫秒」——零等待快路径也要
    落行（旧 [llm-queue] 行只记 >50ms 的等待）。闸语义（单并发+reply 插队）不变。
    """
    stub = FastAPI()

    @stub.post("/v1/chat/completions")
    async def _gen():  # noqa: ANN202
        async def stream():
            for _ in range(2):
                await asyncio.sleep(0.08)
                yield b"data: x\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    proxy = qp.app
    qp._CLIENT = httpx.AsyncClient(transport=httpx.ASGITransport(app=stub), base_url="http://stub")
    try:
        async def call(lane: str | None):
            headers = {"X-Bok-Lane": lane} if lane else {}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://px") as c:
                r = await c.post("/v1/chat/completions", json={"stream": True}, headers=headers)
                b"".join([p async for p in r.aiter_bytes()])
                return r.status_code

        async def scenario():
            bg1 = asyncio.create_task(call(None))        # 占槽长流（零等待快路径）
            await asyncio.sleep(0.02)
            reply = asyncio.create_task(call("reply"))   # 后到 → 真等待（仍排 bg1 之后）
            return await asyncio.gather(bg1, reply)

        codes = asyncio.run(scenario())
        assert all(code == 200 for code in codes)
    finally:
        qp._CLIENT = None

    lines = re.findall(r"GATE lane=(\w+) waited_ms=(\d+) active=(\d+)", capsys.readouterr().out)
    assert len(lines) == 2, f"每条生成请求一行 GATE: {lines}"
    assert {lane for lane, _, _ in lines} == {"bg", "reply"}
    waited = {lane: int(ms) for lane, ms, _ in lines}
    assert waited["bg"] == 0, f"占槽者零等待也要落行: {waited}"
    assert waited["reply"] > 0, f"后到者真等过: {waited}"
    assert all(int(active) >= 1 for _, _, active in lines)


# ---- 源扫描（拓扑接线 + 车道头注入） ----------------------------------------
def test_bok_wiring_and_agent_lane_header_pinned():
    """bok.py 队列拓扑接线 + agent X-Bok-Lane 注入 + cache 档 6GB 缺省在源码钉住。"""
    bok_src = (_ROOT / "tools" / "bok.py").read_text(encoding="utf-8")
    assert "BOK_LLM_QUEUE_PROXY" in bok_src, "kill-switch 在场"
    assert '"1239"' in bok_src, "mlx 内部端口接线在场"
    assert "queue_proxy.py" in bok_src, "代理启动接线在场"
    assert "llm-proxy.pid" in bok_src, "代理 pidfile（down 清扫收编）在场"
    assert '"4GB"' in bok_src, "cache 缺省档在场"

    agent_src = (_ROOT / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py").read_text(encoding="utf-8")
    assert '"X-Bok-Lane", "reply"' in agent_src, "agent reply 车道头注入在场"


# ---- 槽泄漏三道防线（2026-10-02 根修回归钉） --------------------------------
def test_retire_idempotent_and_watchdog_reclaims_leaked_slot():
    """释放令牌化（幂等 retire）+ 租约看门狗强制回收。

    泄漏形态（review 实证）：客户端在响应体开始前断开时,starlette 在 _relay
    首次迭代前取消 stream_response——async generator 未启动则 aclose() 不执行
    生成器体,旧版唯一释放点（_relay finally 的 GATE.release()）永不跑,
    concurrency=1 下整条 LLM 通路卡死到进程重启。修法=retire 幂等多头调用安全
    + sweep_leases 超租约强制回收。
    """
    gate = qp.LaneGate(1)

    async def _hold(g, lane):
        async with g.acquire(lane):
            await asyncio.sleep(0)

    async def run():
        # ctx1 拿槽后模拟「relay 未启动」：既不 __aexit__ 也不 retire（泄漏）
        ctx1 = gate.acquire("bg")
        await ctx1.__aenter__()
        assert gate.stats()["active"] == 1

        # ctx2 排队等待（被泄漏槽卡住）
        ctx2 = gate.acquire("bg")
        t2 = asyncio.create_task(ctx2.__aenter__())
        await asyncio.sleep(0.02)
        assert not t2.done(), "槽被泄漏持有,ctx2 应仍在等待"

        # 看门狗：租约到期前不回收;到期后强制回收并唤醒等待者
        assert gate.sweep_leases(now=time.monotonic() + 1) == [], "未到期不回收"
        expired = gate.sweep_leases(now=time.monotonic() + qp._MAX_HOLD_S + 1)
        assert expired == [ctx1], f"超租约应回收泄漏持有者: {expired}"
        await asyncio.wait_for(t2, 1)
        assert gate.stats()["lease_timeouts"] == 1

        # retire 幂等：看门狗已收,ctx1 再 retire=no-op;ctx2 正常归还一次,
        # 重复归还不再掉 active
        assert ctx1.retire() is False, "已被看门狗回收,幂等 no-op"
        assert ctx2.retire() is True
        assert ctx2.retire() is False
        assert gate.stats()["active"] == 0
        # 重复 retire 后新请求仍可取槽（active 没被多扣成负数/假占用）
        async with gate.acquire("bg") as fresh:
            assert fresh.waited == 0.0
        assert gate.stats()["active"] == 0

    asyncio.run(run())


def test_pop_next_falls_through_to_bg_when_reply_waiters_cancelled():
    """reply 列残留已取消 future 时,release 必须照看 bg 列（饿死根修）。

    旧 bug：_pop_next 只挑一条列——reply 列非空就只清它,列里全是 done()
    future 时本轮返回 None,bg 等待者不被唤醒;配合快路径插队,bg 可被无限期
    推迟。修法=两列按优先级依次排空。
    """
    import contextlib

    gate = qp.LaneGate(1)

    async def _hold(g, lane):
        async with g.acquire(lane):
            await asyncio.sleep(0)

    async def run():
        bg_task = None
        async with gate.acquire("bg"):  # 占槽
            # reply 等待者排队后被取消（打断/挂断的常态路径）
            reply_task = asyncio.create_task(_hold(gate, "reply"))
            await asyncio.sleep(0.02)
            reply_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reply_task
            # reply 列现在只剩 done() future;再排一个真 bg 等待者
            bg_task = asyncio.create_task(_hold(gate, "bg"))
            await asyncio.sleep(0.02)
            assert gate._reply_waiters, "前置:reply 列确有残留"
            # async with 退出（=release）：旧版只弹 reply 列的死 future,bg 饿死
        await asyncio.wait_for(bg_task, 1)
        assert gate.stats()["active"] == 0

    asyncio.run(run())


def test_early_disconnected_client_returns_499_without_upstream():
    """排队前探断连：客户端已消失就不占槽不烧上游（499 短路）。

    手写 ASGI 调用——receive 首个消息即 http.disconnect,模拟「排队期间用户
    挂断、客户端早已不在」。此时尚未进入流式响应,与 starlette 的断连监听
    无 receive 竞争。
    """
    served: list[str] = []
    stub = FastAPI()

    @stub.post("/v1/chat/completions")
    async def _gen():  # noqa: ANN202
        served.append("called")
        return JSONResponse({"ok": True})

    qp._CLIENT = httpx.AsyncClient(transport=httpx.ASGITransport(app=stub), base_url="http://stub")
    try:
        async def run():
            scope = {
                "type": "http", "asgi": {"version": "3.0"},
                "http_version": "1.1", "method": "POST",
                "scheme": "http", "path": "/v1/chat/completions",
                "raw_path": b"/v1/chat/completions", "query_string": b"",
                "root_path": "", "server": ("px", 80), "client": ("127.0.0.1", 1),
                "headers": [(b"content-type", b"application/json")],
            }

            async def receive():
                return {"type": "http.disconnect"}

            sent: list[dict] = []

            async def send(msg):
                sent.append(msg)

            await qp.app(scope, receive, send)
            start = next(m for m in sent if m["type"] == "http.response.start")
            return start["status"]

        assert asyncio.run(run()) == 499
        assert served == [], "客户端已断开,上游不应被烧一次生成"
        assert qp.GATE.stats()["active"] == 0, "断开请求不得占槽"
    finally:
        qp._CLIENT = None
