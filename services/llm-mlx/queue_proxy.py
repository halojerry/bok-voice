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
BOK_LLM_QUEUE_CONCURRENCY（默认 1）、BOK_LLM_QUEUE_HOST/PORT（127.0.0.1:1235）。
观测：等待 >50ms 打一行 `[llm-queue]`；GET /__llmqueue/stats 看队列深度。

kill-switch 在 bok.py 侧：BOK_LLM_QUEUE_PROXY=0 时 mlx 直跑 :1235、本代理
不启动（拓扑回旧）。运行 python=仓库 venv（fastapi/httpx/uvicorn 与 CP 同源）。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

_UPSTREAM_BASE = os.environ.get("BOK_LLM_QUEUE_UPSTREAM", "http://127.0.0.1:1239").rstrip("/")
_CONCURRENCY = max(1, int(os.environ.get("BOK_LLM_QUEUE_CONCURRENCY", "1") or 1))

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
        self._stats = {"reply_served": 0, "bg_served": 0, "reply_waited_ms_total": 0, "bg_waited_ms_total": 0}

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
        waiters = self._reply_waiters if self._reply_waiters else self._bg_waiters
        while waiters:
            fut = waiters.popleft()
            if not fut.done():
                self._active += 1
                return fut
        return None

    def acquire(self, lane: str):
        """取槽（同步工厂返回 async CM;等待发生在 __aenter__）。lane=="reply" 优先。"""
        return _AcquireCtx(self, lane)

    def release(self) -> None:
        self._active = max(0, self._active - 1)
        nxt = self._pop_next()
        if nxt is not None:
            nxt.set_result(None)


class _AcquireCtx:
    def __init__(self, gate: "LaneGate", lane: str) -> None:
        self._gate = gate
        self._lane = lane
        self.waited = 0.0
        self._handed_off = False

    def handoff(self) -> None:
        """闸门所有权移交（2026-09-28 串行化修正）：释放点从「handler 返回」
        移到「流耗尽/断连」。旧版 handler 在 `async with` 内 return
        StreamingResponse——__aexit__ 在响应**头**发出时就放闸，decode 阶段
        背景生成与实时回复并行抢 GPU，串行化名存实亡（实测复现）。"""
        self._handed_off = True

    async def __aenter__(self) -> "_AcquireCtx":
        gate = self._gate
        t0 = time.monotonic()
        key = "reply" if self._lane == "reply" else "bg"
        if gate._active < gate._slots:
            gate._active += 1
            self.waited = 0.0
            gate._stats[f"{key}_served"] += 1
            return self
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        (gate._reply_waiters if self._lane == "reply" else gate._bg_waiters).append(fut)
        await fut
        self.waited = time.monotonic() - t0
        gate._stats[f"{key}_served"] += 1
        gate._stats[f"{key}_waited_ms_total"] += int(self.waited * 1000)
        return self

    async def __aexit__(self, *exc) -> None:
        if not self._handed_off:
            self._gate.release()


GATE = LaneGate(_CONCURRENCY)
app = FastAPI(title="bok-llm-queue", docs_url=None, redoc_url=None, openapi_url=None)

_GENERATION_PATHS = {"/v1/chat/completions", "/v1/completions", "/v1/responses"}


@app.get("/__llmqueue/stats")
async def _stats() -> dict:
    return {"upstream": _UPSTREAM_BASE, "concurrency": _CONCURRENCY, **GATE.stats()}


@app.api_route("/v1/chat/completions", methods=["POST"])
@app.api_route("/v1/completions", methods=["POST"])
@app.api_route("/v1/responses", methods=["POST"])
async def _generate(request: Request):
    """生成类请求：过优先级门后流式转发（门持满整条流——这正是串行化的点）。"""
    lane = "reply" if (request.headers.get("x-bok-lane") or "").strip() == "reply" else "bg"
    body = await request.body()
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
        fwd_headers = {
            "content-type": request.headers.get("content-type", "application/json"),
            # 车道透传（观测/测试用：桩上游可记录每请求的车道归属）
            "x-bok-lane": lane,
        }
        req = _client().build_request("POST", request.url.path, content=body, headers=fwd_headers)
        upstream = await _client().send(req, stream=True)
        acq.handoff()  # 释放点移交：流耗尽/断连时在 _relay finally 放闸

        async def _relay():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                # 先放闸再断连：下一个请求可立刻起跑；aclose 令上游侧
                # （mlx_lm 检测断连中止解码）尽快回收 GPU。
                GATE.release()
                await upstream.aclose()

        return StreamingResponse(
            _relay(),
            status_code=upstream.status_code,
            headers={
                "content-type": upstream.headers.get("content-type", "application/json"),
                "x-bok-queue-waited-ms": f"{acq.waited * 1000:.0f}",
            },
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
