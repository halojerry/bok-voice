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


def test_core_ports_cover_embed_and_optional_exemption():
    """W1b embedding sidecar(:8789) 进单点表 + 享可选豁免(缺模型不算超时/降级)。"""
    assert ("embed", 8789) in bok.CORE_PORTS
    assert "embedding" in bok.OPTIONAL_MODELS
    assert 8789 in bok._OPTIONAL_LLM_PORTS
    # 宽松终检:缺口仅 embed → 放行(镜像 mt/settle 语义)。
    assert bok._only_optional_ports([8789]) is True
    # 孤儿清扫身份映射:殭尸 embed 进程按端口+命令行双条件收割。
    assert any(port == 8789 and "bge-embed" in markers for port, markers in bok._ORPHAN_PORT_OWNERS)
    # 放宽探活面:暖机窗 /health 应答(哪怕 ready=false)算进程在。
    assert bok._SWEEP_HTTP_PATHS.get(8789) == "/health"


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


# ---------------------------------------------------------------------------
# G3 monitor 硬否决（2026-09-25，LANE-AB-2026-09-25.md 附3：offscript 窗 worker
# 被误杀 ×7 全落在场景间隙/swap 颠簸——12 轮/60s 抬门槛仍有窗，veto 才关死）。
# ---------------------------------------------------------------------------

def test_monitor_veto_blocks_kill_with_active_calls():
    """硬 veto 纯函数判定：active_calls>0 任何探活失败都不杀；无通话/CP 不可达
    退回连续失败口径（idle 门槛 2 轮）。"""
    # 无通话在途：2 轮（≥10s）杀（旧 idle 口径不变）
    assert bok._monitor_kill_round(1, 0) == (False, False)
    assert bok._monitor_kill_round(2, 0) == (True, False)
    assert bok._monitor_kill_round(99, 0)[0] is True
    # CP 不可达（None）= 保守不杀(2026-09-28 生命周期护栏:None 曾落 falsy 分支
    # 令 veto 静默失效——CP 抖一下 + worker 探活失败 = 可能杀掉在途 worker)
    assert bok._monitor_kill_round(1, None) == (False, False)
    assert bok._monitor_kill_round(2, None) == (False, True)
    # 有通话在途：恒不杀（硬 veto）——streak 多深都不杀，等场景间隙 active 归零
    for n in (1, 2, 3, 12, 60, 999):
        kill, _veto = bok._monitor_kill_round(n, 2)
        assert kill is False, f"active_calls>0 时 streak={n} 不得杀"
    # veto 打点节奏：首过 idle 门槛一次 + 此后每 12 轮提醒一次（防长窗静默/刷屏）
    assert bok._monitor_kill_round(2, 2) == (False, True)
    assert bok._monitor_kill_round(3, 2) == (False, False)
    assert bok._monitor_kill_round(12, 2) == (False, True)
    assert bok._monitor_kill_round(24, 2) == (False, True)


def test_monitor_probe_uses_real_worker_endpoint(monkeypatch):
    """monitor 探活必须走真 GET :port/worker（_probe_worker 单点）——1s TCP 对
    「进程在、没 register/swap 颠簸假死」不可见（G3①，prod 面同源探针）。"""

    def fake_urlopen(url, timeout=None):
        assert "/worker" in url  # 端点本体，非裸 TCP
        raise urllib.error.URLError("swap stall")

    monkeypatch.setattr(bok.urllib.request, "urlopen", fake_urlopen)
    ok, detail = bok._probe_worker(8081)
    assert not ok and detail.startswith("DOWN")


# ---------------------------------------------------------------------------
# 9B 后端化（2026-09-25，plan §2.7）：:1237 默认不随栈拉起、judge/settle env
# 不注入（消费方各自回退 MLX :1235）；BOK_DEV_9B=1 时行为与改造前逐字节相同。
# ---------------------------------------------------------------------------

