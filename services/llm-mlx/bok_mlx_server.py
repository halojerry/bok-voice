#!/usr/bin/env python
"""bok mlx_lm server wrapper —— 按请求身份的生成中止（abort）支持（2026-10-01）。

背景（实测定案）：mlx_lm 0.31.3 server 是单生成线程（``ResponseGenerator._generate``
从 Queue 串行拉请求），站点内建取消只有 ``ctx._should_stop`` 一条，且只有 handler
写响应失败（客户端断连）才会触发；HTTP 头的 200 已经发出、断连又被 TCP 缓冲吞掉的
窗口里，被弃请求照解码到底不放槽（直打 :1237 实测：长请求断连后立刻发新请求，
新请求 TTFT=2334ms）。生产放大形态=用户打断轮 call-231aa92a TTFT 35.6s 级联。

本 wrapper 不改 site-packages（该目录里有别的 bok 就地补丁，不叠加），只做最小
monkey-patch，跑起 mlx server 原样：

  1. 请求身份：客户端在 OpenAI 兼容请求头带 ``X-Bok-Req-Id``（MLX handler 不读
     未知头，无字节面影响）；``APIHandler.do_POST`` 记在 handler 上，
     ``handle_completion`` 把 id 盖到 ``CompletionRequest`` 上。
  2. 登记：``ResponseGenerator.generate``（handler 线程）进 Queue 前登记
     req_id → threading.Event；ctx 出来后挂弱引用——abort 到达即 Event.set()
     + ``ctx.stop()``（批处理路径经 ``_should_stop`` 逐 token 生效）。
  3. 单发路径：包 ``mlx_server.stream_generate``（模块全局名，``_serve_single``
     经全局查表调用）——每 yield 前查 Event、prefill 期的
     ``prompt_progress_callback`` 也查，置位即抛 ``_BokAbortError`` 让
     ``_serve_single`` 立刻返回队列循环。
  4. abort 端点：``POST /v1/abort`` body ``{"request_id": ...}``。同 server 加
     路由（do_POST 最前端拦截）；先于登记到达的 abort 记 pending，登记时补齐。
     置位同时向该请求的 response_queue 补一个流结束哨兵（None）——批处理路径
     ``_should_stop`` 只把序列移出批、不发 rqueue，handler 阻塞在 get() 上会
     永久挂死（线程/连接泄漏）；哨兵让它干净收尾。
  5. ``/v1/models`` 求实（2026-10-03 I1e）：上游实现扫的是 **HF 缓存目录**
     （列出所有下载过的 mlx 模型、顺序随扫描浮动）——四口同组三模型轮转,检测/
     选型面被误导。接管回「本进程 ``--model``」单条目,如实。

kill-switch：``BOK_MLX_ABORT=0`` 时零 patch（逐字节旧行为）。客户端侧不发 abort、
不带 req_id 头时本 wrapper 全路径 pass-through（零漂移）。

入口（bok.py 三处 mlx 启动点改走这里；argv 原样透传给 mlx server）::

    python services/llm-mlx/bok_mlx_server.py --model <abs path> --port 1239 ...
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
import weakref

# mlx_lm 惰性导入（测试可在无 mlx_lm 的 venv 里注入 fake 模块驱动 patch 逻辑；
# 真跑时 main() 从 site-packages 取）。wrapper 只 patch 这个命名空间，argv 照旧。
_MLX_SERVER_MODULE = None


def load_mlx_server(module=None):
    """取 mlx_lm.server 模块（可注入替身；真跑=site-packages 真模块）。"""
    global _MLX_SERVER_MODULE
    if module is not None:
        _MLX_SERVER_MODULE = module
        return module
    if _MLX_SERVER_MODULE is None:
        from mlx_lm import server as _server

        _MLX_SERVER_MODULE = _server
    return _MLX_SERVER_MODULE


REQ_ID_HEADER = "X-Bok-Req-Id"
ABORT_PATH = "/v1/abort"

ABORT_ENV = "BOK_MLX_ABORT"

# 登记表 TTL/上限：条目只装 Event+弱引用 ctx，开销极小；TTL 盖住 9B 长生成
# （600tok @ ~10tps = 60s 级）后迟到的 abort 也只是空操作。
_ENTRY_TTL_S = 900.0
_MAX_ENTRIES = 8192
# pending（abort 先于登记到达）保留窗：客户端 aclose 与请求真正发出可差数秒。
_PENDING_TTL_S = 120.0
_MAX_PENDING = 2048


class _BokAbortError(Exception):
    """单发路径中断信号：由 stream_generate wrapper 在 Event 置位时抛出。"""


class _BokTrackedQueue(queue.Queue):
    """登记构造实例的 Queue（generate() 的 response_queue）。

    abort 要唤醒已弃请求的 handler 线程：批处理路径 ``_should_stop`` 只会把序列
    移出批（``batch_results.pop``），不向 rqueue 发任何东西——handler 若正好阻塞
    在 ``response_queue.get()`` 上就永远醒不来（线程/连接泄漏，客户端挂死）。
    abort 时向该队列补一个 None（=流结束哨兵）让 handler 干净收尾。
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        _LAST_RESP_QUEUE.q = self


