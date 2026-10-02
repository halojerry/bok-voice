"""sidecar 长文拆句护栏单测（2026-09-28 生产事故防御）。

实测定性：分句任务跨句漂移 -8.9%/-11.7%（自然方差级，非事故）；真事故是
**长文单任务**——客户端 QWEN3_TTS_MAX_TASK_AUDIO_SEC 15s cap 会拦腰截断
（91 字整段第 4 句 4.14s→1.52s）。护栏=sidecar 在 synthesize_chunks 入口把
>N 字输入按句界拆子段拼接（QWEN3_TTS_SPLIT_MAX_CHARS，默认 60，0=关）。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "qwen3-tts-sidecar"))
pytest.importorskip("fastapi")
_mod = importlib.import_module("app")
_split_long_text = _mod._split_long_text


def test_short_text_passthrough():
    assert _split_long_text("你好。", 60) == ["你好。"]
    assert _split_long_text("", 60) == []


def test_long_text_split_at_sentence_bounds():
    text = (
        "不好意思，您的包裹我们这边已经加急处理了。"
        "两三天内就会有结果，请您耐心等待一下。"
        "赔偿方案是根据物品实际价值来定的，最多可以赔到三倍运费。"
        "到时候会有专员主动联系您，跟您确认具体的细节。"
    )
    parts = _split_long_text(text, 60)
    assert len(parts) >= 2
    assert all(len(p) <= 60 for p in parts)
    assert "".join(parts) == text  # 无损：保标点、保顺序


def test_split_prefers_sentence_bounds_over_hard_cut():
    text = "第一句比较长一些呢。" + "很短。" * 30 + "结尾也要完整。"
    parts = _split_long_text(text, 40)
    assert parts[0].endswith("。")  # 首段在句界断开
    assert "".join(parts) == text


def test_runon_text_hard_split():
    text = "一" * 150  # 无标点长串
    parts = _split_long_text(text, 60)
    assert all(len(p) <= 60 for p in parts)
    assert "".join(parts) == text
