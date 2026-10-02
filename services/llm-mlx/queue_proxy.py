#!/usr/bin/env python
""":1235 单并发优先级队列代理（根治 mlx 解码争用，2026-09-26）。

背景（实测归因）：mlx_lm server 是 ThreadingHTTPServer、生成路径零锁——多个
/v1/chat/completions 并发时 GPU 时间片互抢，一次三重派发曾把 10 个请求排到
73s；方差 1:1 映射并发度（solo 轮 tps 13-38，并发窗 3-6）。挂断 settle 的
Summarizer、qa-cluster、judge 等后台消费者与活通话回复同打 :1235，慢的正是
「下一通首轮撞上一通结算」这个窗。

形态：本代理占公网口 :1235，mlx_lm server 挪内部 :1239；生成类请求过
LaneGate（默认并发 1=彻底消灭交错），**reply 车道插队**（agent 回复请求带
`X-Bok-Lane: reply` 头，MlxLlmLLM 注入）——后台长请求（settle 3-4s）在场时
回复只排「当前正在生成的那一个」的尾，不再排在整队后台任务后面。非生成端
点（/v1/models、/health…）直通不排队。

env：BOK_LLM_QUEUE_UPSTREAM（默认 http://127.0.0.1:1239）、
BOK_LLM_QUEUE_CONCURRENCY（默认 1）、BOK_LLM_QUEUE_HOST/PORT（127.0.0.1:1235）、
BOK_LLM_QUEUE_MAX_HOLD（租约看门狗秒数，默认 900，0=关）。

槽位释放三道防线（2026-10-02 槽泄漏根修）：旧版唯一释放点在 _relay 的 finally，
而客户端在响应体开始前断开时 starlette 会把 stream_response 任务在首次迭代前取消
——async generator 未启动则 aclose() 不执行生成器体，finally 永不跑，槽位永久
泄漏（concurrency=1 下整条 LLM 通路卡死到进程重启；打断/挂断掐断 httpx 连接是
常态触发）。修法=释放责任令牌化到 _AcquireCtx（幂等 retire），_relay finally 与
响应 BackgroundTask 双保险，再加租约看门狗兜底任何未知路径（超 MAX_HOLD 强制
回收并打点 lease-timeout）。
观测：等待 >50ms 打一行 `[llm-queue]`；GET /__llmqueue/stats 看队列深度。

kill-switch 在 bok.py 侧：BOK_LLM_QUEUE_PROXY=0 时 mlx 直跑 :1235、本代理
不启动（拓扑回旧）。运行 python=仓库 venv（fastapi/httpx/uvicorn 与 CP 同源）。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

_UPSTREAM_BASE = os.environ.get("BOK_LLM_QUEUE_UPSTREAM", "http://127.0.0.1:1239").rstrip("/")
_CONCURRENCY = max(1, int(os.environ.get("BOK_LLM_QUEUE_CONCURRENCY", "1") or 1))
# 租约看门狗上限（秒）：单条流合法持有≈httpx read 600s+转发缓冲,900 给余量;
# 0=关（只剩 retire 双保险,无终极兜底）。
_MAX_HOLD_S = max(0.0, float(os.environ.get("BOK_LLM_QUEUE_MAX_HOLD", "900") or 900))
_SWEEP_INTERVAL_S = 30.0

# 惰性客户端（测试可用 ASGITransport 换掉指向桩上游）。
_CLIENT: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.AsyncClient(
            base_url=_UPSTREAM_BASE,
            timeout=httpx.Timeout(connect=5.0, read=600.0, write=60.0, pool=None),
        )
    return _CLIENT


class LaneGate:
    """优先级单飞门（纯逻辑可单测）：并发槽 + reply/bg 两条 FIFO 等待列。

    release 时 reply 列优先弹出（队头先到先服务）；槽空闲时 acquire 立即过。
    wait>50ms 的获取在调用方打点（区分车道与排队时长，供 llm-proxy.log 归因）。
    """

    def __init__(self, concurrency: int = 1) -> None:
        self._slots = max(1, int(concurrency))
        self._active = 0
        self._reply_waiters: deque[asyncio.Future] = deque()
        self._bg_waiters: deque[asyncio.Future] = deque()
        # 活跃租约（ctx→到期时刻）：release 责任令牌化的载体。看门狗扫它兜底
        # 任何未走正常 retire 路径的持有者（如 relay 未启动的断连泄漏）。
        self._leases: dict = {}
        self._stats = {"reply_served": 0, "bg_served": 0, "reply_waited_ms_total": 0, "bg_waited_ms_total": 0, "lease_timeouts": 0}

    def stats(self) -> dict:
        return {
            **self._stats,
            "active": self._active,
            "queued_reply": len(self._reply_waiters),
            "queued_bg": len(self._bg_waiters),
        }

    def _pop_next(self) -> asyncio.Future | None:
        if self._active >= self._slots:
            return None
        # 两列按优先级依次排空（2026-10-02 饿死根修）：旧版只挑一条列,reply 列
        # 残留已取消 future 时本轮不看你 bg 列——槽已空 bg 也不被唤醒,新请求又
        # 走快路径插队,bg 等待者可被无限期推迟。
        for waiters in (self._reply_waiters, self._bg_waiters):
            while waiters:
                fut = waiters.popleft()
                if not fut.done():
                    self._active += 1
                    return fut
        return None

    def _grant(self, ctx: "_AcquireCtx", hold_s: float) -> None:
        """登记租约（取槽成功时）。hold_s<=0 = 不设到期（看门狗关）。"""
        self._leases[ctx] = (time.monotonic() + hold_s) if hold_s > 0 else None

    def retire(self, ctx: "_AcquireCtx") -> bool:
        """归还 ctx 持有的槽（幂等）：仍在租约表=真归还并唤醒下一个等待者；
        已被 retire/看门狗回收=no-op。返回是否本次真归还。"""
        if self._leases.pop(ctx, None) is None:
            return False
        self._active = max(0, self._active - 1)
        nxt = self._pop_next()
        if nxt is not None:
            nxt.set_result(None)
        return True

    def sweep_leases(self, now: float | None = None) -> list:
        """看门狗扫描：回收超过租约到期仍持有的槽（正常路径早被 retire,走到
        这里的=泄漏）。返回被回收的 ctx 列表（打点用）。"""
        now = time.monotonic() if now is None else now
        expired = [ctx for ctx, dl in self._leases.items() if dl is not None and now >= dl]
        for ctx in expired:
            if self.retire(ctx):
                self._stats["lease_timeouts"] += 1
        return expired

    def acquire(self, lane: str):
        """取槽（同步工厂返回 async CM;等待发生在 __aenter__）。lane=="reply" 优先。"""
        return _AcquireCtx(self, lane)

    def release(self) -> None:
        """兼容面（旧单测/手工驱动）：归还最早授出且未归还的租约。生产路径
        一律走 _AcquireCtx 的 retire（令牌化幂等）。"""
        for ctx in list(self._leases):
            if self.retire(ctx):
                return


class _AcquireCtx:
    def __init__(self, gate: "LaneGate", lane: str) -> None:
        self._gate = gate
        self._lane = lane
        self.waited = 0.0
        self._handed_off = False
        self._retired = False

    def handoff(self) -> None:
        """闸门所有权移交（2026-09-28 串行化修正）：释放点从「handler 返回」
        移到「流耗尽/断连」。旧版 handler 在 `async with` 内 return
        StreamingResponse——__aexit__ 在响应**头**发出时就放闸，decode 阶段
        背景生成与实时回复并行抢 GPU，串行化名存实亡（实测复现）。"""
        self._handed_off = True

    def retire(self) -> bool:
        """归还槽（幂等,多处调用安全）：正常路径=_relay finally;兜底=响应
        background;终极=租约看门狗。重复 retire 与被看门狗先收均为 no-op。"""
        if self._retired:
            return False
        self._retired = True
        return self._gate.retire(self)

    async def __aenter__(self) -> "_AcquireCtx":
        gate = self._gate
        t0 = time.monotonic()
        key = "reply" if self._lane == "reply" else "bg"
        if gate._active < gate._slots:
            gate._active += 1
            self.waited = 0.0
            gate._grant(self, _MAX_HOLD_S)
            gate._stats[f"{key}_served"] += 1
            return self
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        (gate._reply_waiters if self._lane == "reply" else gate._bg_waiters).append(fut)
        await fut
        self.waited = time.monotonic() - t0
        gate._grant(self, _MAX_HOLD_S)
        gate._stats[f"{key}_served"] += 1
        gate._stats[f"{key}_waited_ms_total"] += int(self.waited * 1000)
        return self

    async def __aexit__(self, *exc) -> None:
        if not self._handed_off:
            self.retire()


GATE = LaneGate(_CONCURRENCY)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """租约看门狗：任何未走正常 retire 的槽持有者（_relay 未启动的断连泄漏、
    未知路径）超过 MAX_HOLD 强制回收。retire 幂等,正常流不受影响。"""
    sweep_task: asyncio.Task | None = None
    if _MAX_HOLD_S > 0:

        async def _sweep():
            while True:
                await asyncio.sleep(_SWEEP_INTERVAL_S)
                expired = GATE.sweep_leases()
                for _ctx in expired:
                    print(
                        f"[llm-queue] lease-timeout forced-reclaim hold_s>{_MAX_HOLD_S:.0f} "
                        f"active={GATE._active} queued(reply={len(GATE._reply_waiters)},bg={len(GATE._bg_waiters)})",
                        flush=True,
                    )

        sweep_task = asyncio.create_task(_sweep())
    try:
        yield
    finally:
        if sweep_task is not None:
            sweep_task.cancel()


app = FastAPI(
    title="bok-llm-queue",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_lifespan,
)

_GENERATION_PATHS = {"/v1/chat/completions", "/v1/completions", "/v1/responses"}


@app.get("/__llmqueue/stats")
async def _stats() -> dict:
    return {"upstream": _UPSTREAM_BASE, "concurrency": _CONCURRENCY, **GATE.stats()}


@app.api_route("/v1/chat/completions", methods=["POST"])
@app.api_route("/v1/completions", methods=["POST"])
@app.api_route("/v1/responses", methods=["POST"])
async def _generate(request: Request):
    """生成类请求：过优先级门后流式转发（门持满整条流——这正是串行化的点）。"""
    # 早断短路（2026-10-02）：排队前探一次断连——客户端已消失（打断/挂断掐断
    # httpx）就不占槽不烧上游。此时尚未进入流式响应,与 starlette 的断连监听
    # 无 receive 竞争,安全。
    # 先读全 body **再**探断连(2026-10-02 实机雷修复):request.is_disconnected
    # 在取消 scope 里试收一条 receive 消息——uvicorn 下若 body 首 chunk 已就绪
    # 会被它偷吃,后续 request.body() 等不到完整流=整条请求挂死(ASGITransport
    # 测试对这一语义结构性失明,第七波同款陷阱;实机 curl 3 分钟超时抓出)。
    # body 读完后再探:通道里只剩 disconnect/尾部消息,偷吃无害。
    body = await request.body()
    if await request.is_disconnected():
        return JSONResponse({"detail": "client disconnected"}, status_code=499)
    lane = "reply" if (request.headers.get("x-bok-lane") or "").strip() == "reply" else "bg"
    async with _AcquireCtx(GATE, lane) as acq:
        # W-GATE 观测(2026-09-27):每条生成请求过闸即打一行(含零等待快路径)——
        # TTFT 分解要从日志面归因到「排队多少毫秒」,仅 >50ms 的旧行看不到快路
        # 占比。一行一 print,不改闸语义(单并发+reply 插队照旧)。
        print(f"GATE lane={lane} waited_ms={acq.waited * 1000:.0f} active={GATE._active}", flush=True)
        if acq.waited > 0.05:
            print(
                f"[llm-queue] lane={lane} waited={acq.waited * 1000:.0f}ms "
                f"queued(reply={len(GATE._reply_waiters)},bg={len(GATE._bg_waiters)})",
                flush=True,
            )
        # 排队后断连复检（2026-10-02 orch2-D）：上面的早断短路只接得住**排队前**
        # 已断的客户端；排队窗口内断连（打断/挂断掐链路是常态）此前无人复检——
        # 拿到槽后照样打上游，为一个幽灵烧满一次 mlx 生成槽（单并发下整条回复链
        # 白等一整轮 decode）。body 已在过闸前读完（与早断短路同一安全前提：通道
        # 里只剩 disconnect/尾部消息，is_disconnected 取消 scope 的试收不会偷吃
        # 正文）。早退经 async with 的 __aexit__ 正常归还槽（尚未 handoff，与
        # _relay/BackgroundTask/看门狗的多头 retire 幂等无冲突）。
        if await request.is_disconnected():
            print(
                f"queue_proxy drop disconnected lane={lane} waited_ms={acq.waited * 1000:.0f}",
                flush=True,
            )
            return JSONResponse({"detail": "client disconnected"}, status_code=499)
        fwd_headers = {
            "content-type": request.headers.get("content-type", "application/json"),
            # 车道透传（观测/测试用：桩上游可记录每请求的车道归属）
            "x-bok-lane": lane,
            # req-id 透传(2026-10-02 审计修):W-ABORT 的注册头——旧版只转
            # content-type/lane 令本代理路径上**一切**请求的 abort 注册失效,
            # 客户端 POST /v1/abort 变静默 no-op(judge/mining/speculator/默认
            # a_reply 全中;/v1/abort 本身走 catch-all 直通不受影响,但请求
            # 从未登记)。缺头照旧省略(非 mlx 上游/无 abort 语义)。
            "x-bok-req-id": request.headers.get("x-bok-req-id", ""),
        }
        fwd_headers = {k: v for k, v in fwd_headers.items() if v}
        req = _client().build_request("POST", request.url.path, content=body, headers=fwd_headers)
        upstream = await _client().send(req, stream=True)
        acq.handoff()  # 释放点移交：流耗尽/断连时在 _relay finally 归还

        async def _relay():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                # 先放闸再断连：下一个请求可立刻起跑；aclose 令上游侧
                # （mlx_lm 检测断连中止解码）尽快回收 GPU。retire 幂等,与
                # background/看门狗多头调用安全。
                acq.retire()
                await upstream.aclose()

        return StreamingResponse(
            _relay(),
            status_code=upstream.status_code,
            headers={
                "content-type": upstream.headers.get("content-type", "application/json"),
                "x-bok-queue-waited-ms": f"{acq.waited * 1000:.0f}",
            },
            # 兜底释放第二道（2026-10-02 槽泄漏根修）：响应完成后由 starlette
            # 调用;正常路径 _relay 已 retire,此处 no-op。断连取消路径 background
            # 不保证执行——终极兜底是 startup 看门狗。
            background=BackgroundTask(acq.retire),
        )


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def _passthrough(path: str, request: Request):
    """非生成端点（/v1/models、/health…）直通——探活/发现零排队。"""
    body = await request.body()
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() in ("content-type", "authorization", "accept")}
    req = _client().build_request(request.method, request.url.path, content=body or None, headers=fwd_headers)
    upstream = await _client().send(req, stream=True)
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers={"content-type": upstream.headers.get("content-type", "application/json")},
        background=BackgroundTask(upstream.aclose),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("BOK_LLM_QUEUE_HOST", "127.0.0.1"),
        port=int(os.environ.get("BOK_LLM_QUEUE_PORT", "1235") or 1235),
        log_level=os.environ.get("BOK_LLM_QUEUE_LOG_LEVEL", "warning"),
    )