_LAST_RESP_QUEUE = threading.local()


class _AbortEntry:
    __slots__ = ("req_id", "event", "ctx_ref", "resp_queue", "ctx_attached", "created")

    def __init__(self, req_id: str) -> None:
        self.req_id = req_id
        self.event = threading.Event()
        self.ctx_ref: "weakref.ReferenceType | None" = None
        self.resp_queue: "queue.Queue | None" = None
        self.ctx_attached = False
        self.created = time.monotonic()

    @property
    def aborted(self) -> bool:
        return self.event.is_set()


class _AbortRegistry:
    """req_id → Event 登记表（锁保护；生成线程/HTTP 线程双边界）。"""

    def __init__(
        self,
        ttl: float = _ENTRY_TTL_S,
        max_entries: int = _MAX_ENTRIES,
        pending_ttl: float = _PENDING_TTL_S,
        max_pending: int = _MAX_PENDING,
    ) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _AbortEntry] = {}
        self._pending: dict[str, float] = {}
        self._ttl = ttl
        self._max_entries = max_entries
        self._pending_ttl = pending_ttl
        self._max_pending = max_pending

    # ---- 内部（调用方已持锁） ----

    def _prune_locked(self, now: float) -> None:
        # TTL 扫一遍（条目 dict 有上限，量级万级内，逐请求 O(n) 可忽略）。
        for rid, ent in list(self._entries.items()):
            if now - ent.created > self._ttl:
                self._entries.pop(rid, None)
        # 仍超上限：按创建时间丢最旧一批（保底防无界增长）。
        if len(self._entries) > self._max_entries:
            for rid, _ in sorted(self._entries.items(), key=lambda kv: kv[1].created)[
                : len(self._entries) - self._max_entries
            ]:
                self._entries.pop(rid, None)
        if self._pending:
            for rid, ts in list(self._pending.items()):
                if now - ts > self._pending_ttl:
                    self._pending.pop(rid, None)
            if len(self._pending) > self._max_pending:
                for rid, _ in sorted(self._pending.items(), key=lambda kv: kv[1])[
                    : len(self._pending) - self._max_pending
                ]:
                    self._pending.pop(rid, None)

    # ---- 公开面 ----

    def register(self, req_id: str) -> _AbortEntry:
        with self._lock:
            now = time.monotonic()
            ent = self._entries.get(req_id)
            if ent is not None and now - ent.created <= self._ttl:
                self._prune_locked(now)
                return ent
            ent = _AbortEntry(req_id)
            if req_id in self._pending:
                # abort 先于登记到达：登记即视为已中止（消 ordering 竞态）。
                self._pending.pop(req_id, None)
                ent.event.set()
            self._entries[req_id] = ent
            self._prune_locked(now)
            return ent

    def get(self, req_id: str) -> "_AbortEntry | None":
        with self._lock:
            ent = self._entries.get(req_id)
            if ent is None:
                return None
            if time.monotonic() - ent.created > self._ttl:
                self._entries.pop(req_id, None)
                return None
            return ent

    def attach_ctx(self, entry: _AbortEntry, ctx, resp_queue=None) -> None:
        """把生成 ctx 挂到登记条目（批处理路径靠 ctx._should_stop 生效）。

        resp_queue=该请求的 response_queue（_BokTrackedQueue 登记）；若 abort
        已先到，这里补发唤醒哨兵（见 _BokTrackedQueue 注释）。"""
        wake = None
        with self._lock:
            try:
                entry.ctx_ref = weakref.ref(ctx)
            except TypeError:  # pragma: no cover - 理论上 dataclass 恒可弱引用
                entry.ctx_ref = None
            entry.ctx_attached = True
            if resp_queue is not None and entry.resp_queue is None:
                entry.resp_queue = resp_queue
            already = entry.event.is_set()
            if already and entry.resp_queue is not None:
                wake, entry.resp_queue = entry.resp_queue, None
        if already:
            _stop_ctx(ctx)
            _wake_handler(wake)

    def abort(self, req_id: str) -> str:
        """置位中止。返回 signalled（请求在登记表中）/ pending（先到记待）。"""
        with self._lock:
            now = time.monotonic()
            self._prune_locked(now)
            ent = self._entries.get(req_id)
            wake = None
            ctx = None
            if ent is not None:
                ent.event.set()
                if ent.ctx_ref is not None:
                    ctx = ent.ctx_ref()
                if ent.ctx_attached and ent.resp_queue is not None:
                    wake, ent.resp_queue = ent.resp_queue, None
            else:
                self._pending[req_id] = now
        if ctx is not None:
            _stop_ctx(ctx)
        _wake_handler(wake)
        return "signalled" if ent is not None else "pending"


