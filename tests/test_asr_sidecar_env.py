"""ASR sidecar 启动 env 前缀透传（2026-10-02 编排审计第二波 · PR-E item 1）。

背景：TTS sidecar 启动 env 已走 `_qwen3_tts_sidecar_env` 前缀整族透传
（prod 封闭 env 下调参键可达）；ASR 启动点还是硬编码最小集——sidecar 实际读
~25 个 `QWEN3_ASR_*` 键（SAMPLE_RATE/INC_MIN_CPS/PARTIAL_*/FINISH_*/TRIM_*/
CONFIDENCE…），调参键 dev 靠 `_start_proc` merge `os.environ` 才活、prod 结构性
死门。本文件钉新 `_qwen3_asr_sidecar_env` 的成员/优先级语义 + 启动点接线。

约定：只读函数行为，不碰真进程/真 app-data。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import bok  # noqa: E402


def test_prefix_family_passthrough(monkeypatch) -> None:
    """`QWEN3_ASR_*` 整族透传（sidecar 读面 25 键从此全部可达）。"""
    monkeypatch.setenv("QWEN3_ASR_SAMPLE_RATE", "8000")
    monkeypatch.setenv("QWEN3_ASR_CONFIDENCE", "0")
    monkeypatch.setenv("QWEN3_ASR_FINISH_LOCK_WAIT", "1.0")
    monkeypatch.setenv("QWEN3_ASR_PARTIAL_MAX_SEC", "12")
    env = bok._qwen3_asr_sidecar_env(
        {"QWEN3_ASR_MODEL": "/models/asr", "QWEN3_ASR_BACKEND": "mlx"}
    )
    assert env["QWEN3_ASR_SAMPLE_RATE"] == "8000"
    assert env["QWEN3_ASR_CONFIDENCE"] == "0"
    assert env["QWEN3_ASR_FINISH_LOCK_WAIT"] == "1.0"
    assert env["QWEN3_ASR_PARTIAL_MAX_SEC"] == "12"


def test_base_keys_win_and_empty_values_skipped(monkeypatch) -> None:
    """必填键优先（setdefault 语义）；空值不透传（TTS 先例同款）。"""
    monkeypatch.setenv("QWEN3_ASR_MODEL", "/env/model")
    monkeypatch.setenv("QWEN3_ASR_SAMPLE_RATE", "")
    env = bok._qwen3_asr_sidecar_env({"QWEN3_ASR_MODEL": "/base/model"})
    assert env["QWEN3_ASR_MODEL"] == "/base/model"
    assert "QWEN3_ASR_SAMPLE_RATE" not in env


def test_other_prefixes_not_leaked(monkeypatch) -> None:
    """TTS 等近邻前缀不串门（sidecar 各自的族互不污染）。"""
    monkeypatch.setenv("QWEN3_TTS_STREAM_INTERVAL", "0.1")
    monkeypatch.setenv("BOK_TTS_BOTH_MODELS", "1")
    env = bok._qwen3_asr_sidecar_env({"QWEN3_ASR_MODEL": "/m"})
    assert "QWEN3_TTS_STREAM_INTERVAL" not in env
    assert "BOK_TTS_BOTH_MODELS" not in env


def test_bok_asr_engine_and_device_stay_explicit() -> None:
    """BOK_ASR_ENGINE / QWEN3_ASR_DEVICE 的显式处理保留在启动点（不进 helper）。"""
    src = inspect.getsource(bok._cmd_up_services)
    assert 'asr_env["BOK_ASR_ENGINE"]' in src, "BOK_ASR_ENGINE 显式注入不得丢"
    assert 'asr_env["QWEN3_ASR_DEVICE"]' in src, "Windows/transformers 的 DEVICE 分支不得丢"
    # 启动点必须把 hand-built dict 过 helper（前缀整族透传的接线点）。
    assert "env=_qwen3_asr_sidecar_env(asr_env)" in src, (
        "ASR 启动点必须经 _qwen3_asr_sidecar_env 包裹（镜像 TTS 先例）")
