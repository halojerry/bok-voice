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
from _bokpatch import patch_bok  # noqa: E402


def test_settle_model_env_override(monkeypatch):
    """BOK_SETTLE_LLM_MODEL 显式覆盖 > MODELS 表;不存在的表条目回空串。"""
    monkeypatch.setenv("BOK_SETTLE_LLM_MODEL", "/tmp/fake-settle-model")
    assert bok.models._settle_llm_model(bok.models.MODELS["mac"]) == "/tmp/fake-settle-model"
    monkeypatch.delenv("BOK_SETTLE_LLM_MODEL", raising=False)
    # 表里 settle 指向 huihui 9B(lmstudio 布局);无盘环境 model_path 回 lmstudio
    # 期望路径字符串(不 raise)——断言只锁「非空/指向 settle repo」。
    path = bok.models._settle_llm_model({"settle": "huihui-ai/Huihui-Qwen3.5-9B-abliterated-mlx-4bit"})
    assert "Huihui-Qwen3.5-9B" in path
    assert bok.models._settle_llm_model({}) == "" or "settle" not in str(bok.models.MODELS)


def test_apply_judge_env_present_and_absent(monkeypatch, tmp_path):
    """BOK_DEV_9B=1 且模型在盘 → 注入 FLOW_JUDGE_*(:1237);不在盘 → 不注入
    (零配置回退 :1235)。默认(9B 后端化,2026-09-25)不注入——9B 不随栈常驻。"""
    fake = tmp_path / "fake-settle-model"
    fake.mkdir()
    monkeypatch.setenv("BOK_SETTLE_LLM_MODEL", str(fake))
    monkeypatch.setenv("BOK_DEV_9B", "1")
    env: dict[str, str] = {}
    bok.env._apply_judge_env(env, bok.models.MODELS["mac"])
    assert env.get("FLOW_JUDGE_LLM_BASE_URL") == "http://127.0.0.1:1237/v1"
    assert env.get("FLOW_JUDGE_LLM_MODEL") == str(fake)

    monkeypatch.setenv("BOK_SETTLE_LLM_MODEL", str(tmp_path / "definitely-not-on-disk-xyz"))
    env2: dict[str, str] = {}
    bok.env._apply_judge_env(env2, bok.models.MODELS["mac"])
    assert "FLOW_JUDGE_LLM_BASE_URL" not in env2
    assert "FLOW_JUDGE_LLM_MODEL" not in env2

    # 2026-10-01 P2 翻档:默认(=1)在盘即注入;BOK_DEV_9B=0 显式关才不注入。
    monkeypatch.setenv("BOK_DEV_9B", "0")
    monkeypatch.setenv("BOK_SETTLE_LLM_MODEL", str(fake))
    env3: dict[str, str] = {}
    bok.env._apply_judge_env(env3, bok.models.MODELS["mac"])
    assert "FLOW_JUDGE_LLM_BASE_URL" not in env3
    assert "FLOW_JUDGE_LLM_MODEL" not in env3
    # 缺省(env 不设)=开——模型在盘即注入。
    monkeypatch.delenv("BOK_DEV_9B", raising=False)
    env4: dict[str, str] = {}
    bok.env._apply_judge_env(env4, bok.models.MODELS["mac"])
    assert env4.get("FLOW_JUDGE_LLM_BASE_URL") == "http://127.0.0.1:1237/v1"


def test_control_plane_env_carries_settle(monkeypatch, tmp_path):
    """CP env 带 BOK_SETTLE_*:Summarizer 专线入口(bok.py 注入面);9B 后端化后
    需 BOK_DEV_9B=1 显式开(默认档不注入,Summarizer 回退 MLX)。"""
    fake = tmp_path / "fake-settle-model"
    fake.mkdir()
    monkeypatch.setenv("BOK_SETTLE_LLM_MODEL", str(fake))
    monkeypatch.setenv("BOK_DEV_9B", "1")
    env = bok.env._control_plane_env(tmp_path / "x.db")
    # I1(2026-10-03):queue 拓扑下消费口=前门闸 :1238(reply 插队+GATE 观测)。
    assert env.get("BOK_SETTLE_LLM_BASE_URL") == "http://127.0.0.1:1238/v1"
    assert env.get("BOK_SETTLE_LLM_MODEL") == str(fake)
    # 2026-10-01 P2 翻档:缺省=开(在盘即注入);BOK_DEV_9B=0 显式关。
    monkeypatch.delenv("BOK_DEV_9B", raising=False)
    env_on = bok.env._control_plane_env(tmp_path / "x.db")
    assert env_on.get("BOK_SETTLE_LLM_BASE_URL") == "http://127.0.0.1:1238/v1"
    # queue 代理关=旧形状裸 :1237(I1 同判据)
    monkeypatch.setenv("BOK_LLM_QUEUE_PROXY", "0")
    env_plain = bok.env._control_plane_env(tmp_path / "x.db")
    assert env_plain.get("BOK_SETTLE_LLM_BASE_URL") == "http://127.0.0.1:1237/v1"
    monkeypatch.delenv("BOK_LLM_QUEUE_PROXY", raising=False)
    monkeypatch.setenv("BOK_DEV_9B", "0")
    env_off = bok.env._control_plane_env(tmp_path / "x.db")
    assert "BOK_SETTLE_LLM_BASE_URL" not in env_off
    assert "BOK_SETTLE_LLM_MODEL" not in env_off


def test_prompt_cache_bytes_tiers(monkeypatch):
    """缓存档位:显式 env > 恒 4GB(P1.d 2026-09-29 定档;回 6GB 走 env;探测失败同 4GB)。"""
    monkeypatch.setenv("BOK_LLM_PROMPT_CACHE_BYTES", "8GB")
    assert bok.servers._default_prompt_cache_bytes() == "8GB"
    monkeypatch.delenv("BOK_LLM_PROMPT_CACHE_BYTES", raising=False)
    patch_bok(monkeypatch, "_physical_mem_gib", lambda: 48.0)
    assert bok.servers._default_prompt_cache_bytes() == "4GB"
    patch_bok(monkeypatch, "_physical_mem_gib", lambda: 16.0)
    assert bok.servers._default_prompt_cache_bytes() == "4GB"
    patch_bok(monkeypatch, "_physical_mem_gib", lambda: 0.0)
    assert bok.servers._default_prompt_cache_bytes() == "4GB"


def test_settle_in_optional_models():
    """settle 是可选增强(首启向导不门禁,缺失回退 :1235)——防有人误挪进门禁集。"""
    assert "settle" in bok.models.OPTIONAL_MODELS
    assert bok.models.MODELS["mac"].get("settle", "").endswith("9B-abliterated-mlx-4bit")