def _stop_ctx(ctx) -> None:
    """尽力置 ctx._should_stop（批处理路径逐 token 检查；异常全吞）。"""
    try:
        ctx.stop()
    except Exception:  # noqa: BLE001 - 中止是尽力语义，绝不向调用方炸
        try:
            ctx._should_stop = True
        except Exception:  # noqa: BLE001
            pass


def _wake_handler(resp_queue) -> None:
    """向被弃请求的 response_queue 补流结束哨兵（None），唤醒阻塞的 handler。"""
    if resp_queue is None:
        return
    try:
        resp_queue.put(None)
    except Exception:  # noqa: BLE001 - 唤醒是尽力语义
        pass


_REGISTRY = _AbortRegistry()

# 生成线程本地：当前正在服务的请求登记条目。单发路径经 _serve_single 设置，
# stream_generate wrapper 读取（同一线程，无线程安全问题）。
_CURRENT_ENTRY = threading.local()

_ABORT_LOG_TAG = "[bok-abort]"


def _log(msg: str) -> None:
    print(f"{_ABORT_LOG_TAG} {msg}", flush=True)


def _json_response(handler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode()
    try:
        handler.send_response(status)
        handler.send_header("Content-type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        handler.wfile.flush()
    except Exception:  # noqa: BLE001 - 客户端已断，响应写不出就算了
        pass


def _handle_abort(handler) -> None:
    req_id = ""
    try:
        n = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(n) if n > 0 else b""
        payload = json.loads(raw.decode() or "{}")
        if isinstance(payload, dict):
            req_id = str(payload.get("request_id") or "").strip()
    except Exception as exc:  # noqa: BLE001 - 坏 body 不炸 server
        _json_response(handler, 400, {"error": f"invalid body: {exc}"})
        return
    if not req_id:
        _json_response(handler, 400, {"error": "request_id required"})
        return
    state = _REGISTRY.abort(req_id)
    _log(f"request_id={req_id} state={state}")
    _json_response(handler, 200, {"aborted": True, "state": state, "request_id": req_id})


def _loaded_model_id() -> str:
    """本进程 mlx server 实际加载的模型 id（argv ``--model``;``--model=x`` 两式都认）。"""
    argv = list(sys.argv)
    for i, a in enumerate(argv):
        if a == "--model" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--model="):
            return a.split("=", 1)[1]
    return ""


def _handle_models(handler) -> None:
    """``GET /v1/models`` 接管：单条目=本实例 ``--model``（非上游的 HF 缓存扫描）。"""
    model = _loaded_model_id()
    _json_response(handler, 200, {
        "object": "list",
        "data": [{"id": model or "unknown", "object": "model", "created": int(time.time())}],
    })


_INSTALLED = {"module": None, "done": False}


def install(mlx_server_module=None) -> None:
    """装入全部 patch（同模块幂等；换模块=重装，测试可注入替身）。"""
    srv = load_mlx_server(mlx_server_module)
    if _INSTALLED["done"] and _INSTALLED["module"] is srv:
        return
    _INSTALLED["module"] = srv
    _INSTALLED["done"] = True

    _orig_do_post = srv.APIHandler.do_POST

    def _do_POST(self):  # noqa: N802 - 覆写 BaseHTTPRequestHandler 协议方法
        if self.path == ABORT_PATH:
            _handle_abort(self)
            return
        req_id = ""
        try:
            req_id = (self.headers.get(REQ_ID_HEADER) or "").strip()
        except Exception:  # noqa: BLE001 - 头读不出=无身份，走旧路径
            req_id = ""
        self._bok_req_id = req_id or None
        return _orig_do_post(self)

    srv.APIHandler.do_POST = _do_POST

    # do_GET 接管（I1e）：替身模块可能没有 do_GET（测试 fake）——取不到即跳过
    # 本层；真 mlx server 恒有（BaseHTTPRequestHandler 子类）。
    _orig_do_get = getattr(srv.APIHandler, "do_GET", None)

    if callable(_orig_do_get):

        def _do_GET(self):  # noqa: N802 - 覆写 BaseHTTPRequestHandler 协议方法
            if self.path.startswith("/v1/models"):
                _handle_models(self)
                return
            return _orig_do_get(self)

        srv.APIHandler.do_GET = _do_GET

    _orig_handle_completion = srv.APIHandler.handle_completion

    def _handle_completion(self, request, stop_words):
        rid = getattr(self, "_bok_req_id", None)
        if rid:
            try:
                request._bok_req_id = rid
            except Exception:  # noqa: BLE001 - 盖不上就退化为无身份
                pass
        return _orig_handle_completion(self, request, stop_words)

    srv.APIHandler.handle_completion = _handle_completion

    _orig_generate = srv.ResponseGenerator.generate

    # response_queue 身份追踪（handler 线程本地）：abort 需要它来唤醒被弃请求的
    # handler（批处理路径移除序列不发 rqueue，见 _BokTrackedQueue）。
    srv.Queue = _BokTrackedQueue

    def _generate(self, request, generation_args, progress_callback=None):
        rid = getattr(request, "_bok_req_id", None)
        entry = _REGISTRY.register(rid) if rid else None
        try:
            ctx, stream = _orig_generate(self, request, generation_args, progress_callback)
        except BaseException:
            # 登记条目留给 TTL 自然过期（abort 到达=空操作，无害）。
            raise
        if entry is not None:
            _REGISTRY.attach_ctx(
                entry, ctx, resp_queue=getattr(_LAST_RESP_QUEUE, "q", None)
            )
        return ctx, stream

    srv.ResponseGenerator.generate = _generate

    _orig_serve_single = srv.ResponseGenerator._serve_single

    def _serve_single(self, request):
        entry = None
        try:
            req = request[1]
            rid = getattr(req, "_bok_req_id", None)
            entry = _REGISTRY.get(rid) if rid else None
        except Exception:  # noqa: BLE001 - 取不到=无身份直通
            entry = None
        _CURRENT_ENTRY.entry = entry
        try:
            return _orig_serve_single(self, request)
        finally:
            _CURRENT_ENTRY.entry = None

    srv.ResponseGenerator._serve_single = _serve_single

    _orig_stream_generate = srv.stream_generate

    def _stream_generate(*args, **kwargs):
        entry = getattr(_CURRENT_ENTRY, "entry", None)
        if entry is None:
            # 无身份（未带 X-Bok-Req-Id / kill-switch）：逐字节旧路径。
            return _orig_stream_generate(*args, **kwargs)

        progress = kwargs.get("prompt_progress_callback")

        def _guard_progress(processed, total):
            if entry.aborted:
                raise _BokAbortError(f"aborted request_id={entry.req_id}")
            if progress is not None:
                progress(processed, total)

        kwargs = dict(kwargs)
        kwargs["prompt_progress_callback"] = _guard_progress

        def _gen():
            inner = _orig_stream_generate(*args, **kwargs)
            try:
                for item in inner:
                    if entry.aborted:
                        _log(f"stopped request_id={entry.req_id} path=single")
                        raise _BokAbortError(f"aborted request_id={entry.req_id}")
                    yield item
            finally:
                close = getattr(inner, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:  # noqa: BLE001 - 收尾尽力
                        pass

        return _gen()

    srv.stream_generate = _stream_generate


def _abort_enabled() -> bool:
    return os.environ.get(ABORT_ENV, "1") == "1"


def main() -> None:
    srv = load_mlx_server()
    if _abort_enabled():
        install(srv)
        _log(f"enabled header={REQ_ID_HEADER} endpoint={ABORT_PATH} pid={os.getpid()}")
    else:
        logging.info("%s disabled (%s=0)", _ABORT_LOG_TAG, ABORT_ENV)
    srv.main()


if __name__ == "__main__":
    if __package__:  # pragma: no cover - 只作脚本入口，不支持包内导入执行
        raise SystemExit("run as a script: python bok_mlx_server.py --model ...")
    main()
