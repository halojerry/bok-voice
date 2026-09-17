"""后台重活专线(:1237 settle/judge)接线单测(2026-09-17)。

settle 纪要/知识蒸馏与 flow judge 指到 :1237 的 9B——延迟不敏感岗位吃大模型
质量,与活通话的 :1235 分进程。模型缺失时整条链路静默回退 :1235(env 不下发),
这是「可选增强」契约:任何断言都不得依赖真实模型在盘。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import bok  # noqa: E402


def test_settle_model_env_override(monkeypatch):
    """BOK_SETTLE_LLM_MODEL 显式覆盖 > MODELS 表;不存在的表条目回空串。"""
    monkeypatch.setenv("BOK_SETTLE_LLM_MODEL", "/tmp/fake-settle-model")
    assert bok._settle_llm_model(bok.MODELS["mac"]) == "/tmp/fake-settle-model"
    monkeypatch.delenv("BOK_SETTLE_LLM_MODEL", raising=False)
    # 表里 settle 指向 huihui 9B(lmstudio 布局);无盘环境 model_path 回 lmstudio
    # 期望路径字符串(不 raise)——断言只锁「非空/指向 settle repo」。
    path = bok._settle_llm_model({"settle": "huihui-ai/Huihui-Qwen3.5-9B-abliterated-mlx-4bit"})
    assert "Huihui-Qwen3.5-9B" in path
    assert bok._settle_llm_model({}) == "" or "settle" not in str(bok.MODELS)


def test_apply_judge_env_present_and_absent(monkeypatch, tmp_path):
    """模型在盘 → 注入 FLOW_JUDGE_*(:1237);不在盘 → 不注入(零配置回退 :1235)。"""
    fake = tmp_path / "fake-settle-model"
    fake.mkdir()
    monkeypatch.setenv("BOK_SETTLE_LLM_MODEL", str(fake))
    env: dict[str, str] = {}
    bok._apply_judge_env(env, bok.MODELS["mac"])
    assert env.get("FLOW_JUDGE_LLM_BASE_URL") == "http://127.0.0.1:1237/v1"
    assert env.get("FLOW_JUDGE_LLM_MODEL") == str(fake)

    monkeypatch.setenv("BOK_SETTLE_LLM_MODEL", str(tmp_path / "definitely-not-on-disk-xyz"))
    env2: dict[str, str] = {}
    bok._apply_judge_env(env2, bok.MODELS["mac"])
    assert "FLOW_JUDGE_LLM_BASE_URL" not in env2
    assert "FLOW_JUDGE_LLM_MODEL" not in env2


def test_control_plane_env_carries_settle(monkeypatch, tmp_path):
    """CP env 带 BOK_SETTLE_*:Summarizer 专线入口(bok.py 注入面)。"""
    fake = tmp_path / "fake-settle-model"
    fake.mkdir()
    monkeypatch.setenv("BOK_SETTLE_LLM_MODEL", str(fake))
    env = bok._control_plane_env(tmp_path / "x.db")
    assert env.get("BOK_SETTLE_LLM_BASE_URL") == "http://127.0.0.1:1237/v1"
    assert env.get("BOK_SETTLE_LLM_MODEL") == str(fake)


def test_prompt_cache_bytes_tiers(monkeypatch):
    """缓存档位:显式 env > ≥32GB 12GB > 小内存 6GB;探测失败安全落 6GB。"""
    monkeypatch.setenv("BOK_LLM_PROMPT_CACHE_BYTES", "8GB")
    assert bok._default_prompt_cache_bytes() == "8GB"
    monkeypatch.delenv("BOK_LLM_PROMPT_CACHE_BYTES", raising=False)
    monkeypatch.setattr(bok, "_physical_mem_gib", lambda: 48.0)
    assert bok._default_prompt_cache_bytes() == "12GB"
    monkeypatch.setattr(bok, "_physical_mem_gib", lambda: 16.0)
    assert bok._default_prompt_cache_bytes() == "6GB"
    monkeypatch.setattr(bok, "_physical_mem_gib", lambda: 0.0)
    assert bok._default_prompt_cache_bytes() == "6GB"


def test_settle_in_optional_models():
    """settle 是可选增强(首启向导不门禁,缺失回退 :1235)——防有人误挪进门禁集。"""
    assert "settle" in bok.OPTIONAL_MODELS
    assert bok.MODELS["mac"].get("settle", "").endswith("9B-abliterated-mlx-4bit")
