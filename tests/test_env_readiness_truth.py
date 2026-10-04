"""CP env 面收编 + :1239 表收编 + readiness 真话（2026-10-02 编排审计第二波 · PR-A）。

三件事一句话：

① ``_control_plane_env``（prod mac launchd / Windows schtasks 的 CP 单元 env
   白名单，`_prod_units` 消费）收编 CP 会读而此前未登记的键——未登记 = prod
   封闭 env 面结构性死门（dev 靠 `_start_proc` merge 才活着）；
② :1239（queue proxy 背后的内部 mlx）进健康/孤儿表，且按可选线语义
   （queue proxy 关 / 整栈未起都不算缺口；代理活而 1239 死 = 半瘫必须点名）；
③ readiness 真话：/v1/models、/health 必须 HTTP 200 才算就绪（mlx/sidecar
   先绑端口后装权重，TCP 探测 = 谎）；serve 等待环的 1235/8787/8788 逐口
   HTTP 化，宽松终检同步。

任何断言不依赖真实栈在跑——urlopen/TCP 探测全部打桩（照
``tests/test_health_surface.py`` 约定）。
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import bok  # noqa: E402
from _bok_src import bok_source  # noqa: E402
from _bokpatch import patch_bok  # noqa: E402

_LIVE_WORKER_PAYLOAD = {
    "worker_type": "JT_ROOM",
    "agent_name": "bok-voice",
    "sdk_version": "1.8.2",
    "worker_load": 0.0952,
    "protocol_version": 1,
}


class _FakeResp(io.BytesIO):
    """BytesIO 带 status 属性（prod status / 就绪探针读 r.status）。"""

    def __init__(self, data: bytes = b"{}", status: int = 200):
        super().__init__(data)
        self.status = status


# ---------------------------------------------------------------------------
# ① prod 渲染探针：auth 三键必须进 CP 单元 env（plist grep 的单测版）
# ---------------------------------------------------------------------------


def test_control_plane_env_carries_auth_trio(monkeypatch, tmp_path):
    """BOK_AUTH_REQUIRED / BOK_JWT_SECRET / BOK_CP_TOKEN 显式设了必须下发——
    prod 封闭面收不到 BOK_AUTH_REQUIRED=1 = CP 静默 auth-off（事故形状）。"""
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_JWT_SECRET", "test-jwt-secret-dummy")
    monkeypatch.setenv("BOK_CP_TOKEN", "test-cp-token")
    env = bok._control_plane_env(tmp_path / "bok_voice.db")
    assert env["BOK_AUTH_REQUIRED"] == "1"
    assert env["BOK_JWT_SECRET"] == "test-jwt-secret-dummy"
    assert env["BOK_CP_TOKEN"] == "test-cp-token"


def test_prod_units_control_plane_env_carries_auth_trio(monkeypatch, tmp_path):
    """「prod plist grep」渲染探针：`_prod_units` 的 CP 单元 env 里必须真有
    BOK_AUTH_REQUIRED——_control_plane_env 只是中间层，单元定义才是 prod 真源。"""
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_CP_TOKEN", "test-cp-token")
    patch_bok(monkeypatch, "app_data_dir", lambda: tmp_path)
    patch_bok(monkeypatch, "repo_python", lambda: "py")
    patch_bok(monkeypatch, "_embedded_livekit", lambda: None)
    patch_bok(monkeypatch, "_agent_prod_env", lambda: {})
    patch_bok(monkeypatch, "_interp_env", lambda env: {})
    units = {name: env for name, _args, env, _comment in bok._prod_units()}
    cp_env = units["bok-control-plane"]
    assert cp_env["BOK_AUTH_REQUIRED"] == "1"
    assert cp_env["BOK_CP_TOKEN"] == "test-cp-token"


# ---------------------------------------------------------------------------
# ② :1239（llm-raw）表收编 + 可选线语义
# ---------------------------------------------------------------------------


def test_llm_raw_1239_in_health_tables():
    """:1239 曾在所有健康/孤儿表缺席——queue proxy 活着而上游 mlx 死了
    （半瘫）四表全绿。四表收编 + CORE_PORTS 可见（可选语义）。"""
    assert ("llm-raw", 1239, "/v1/models") in bok.PROD_HTTP_CHECKS
    assert bok._SWEEP_HTTP_PATHS.get(1239) == "/v1/models"
    assert any(port == 1239 and "mlx_lm" in markers for port, markers in bok._ORPHAN_PORT_OWNERS)
    assert ("llm-raw", 1239) in bok.CORE_PORTS


def test_llm_raw_1239_optional_semantics(monkeypatch):
    """:1239 是 queue proxy 拓扑专属可选线（镜像 :1237 语义）：代理关/=0 或缺
    模型而 mlx 不在盘时，1239 缺席是设计态——不得判死/报 DOWN。"""
    assert 1239 in bok._OPTIONAL_LLM_PORTS
    assert bok._only_optional_ports([1239]) is True
    # 队列代理关 → 不预期；mac + 开 → 预期
    patch_bok(monkeypatch, "is_mac", lambda: True)
    monkeypatch.setenv("BOK_LLM_QUEUE_PROXY", "0")
    assert bok._llm_raw_expected() is False
    monkeypatch.setenv("BOK_LLM_QUEUE_PROXY", "1")
    assert bok._llm_raw_expected() is True
    # 非 mac（Windows/Linux 走 llama.cpp，无 :1239 拓扑）→ 不预期
    patch_bok(monkeypatch, "is_mac", lambda: False)
    assert bok._llm_raw_expected() is False


def test_prod_status_queue_off_does_not_report_1239(monkeypatch, capsys):
    """queue proxy 关 = mlx 直跑 :1235：prod status 不得因 :1239 缺席判 DEGRADED。"""
    monkeypatch.setenv("BOK_LLM_QUEUE_PROXY", "0")
    patch_bok(monkeypatch, "is_mac", lambda: True)
    patch_bok(monkeypatch, "healthy", lambda port: False)  # 无栈在场

    def fake_urlopen(url, timeout=None):
        if url.endswith("/worker"):
            return _FakeResp(json.dumps(_LIVE_WORKER_PAYLOAD).encode())
        return _FakeResp(b'{"ok": true}')

    monkeypatch.setattr(bok.urllib.request, "urlopen", fake_urlopen)
    rc = bok.cmd_prod_status()
    out = capsys.readouterr().out
    assert rc == 0
    assert "prod: OK" in out
    assert "1239" not in out


def test_prod_status_flags_half_dead_mlx(monkeypatch, capsys):
    """代理活（:1235 在听）而 :1239 死 = 半瘫——prod status 必须点名 DEGRADED
    （本轮收编的核心收益；旧四表对此结构性失明）。"""
    monkeypatch.setenv("BOK_LLM_QUEUE_PROXY", "1")
    patch_bok(monkeypatch, "is_mac", lambda: True)
    patch_bok(monkeypatch, "healthy", lambda port: port == 1235)

    def fake_urlopen(url, timeout=None):
        if ":1239" in url:
            raise urllib.error.URLError("connection refused")
        if url.endswith("/worker"):
            return _FakeResp(json.dumps(_LIVE_WORKER_PAYLOAD).encode())
        return _FakeResp(b'{"ok": true}')

    monkeypatch.setattr(bok.urllib.request, "urlopen", fake_urlopen)
    rc = bok.cmd_prod_status()
    out = capsys.readouterr().out
    assert rc == 1
    assert "prod: DEGRADED" in out
    assert "llm-raw" in out and "1239" in out


# ---------------------------------------------------------------------------
# ③ readiness 真话：_llm_http_ready / _http_ok
# ---------------------------------------------------------------------------


def test_llm_http_ready_only_200(monkeypatch):
    """/v1/models 只有 HTTP 200 算就绪；404/503/超时/拒连一律 False，永不抛。"""
    seen: dict = {}

    # 出站 seam=bok._http_call（http.client 单点，(status, body) 形）
    def fake_200(url, method="GET", *, body=None, headers=None, timeout_s=10.0):
        seen["url"] = url
        seen["timeout"] = timeout_s
        return 200, b'{"data": []}'

    patch_bok(monkeypatch, "_http_call", fake_200)
    assert bok._llm_http_ready(1235) is True
    assert seen["url"] == "http://127.0.0.1:1235/v1/models"
    assert seen["timeout"] == 1.5

    patch_bok(monkeypatch, "_http_call",
                        lambda url, method="GET", *, body=None, headers=None, timeout_s=10.0: (404, b"nf"))
    assert bok._llm_http_ready(1235) is False

    patch_bok(monkeypatch, "_http_call",
                        lambda url, method="GET", *, body=None, headers=None, timeout_s=10.0: (503, b"loading"))
    assert bok._llm_http_ready(1239) is False

    def fake_timeout(url, method="GET", *, body=None, headers=None, timeout_s=10.0):
        raise TimeoutError("timed out")

    patch_bok(monkeypatch, "_http_call", fake_timeout)
    assert bok._llm_http_ready(1239) is False
    # 显式放宽窗（宽松终检档）透传到 _http_call
    patch_bok(monkeypatch, "_http_call",
                        lambda url, method="GET", *, body=None, headers=None, timeout_s=10.0:
                        seen.update({"t2": timeout_s}) or (200, b""))
    assert bok._llm_http_ready(1235, timeout_s=5.0) is True
    assert seen["t2"] == 5.0


def test_http_ok_requires_200_on_path(monkeypatch):
    """sidecar /health：装载期 503（loading）不得当就绪；200 才算。"""
    seen: dict = {}

    def fake(url, method="GET", *, body=None, headers=None, timeout_s=10.0):
        seen["url"] = url
        return 503, b'{"status": "loading"}'

    patch_bok(monkeypatch, "_http_call", fake)
    assert bok._http_ok(8788, "/health") is False
    assert seen["url"] == "http://127.0.0.1:8788/health"

    patch_bok(monkeypatch, "_http_call",
                        lambda url, method="GET", *, body=None, headers=None, timeout_s=10.0:
                        (200, b'{"status": "ready"}'))
    assert bok._http_ok(8787, "/health") is True

    def fake_err(url, method="GET", *, body=None, headers=None, timeout_s=10.0):
        raise OSError("refused")

    patch_bok(monkeypatch, "_http_call", fake_err)
    assert bok._http_ok(8787, "/health") is False


# ---------------------------------------------------------------------------
# ④ serve 就绪等待环：1235/8787/8788 HTTP 化（行为 + 源级 pin）
# ---------------------------------------------------------------------------


def _patch_serve_probes(monkeypatch, *, llm_ready=True, sidecar_ready=True,
                        tcp_ready=True):
    calls: list[tuple] = []

    def fake_healthy(port):
        calls.append(("tcp", port))
        return tcp_ready

    def fake_llm(port, timeout_s=1.5):
        calls.append(("llm", port, timeout_s))
        return llm_ready

    def fake_http(port, path, timeout_s=1.5):
        calls.append(("http", port, path, timeout_s))
        return sidecar_ready

    patch_bok(monkeypatch, "healthy", fake_healthy)
    patch_bok(monkeypatch, "_llm_http_ready", fake_llm)
    patch_bok(monkeypatch, "_http_ok", fake_http)
    return calls


def test_serve_probe_uses_http_truth(monkeypatch):
    """逐口判据：1235 走 _llm_http_ready；8787/8788 走 /health HTTP-200；
    其余（8000/7880/worker）仍 TCP——文档面不变。"""
    calls = _patch_serve_probes(monkeypatch)
    assert bok._serve_ready_probe(1235) is True
    assert calls == [("llm", 1235, 1.5)]
    calls.clear()
    assert bok._serve_ready_probe(1235) is True
    assert calls == [("llm", 1235, 1.5)]
    for port in (8787, 8788):
        calls.clear()
        assert bok._serve_ready_probe(port) is True
        assert calls == [("http", port, "/health", 1.5)]
    for port in (8000, 7880, 8081, 8082, 8083):
        calls.clear()
        assert bok._serve_ready_probe(port) is True
        assert calls == [("tcp", port)]


def test_wait_desktop_ready_blocks_until_http_ready(monkeypatch):
    """等待环：8788 装载中（/health 503）→ 不算就绪；模型装完 200 → 放行。
    120×1s 环形状保留（tries 参数=形状可测）。"""
    calls = _patch_serve_probes(monkeypatch, sidecar_ready=False)
    sleeps: list[int] = []
    monkeypatch.setattr(bok.time, "sleep", lambda s: sleeps.append(s))
    asserts = {"n": 0}

    def sidecar_flips(port, path, timeout_s=1.5):
        calls.append(("http", port, path, timeout_s))
        asserts["n"] += 1
        return asserts["n"] > 2  # 第三轮起就绪

    patch_bok(monkeypatch, "_http_ok", sidecar_flips)
    assert bok._wait_desktop_ready([8788, 8000], tries=5) is True
    assert sleeps == [1, 1]


def test_wait_desktop_ready_times_out_truthfully(monkeypatch):
    """llm /v1/models 一直不就绪 → 等待环如实超时（sleep 逐轮一次，环形状不变）。"""
    _patch_serve_probes(monkeypatch, llm_ready=False)
    sleeps: list[int] = []
    monkeypatch.setattr(bok.time, "sleep", lambda s: sleeps.append(s))
    assert bok._wait_desktop_ready([1235, 8000], tries=3) is False
    assert sleeps == [1, 1, 1]


def test_serve_relaxed_probe_keeps_http_and_relaxed_semantics(monkeypatch):
    """宽松终检：严格口维持 HTTP-200（但用 5s 窗吸收调度延迟），其余端口退回
    _relaxed_healthy 旧语义（互杀事故收编不得因本轮收窄）。"""
    relaxed_calls: list[int] = []
    patch_bok(monkeypatch, "_relaxed_healthy",
                        lambda port, timeout_s=5.0: relaxed_calls.append(port) or True)
    calls = _patch_serve_probes(monkeypatch)
    assert bok._serve_ready_probe_relaxed(1235) is True
    assert calls == [("llm", 1235, 5.0)]
    calls.clear()
    assert bok._serve_ready_probe_relaxed(8787) is True
    assert calls == [("http", 8787, "/health", 5.0)]
    calls.clear()
    assert bok._serve_ready_probe_relaxed(8000) is True
    assert calls == [] and relaxed_calls == [8000]


def test_serve_wait_wiring_source_pins():
    """源级 pin（test_slot_wiring 惯例）：serve 等待环必须走 _wait_desktop_ready，
    宽松终检必须带 _serve_ready_probe_relaxed；裸 all(healthy(...)) 不得回潮。"""
    import inspect

    src = bok_source()
    assert "def _wait_desktop_ready(" in src
    assert "def _serve_ready_probe(" in src
    assert "def _serve_ready_probe_relaxed(" in src
    assert "_SERVE_HTTP_READY_PORTS" in src
    serve_src = inspect.getsource(bok.cmd_serve)
    assert "if _wait_desktop_ready(targets):" in serve_src
    assert "_ports_down_after_grace(targets, probe=_serve_ready_probe_relaxed)" in serve_src
    # 旧形状（对全部 target 用 1s TCP）绝不得回潮到 serve 等待环
    assert "all(healthy(p) for p in targets)" not in serve_src


def test_start_llm_tcp_skip_warns_when_http_not_ready(monkeypatch, tmp_path, capsys):
    """TCP 健康跳过点必须补一次 /v1/models 真话探针：权重装载中/半死 → 大声
    告警一行；TCP 跳过语义本身不变（绝不双起）。"""
    patch_bok(monkeypatch, "is_mac", lambda: True)
    monkeypatch.setenv("BOK_LLM_QUEUE_PROXY", "1")
    patch_bok(monkeypatch, "healthy", lambda port: True)
    started: list = []
    patch_bok(monkeypatch, "_start_proc", lambda *a, **k: started.append(a))
    patch_bok(monkeypatch, "_llm_http_ready", lambda port, timeout_s=1.5: False)
    bok._start_llm({}, tmp_path, tmp_path)
    assert started == []
    err = capsys.readouterr().err
    assert "tcp-up but /v1/models not ready" in err
    assert "1235" in err and "1239" in err

    # /v1/models 就绪 → 零告警、照旧跳过
    patch_bok(monkeypatch, "_llm_http_ready", lambda port, timeout_s=1.5: True)
    bok._start_llm({}, tmp_path, tmp_path)
    assert started == []
    assert "not ready" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# ⑤ sidecar /health 真话：模型未载 = 503（不是假 200）
# ---------------------------------------------------------------------------


def _load_asr_app():
    import importlib.util
    import os

    os.environ.setdefault("QWEN3_ASR_BACKEND", "mlx")
    spec = importlib.util.spec_from_file_location(
        "qwen3_asr_sidecar_readiness", ROOT / "services" / "qwen3-asr-sidecar" / "app.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_asr_health_503_until_model_loaded(monkeypatch):
    """eager 档模型未载 = 503 loading（TCP 绑定先于权重装载，旧 200 是谎）。"""
    pytest.importorskip("fastapi")
    mod = _load_asr_app()
    monkeypatch.setattr(mod.service, "_model", None, raising=False)
    monkeypatch.setattr(mod.service, "_qwen3_deferred", False, raising=False)
    monkeypatch.setattr(mod.service, "_load_error", None, raising=False)
    resp = mod.health()
    assert resp.status_code == 503
    body = json.loads(resp.body)
    assert body["status"] == "loading"
    assert body["model_ready"] is False
    # 向后兼容键面保留
    assert body["ok"] is True and body["backend"] == mod.BACKEND

    monkeypatch.setattr(mod.service, "_model", object(), raising=False)
    resp2 = mod.health()
    assert resp2.status_code == 200
    assert json.loads(resp2.body)["status"] == "ready"


def test_asr_health_deferred_is_ready_not_fake_down(monkeypatch):
    """设计跳载档（sensevoice，SV CPU 车道在役）：就绪=200 且 qwen3_deferred=True
    ——首个 qwen3 路径请求会懒加载，不是故障（smoke_sidecars 同款判据）。"""
    pytest.importorskip("fastapi")
    mod = _load_asr_app()
    monkeypatch.setattr(mod.service, "_model", None, raising=False)
    monkeypatch.setattr(mod.service, "_qwen3_deferred", True, raising=False)
    monkeypatch.setattr(mod.service, "_load_error", None, raising=False)
    resp = mod.health()
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["status"] == "ready" and body["qwen3_deferred"] is True


def test_asr_health_503_on_load_error(monkeypatch):
    """加载失败 = 503 error（半死不装活）。"""
    pytest.importorskip("fastapi")
    mod = _load_asr_app()
    monkeypatch.setattr(mod.service, "_model", None, raising=False)
    monkeypatch.setattr(mod.service, "_qwen3_deferred", False, raising=False)
    monkeypatch.setattr(mod.service, "_load_error", "boom", raising=False)
    resp = mod.health()
    assert resp.status_code == 503
    body = json.loads(resp.body)
    assert body["status"] == "error" and body["load_error"] == "boom"


def test_tts_health_503_until_preset_loaded(monkeypatch):
    """TTS sidecar：preset 未载（装载中/失败）= 503 loading；载好 = 200 ready。
    双载档 clone 缺席不算缺口（preset 车道在役）。"""
    pytest.importorskip("fastapi")
    import importlib

    sys.path.insert(0, str(ROOT / "services" / "qwen3-tts-sidecar"))
    tts_app = importlib.import_module("app")
    monkeypatch.setattr(tts_app.service, "_preset_model", None, raising=False)
    monkeypatch.setattr(tts_app.service, "_clone_model", None, raising=False)
    resp = tts_app.health()
    assert resp.status_code == 503
    body = json.loads(resp.body)
    assert body["status"] == "loading" and body["model_ready"] is False
    assert body["ok"] is True  # 键面向后兼容

    monkeypatch.setattr(tts_app.service, "_preset_model", object(), raising=False)
    resp2 = tts_app.health()
    assert resp2.status_code == 200
    body2 = json.loads(resp2.body)
    assert body2["status"] == "ready" and body2["model_ready"] is True
