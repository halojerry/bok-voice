"""健康面完整性单测(2026-09-17 体检缺口收编)。

三张表(cmd_status / cmd_doctor / cmd_prod_status)共用 CORE_PORTS/WORKER_PORTS
单点;worker 走 livekit-agents 内建 GET /worker 真端点(TCP UP 对「进程在、没
register / 错码假活」不可见);LLM 走 max_tokens=1 功能探针(端口 UP ≠ 能用)。
任何断言不得依赖真实栈在跑——urlopen 全部打桩,端口探活依赖的 1236/1237 在
CI 机器不在跑属正常(cmd_prod_status 对可选线是「起了才查」语义)。
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import bok  # noqa: E402

# 2026-09-17 实机抓录的 livekit-agents 1.8.0 GET /worker 真实 payload 形状
# (注意:没有 active_jobs 字段——旧 prod status 打印它恒 None 属谎报)。
_LIVE_WORKER_PAYLOAD = {
    "worker_type": "JT_ROOM",
    "agent_name": "bok-voice",
    "sdk_version": "1.8.0",
    "worker_load": 0.0952,
    "protocol_version": 1,
}


class _FakeResp(io.BytesIO):
    """BytesIO 带 status 属性(prod status 读 r.status)。"""

    status = 200


def test_core_ports_cover_settle():
    """settle-llm(1237) 必须在健康面单点表里——缺席=9B 静默退回 4B 无人知。"""
    assert ("settle-llm", 1237) in bok.CORE_PORTS
    assert ("llm", 1235) in bok.CORE_PORTS
    assert ("mt-llm", 1236) in bok.CORE_PORTS


def test_worker_ports_triple_matches_prod_units(monkeypatch, tmp_path):
    """worker 探针表 = 生产常驻单元(agent + interp-fwd/rev)三件,8081-8083。"""
    assert bok.WORKER_PORTS == (
        ("agent-worker", 8081),
        ("interp-fwd", 8082),
        ("interp-rev", 8083),
    )
    # 与 _prod_units 单元名交叉对齐(桩法对齐 test_prod_windows,不碰真实 app-data)。
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(bok, "repo_python", lambda: "py")
    monkeypatch.setattr(bok, "_embedded_livekit", lambda: None)
    monkeypatch.setattr(bok, "_agent_prod_env", lambda: {})
    monkeypatch.setattr(bok, "_interp_env", lambda env: {})
    monkeypatch.setattr(bok, "_control_plane_env", lambda db: {})
    unit_names = {name for name, _args, _env, _comment in bok._prod_units()}
    assert {"bok-agent", "bok-interp-fwd", "bok-interp-rev"} <= unit_names


def test_probe_worker_parses_live_payload(monkeypatch):
    def fake_urlopen(url, timeout=None):
        assert url.endswith(":8081/worker")
        return _FakeResp(json.dumps(_LIVE_WORKER_PAYLOAD).encode())

    monkeypatch.setattr(bok.urllib.request, "urlopen", fake_urlopen)
    ok, detail = bok._probe_worker(8081)
    assert ok
    assert "agent_name=bok-voice" in detail
    assert "load=0.10" in detail
    assert "sdk=1.8.0" in detail
    # 谎报修复:1.8.0 无此字段,输出不得再打印 active_jobs=None。
    assert "active_jobs" not in detail


def test_probe_worker_down(monkeypatch):
    def fake_urlopen(url, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(bok.urllib.request, "urlopen", fake_urlopen)
    ok, detail = bok._probe_worker(8082)
    assert not ok
    assert detail.startswith("DOWN")


def test_probe_llm_uses_absolute_model_path(monkeypatch):
    """model 字段必须取 /v1/models 的绝对路径 id(repo id 触发 HF hub 解析);
    探针请求必须 max_tokens=1(prefill-only,不吃解码租)。"""
    seen: dict = {}

    def fake_urlopen(arg, timeout=None):
        seen["timeout"] = timeout
        if isinstance(arg, str):
            return _FakeResp(json.dumps({"data": [
                {"id": "mlx-community/Hy-MT2-1.8B-Abliterated-8bit"},
                {"id": "/models/avan-ag/Qwen3.5-4B-Uncensored-MLX-4bit"},
            ]}).encode())
        seen["body"] = json.loads(arg.data.decode())
        return _FakeResp(b'{"choices":[{"message":{"content":"a"}}]}')

    monkeypatch.setattr(bok.urllib.request, "urlopen", fake_urlopen)
    ok, detail = bok._probe_llm()
    assert ok
    assert seen["body"]["model"] == "/models/avan-ag/Qwen3.5-4B-Uncensored-MLX-4bit"
    assert seen["body"]["max_tokens"] == 1
    assert "ok" in detail and "ms" in detail


def test_probe_llm_timeout_env_and_fail_wording(monkeypatch):
    monkeypatch.setenv("BOK_DOCTOR_LLM_PROBE_TIMEOUT_S", "4")

    def fake_urlopen(arg, timeout=None):
        assert timeout == 4.0
        raise TimeoutError("timed out")

    monkeypatch.setattr(bok.urllib.request, "urlopen", fake_urlopen)
    ok, detail = bok._probe_llm()
    assert not ok
    assert "FAIL" in detail
    assert "wedge" in detail  # 冷启动页入与 wedge 的区分提示必须带


def test_probe_llm_explicit_model_beats_models_scan(monkeypatch):
    """显式传 model(:1236 MT 探针,2026-09-19 同传挂死实案):必须以传入路径为准、
    忽略 /v1/models 扫描结果——扫描列表含外来 repo-id,MT 专用 server 上被采信
    即挂死;prompt 必须透传(Hy-MT2 对超短 ASCII 输入会 template 404)。"""
    seen: dict = {}

    def fake_urlopen(arg, timeout=None):
        # /models 只回 repo-id(无绝对路径)——缺省路径会 FAIL,显式 model 必须无视它
        if isinstance(arg, str):
            return _FakeResp(json.dumps({"data": [
                {"id": "mlx-community/Hy-MT2-1.8B-Abliterated-8bit"},
            ]}).encode())
        seen["body"] = json.loads(arg.data.decode())
        return _FakeResp(b'{"choices":[{"message":{"content":"a"}}]}')

    monkeypatch.setattr(bok.urllib.request, "urlopen", fake_urlopen)
    ok, detail = bok._probe_llm(
        "http://127.0.0.1:1236/v1",
        model="/Users/x/Hy-MT2-1.8B-8bit",
        prompt="Translate to English: 你好世界")
    assert ok
    assert seen["body"]["model"] == "/Users/x/Hy-MT2-1.8B-8bit"
    assert seen["body"]["messages"][0]["content"] == "Translate to English: 你好世界"
    assert "Hy-MT2-1.8B-8bit" in detail  # model 名取 Path(...).name 进输出


def test_model_present_recognizes_lmstudio_layout(monkeypatch, tmp_path):
    """9B settle 只以 lmstudio 布局在盘时 doctor 不得报 MISSING(与 cmd_download
    的 ensure 同款判定;app-data 布局优先不变)。"""
    monkeypatch.setattr(bok, "model_dir", lambda repo: tmp_path / "appdata" / repo)
    monkeypatch.setattr(bok, "is_mac", lambda: True)
    monkeypatch.setattr(bok, "_lmstudio_models_dir", lambda: tmp_path / "lmstudio")
    repo = "huihui-ai/Huihui-Qwen3.5-9B-abliterated-mlx-4bit"
    # 两处都不在 → MISSING
    assert not bok._model_present(repo)
    # 只有 app-data 在 → ok(lmstudio 不用看)
    app = tmp_path / "appdata" / repo
    app.mkdir(parents=True)
    (app / "model.safetensors").write_text("x")
    assert bok._model_present(repo)
    # 只有 lmstudio 在 → ok(mac 上旧行为恒 MISSING 的断层)
    lm = tmp_path / "lmstudio" / repo
    lm.mkdir(parents=True)
    (lm / "model.safetensors").write_text("x")
    assert bok._model_present(repo)
    # 非 mac 平台不认 lmstudio 布局(用两边都不在盘的另一个 repo 验证)
    monkeypatch.setattr(bok, "is_mac", lambda: False)
    assert not bok._model_present("mlx-community/Hy-MT2-1.8B-Abliterated-8bit")


def test_prod_status_degraded_when_bline_worker_down(monkeypatch, capsys):
    """B 线 worker 失联必须 DEGRADED——旧版只探 :8081,fwd/rev 静默缺失照绿。"""

    def fake_urlopen(url, timeout=None):
        if ":8083/" in url:
            raise urllib.error.URLError("connection refused")
        if url.endswith("/worker"):
            return _FakeResp(json.dumps(_LIVE_WORKER_PAYLOAD).encode())
        return _FakeResp(b'{"ok": true}')

    monkeypatch.setattr(bok.urllib.request, "urlopen", fake_urlopen)
    rc = bok.cmd_prod_status()
    out = capsys.readouterr().out
    assert rc == 1
    assert "prod: DEGRADED" in out
    assert ":8083" in out and "DOWN" in out


def test_prod_status_ok_when_workers_alive(monkeypatch, capsys):
    def fake_urlopen(url, timeout=None):
        if url.endswith("/worker"):
            return _FakeResp(json.dumps(_LIVE_WORKER_PAYLOAD).encode())
        return _FakeResp(b'{"ok": true}')

    monkeypatch.setattr(bok.urllib.request, "urlopen", fake_urlopen)
    rc = bok.cmd_prod_status()
    out = capsys.readouterr().out
    assert rc == 0
    assert "prod: OK" in out
    # 三件 worker 行都带 agent_name(真端点证据,非 TCP UP 空话);无 active_jobs 谎报。
    assert out.count("agent_name=bok-voice") == 3
    assert "active_jobs" not in out
