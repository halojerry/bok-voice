"""mlx 生成中止（abort，2026-10-01 W-ABORT）测试。

服务端 wrapper ``services/llm-mlx/bok_mlx_server.py``：mlx_lm 0.31.3 server 单
生成线程零取消路径（被弃请求照解码到底），wrapper 按 ``X-Bok-Req-Id`` 登记
threading.Event、``POST /v1/abort`` 置位、生成循环逐 token 查旗退出。本测用
**fake mlx_lm.server 模块**（无需真模型/mlx_lm 依赖）驱动 patch 逻辑，另加
bok.py/livekit_plugins 源级 pin。
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import sys
import types
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[1]
WRAPPER_PATH = ROOT / "services" / "llm-mlx" / "bok_mlx_server.py"

sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.providers import livekit_plugins as lp  # noqa: E402


# ---------------------------------------------------------------------------
# fake mlx_lm.server —— 只含 wrapper 触碰的四个名字（APIHandler/ResponseGenerator/
# stream_generate/main）。fake 的 _serve_single 与真源码同姿势：从「模块命名空间」
# 取 stream_generate（wrapper 替换的正是这个属性）。
# ---------------------------------------------------------------------------

class _FakeCtx:
    def __init__(self):
        self._should_stop = False
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1
        self._should_stop = True


class _FakeStream:
    pass


def _make_fake_mlx_server() -> types.ModuleType:
    mod = types.ModuleType("fake_mlx_server")
    events: dict = {}

    class FakeAPIHandler:
        def __init__(self, headers=None, path="/v1/chat/completions", body=b""):
            self.headers = headers or {}
            self.path = path
            self.rfile = io.BytesIO(body)
            self.wfile = io.BytesIO()
            self.status = None
            self.sent_headers: list[tuple[str, str]] = []
            self.completion_request = None
            self.abort_handled = False
            self.get_delegated = False

        # 真源码 APIHandler 的响应原语（_json_response 用）
        def send_response(self, status):
            self.status = status

        def send_header(self, k, v):
            self.sent_headers.append((k, v))

        def end_headers(self):
            pass

        def do_POST(self):
            if self.path == "/v1/chat/completions":
                req = types.SimpleNamespace()
                self.handle_completion(req, [])
                self.completion_request = req
            elif self.path == "/v1/abort":
                self.abort_handled = True

        def do_GET(self):
            # 上游原版 do_GET：wrapper 的 /v1/models 接管以外路径走这里
            self.get_delegated = True

        def handle_completion(self, request, stop_words):
            self.completion_request = request

    class FakeResponseGenerator:
        def __init__(self):
            self.ctx = None
            self.stream = None

        def generate(self, request, generation_args, progress_callback=None):
            self.ctx = _FakeCtx()
            self.stream = _FakeStream()
            return self.ctx, self.stream

        def _serve_single(self, request):
            # 真源码同构：rqueue, request, args = request；异常落 rqueue（此处记
            # 在 self.error）。stream_generate 经模块命名空间查表（=被 patch 的
            # 那个属性），与真 _serve_single 的全局名解析等价。
            _rqueue, req, _args = request
            self.iterated = []
            self.error = None
            self.seen_kwargs = None
            try:
                out = mod.stream_generate(model=None, tokenizer=None, prompt=[1])
                self.seen_kwargs = getattr(out, "_bok_seen_kwargs", None)
                for item in out:
                    self.iterated.append(item)
            except Exception as exc:  # noqa: BLE001 - 镜像真源码包异常进 rqueue
                self.error = exc

    def fake_main():
        events["main_called"] = True

    mod.APIHandler = FakeAPIHandler
    mod.ResponseGenerator = FakeResponseGenerator
    mod.events = events

    def fake_stream_generate(**kwargs):
        events["seen_kwargs"] = kwargs
        yield 0

    mod.stream_generate = fake_stream_generate
    mod.main = fake_main
    return mod


@pytest.fixture()
def wrapper():
    spec = importlib.util.spec_from_file_location("bok_mlx_server_ut", WRAPPER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._REGISTRY = mod._AbortRegistry()
    return mod


# ---------------------------------------------------------------------------
# 登记表 / abort 端点
# ---------------------------------------------------------------------------

def test_registry_register_abort_stop_ctx(wrapper):
    reg = wrapper._REGISTRY
    entry = reg.register("r1")
    assert entry.aborted is False
    ctx = _FakeCtx()
    reg.attach_ctx(entry, ctx)
    assert reg.abort("r1") == "signalled"
    assert entry.aborted is True
    assert ctx._should_stop is True and ctx.stop_calls == 1
    # 幂等：再 abort 仍 signalled，ctx 再停一次（stop() 幂等由 store 语义保证）
    assert reg.abort("r1") == "signalled"


def test_registry_pending_abort_applied_at_register_and_attach(wrapper):
    reg = wrapper._REGISTRY
    assert reg.abort("late") == "pending"
    entry = reg.register("late")
    assert entry.aborted is True  # 先到 abort 在登记时补齐
    ctx = _FakeCtx()
    reg.attach_ctx(entry, ctx)
    assert ctx._should_stop is True


def test_registry_ttl_prune_keeps_memory_bounded(wrapper):
    reg = wrapper._AbortRegistry(ttl=60.0, max_entries=3)
    for rid in ("a", "b", "c", "d", "e"):
        reg.register(rid)  # 超上限即按最旧丢（保底防无界增长）
    assert len(reg._entries) <= 3
    assert reg.get("a") is None and reg.get("b") is None  # 最旧被 cap-prune
    assert reg.get("e") is not None and reg.get("d") is not None


def test_abort_http_endpoint_ok_and_400(wrapper, capsys):
    fake = _make_fake_mlx_server()
    wrapper.install(fake)
    wrapper._REGISTRY.register("r9")

    body = json.dumps({"request_id": "r9"}).encode()
    h = fake.APIHandler(headers={"Content-Length": str(len(body))}, path="/v1/abort", body=body)
    h.do_POST()
    assert h.status == 200
    payload = json.loads(h.wfile.getvalue().decode())
    assert payload["state"] == "signalled" and payload["request_id"] == "r9"
    assert wrapper._REGISTRY.get("r9").aborted is True
    assert "[bok-abort] request_id=r9 state=signalled" in capsys.readouterr().out

    h2 = fake.APIHandler(headers={"Content-Length": "2"}, path="/v1/abort", body=b"{}")
    h2.do_POST()
    assert h2.status == 400


def test_do_post_tags_request_with_header(wrapper):
    fake = _make_fake_mlx_server()
    wrapper.install(fake)
    h = fake.APIHandler(headers={"X-Bok-Req-Id": "abc123"})
    h.do_POST()
    assert h.completion_request._bok_req_id == "abc123"
    # 无头=不盖（零漂移）
    h2 = fake.APIHandler()
    h2.do_POST()
    assert getattr(h2.completion_request, "_bok_req_id", None) is None


def test_models_endpoint_returns_loaded_model(wrapper, monkeypatch):
    """I1e（2026-10-03）：/v1/models 接管为「本实例 --model」单条目。

    上游实现=HF 缓存目录扫描（四口同组三模型轮转,检测/选型面被误导）——
    本测钉接管后的如实单条目与非 models 路径的委托。"""
    fake = _make_fake_mlx_server()
    wrapper.install(fake)
    monkeypatch.setattr(sys, "argv", ["bok_mlx_server.py", "--model", "/tmp/mx/model-x", "--port", "1237"])
    h = fake.APIHandler(path="/v1/models")
    h.do_GET()
    assert h.status == 200
    payload = json.loads(h.wfile.getvalue().decode())
    assert [d["id"] for d in payload["data"]] == ["/tmp/mx/model-x"]
    # 非 models 路径照旧委托上游（/health 走原 do_GET）
    h2 = fake.APIHandler(path="/health")
    h2.do_GET()
    assert h2.get_delegated is True


def test_loaded_model_id_parses_both_forms(wrapper, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["x", "--model=/tmp/a/b"])
    assert wrapper._loaded_model_id() == "/tmp/a/b"
    monkeypatch.setattr(sys, "argv", ["x"])
    assert wrapper._loaded_model_id() == ""


def test_generate_registers_and_attach_ctx(wrapper):
    fake = _make_fake_mlx_server()
    wrapper.install(fake)
    gen = fake.ResponseGenerator()
    req = types.SimpleNamespace(_bok_req_id="g1")
    ctx, _stream = gen.generate(req, None)
    assert wrapper._REGISTRY.get("g1") is not None
    assert wrapper._REGISTRY.abort("g1") == "signalled"
    assert ctx._should_stop is True
    # 无身份请求不登记
    gen2 = fake.ResponseGenerator()
    gen2.generate(types.SimpleNamespace(), None)
    assert len(wrapper._REGISTRY._entries) == 1


# ---------------------------------------------------------------------------
# 单发路径：stream_generate 包装（逐 token 查旗 + prefill 进度守卫）
# ---------------------------------------------------------------------------

def test_single_path_aborts_mid_decode(wrapper):
    fake = _make_fake_mlx_server()
    total = 50
    consumed = {"n": 0, "kwargs": None}

    def fake_stream_generate(**kwargs):
        consumed["kwargs"] = kwargs
        for i in range(total):
            if i == 5:
                wrapper._REGISTRY.abort("d1")  # 生成中途 abort 到达
            consumed["n"] += 1
            yield i

    fake.stream_generate = fake_stream_generate
    wrapper.install(fake)
    wrapper._REGISTRY.register("d1")

    gen = fake.ResponseGenerator()
    gen._serve_single((None, types.SimpleNamespace(_bok_req_id="d1"), None))
    assert isinstance(gen.error, wrapper._BokAbortError)
    assert consumed["n"] == 6  # 内芯产到第 6 个（i=5 触发 abort），未 yield 出去
    assert gen.iterated == [0, 1, 2, 3, 4]
    assert wrapper._CURRENT_ENTRY.entry is None  # 线程本地清理


def test_single_path_prefill_progress_guard(wrapper):
    """prefill 期（首 token 之前）abort：prompt_progress_callback 即抛。"""
    fake = _make_fake_mlx_server()
    consumed = {"n": 0}

    def fake_stream_generate(**kwargs):
        wrapper._REGISTRY.abort("p1")  # prefill 期间 abort 到达
        cb = kwargs.get("prompt_progress_callback")
        assert cb is not None  # wrapper 恒注入守卫
        cb(1, 100)  # 守卫在此检查并抛
        consumed["n"] += 1
        yield 0

    fake.stream_generate = fake_stream_generate
    wrapper.install(fake)
    wrapper._REGISTRY.register("p1")
    gen = fake.ResponseGenerator()
    gen._serve_single((None, types.SimpleNamespace(_bok_req_id="p1"), None))
    assert isinstance(gen.error, wrapper._BokAbortError)
    assert consumed["n"] == 0
    assert gen.iterated == []


def test_single_path_progress_forwarded_when_not_aborted(wrapper):
    fake = _make_fake_mlx_server()
    seen = {"kwargs": None, "progress": []}

    def fake_stream_generate(**kwargs):
        seen["kwargs"] = kwargs
        kwargs["prompt_progress_callback"](3, 9)
        yield 1

    fake.stream_generate = fake_stream_generate
    wrapper.install(fake)
    wrapper._REGISTRY.register("ok1")
    gen = fake.ResponseGenerator()
    gen._serve_single((None, types.SimpleNamespace(_bok_req_id="ok1"), None))
    assert gen.error is None and gen.iterated == [1]
    assert callable(seen["kwargs"]["prompt_progress_callback"])


def test_no_req_id_is_byte_passthrough(wrapper):
    """无 X-Bok-Req-Id：stream_generate 原样（无守卫注入、无包装）。"""
    fake = _make_fake_mlx_server()
    calls = {"n": 0}

    def fake_stream_generate(**kwargs):
        calls["n"] += 1
        assert "prompt_progress_callback" not in kwargs
        yield from (1, 2, 3)

    fake.stream_generate = fake_stream_generate
    wrapper.install(fake)
    gen = fake.ResponseGenerator()
    gen._serve_single((None, types.SimpleNamespace(), None))
    assert gen.iterated == [1, 2, 3] and gen.error is None
    assert calls["n"] == 1


def test_install_idempotent_per_module(wrapper):
    fake = _make_fake_mlx_server()
    wrapper.install(fake)
    first = fake.APIHandler.do_POST
    wrapper.install(fake)
    assert fake.APIHandler.do_POST is first
    # 换模块=重装（测试注入路径）
    fake2 = _make_fake_mlx_server()
    wrapper.install(fake2)
    assert fake2.APIHandler.do_POST is not first


# ---------------------------------------------------------------------------
# response_queue 唤醒（批处理路径移除序列不发 rqueue，handler 会永久阻塞）
# ---------------------------------------------------------------------------

def test_abort_wakes_tracked_response_queue(wrapper):
    q = wrapper._BokTrackedQueue()
    entry = wrapper._REGISTRY.register("w1")
    wrapper._REGISTRY.attach_ctx(entry, _FakeCtx(), resp_queue=q)
    assert q.empty()  # 尚未 abort：无哨兵
    assert wrapper._REGISTRY.abort("w1") == "signalled"
    assert q.get_nowait() is None  # abort 补 None 唤醒 handler


def test_abort_before_attach_wakes_at_attach(wrapper):
    q = wrapper._BokTrackedQueue()
    assert wrapper._REGISTRY.abort("w2") == "pending"
    entry = wrapper._REGISTRY.register("w2")
    ctx = _FakeCtx()
    wrapper._REGISTRY.attach_ctx(entry, ctx, resp_queue=q)
    assert ctx._should_stop is True
    assert q.get_nowait() is None


def test_install_tracks_generate_response_queue(wrapper):
    fake = _make_fake_mlx_server()
    wrapper.install(fake)
    assert fake.Queue is wrapper._BokTrackedQueue
    fake.Queue()  # 模拟 generate() 在 handler 线程构造 response_queue
    gen = fake.ResponseGenerator()
    req = types.SimpleNamespace(_bok_req_id="q1")
    _ctx, _stream = gen.generate(req, None)
    entry = wrapper._REGISTRY.get("q1")
    assert entry.resp_queue is not None
    wrapper._REGISTRY.abort("q1")
    assert entry.resp_queue is None  # 哨兵已发（队列引用清掉防滞留）


# ---------------------------------------------------------------------------
# 队列代理透传（:1235 前置代理 → :1239 wrapper）：a_reply 车道的 abort 走这条
# ---------------------------------------------------------------------------

def test_queue_proxy_passthrough_forwards_abort():
    """POST /v1/abort 不在生成路径 → 走代理 catch-all 直通（不过 LaneGate）。"""
    spec = importlib.util.spec_from_file_location(
        "bok_llm_queue_proxy_abort_ut", ROOT / "services" / "llm-mlx" / "queue_proxy.py"
    )
    qp = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("bok_llm_queue_proxy_abort_ut", qp)
    spec.loader.exec_module(qp)

    seen: dict = {}
    stub = FastAPI()

    @stub.post("/v1/abort")
    async def _abort(request: Request):
        seen["body"] = await request.json()
        return JSONResponse({"aborted": True, "state": "signalled"})

    qp._CLIENT = httpx.AsyncClient(transport=httpx.ASGITransport(app=stub), base_url="http://stub")
    try:
        async def scenario():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=qp.app), base_url="http://px"
            ) as c:
                r = await c.post("/v1/abort", json={"request_id": "abc"})
                return r.status_code, r.json()

        code, payload = asyncio.run(scenario())
    finally:
        qp._CLIENT = None
    assert code == 200 and payload["state"] == "signalled"
    assert seen["body"] == {"request_id": "abc"}


# ---------------------------------------------------------------------------
# kill-switch
# ---------------------------------------------------------------------------

def test_abort_env_default_on_and_off(monkeypatch):
    monkeypatch.delenv("BOK_MLX_ABORT", raising=False)
    spec = importlib.util.spec_from_file_location("bok_mlx_server_env_ut", WRAPPER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._abort_enabled() is True
    monkeypatch.setenv("BOK_MLX_ABORT", "0")
    assert mod._abort_enabled() is False


def test_main_disabled_no_patch(monkeypatch):
    spec = importlib.util.spec_from_file_location("bok_mlx_server_main_ut", WRAPPER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("BOK_MLX_ABORT", "0")
    fake = _make_fake_mlx_server()
    orig = fake.APIHandler.do_POST
    mod.load_mlx_server(fake)
    mod.main()
    assert fake.events.get("main_called") is True
    assert fake.APIHandler.do_POST is orig  # 零 patch=旧行为


def test_main_enabled_installs_and_runs(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("bok_mlx_server_main2_ut", WRAPPER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.delenv("BOK_MLX_ABORT", raising=False)
    fake = _make_fake_mlx_server()
    orig = fake.APIHandler.do_POST
    mod.load_mlx_server(fake)
    mod.main()
    assert fake.events.get("main_called") is True
    assert fake.APIHandler.do_POST is not orig
    assert "endpoint=/v1/abort" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 客户端侧（livekit_plugins.MlxLlmLLM / _LlmFallbackStream）
# ---------------------------------------------------------------------------

def test_client_abort_gate_local_only(monkeypatch):
    monkeypatch.delenv("BOK_MLX_ABORT", raising=False)
    assert lp._mlx_abort_on_for("http://127.0.0.1:1235/v1") is True
    assert lp._mlx_abort_on_for("http://localhost:1237/v1") is True
    assert lp._mlx_abort_on_for("https://api.deepseek.com/v1") is False
    monkeypatch.setenv("BOK_MLX_ABORT", "0")
    assert lp._mlx_abort_on_for("http://127.0.0.1:1235/v1") is False


def test_client_abort_url():
    assert lp._mlx_abort_url("http://127.0.0.1:1235/v1") == "http://127.0.0.1:1235/v1/abort"
    assert lp._mlx_abort_url("http://127.0.0.1:1235/v1/") == "http://127.0.0.1:1235/v1/abort"
    assert lp._mlx_abort_url("http://127.0.0.1:1237") == "http://127.0.0.1:1237/v1/abort"


def test_client_send_abort_posts_body(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"aborted": True})

    monkeypatch.setattr(
        lp, "_MLX_ABORT_CLIENT",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    asyncio.run(lp._send_mlx_abort("http://127.0.0.1:1235/v1", "rid-1"))
    assert len(seen) == 1
    assert str(seen[0].url) == "http://127.0.0.1:1235/v1/abort"
    assert json.loads(seen[0].content.decode()) == {"request_id": "rid-1"}


def test_client_send_abort_swallows_errors(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    monkeypatch.setattr(
        lp, "_MLX_ABORT_CLIENT",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    asyncio.run(lp._send_mlx_abort("http://127.0.0.1:1235/v1", "rid-err"))  # 不抛


class _DoneStub:
    def __init__(self, done: bool):
        self._done = done

    def done(self) -> bool:
        return self._done


class _FakeInnerStream:
    def __init__(self, done: bool = False):
        self._task = _DoneStub(done)

    async def __anext__(self):  # pragma: no cover - 不会跑到（abort 断言在 aclose）
        raise StopAsyncIteration

    async def aclose(self):
        pass


class _DummyLLM:
    """_LlmFallbackStream 基类构造要真 plugin（trace 取 provider）。"""

    provider = "dummy"


def _run_fallback_close(monkeypatch, *, inner_done: bool, drain_owns: bool, force_calls=None):
    fired: list[tuple[str, str]] = []
    monkeypatch.setattr(lp, "_fire_mlx_abort", lambda base, rid: fired.append((base, rid)))

    async def _inner():
        stream = lp._LlmFallbackStream(
            _DummyLLM(), _FakeInnerStream(done=inner_done), "f",
            req_id="cr1", abort_base="http://127.0.0.1:1235/v1",
        )
        stream._drain_owns = drain_owns
        await stream.aclose()
        return stream

    stream = asyncio.run(_inner())
    return stream, fired


def test_fallback_stream_aclose_fires_abort(monkeypatch):
    stream, fired = _run_fallback_close(monkeypatch, inner_done=False, drain_owns=False)
    assert fired == [("http://127.0.0.1:1235/v1", "cr1")]
    assert stream._abort_fired is True


def test_fallback_stream_aclose_no_abort_when_inner_done(monkeypatch):
    _stream, fired = _run_fallback_close(monkeypatch, inner_done=True, drain_owns=False)
    assert fired == []


def test_fallback_stream_aclose_respects_drain_owns(monkeypatch):
    """drain 接手时框架 aclose 不动 abort（drain 的语义=继续读）；force 才发。"""
    _stream, fired = _run_fallback_close(monkeypatch, inner_done=False, drain_owns=True)
    assert fired == []


def test_fallback_aclose_inner_forces_abort_under_drain(monkeypatch):
    fired: list[tuple[str, str]] = []
    monkeypatch.setattr(lp, "_fire_mlx_abort", lambda base, rid: fired.append((base, rid)))

    async def _inner():
        stream = lp._LlmFallbackStream(
            _DummyLLM(), _FakeInnerStream(done=False), "f",
            req_id="cr2", abort_base="http://127.0.0.1:1235/v1",
        )
        stream._drain_owns = True
        await stream._aclose_inner()

    asyncio.run(_inner())
    assert fired == [("http://127.0.0.1:1235/v1", "cr2")]


def test_attach_mlx_abort_on_raw_stream(monkeypatch):
    fired: list[tuple[str, str]] = []
    monkeypatch.setattr(lp, "_fire_mlx_abort", lambda base, rid: fired.append((base, rid)))

    class _Raw:
        def __init__(self, done):
            self._task = _DoneStub(done)

        async def aclose(self):
            self.closed = True

    async def _inner():
        alive = _Raw(done=False)
        lp._attach_mlx_abort(alive, "http://127.0.0.1:1236/v1", "mt1")
        await alive.aclose()
        assert alive.closed is True
        done = _Raw(done=True)
        lp._attach_mlx_abort(done, "http://127.0.0.1:1236/v1", "mt2")
        await done.aclose()

    asyncio.run(_inner())
    assert fired == [("http://127.0.0.1:1236/v1", "mt1")]


# ---------------------------------------------------------------------------
# 源级 pin：启动点/转发键/挂钩点存在（防重构静默拆线）
# ---------------------------------------------------------------------------

def test_bok_launch_points_use_wrapper():
    import tools.bok as bok

    src = (ROOT / "tools" / "bok.py").read_text()
    assert bok.MLX_SERVER_WRAPPER == ROOT / "services" / "llm-mlx" / "bok_mlx_server.py"
    assert bok.MLX_SERVER_WRAPPER.is_file()
    argv = bok._mac_llm_server_argv(Path("py"), "/m", "1239", {"llm_draft": ""})
    assert argv[:2] == ["py", str(bok.MLX_SERVER_WRAPPER)]
    # 三处启动点全走 wrapper；mac 旧入口 `-m mlx_lm server` 清零
    assert src.count("str(MLX_SERVER_WRAPPER)") >= 3
    assert '"-m", "mlx_lm", "server"' not in src


def test_forward_env_has_mlx_abort():
    import tools.bok as bok

    assert "BOK_MLX_ABORT" in bok._FORWARD_ENV


def test_livekit_plugins_wiring_pins():
    src = (ROOT / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py").read_text()
    assert '_MLX_REQ_ID_HEADER = "X-Bok-Req-Id"' in src
    # req_id 在 super().chat() 之前 set（ContextVar 随流任务上下文）
    assert "_MLX_REQ_ID_VAR.set(req_id)" in src
    assert src.index("_MLX_REQ_ID_VAR.set(req_id)") < src.index("stream = super().chat(")
    # aclose / cancel / 真弃流三点挂钩
    assert "def _fire_abort(self, force: bool = False)" in src
    assert "self._fire_abort()\n        await super().aclose()" in src
    assert "self._fire_abort(force=True)" in src
    assert "_attach_mlx_abort(stream, self._bok_abort_base, req_id)" in src


def test_wrapper_source_pins():
    src = WRAPPER_PATH.read_text()
    assert 'ABORT_PATH = "/v1/abort"' in src
    assert 'REQ_ID_HEADER = "X-Bok-Req-Id"' in src
    assert 'ABORT_ENV = "BOK_MLX_ABORT"' in src
    assert 'os.environ.get(ABORT_ENV, "1")' in src
    # 不碰 site-packages：只在导入时取模块引用做替换
    assert "from mlx_lm import server" in src
