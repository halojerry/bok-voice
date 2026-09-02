"""Summarizer 的本机 LLM 回退与 fallback 行为。

背景：设置页保存的 llm 卡片可能是空 base_url + 占位 model="local"，
只读 settings 会让结算摘要/蒸馏打到空地址或 mlx_lm 的 model=local(404)，
导致 new_topics/insight 从不写入。修复后 Summarizer 在 settings 缺省/占位时
回退读 MLX_LLM_BASE_URL / MLX_LLM_MODEL（与 agent 的 MlxLlmLLM 同源）。
"""

from __future__ import annotations

import os

import httpx
import pytest

from control_plane.summarize import Summarizer


def _turn(role: str, text: str):
    class _T:
        def __init__(self, role: str, transcript: str):
            self.role = role
            self.transcript = transcript

    return _T(role, text)


_TURNS = [_turn("user", "你好，我想了解一下你们的产品。"), _turn("assistant", "好的，我哋嘅產品主打防水同防滑。")]


def _call() -> dict:
    return {"object_id": "obj-1", "account_id": "acc-001"}


def test_settings_model_local_falls_back_to_env(monkeypatch):
    """settings 给占位 model='local' + 空 base_url 时，须用 env 注入的真实 MLX 地址。"""
    monkeypatch.delenv("MLX_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("MLX_LLM_MODEL", raising=False)
    monkeypatch.setenv("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1")
    monkeypatch.setenv("MLX_LLM_MODEL", "/models/Huihui-Qwen3.5-9B")

    settings = {"llm": {"provider": "local_openai", "base_url": "", "model": "local", "api_key": "mlx"}}

    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"summary":"客户关注防水","new_topics":[{"topic":"防水性能","summary":"关注防水"}],'
                            '"insight":{"statement":"客户普遍关注防水","confidence":0.8,"language":"zh"}}'
                        }
                    }
                ]
            }

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["payload_model"] = json.get("model") if isinstance(json, dict) else None
        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)

    summ = Summarizer()
    result = summ.build(_TURNS, _call(), settings)

    # 请求确实打到了 env 注入的 MLX 端点，且 model 用 env 真实路径而非 "local"。
    assert captured["url"] == "http://127.0.0.1:1235/v1/chat/completions"
    assert captured["payload_model"] == "/models/Huihui-Qwen3.5-9B"
    assert result["summary"] == "客户关注防水"
    assert result["new_topics"][0]["topic"] == "防水性能"
    assert result["insight"]["statement"] == "客户普遍关注防水"


def test_no_env_no_model_falls_back(monkeypatch):
    """settings 无 base_url/model 且 env 也没注入时，须回退纯指标 fallback（不抛错）。"""
    monkeypatch.delenv("MLX_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("MLX_LLM_MODEL", raising=False)

    settings = {"llm": {"provider": "local_openai", "base_url": "", "model": ""}}
    result = Summarizer().build(_TURNS, _call(), settings)

    assert result["summary"]
    assert result["new_topics"] == []
    assert result["insight"] is None


def test_explicit_settings_used(monkeypatch):
    """settings 显式给了 base_url + 非占位 model 时，直接用 settings（不回退 env）。"""
    monkeypatch.delenv("MLX_LLM_BASE_URL", raising=False)
    monkeypatch.setenv("MLX_LLM_MODEL", "/env/model")

    settings = {"llm": {"provider": "deepseek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"}}
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": '{"summary":"s","new_topics":[],"insight":null}'}}]}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)

    result = Summarizer().build(_TURNS, _call(), settings)
    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert result["summary"] == "s"
