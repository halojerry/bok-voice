"""CUDA 原型延迟基线探针（spec §9 门禁）：在 CUDA 节点对 llama-server(:1235)
与 qwen-asr(:8787) 跑与 Mac 侧 measure_latency 同口径的 TTFT/ASR 采样，
出 JSON 基线供 CUDA vs Mac Studio 档决策。本机 Mac 上 --dry-run 只校验参数。"""

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
import base64
import json
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _safe_urlopen(req, *, timeout: float):
    """出站闸门（tools/bok.py 同形状）：urlopen 前就地校验 Request.full_url
    ——仅 http/https、host 非空、无 userinfo；不过闸=PermissionError。
    远端 CUDA 节点目标由 main 入口的 urlguard gate（BOK_PROBE_EXTRA_HOSTS
    显式放行）先行把守；本闸是 sink 级第二道。"""
    parts = urllib.parse.urlsplit(req.full_url)
    host = (parts.hostname or "").lower()
    if not (
        parts.scheme in ("http", "https")
        and (host in _LOOPBACK_HOSTS or bool(host))
        and not parts.username
        and not parts.password
    ):
        raise PermissionError(f"出站 URL 未过护栏（拒发）: {req.full_url}")
    return urllib.request.urlopen(req, timeout=timeout)


def _pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))] if s else 0.0


def _post_json(url: str, payload: dict, timeout: float = 60.0) -> tuple[dict, float]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    t0 = time.perf_counter()
    with _safe_urlopen(req, timeout=timeout) as resp:
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
        with _safe_urlopen(req, timeout=120) as resp:
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

    from urlguard_gate import gate

    if not args.dry_run:
        gate(args.llm, args.asr)  # 本地诊断白名单；远端 CUDA 节点用 BOK_PROBE_EXTRA_HOSTS 显式放行

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
