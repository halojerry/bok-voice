"""MT 模型路径门禁单测：非法 MT_LLM_MODEL 不得透传 mlx_lm server。

mlx_lm server 收到 repo-id/占位符等非本地路径会尝试 HF hub 解析，断网时
持锁挂死整个 server（B 线同传 2/8 FAIL 实弹，MT :1236 全灭）——装配侧
_build_llm_provider 必须跳过 MT 分支走既有回退链。env 断言全部走
monkeypatch（终了自动还原，唔污染其他测试）。
"""

from __future__ import annotations

import os

from agent_runtime import interpret

# 官方推荐采样 env（MT 分支经 MlxLlmLLM 构造参数下发、唔写进程 env——评审 P2-3；
# 跳 MT 档同样唔得沾这些键，采样档属 MT 专有唔好污染回退主 LLM）。
_MT_SAMPLING_ENVS = ("LLM_TEMPERATURE", "LLM_TOP_P", "LLM_TOP_K", "LLM_REPETITION_PENALTY")

_MT_ENV_KEYS = ("MT_LLM_BASE_URL", "MT_LLM_MODEL", "MLX_LLM_MODEL") + _MT_SAMPLING_ENVS


def test_mt_model_valid_branches(tmp_path):
    """纯函数三分支：真实绝对路径=True；空串/repo-id(不存在)=False。"""
    real = tmp_path / "mt-model"
    real.mkdir()
    assert interpret._mt_model_valid(str(real)) is True
    assert interpret._mt_model_valid("") is False
    assert interpret._mt_model_valid("mlx-community/foo") is False
    # 带空白 strip 后仍认；相对路径（即使存在）唔算——server 侧只认绝对路径。
    assert interpret._mt_model_valid(f"  {real}  ") is True
    assert interpret._mt_model_valid("relative/mt-model") is False


def test_build_llm_provider_invalid_mt_model_falls_back(monkeypatch, tmp_path, capsys):
    """base 有值但 model 非法（repo-id/空）→ 唔走 MT，落既有回退链。

    DeepSeek 回退可能因缺 key 再落主 LLM 分支——只断言「不是 MT 内芯」，
    唔钉死具体回退档。采样档只在 MT 真正生效时经构造参数下发（唔写 env），
    非法跳过档唔得污染回退 LLM。"""
    from agent_runtime.providers.livekit_plugins import StatelessMTLLM

    for key in _MT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MT_LLM_BASE_URL", "http://127.0.0.1:1236/v1")
    monkeypatch.setenv("MT_LLM_MODEL", "mlx-community/Hy-MT2-1.8B-Abliterated-8bit")

    provider = interpret._build_llm_provider({}, "cantonese")
    assert not isinstance(provider, StatelessMTLLM)
    assert not isinstance(interpret._build_llm_provider({}, "zh"), StatelessMTLLM)
    # 采样档唔执行（回退主 LLM 唔好吃 MT 档采样）。
    for key in _MT_SAMPLING_ENVS:
        assert key not in os.environ
    # 日志留值（值或空），方便查 env。
    out = capsys.readouterr().out
    assert "[interp] mt model invalid" in out
    assert "mlx-community/Hy-MT2-1.8B-Abliterated-8bit" in out

    # 空串同非法：旧代码空值也透传（回落占位符 "local"=挂死类输入），现同走回退。
    monkeypatch.setenv("MT_LLM_MODEL", "")
    assert not isinstance(interpret._build_llm_provider({}, "cantonese"), StatelessMTLLM)
    assert "[interp] mt model invalid ('') " in capsys.readouterr().out

    # 超 60 字截断，唔刷屏。
    long_val = "mlx-community/" + "x" * 80
    monkeypatch.setenv("MT_LLM_MODEL", long_val)
    interpret._build_llm_provider({}, "cantonese")
    out = capsys.readouterr().out
    assert long_val not in out
    assert long_val[:60] in out


def test_build_llm_provider_valid_mt_model_uses_mt(monkeypatch, tmp_path):
    """MT_LLM_MODEL 为真实存在的本地绝对路径 → MT 分支照常生效（StatelessMTLLM）。"""
    from agent_runtime.providers.livekit_plugins import MlxLlmLLM, StatelessMTLLM

    for key in _MT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    mt_model = tmp_path / "Hy-MT2-8bit"
    mt_model.mkdir()
    monkeypatch.setenv("MT_LLM_BASE_URL", "http://127.0.0.1:1236/v1")
    monkeypatch.setenv("MT_LLM_MODEL", str(mt_model))

    provider = interpret._build_llm_provider({}, "cantonese")
    assert isinstance(provider, StatelessMTLLM)
    assert isinstance(provider._inner, MlxLlmLLM)
    assert provider._inner._opts.model == str(mt_model)


def test_mt_model_valid_rejects_existing_relative_path(tmp_path, monkeypatch):
    """钉死 is_absolute 半边：相对路径即使真实存在也唔算——纯存在性实现会放行,
    而「相对但存在」(cd 到模型目录跑 worker)正是 HF hub 解析挂死类输入。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel-mt").mkdir()
    assert interpret._mt_model_valid("rel-mt") is False


def test_mt_sampling_nonfinite_falls_back(monkeypatch):
    """inf/nan/1e400 过得了 float() 但属非法采样档——回落推荐值,防 int(top_k)
    在装配期 OverflowError/ValueError 崩掉整条 B 线 job;合法显式 env 照常优先。"""
    monkeypatch.setenv("LLM_TEMPERATURE", "inf")
    assert interpret._mt_sampling("LLM_TEMPERATURE", 0.7) == 0.7
    monkeypatch.setenv("LLM_TOP_K", "nan")
    assert interpret._mt_sampling("LLM_TOP_K", 20) == 20
    monkeypatch.setenv("LLM_REPETITION_PENALTY", "1e400")
    assert interpret._mt_sampling("LLM_REPETITION_PENALTY", 1.05) == 1.05
    monkeypatch.setenv("LLM_TEMPERATURE", "0.5")
    assert interpret._mt_sampling("LLM_TEMPERATURE", 0.7) == 0.5
