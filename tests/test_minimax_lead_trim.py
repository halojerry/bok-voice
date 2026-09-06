"""MiniMax 首包前导静音修剪 + fade-in（RealtimeTTS base_engine 同款借鉴）。

零静音必须字节不变（零害）；剪过才做 15ms 淡入防剪口咔哒；上限防误剪气声起句。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.providers.livekit_plugins import _trim_lead_silence  # noqa: E402

SR = 24000
_FRAME_BYTES = SR // 50 * 2  # 20ms s16le mono


def _tone_ms(ms: int, amp: int = 3000) -> bytes:
    """余弦波(s16le，首样本=峰值)，RMS≈amp/√2 远超静音门限。"""
    n = SR * ms // 1000
    out = bytearray()
    for i in range(n):
        v = int(amp * math.cos(2 * math.pi * 220 * i / SR))
        out += v.to_bytes(2, "little", signed=True)
    return bytes(out)


def _silence_ms(ms: int) -> bytes:
    return bytes(SR * ms // 1000 * 2)


def test_no_leading_silence_returns_bytes_unchanged():
    pcm = _tone_ms(60)
    out, trimmed = _trim_lead_silence(pcm, SR)
    assert trimmed == 0
    assert out == pcm, "零静音必须原样返回(字节不变,零害)"


def test_trims_frame_aligned_leading_silence_and_fades_head():
    pcm = _silence_ms(60) + _tone_ms(60)
    out, trimmed = _trim_lead_silence(pcm, SR)
    assert trimmed == 60
    assert out == _tone_ms(60)[: len(out)] or len(out) == len(_tone_ms(60))
    # 剪口做了 15ms 线性淡入:首样本幅度被压低,且单调递增
    first = int.from_bytes(out[0:2], "little", signed=True)
    raw_first = int.from_bytes(_tone_ms(60)[0:2], "little", signed=True)
    assert abs(first) < abs(raw_first)
    nf = SR * 15 // 1000
    mid = int.from_bytes(out[(nf // 2) * 2 : (nf // 2) * 2 + 2], "little", signed=True)
    assert abs(mid) <= abs(raw_first)


def test_all_silence_trims_to_empty():
    out, trimmed = _trim_lead_silence(_silence_ms(100), SR)
    assert trimmed == 100
    assert out == b""


def test_cap_limits_trim_to_max_ms():
    out, trimmed = _trim_lead_silence(_silence_ms(400) + _tone_ms(60), SR, max_ms=200)
    assert trimmed == 200
    # 残余 200ms 静音 + 60ms 语音,原样保留(残余留给下次首帧检查继续收)
    assert len(out) == (200 + 60) * SR // 1000 * 2


def test_partial_frame_tail_kept():
    """不足一帧(20ms)的尾块唔动——只按整帧扫。"""
    pcm = _tone_ms(30) + b"\x01\x00"  # 30ms 语音 + 1 样本尾巴
    out, trimmed = _trim_lead_silence(pcm, SR)
    assert trimmed == 0
    assert out == pcm
