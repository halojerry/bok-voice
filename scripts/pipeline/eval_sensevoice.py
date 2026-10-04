#!/usr/bin/env python3
"""SenseVoice-small ONNX 评估:粤语 CER + WA 数字 + 8k 窄带,双引擎对照。

动机(2026-10-01):ASR 是当前唯一留在 MPS 上和 LLM 抢卡的重件;papercup 实证
SenseVoiceSmall int8 ONNX 本机 ~100× 实时(CPU 可跑)。若粤语质量/数字过得
了门,做成 ASR 快车道=速度+让出 MPS+零出境三收。

语料:本地 TTS(:8788)按已知文本合成=完美参照;每句两个带宽臂
(16k 干净 / narrowband 8k 模拟 SIP PCMU)。
对照:Qwen3-ASR sidecar(:8787,cantonese 钉定+域词 context,生产姿态)。

门(建议,输出里同时报两引擎原始数):
  canto_clean CER ≤ 10%(Qwen3 基线 5-6%,快车道允让)
  数字句 exact ≥ Qwen3 同臂(WA 收号零降级铁律)

用法:.venv312/bin/python scripts/pipeline/eval_sensevoice.py [--model-dir /tmp/sensevoice/...]
前置:栈在跑(TTS 8788/ASR 8787);sherpa-onnx 已装;模型已下。
"""
from __future__ import annotations
# --- scripts import bootstrap (G1) ---
# sys.path 引导(G1 迁移解耦,见 docs/superpowers/plans/2026-10-04-repo-governance-plan.md §3.1):
# 同层时是 no-op;文件挪进任何桶后裸 import 兄弟模块继续解析。
import sys as _sys, pathlib as _pathlib
_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))


import argparse
import json
import os
import sys
import time
import unicodedata
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from probe_stimulus import narrowband_pcm, stimulus_pcm  # noqa: E402

ASR_URL = os.environ.get("ASR_URL", "http://127.0.0.1:8787")
TTS_URL = os.environ.get("TTS_URL", "http://127.0.0.1:8788")
DEFAULT_MODEL_DIR = "/tmp/sensevoice/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
QW3_ONNX_DIR = "/tmp/sensevoice/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"

# 生产姿态域词 context(A 线热词四源里的模板/行业词代表)。
HOTWORD_CONTEXT = "新龙 集运 速遞 拼多多 淘寶 京東 WhatsApp 微信 賠償 銅鑼灣 聽唔清 講多次"

# 粤语日常句(含域词/重问族/身份质疑,长度贴近真实轮次)。
CANTO_LINES = [
    "你好，請問你搵邊位？",
    "我個件遲咗成個禮拜",
    "拼多多買嘅",
    "可以點樣賠",
    "唔該幫我跟進吓個件",
    "我係喺淘寶買嘅，上個禮拜寄出",
    "你講多次，頭先聽唔清",
    "你哋係邊間公司嚟嘅",
    "我唔係新龍，你打錯咗啦",
    "賠償幾時到賬呀",
    "留 WhatsApp 號碼俾你哋得唔得",
    "好啦好啦，我接受呢個方案",
]

# WA 报号句(真实形态:口语引导+数字串;6699 陷阱=数量级误读高发)。
DIGIT_LINES = [
    "我嘅 WhatsApp 係六六九九四五",
    "號碼係三七九二零五八",
    "WhatsApp 係九二三一六六九九",
    "單號尾號係八八零七",
    "我電話係五五二一一三四七八九",
    "係二零二四一一三零六六",
    "尾數九九六六八八",
    "WhatsApp 六六九九三四五零一七",
]


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _norm_text(s: str) -> str:
    """繁简归一(zhconv)+剥标点空白+小写——CER 判分域。"""
    try:
        from zhconv import convert as zh_convert

        s = zh_convert(s, "zh-cn")
    except Exception:  # pragma: no cover - zhconv 缺席退原始
        pass
    s = unicodedata.normalize("NFKC", s).lower()
    return "".join(ch for ch in s if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


_CN_DIGIT = {"零": "0", "一": "1", "二": "2", "兩": "2", "两": "2", "三": "3", "四": "4",
             "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "幺": "1", "么": "1"}


def _digit_seq(s: str) -> str:
    """中/阿数字统一成纯数字序列(其余丢弃)——数字句 exact 判分域。"""
    out = []
    for ch in _norm_text(s):
        if ch.isdigit():
            out.append(ch)
        elif ch in _CN_DIGIT:
            out.append(_CN_DIGIT[ch])
    return "".join(out)


def cer(pred: str, ref: str) -> float:
    p, r = _norm_text(pred), _norm_text(ref)
    if not r:
        return 0.0
    return _levenshtein(p, r) / len(r)


def ref_digits_of(text: str) -> str:
    return _digit_seq(text)


def synth(text: str) -> bytes:
    return stimulus_pcm(text, "cantonese", tts_url=TTS_URL)


def qwen3_transcribe(pcm: bytes, *, context: str = HOTWORD_CONTEXT) -> str:
    with httpx.Client(timeout=60) as c:
        s = c.post(f"{ASR_URL}/api/start",
                   params={"language": "cantonese", "context": context}).json()["session_id"]
        out = c.post(f"{ASR_URL}/api/finish", params={"session_id": s}, content=pcm).json()
        return str(out.get("text") or "")


def sensevoice_recognizer(model_dir: str, language: str):
    import sherpa_onnx

    d = Path(model_dir)
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(d / "model.int8.onnx"),
        tokens=str(d / "tokens.txt"),
        use_itn=True,
        language=language,  # "auto" | "yue" ...
        num_threads=2,
    )


def qwen3_onnx_recognizer(model_dir: str = QW3_ONNX_DIR, *, hotwords: str = ""):
    """Qwen3-ASR-0.6B int8 ONNX 纯 CPU(sherpa-onnx from_qwen3_asr)——同一模型
    只换运行时,质量差=int8 量化差;支持官方 hotwords 通道(与 transformers 版
    的 context 对位)。"""
    import sherpa_onnx

    d = Path(model_dir)
    return sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
        conv_frontend=str(d / "conv_frontend.onnx"),
        encoder=str(d / "encoder.int8.onnx"),
        decoder=str(d / "decoder.int8.onnx"),
        tokenizer=str(d / "tokenizer"),
        num_threads=4,
        provider="cpu",
        hotwords=hotwords,
    )


