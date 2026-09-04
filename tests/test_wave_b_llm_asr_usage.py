"""Wave B 单测：LLM 内芯官方化回归 + ASR 滑窗 partial 逻辑 + 真实用量入库。

不依赖真实模型/服务：LLM 断言走纯逻辑、ASR partial 用 importlib 载 sidecar + mock
后端模型、session-report/usage 用 CP in-memory repo。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))
sys.path.insert(0, str(ROOT / "apps" / "control-plane"))


def _load_sidecar_app():
    """从文件路径加载 qwen3-asr-sidecar app.py(无 __init__ 包,用 importlib)。

    BACKEND 是 import 时读的常量:置 mlx 才能走滑窗 partial 分支(model 由测试注入,
    不真加载)。STREAM_PARTIAL 同理由 QWEN3_ASR_STREAM 控制。
    """
    os.environ.setdefault("QWEN3_ASR_BACKEND", "mlx")
    os.environ.setdefault("QWEN3_ASR_STREAM", "1")
    spec = importlib.util.spec_from_file_location(
        "qwen3_asr_sidecar", ROOT / "services" / "qwen3-asr-sidecar" / "app.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- LLM：官方 openai 内芯 + KV-cache 包装层回归 ----
def test_context_aware_llm_keeps_byte_stable_prefix():
    """换官方内芯后,ContextAwareLLM 的前缀重组必须保持(指令+流程步注入,逐轮稳定)。"""
    from livekit.agents import llm as agents_llm

    from agent_runtime.providers.livekit_plugins import ContextAwareLLM, ContextState

    class _Recorder(agents_llm.LLM):
        def __init__(self):
            super().__init__()
            self.calls = []

        def chat(self, *, chat_ctx, tools=None, conn_options=None, parallel_tool_calls=None,
                 tool_choice=None, extra_kwargs=None):
            self.calls.append([(m.role, m.text_content) for m in chat_ctx.items])
            return None

    inner = _Recorder()
    ctx_state = ContextState(account_id="acc-001")
    ctx_state.set_user_language("cantonese")
    ctx_state.set_flow("话术总览", "第 1 步：开场确认")
    llm = ContextAwareLLM(inner, context_state=ctx_state)

    chat_ctx = agents_llm.ChatContext()
    chat_ctx.add_message(role="system", content="你是 Bok Voice 客服。")
    chat_ctx.add_message(role="user", content="你好")
    llm.chat(chat_ctx=chat_ctx)
    chat_ctx.add_message(role="assistant", content="你好,请问有什么可以帮您?")
    chat_ctx.add_message(role="user", content="我想查件")
    llm.chat(chat_ctx=chat_ctx)

    for call in inner.calls:
        system = call[0][1]
        # 用户语言指令(粤语客服锚定讲粤语) + 流程当前步都进了稳定前缀。
        assert "粤语" in system or "cantonese" in system, system[:80]
        assert "开场确认" in system, system[:80]
    # 前缀系统段不被历史轮次污染(两轮第一段都是 system 注入)。
    assert inner.calls[0][0][0] == "system" and inner.calls[1][0][0] == "system"


def test_mlx_llm_uses_official_openai_core():
    """MlxLlmLLM 内芯=官方 livekit-plugins-openai;stop/max_tokens 走 extra_body。"""
    from agent_runtime.providers.livekit_plugins import MlxLlmLLM

    llm = MlxLlmLLM(base_url="http://127.0.0.1:1235/v1", model="some/local/model")
    assert llm.provider == "mlx"
    assert hasattr(llm, "chat") and hasattr(llm, "prewarm")
    opts = llm._opts
    assert opts.model == "some/local/model"
    # 本地服务吃经典 stop/max_tokens(extra_body),唔传新的 max_completion_tokens(保持 NOT_GIVEN)。
    from livekit.agents.types import NOT_GIVEN

    assert opts.max_completion_tokens is NOT_GIVEN
    body = opts.extra_body or {}
    assert body.get("stop") and "<|im_end|>" in body["stop"]
    assert body.get("max_tokens") == int(os.environ.get("LLM_MAX_TOKENS", "160"))


def test_asr_common_prefix_and_live_capabilities():
    """稳定前缀=连续两窗公共前缀;Qwen3ASRLiveSTT 声明流式+interim 能力。"""
    from agent_runtime.providers.livekit_plugins import Qwen3ASRLiveSTT, Qwen3ASRSTT, _common_prefix

    assert _common_prefix("", "") == ""
    assert _common_prefix("有冇人知道", "有冇人知道灣仔") == "有冇人知道"
    assert _common_prefix("我係想問下你哋", "我係想問下你哋公司") == "我係想問下你哋"
    assert _common_prefix("ABC", "ABX") == "AB"

    inner = Qwen3ASRSTT(base_url="http://127.0.0.1:8787")
    assert inner.capabilities.streaming is False  # 裸 Qwen3-ASR 仍是离线式

    class _FakeVAD:
        pass

    live = Qwen3ASRLiveSTT(stt_=inner, vad_=_FakeVAD())
    assert live.capabilities.streaming is True
    assert live.capabilities.interim_results is True
    # recognize() 委托内层(保官方重试/metrics)。
    assert live._recognize_impl is not None


def test_sidecar_partial_runs_and_caps_buffer():
    """sidecar 滑窗 partial:mlx 后端注入 mock 模型——>0.6s 触发推理、buffer 裁剪生效、
    忙时跳过不叠窗;finish 仍走整句(partial 状态与 final 分离)。"""
    mod = _load_sidecar_app()

    calls = {"n": 0, "last_pcm_len": 0}

    class _FakeModel:
        def generate(self, wav, language=None, max_tokens=256):
            calls["n"] += 1
            calls["last_pcm_len"] = len(wav)
            out = types.SimpleNamespace(text="有冇人知道灣仔活道係點去㗎", language=["Cantonese"])
            return out

    svc = mod.ASRService()
    svc._model = _FakeModel()

    sid = svc.start(language="cantonese")
    # 2s 音频(16k mono = 32000 B/s)。force 节流窗口为 0,直接可推理。
    pcm = b"\x00\x00" * (32000 * 2)
    svc._sessions[sid]["last_partial_at"] = 0.0
    out = svc.chunk(sid, pcm)
    assert out["partial"] is True
    assert out["text"] == "有冇人知道灣仔活道係點去㗎"
    assert calls["n"] == 1

    # 忙时跳过:把推理锁占住 → chunk 返回缓存结果、唔叠新推理。
    from threading import Lock

    svc._sessions[sid]["inf_lock"] = Lock()
    svc._sessions[sid]["inf_lock"].acquire()
    try:
        out2 = svc.chunk(sid, pcm)
        assert out2["text"] == "有冇人知道灣仔活道係點去㗎"  # cached
        assert calls["n"] == 1
    finally:
        svc._sessions[sid]["inf_lock"].release()

    # 超长 buffer 裁剪:40s 音频 partial 只喂最近 ~25s(裁到 PARTIAL_MAX_SEC)。
    mod.PARTIAL_MAX_SEC = 25  # 收紧便于断言
    long_pcm = b"\x00\x00" * (32000 * 40)
    svc._sessions[sid]["chunks"] = bytearray(long_pcm)
    svc._sessions[sid]["last_partial_at"] = 0.0
    out3 = svc.chunk(sid, long_pcm[: 32000 * 40])
    # 40s 超上限:喂给 generate 的应为 25s = 800000 B pcm16 → 400000 float32 samples。
    assert calls["last_pcm_len"] == 32000 * 25 // 2

    # finish 整句兜底(partial 状态在 finish 时唔干扰)。
    final = svc.finish(sid)
    assert final["partial"] is False and final["text"] == "有冇人知道灣仔活道係點去㗎"


def test_session_report_endpoint_and_real_usage():
    """session-report 端点入库 + usage 聚合读真数据(唔伪造轮次)。"""
    from fastapi.testclient import TestClient

    from control_plane.main import app

    with TestClient(app) as client:
        created = client.post(
            "/api/calls",
            json={"account_id": "acc-001", "object_id": "obj-1", "persona_id": "p-1", "mode": "simulation"},
        ).json()
        call_id = created["id"]
        report = {
            "job_id": "j1", "room": call_id,
            "usage": [{"type": "llm_usage", "provider": "openai", "model": "m", "input_tokens": 1000, "output_tokens": 250}],
        }
        r = client.post(f"/api/calls/{call_id}/session-report", json=report)
        assert r.status_code == 200 and r.json()["stored"] is True
        got = client.get(f"/api/calls/{call_id}").json()
        assert json.loads(got["session_report"])["usage"][0]["input_tokens"] == 1000
        # usage 聚合读真数据:1000+250,唔等于轮次数。
        u = client.get("/api/reports/usage?account_id=acc-001").json()
        assert u["llm_tokens"] == 1250
        assert u["llm_tokens_estimated_calls"] == 0


def test_livekit_webhook_redispatch_gate():
    """webhook 崩溃补位:只对 A 线 agent(bok-voice)的 participant_left 触发重派。"""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from control_plane.main import app

    with TestClient(app) as client:
        # 非 participant_left → 忽略
        r = client.post("/api/webhook/livekit", json={"event": "room_started", "room": {"name": "r1"}})
        assert r.json()["handled"] is False
        # 非我方 agent 离开 → 忽略(真人断开不重派)
        r = client.post("/api/webhook/livekit", json={"event": "participant_left", "room": {"name": "r1"}, "participant": {"identity": "me-r1"}})
        assert r.json()["handled"] is False
        # bok-voice 离开 → 触发重派(用 patch 拦住真 LiveKitAPI 连接)
        with patch("control_plane.main.os.environ.get", side_effect=lambda k, d=None: {"LIVEKIT_API_KEY": "k", "LIVEKIT_API_SECRET": "s", "LIVEKIT_URL": "ws://127.0.0.1:7880"}.get(k, d)):
            from livekit import api as lk_api

            with patch.object(lk_api, "LiveKitAPI") as mk:
                r = client.post("/api/webhook/livekit", json={"event": "participant_left", "room": {"name": "call-x"}, "participant": {"identity": "bok-voice"}})
        assert r.json()["handled"] is True
