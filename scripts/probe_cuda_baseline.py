"""CUDA 原型延迟基线探针（spec §9 门禁）：在 CUDA 节点对 llama-server(:1235)
与 qwen-asr(:8787) 跑与 Mac 侧 measure_latency 同口径的 TTFT/ASR 采样，
出 JSON 基线供 CUDA vs Mac Studio 档决策。本机 Mac 上 --dry-run 只校验参数。"""

from __future__ import annotations

import argparse
import base64
import json
import time
import urllib.request
from datetime import date
from pathlib import Path


def _pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))] if s else 0.0


def _post_json(url: str, payload: dict, timeout: float = 60.0) -> tuple[dict, float]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return data, (time.perf_counter() - t0) * 1000


def probe_llm_ttft(base_url: str, model_path: str, rounds: int) -> list[float]:
    """流式首 token 延迟：TTFT 从请求发出计到首个 chunk 到达。"""
    ttfts: list[float] = []
    req_body = json.dumps({
        "model": model_path, "stream": True,
        "messages": [{"role": "user", "content": "用一句粤语回答：而家幾點？"}],
    }).encode()
    for _ in range(rounds):
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/v1/chat/completions", data=req_body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.readline()  # 首个 SSE chunk 到达即 TTFT
        ttfts.append((time.perf_counter() - t0) * 1000)
    return ttfts


def probe_asr(base_url: str, wav: Path, language: str, rounds: int) -> list[float]:
    audio_b64 = base64.b64encode(wav.read_bytes()).decode()
    results: list[float] = []
    for _ in range(rounds):
        _, ms = _post_json(
            f"{base_url.rstrip('/')}/transcribe",
            {"audio": audio_b64, "language": language},
            timeout=120.0,
        )
        results.append(ms)
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", default="http://127.0.0.1:1235")
    ap.add_argument("--asr", default="http://127.0.0.1:8787")
    ap.add_argument("--model-path", required=True, help="本地模型绝对路径（勿用 repo id，防 HF hub 解析）")
    ap.add_argument("--wav", required=True, type=Path)
    ap.add_argument("--language", default="cantonese")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的探针计划")
    ap.add_argument("--out", default=f"reports/cuda_baseline_{date.today().isoformat()}.json")
    args = ap.parse_args()

    if args.dry_run:
        print(f"[plan] LLM TTFT x{args.rounds} -> {args.llm}/v1/chat/completions (model={args.model_path})")
        print(f"[plan] ASR x{args.rounds} -> {args.asr}/transcribe ({args.wav}, {args.language})")
        print(f"[plan] 输出 -> {args.out}")
        return 0

    assert args.wav.exists(), f"wav 不存在: {args.wav}"
    ttfts = probe_llm_ttft(args.llm, args.model_path, args.rounds)
    asrs = probe_asr(args.asr, args.wav, args.language, args.rounds)
    report = {
        "date": date.today().isoformat(),
        "llm": {"ttft_ms": {"p50": _pct(ttfts, 0.5), "p90": _pct(ttfts, 0.9), "max": max(ttfts), "n": len(ttfts)}},
        "asr": {"ms": {"p50": _pct(asrs, 0.5), "p90": _pct(asrs, 0.9), "max": max(asrs), "n": len(asrs)}},
        "raw": {"ttft_ms": ttfts, "asr_ms": asrs},
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report["llm"] | report["asr"], indent=2))
    print(f"基线已写 {out} —— 回填 spec §9 对比 Mac 基线（PERCEIVED_MS 分解: eou+llm+tts）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
