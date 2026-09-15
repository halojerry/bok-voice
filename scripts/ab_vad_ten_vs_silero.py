#!/usr/bin/env python3
"""TEN VAD vs inference.VAD(silero) 语音端点 A/B。

对同一 wav 分别用两条 VAD 管线做流式判定，对比：
- 语音段 END 判定时刻（对单句 wav，以「文件末样本」为真值近似）
- 分段数（过劈检测，长音频）
- 实时率 RTF（CPU 单核可负担性）

用法:
    .venv312/bin/python scripts/ab_vad_ten_vs_silero.py tests/fixtures/audio/cantonese.wav ...
不带参数则跑默认资产集。只读评估脚本,不改任何运行时代码。
"""
from __future__ import annotations

import asyncio
import sys
import time
import wave
from pathlib import Path

import numpy as np

SR = 16000
SILERO_FRAME = 512          # 32ms @16k
TEN_HOP = 256               # 16ms @16k (TenVad 默认)
PROD_MIN_SILENCE = 0.45     # A 线生产基线
RAW_MIN_SILENCE = 0.05      # raw 档:消除静音策略差,只看引擎自身检测响应

DEFAULT_ASSETS = [
    "tests/fixtures/audio/zh.wav",
    "tests/fixtures/audio/cantonese.wav",
    "tests/fixtures/audio/en.wav",
    "tests/fixtures/audio/e2e_multi/cust_00.wav",
    "tests/fixtures/audio/e2e_multi/cust_03.wav",
    "tests/fixtures/audio/e2e_multi/cust_07.wav",
]


def load_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        assert w.getsampwidth() == 2, f"{path}: 期望 16bit"
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if channels > 1:
        pcm = pcm.reshape(-1, channels)[:, 0]
    if rate != SR:
        # 线性重采样(VAD 对比足够,不引 librosa 重依赖)
        n_out = int(round(len(pcm) * SR / rate))
        pcm = np.interp(
            np.linspace(0.0, len(pcm) - 1, n_out),
            np.arange(len(pcm)),
            pcm.astype(np.float32),
        ).astype(np.int16)
    return pcm


def rtc_frame(pcm_i16: np.ndarray, samples: int) -> "object":
    import livekit.rtc as rtc

    chunk = pcm_i16[: samples * 1]
    return rtc.AudioFrame(
        data=chunk.tobytes(),
        sample_rate=SR,
        num_channels=1,
        samples_per_channel=samples,
    )


async def run_silero(pcm: np.ndarray, min_silence: float) -> dict:
    """inference.VAD 流式跑全量音频,返回段边界与耗时。"""
    from livekit.agents import inference

    vad = inference.VAD(min_silence_duration=min_silence, activation_threshold=0.5)
    stream = vad.stream()
    frame = SILERO_FRAME
    t0 = time.perf_counter()
    total_inf_ms = 0.0

    async def pump() -> None:
        nonlocal total_inf_ms
        for i in range(0, len(pcm) - frame + 1, frame):
            stream.push_frame(rtc_frame(pcm[i : i + frame], frame))
        stream.end_input()

    ends: list[int] = []
    starts: list[int] = []

    async def drain() -> None:
        nonlocal total_inf_ms
        async for ev in stream:
            if ev.type.name == "END_OF_SPEECH":
                ends.append(int(ev.samples_index))
            elif ev.type.name == "START_OF_SPEECH":
                starts.append(int(ev.samples_index))
            total_inf_ms += getattr(ev, "inference_duration", 0.0) * 1000

    await asyncio.gather(pump(), drain())
    wall = time.perf_counter() - t0
    return {
        "engine": f"silero(ms={min_silence})",
        "starts": starts,
        "ends": ends,
        "rtf": wall / (len(pcm) / SR),
        "infer_ms_total": total_inf_ms,
    }


def run_ten(pcm: np.ndarray, threshold: float = 0.5) -> dict:
    """TenVad 逐帧跑全量音频,flag 下降沿=语音段结束。"""
    from ten_vad import TenVad

    vad = TenVad(hop_size=TEN_HOP, threshold=threshold)
    flags: list[int] = []
    t0 = time.perf_counter()
    buf = pcm.astype(np.int16)
    n_frames = len(buf) // TEN_HOP
    for i in range(n_frames):
        hop = buf[i * TEN_HOP : (i + 1) * TEN_HOP]
        _, flag = vad.process(hop)
        flags.append(int(flag))
    wall = time.perf_counter() - t0

    ends: list[int] = []
    starts: list[int] = []
    prev = 0
    for idx, f in enumerate(flags):
        if f == 1 and prev != 1:
            starts.append(idx * TEN_HOP)
        elif f != 1 and prev == 1:
            ends.append(idx * TEN_HOP)
        prev = f
    if prev == 1:
        ends.append(n_frames * TEN_HOP)
    return {
        "engine": "ten_vad(th=0.5)",
        "starts": starts,
        "ends": ends,
        "rtf": wall / (len(pcm) / SR),
        "infer_ms_total": wall * 1000,
    }


def seg_table(ends: list[int], audio_len_ms: int) -> str:
    if not ends:
        return "  (无段)"
    return "  " + " ".join(f"{e / SR * 1000:.0f}" for e in ends)


def compare(pcm: np.ndarray, name: str) -> None:
    audio_len_ms = len(pcm) / SR * 1000
    print(f"\n=== {name}  ({audio_len_ms:.0f}ms, {len(pcm)/SR:.1f}s) ===")

    res = {}
    for tag, fn in (
        ("raw", lambda: asyncio.run(_both_raw(pcm))),
        ("prod", lambda: asyncio.run(_both_prod(pcm))),
    ):
        res[tag] = fn()

    for tag, pair in res.items():
        s, t = pair
        print(f"[{tag}] {s['engine']}: 段数={len(s['ends'])} rtf={s['rtf']:.3f} ends_ms={seg_table(s['ends'], audio_len_ms)}")
        print(f"[{tag}] {t['engine']}: 段数={len(t['ends'])} rtf={t['rtf']:.3f} ends_ms={seg_table(t['ends'], audio_len_ms)}")

    # 单句短音频:以文件末尾为真值近似,比较 raw 档 END 相对文件末尾的提前量
    if audio_len_ms < 15000:
        s_raw, t_raw = res["raw"]
        if s_raw["ends"] and t_raw["ends"]:
            s_lag = s_raw["ends"][-1] / SR * 1000 - audio_len_ms
            t_lag = t_raw["ends"][-1] / SR * 1000 - audio_len_ms
            print(f"[结论] raw 档 END 相对文件末尾: silero {s_lag:+.0f}ms vs ten {t_lag:+.0f}ms → ten 提前 {s_lag - t_lag:.0f}ms")


async def _both_raw(pcm: np.ndarray) -> tuple[dict, dict]:
    s = await run_silero(pcm, RAW_MIN_SILENCE)
    t = await asyncio.to_thread(run_ten, pcm)
    return s, t


async def _both_prod(pcm: np.ndarray) -> tuple[dict, dict]:
    s = await run_silero(pcm, PROD_MIN_SILENCE)
    t = await asyncio.to_thread(run_ten, pcm)
    return s, t


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    paths = sys.argv[1:] or [str(root / p) for p in DEFAULT_ASSETS]
    for p in paths:
        pcm = load_wav(p)
        compare(pcm, Path(p).name)


if __name__ == "__main__":
    main()