def sv_transcribe(rec, pcm: bytes) -> tuple[str, float]:
    import numpy as np

    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    t0 = time.perf_counter()
    stream = rec.create_stream()
    stream.accept_waveform(sample_rate=16000, waveform=x)
    rec.decode_stream(stream)
    dt = time.perf_counter() - t0
    return str(stream.result.text), dt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default=DEFAULT_MODEL_DIR)
    ap.add_argument("--language", default="auto", help="SenseVoice language hint: auto|yue|zh")
    ap.add_argument("--skip-qwen3", action="store_true")
    ap.add_argument("--skip-sv", action="store_true", help="只跑 qw3cpu 臂")
    ap.add_argument("--qw3-onnx-dir", default=QW3_ONNX_DIR)
    args = ap.parse_args()

    sv_rec = None
    if not args.skip_sv:
        print(f"[sv] loading {args.model_dir} lang={args.language} ...", flush=True)
        sv_rec = sensevoice_recognizer(args.model_dir, args.language)
        print("[sv] loaded", flush=True)
    print("[qw3cpu] loading Qwen3-ASR int8 ONNX (cpu) ...", flush=True)
    qw3_rec = qwen3_onnx_recognizer(args.qw3_onnx_dir, hotwords=HOTWORD_CONTEXT)
    print("[qw3cpu] loaded", flush=True)

    rows = []
    for kind, lines in (("canto", CANTO_LINES), ("digits", DIGIT_LINES)):
        for text in lines:
            pcm = synth(text)
            if not pcm:
                print(f"[synth-fail] {text}", flush=True)
                continue
            for band, wav in (("clean", pcm), ("narrow", narrowband_pcm(pcm))):
                sv_text, sv_ms = ("", 0)
                if sv_rec is not None:
                    sv_text, sv_ms = sv_transcribe(sv_rec, wav)
                q3_text, q3_ms = sv_transcribe(qw3_rec, wav)  # 同款流式 API
                qw_text = "" if args.skip_qwen3 else qwen3_transcribe(wav)
                row = {
                    "kind": kind, "band": band, "ref": text,
                    "sv": sv_text, "sv_ms": round(sv_ms * 1000),
                    "q3": q3_text, "q3_ms": round(q3_ms * 1000),
                    "qwen": qw_text,
                }
                if kind == "canto":
                    row["sv_cer"] = round(cer(sv_text, text), 4) if sv_rec is not None else None
                    row["q3_cer"] = round(cer(q3_text, text), 4)
                    row["qw_cer"] = round(cer(qw_text, text), 4) if qw_text else None
                else:
                    rd = ref_digits_of(text)
                    row["ref_digits"] = rd
                    row["sv_digits"] = _digit_seq(sv_text) if sv_rec is not None else ""
                    row["q3_digits"] = _digit_seq(q3_text)
                    row["qw_digits"] = _digit_seq(qw_text) if qw_text else ""
                rows.append(row)
                tag = f"{kind}/{band}"
                if kind == "canto":
                    print(f"[{tag}] ref={text} | sv_cer={row['sv_cer']} | q3cpu_cer={row['q3_cer']} "
                          f"({q3_ms*1000:.0f}ms) {q3_text} | qw_cer={row['qw_cer']}", flush=True)
                else:
                    print(f"[{tag}] ref_digits={rd} | sv={row['sv_digits']} | "
                          f"q3cpu={row['q3_digits']} ({q3_ms*1000:.0f}ms) | qw={row['qw_digits']}", flush=True)

    def _summary(kind: str, band: str) -> dict:
        sel = [r for r in rows if r["kind"] == kind and r["band"] == band]
        if kind == "canto":
            out: dict = {"n": len(sel)}
            for eng, key in (("sv", "sv_cer"), ("q3", "q3_cer"), ("qw", "qw_cer")):
                vals = [r[key] for r in sel if r[key] is not None]
                if vals:
                    out[f"{eng}_cer_mean"] = round(sum(vals) / len(vals), 4)
            ms = [r["q3_ms"] for r in sel]
            out["q3_ms_avg"] = round(sum(ms) / len(ms)) if ms else None
            return out
        n = len(sel)
        return {
            "n": n,
            "sv_exact": f"{sum(1 for r in sel if r['sv_digits'] == r['ref_digits'])}/{n}",
            "q3_exact": f"{sum(1 for r in sel if r['q3_digits'] == r['ref_digits'])}/{n}",
            "qw_exact": f"{sum(1 for r in sel if r['qw_digits'] == r['ref_digits'])}/{n}",
        }

    report = {
        "model_dir": args.model_dir, "language": args.language,
        "qw3_onnx_dir": args.qw3_onnx_dir,
        "summary": {f"{k}-{b}": _summary(k, b) for k in ("canto", "digits") for b in ("clean", "narrow")},
        "rows": rows,
    }
    out = ROOT / "reports" / "sensevoice-eval" / f"{int(time.time())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n===== 汇总 =====", flush=True)
    for k, v in report["summary"].items():
        print(f"  {k}: {v}", flush=True)
    print(f"[sv] JSON → {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