def test_dev_9b_off_gates_judge_and_settle_env(monkeypatch, tmp_path):
    """BOK_DEV_9B 未开：judge/CP-settle 两路都不注入 :1237 指向；=1 时逐字节同旧；
    9B 关但外部显式设了端点 → 照传（云端钩子不受开关误伤）。"""
    fake_model = tmp_path / "settle-9b"
    fake_model.write_text("x")
    monkeypatch.setattr(bok, "_settle_llm_model", lambda cur: str(fake_model))
    for key in ("BOK_DEV_9B", "FLOW_JUDGE_LLM_BASE_URL", "FLOW_JUDGE_LLM_MODEL",
                "BOK_SETTLE_LLM_BASE_URL", "BOK_SETTLE_LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)

    # 9B 关(2026-10-01 P2 翻档后须显式 =0)：judge env 不注入（agent.py 回退链落 MLX :1235）
    monkeypatch.setenv("BOK_DEV_9B", "0")
    env: dict[str, str] = {}
    bok._apply_judge_env(env, {})
    assert "FLOW_JUDGE_LLM_BASE_URL" not in env
    assert "FLOW_JUDGE_LLM_MODEL" not in env
    # 9B 开：与改造前逐字节相同
    monkeypatch.setenv("BOK_DEV_9B", "1")
    env_on: dict[str, str] = {}
    bok._apply_judge_env(env_on, {})
    assert env_on["FLOW_JUDGE_LLM_BASE_URL"] == "http://127.0.0.1:1237/v1"
    assert env_on["FLOW_JUDGE_LLM_MODEL"] == str(fake_model)
    # 9B 关 + 外部显式设定：照传（不动 :1237 缺省）
    monkeypatch.delenv("BOK_DEV_9B", raising=False)
    monkeypatch.setenv("FLOW_JUDGE_LLM_BASE_URL", "https://cloud.example/v1")
    monkeypatch.setenv("FLOW_JUDGE_LLM_MODEL", "cloud-model")
    env_ext: dict[str, str] = {}
    bok._apply_judge_env(env_ext, {})
    assert env_ext["FLOW_JUDGE_LLM_BASE_URL"] == "https://cloud.example/v1"
    assert env_ext["FLOW_JUDGE_LLM_MODEL"] == "cloud-model"

    # CP 面 settle env：9B 显式关(=0)不注入（Summarizer 回退 MLX）；缺省/=1 注入
    monkeypatch.setenv("BOK_DEV_9B", "0")
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp_path)
    monkeypatch.delenv("FLOW_JUDGE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("FLOW_JUDGE_LLM_MODEL", raising=False)
    cp_off = bok._control_plane_env(tmp_path / "db.sqlite")
    assert "BOK_SETTLE_LLM_BASE_URL" not in cp_off
    assert "BOK_SETTLE_LLM_MODEL" not in cp_off
    monkeypatch.setenv("BOK_DEV_9B", "1")
    cp_on = bok._control_plane_env(tmp_path / "db.sqlite")
    assert cp_on["BOK_SETTLE_LLM_BASE_URL"] == "http://127.0.0.1:1238/v1"  # I1 前门闸
    assert cp_on["BOK_SETTLE_LLM_MODEL"] == str(fake_model)


def test_dev_9b_off_skips_settle_llm_start(monkeypatch, tmp_path, capsys):
    """serve/up 侧：BOK_DEV_9B=0 显式关时 _start_settle_llm 直接跳过（不起进程、
    不等 :1237）；stderr 留一行明示回退。2026-10-01 P2 翻档后缺省=开。"""
    monkeypatch.setenv("BOK_DEV_9B", "0")
    started: list[list[str]] = []
    monkeypatch.setattr(bok, "_start_proc", lambda args, pidfile, logfile, env=None, cwd=None: started.append(args))
    rc = bok._start_settle_llm({}, tmp_path, tmp_path)
    assert rc is False
    assert not started
    err = capsys.readouterr().err
    assert "BOK_DEV_9B" in err and "1237" in err
