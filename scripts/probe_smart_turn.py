#!/usr/bin/env python3
"""smart-turn v3.2 (CPU int8 ONNX, 8.7MB) 三语 finished/unfinished 探针。

测试集:真值 finished=完整短句(preset-zh/yue/en + e2e_multi/cust_00..09);
unfinished=同音频在 40%/60%/80% 处掐断(掐点在语音中=语义未完)。
输出:按语言的准确率矩阵 + 单次推理延迟 p50/p95(首拍预热除外)。
只读评估脚本,不改任何运行时代码。
"""
from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

import numpy as np

SR = 16000
MODEL = "/tmp/smart-turn/smart-turn-v3.2-cpu.onnx"
ROOT = Path(__file__).resolve().parent.parent

FINISHED_FILES = [
    "data/smoke-out/preset-zh.wav",
    "data/smoke-out/preset-yue.wav",
    "data/smoke-out/preset-en.wav",
    "tests/fixtures/audio/e2e_multi/cust_00.wav",
    "tests/fixtures/audio/e2e_multi/cust_01.wav",
    "tests/fixtures/audio/e2e_multi/cust_02.wav",
    "tests/fixtures/audio/e2e_multi/cust_03.wav",
    "tests/fixtures/audio/e2e_multi/cust_04.wav",
    "tests/fixtures/audio/e2e_multi/cust_05.wav",
    "tests/fixtures/audio/e2e_multi/cust_06.wav",
    "tests/fixtures/audio/e2e_multi/cust_07.wav",
    "tests/fixtures/audio/e2e_multi/cust_08.wav",
    "tests/fixtures/audio/e2e_multi/cust_09.wav",
]


def load_wav_f32(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        rate, ch = w.getframerate(), w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if ch > 1:
        pcm = pcm.reshape(-1, ch)[:, 0]
    x = pcm.astype(np.float32) / 32768.0
    if rate != SR:
        n_out = int(round(len(x) * SR / rate))
        x = np.interp(np.linspace(0.0, len(x) - 1, n_out), np.arange(len(x)), x)
    return x.astype(np.float32)


def truncate_keep_end(x: np.ndarray, n_seconds: int = 8) -> np.ndarray:
    need = n_seconds * SR
    return x[-need:] if len(x) > need else np.pad(x, (need - len(x), 0))


def main() -> None:
    import onnxruntime as ort
    from transformers import WhisperFeatureExtractor

    so = ort.SessionOptions()
    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    so.inter_op_num_threads = 1
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    session = ort.InferenceSession(MODEL, sess_options=so)
    fe = WhisperFeatureExtractor(chunk_length=8)

    def predict(x: np.ndarray) -> tuple[int, float, float]:
        x = truncate_keep_end(x)
        inputs = fe(
            x, sampling_rate=SR, return_tensors="np", padding="max_length",
            max_length=8 * SR, truncation=True, do_normalize=True,
        )
        feats = inputs.input_features.squeeze(0).astype(np.float32)[np.newaxis, ...]
        t0 = time.perf_counter()
        out = session.run(None, {"input_features": feats})
        ms = (time.perf_counter() - t0) * 1000
        prob = float(out[0][0].item())
        return (1 if prob > 0.5 else 0), prob, ms

    predict(np.zeros(SR))  # 预热(首拍含 session/特征器初始化)

    rows: list[dict] = []
    for rel in FINISHED_FILES:
        p = ROOT / rel
        if not p.exists():
            print(f"missing: {rel}")
            continue
        lang = "zh" if "zh" in p.stem else ("yue" if "yue" in p.stem else ("en" if "en" in p.stem else "e2e"))
        full = load_wav_f32(p)
        rows.append({"lang": lang, "label": 1, "cut": 1.00, "x": full, "file": p.name})
        for cut in (0.40, 0.60, 0.80):
            rows.append({"lang": lang, "label": 0, "cut": cut, "x": full[: int(len(full) * cut)], "file": p.name})

    lats: list[float] = []
    hit = {k: [0, 0] for k in ("zh", "yue", "en", "e2e")}
    for r in rows:
        pred, prob, ms = predict(r["x"])
        lats.append(ms)
        ok = int(pred == r["label"])
        hit[r["lang"]][0] += ok
        hit[r["lang"]][1] += 1
        mark = "OK " if ok else "MISS"
        print(f"{mark} {r['lang']:3s} cut={r['cut']:.2f} label={r['label']} pred={pred} p={prob:.3f} {ms:.0f}ms  {r['file']}")

    print("\n—— 汇总 ——")
    tot_ok = tot = 0
    for lang, (ok, n) in hit.items():
        if n:
            print(f"{lang}: {ok}/{n} = {ok/n:.0%}")
        tot_ok += ok
        tot += n
    lat = sorted(lats)
    print(f"整体: {tot_ok}/{tot} = {tot_ok/tot:.0%}  推理延迟 p50={lat[len(lat)//2]:.0f}ms p95={lat[int(len(lat)*0.95)]:.0f}ms min={lat[0]:.0f}ms")


if __name__ == "__main__":
    main()
