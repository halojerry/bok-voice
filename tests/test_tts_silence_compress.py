"""Qwen3-TTS sidecar silence compression (long inter-sentence pause clamp)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "qwen3-tts-sidecar"))

from app import _SilenceCompressor, SILENCE_FRAME_SEC  # noqa: E402

SR = 24000
FRAME_BYTES = int(SR * 0.02) * 2  # 20ms analysis frame (16-bit mono)
OUT_BYTES = SR * 2  # output chunk size (1s) for test convenience


def _tone(dur_s: float, freq: float = 220.0, amp: float = 0.3) -> bytes:
    n = int(SR * dur_s)
    t = np.arange(n) / SR
    w = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return (w * 32767).astype("<i2").tobytes()


def _silence(dur_s: float) -> bytes:
    return bytes(int(SR * dur_s) * 2)


def _longest_silence(pcm: bytes, gate: float = 0.01) -> float:
    """Longest run of near-silence (seconds) inside a full PCM buffer."""
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    frame_n = int(SR * 0.02)
    longest = 0.0
    cur = 0.0
    for i in range(0, len(samples) - frame_n, frame_n):
        rms = float(np.sqrt(np.mean(samples[i : i + frame_n] ** 2)))
        if rms < gate:
            cur += 0.02
            longest = max(longest, cur)
        else:
            cur = 0.0
    return longest


def _compress_full(pcm: bytes) -> bytes:
    c = _SilenceCompressor(sample_rate=SR)
    out = bytearray()
    for fr in c.push(pcm, len(pcm)):
        out.extend(fr)
    for fr in c.flush(len(pcm)):
        out.extend(fr)
    return bytes(out)


def test_long_gap_is_clamped_while_speech_survives():
    audio = _tone(0.9) + _silence(1.2) + _tone(0.9)
    out = _compress_full(audio)
    # 1.2s silence must be clamped to <= keep (~0.3s) + frame slack.
    longest = _longest_silence(out)
    assert longest <= 0.3 + 3 * SILENCE_FRAME_SEC + 0.05
    # Both speech bursts must still be present (~0.9s each => >=1.7s audio).
    assert len(out) / 2 / SR >= 1.7


def test_short_natural_pause_is_preserved():
    # A 0.15s pause (shorter than keep) must not be trimmed into nothing.
    audio = _tone(0.6) + _silence(0.15) + _tone(0.6)
    out = _compress_full(audio)
    longest = _longest_silence(out)
    assert 0.1 <= longest <= 0.3 + 3 * SILENCE_FRAME_SEC + 0.05


def test_trailing_silence_is_kept():
    audio = _tone(0.5) + _silence(0.8)
    out = _compress_full(audio)
    assert len(out) / 2 / SR >= 0.5 + 0.7  # tail not clipped into speech


def test_streaming_chunks_and_full_buffer_agree():
    # Feed the same audio in awkward chunk sizes and compare to whole-buffer.
    audio = _tone(0.8) + _silence(0.9) + _tone(0.8) + _silence(0.4)
    c = _SilenceCompressor(sample_rate=SR)
    out = bytearray()
    # 123-byte chunks (not a multiple of frame or out size) to exercise buffering.
    for i in range(0, len(audio), 123):
        for fr in c.push(audio[i : i + 123], OUT_BYTES):
            out.extend(fr)
    for fr in c.flush(OUT_BYTES):
        out.extend(fr)
    whole = _compress_full(audio)
    # Both must clamp the interior gap and keep roughly the same total.
    assert abs(len(out) - len(whole)) <= 2 * SR * 2
    assert _longest_silence(bytes(out)) <= 0.3 + 3 * SILENCE_FRAME_SEC + 0.05
