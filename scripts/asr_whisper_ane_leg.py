#!/usr/bin/env python3
"""Whisper CoreML(ANE) 档决定性腿：LLM TTFT 三格对照（idle / Metal 循环 / CoreML 循环）。

假设：ASR encoder 走 ANE 能把 GPU 还给 LLM。测法：mlx_lm sidecar :1235（OpenAI
兼容流式）TTFT 在三种条件下各测 N 样本：
  idle      — 无 whisper 负载
  metal     — whisper.cpp (ggml-large-v3-turbo, encoder=Metal) 循环转写中
  coreml    — 同仓库 -DWHISPER_COREML=ON 构建，encoder 走 CoreML(.mlmodelc→ANE) 循环转写中
第三格 ≈ idle 格 ⇒ 假设成立。

Metal/CoreML 切换：-DWHISPER_COREML=ON 编译后 encoder 强制自动加载
`<model>-encoder.mlmodelc`，缺席即拒绝启动（WHISPER_COREML_ALLOW_FALLBACK 默认
OFF、无 CLI 开关）——所以 Metal 档用独立无-CoreML 构建（build-metal），CoreML 档
用 build/（COREML=1）；同一份源码同一份 ggml 模型，唯一差异=encoder 后端。

附带单发转写延迟 Metal vs CoreML 对照（whisper-cli 打印的 load/encode/total 时间）。

安全约束（硬性）：脚本内 HTTP 目标全部经 _url_ok() 白名单（仅 127.0.0.1:1235），
模式抄 scripts/gpu_contention_probe.py。负载子进程按进程组管理（记录 PID，
os.killpg 结束，禁 pkill）。产物：reports/asr-whisper-bench/ane-leg.json + ane-leg.md。

用法： .venv312/bin/python scripts/asr_whisper_ane_leg.py [--samples 20] [--audio PATH]
      [--skip-metal] [--skip-coreml] [--no-single]
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
import platform
import signal
import socket
import statistics
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BENCH = Path.home() / ".cache" / "bok-bench"
WHISPER_DIR = BENCH / "whisper.cpp"
WHISPER_CLI_METAL = WHISPER_DIR / "build-metal" / "bin" / "whisper-cli"  # WHISPER_COREML=OFF
WHISPER_CLI_COREML = WHISPER_DIR / "build" / "bin" / "whisper-cli"       # WHISPER_COREML=ON
WHISPER_MODEL = WHISPER_DIR / "models" / "ggml-large-v3-turbo.bin"
COREML_DIR = WHISPER_DIR / "models" / "ggml-large-v3-turbo-encoder.mlmodelc"
LOGS = BENCH / "logs"
DEFAULT_AUDIO = BENCH / "audio" / "bench_long.wav"
CORPUS_DIR = REPO / "reports" / "asr-whisper-bench" / "corpus"
OUT_DIR = REPO / "reports" / "asr-whisper-bench"

LLM_BASE = "http://127.0.0.1:1235"
LLM_CHAT = LLM_BASE + "/v1/chat/completions"
LLM_MODELS = LLM_BASE + "/v1/models"
ALLOWED = {
    ("127.0.0.1", 1235),
}

# ≈2k token 静态前缀（Qwen 系中文 ~1.3-1.5 字/token，2700 字 ≈ 1.9-2.1k token）
PROMPT_BLOCK = (
    "家电延保服务条款：覆盖压缩机、电机、控制板等主要部件的维修与更换；"
    "上门服务需提前一天预约；人为损坏不在保修范围；续费用户享受优先派单与免费清洗一次；"
    "服务热线工作时间为每日九点到十八点；退货需保留原包装与发票。"
)
PROMPT_SYSTEM = (
    "你是电话客服助理，请保持礼貌、简洁、直接回答客户问题。以下是服务条款背景资料："
    + PROMPT_BLOCK * 30
)


def _url_ok(url: str) -> bool:
    """SSRF 护栏：host/port 白名单 + 解析地址必须回环（抄 scripts/gpu_contention_probe.py）。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http":
        return False
    host = parsed.hostname or ""
    if (host, parsed.port) not in ALLOWED:
        return False
    for info in socket.getaddrinfo(host, parsed.port):
        ip = __import__("ipaddress").ip_address(info[4][0])
        if not ip.is_loopback:
            return False
    return True


def post_stream(url: str, payload: dict, timeout: float = 120):
    assert _url_ok(url), f"非白名单目标: {url}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def get_json(url: str, timeout: float = 10):
    assert _url_ok(url), f"非白名单目标: {url}"
    return json.loads(urllib.request.urlopen(url, timeout=timeout).read())


def pick_llm_model() -> str:
    env = (os.environ.get("ANE_LEG_LLM_MODEL") or "").strip()
    if env:
        return env
    import re

    ids = [m.get("id", "") for m in get_json(LLM_MODELS).get("data", [])]
    for mid in ids:
        # 独立 "4b" token（-4b- / 4b 结尾），不被 "mlx-4bit" 之类的子串误命中
        if re.search(r"(?:^|[^0-9a-z])4b(?:[^0-9a-z]|$)", mid.lower()):
            return mid
    return ids[0] if ids else "unknown"


def llm_ttft_once(model: str, seed: int) -> tuple[float, str]:
    """流式首 token 延迟（ms）；返回 (ttft_ms, system_fingerprint)。"""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": PROMPT_SYSTEM},
            {"role": "user",
             "content": f"客户第{seed}轮说：你们这个延保到底保什么？请用一句话回答。"},
        ],
        "max_tokens": 40,
        "stream": True,
        "temperature": 0.3,
    }
    fingerprint = ""
    t0 = time.time()
    with post_stream(LLM_CHAT, body) as resp:
        for line in resp:
            if line.startswith(b":"):  # mlx_lm keepalive 注释行
                continue
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                j = json.loads(payload)
            except Exception:
                continue
            fingerprint = fingerprint or j.get("system_fingerprint", "")
            d = ((j.get("choices") or [{}])[0].get("delta") or {})
            if d.get("content"):
                return (time.time() - t0) * 1000, fingerprint
    raise RuntimeError("流内未收到任何 content delta")


def llm_ttft_cell(model: str, n: int, label: str) -> dict:
    xs, errs = [], []
    fingerprint = ""
    for i in range(n):
        for attempt in range(3):
            try:
                ms, fp = llm_ttft_once(model, i)
                xs.append(ms)
                fingerprint = fingerprint or fp
                break
            except Exception as exc:
                if attempt == 2:
                    errs.append(f"sample{i}: {exc!r}")
                else:
                    time.sleep(1.0)
        time.sleep(0.35)
    r = {
        "label": label, "n": len(xs), "samples_ms": [round(x, 1) for x in xs],
        "p50_ms": round(pct(xs, 0.50), 1) if xs else None,
        "p95_ms": round(pct(xs, 0.95), 1) if xs else None,
        "errors": errs,
    }
    if xs:
        print(f"[cell] {label}: n={len(xs)} p50={r['p50_ms']}ms p95={r['p95_ms']}ms "
              f"all={[round(x) for x in xs]}")
    else:
        print(f"[cell] {label}: 全部失败 {errs}")
    if fingerprint:
        r["llm_system_fingerprint"] = fingerprint
    return r


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = (len(s) - 1) * q
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


class WhisperLoad:
    """循环转写负载：后台子进程 while-loop 连续跑 whisper-cli；进程组管理，禁 pkill。"""

    def __init__(self, mode: str, audio: Path, threads: int | None):
        self.mode = mode
        self.audio = audio
        self.threads = threads
        self.cli = WHISPER_CLI_COREML if mode == "coreml" else WHISPER_CLI_METAL
        self.proc: subprocess.Popen | None = None
        self.log_path = LOGS / f"ane-leg-load-{mode}.log"

    def start(self) -> int:
        if not self.cli.exists():
            raise SystemExit(f"{self.mode} 档 whisper-cli 缺失: {self.cli}")
        if self.mode == "coreml" and not COREML_DIR.exists():
            raise SystemExit(f"CoreML encoder 缺失: {COREML_DIR}（先完成转换与编译）")
        if self.log_path.exists():
            self.log_path.unlink()
        t = ["-t", str(self.threads)] if self.threads else []
        inner = (
            f'exec 2>>"{self.log_path}"\n'
            f'while true; do "{self.cli}" -m "{WHISPER_MODEL}" -f "{self.audio}" '
            f'{" ".join(t)} >> "{self.log_path}" 2>&1; sleep 0.2; done'
        )
        self.proc = subprocess.Popen(
            ["/bin/zsh", "-c", inner], start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        print(f"[load] mode={self.mode} pid={self.proc.pid} cli={self.cli.name} "
              f"log={self.log_path}")
        return self.proc.pid

    def wait_warm(self, timeout: float = 180.0) -> bool:
        """等第一轮转写完整跑完（日志出现 encode/total time），CoreML 首轮含 ANE 编译。"""
        t0 = time.time()
        markers = ("encode time", "total time")
        while time.time() - t0 < timeout:
            if self.proc and self.proc.poll() is not None:
                print(f"[load] mode={self.mode} 循环进程退出 rc={self.proc.returncode}")
                return False
            try:
                if self.log_path.exists() and all(
                        m in self.log_path.read_text(errors="ignore") for m in markers):
                    time.sleep(2.0)
                    return True
            except OSError:
                pass
            time.sleep(1.0)
        return False

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            self.proc.wait(timeout=10)
        print(f"[load] mode={self.mode} stopped pid={self.proc.pid}")
        self.proc = None


def parse_whisper_timings(out: str) -> dict:
    import re

    def num(pat: str) -> float | None:
        m = re.search(pat, out)
        return float(m.group(1)) if m else None

    return {
        "load_ms": num(r"load time\s*=\s*([\d.]+)\s*ms"),
        "encode_ms": num(r"encode time\s*=\s*([\d.]+)\s*ms"),
        "encode_runs": num(r"encode time\s*=\s*[\d.]+\s*ms\s*/\s*(\d+)\s*runs"),
        "decode_ms": num(r"decode time\s*=\s*([\d.]+)\s*ms"),
        "total_ms": num(r"total time\s*=\s*([\d.]+)\s*ms"),
    }


def single_shot(audio: Path, runs: int) -> dict:
    """单发转写延迟对照：每种模式各跑 runs 次，取全部 + 中位。"""
    result: dict = {}
    for mode in ("metal", "coreml"):
        cli = WHISPER_CLI_COREML if mode == "coreml" else WHISPER_CLI_METAL
        if not cli.exists():
            raise SystemExit(f"{mode} 档 whisper-cli 缺失: {cli}")
        if mode == "coreml" and not COREML_DIR.exists():
            raise SystemExit(f"CoreML encoder 缺失: {COREML_DIR}")
        entries = []
        for i in range(runs):
            t0 = time.time()
            proc = subprocess.run(
                ["/usr/bin/env", str(cli), "-m", str(WHISPER_MODEL), "-f", str(audio)],
                capture_output=True, text=True, timeout=600)
            wall_ms = (time.time() - t0) * 1000
            timings = parse_whisper_timings(proc.stdout + proc.stderr)
            entries.append({"run": i + 1, "wall_ms": round(wall_ms, 1), **timings})
            print(f"[single] {mode} run{i+1}: wall={wall_ms:.0f}ms "
                  f"load={timings['load_ms']} encode={timings['encode_ms']} "
                  f"total={timings['total_ms']}")
            time.sleep(0.5)
        walls = [e["wall_ms"] for e in entries]
        totals = [e["total_ms"] for e in entries if e["total_ms"] is not None]
        encodes = [e["encode_ms"] for e in entries if e["encode_ms"] is not None]
        result[mode] = {
            "runs": entries,
            "wall_ms_median": round(statistics.median(walls), 1),
            "total_ms_median": round(statistics.median(totals), 1) if totals else None,
            "encode_ms_median": round(statistics.median(encodes), 1) if encodes else None,
        }
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--audio", type=str, default="")
    ap.add_argument("--threads", type=int, default=0, help="whisper-cli -t，0=默认")
    ap.add_argument("--skip-metal", action="store_true")
    ap.add_argument("--skip-coreml", action="store_true")
    ap.add_argument("--no-single", action="store_true")
    args = ap.parse_args()

    audio = Path(args.audio) if args.audio else DEFAULT_AUDIO
    if not audio.exists() and CORPUS_DIR.exists():
        wavs = sorted(CORPUS_DIR.glob("*.wav"))
        if wavs:
            audio = wavs[0]
    assert audio.exists(), f"转写素材不存在: {audio}"
    assert WHISPER_CLI_METAL.exists(), f"Metal 档 whisper-cli 不存在: {WHISPER_CLI_METAL}"
    assert WHISPER_CLI_COREML.exists(), f"CoreML 档 whisper-cli 不存在: {WHISPER_CLI_COREML}"
    assert WHISPER_MODEL.exists(), f"模型不存在: {WHISPER_MODEL}"
    assert COREML_DIR.exists(), f"CoreML encoder 不存在: {COREML_DIR}"

    LOGS.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    model = pick_llm_model()
    fingerprint = ""
    print(f"[cfg] llm={model} audio={audio} samples={args.samples}")
    print(f"[cfg] metal_cli={WHISPER_CLI_METAL}")
    print(f"[cfg] coreml_cli={WHISPER_CLI_COREML}")

    load_meta = {}
    result: dict = {
        "schema": "ane-leg/v1",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "llm_model": model,
        "llm_endpoint": LLM_CHAT,
        "audio": str(audio),
        "audio_seconds": None,
        "whisper_cli_metal": str(WHISPER_CLI_METAL),
        "whisper_cli_coreml": str(WHISPER_CLI_COREML),
        "whisper_model": str(WHISPER_MODEL),
        "whisper_commit": _git_rev(),
        "coreml_encoder": str(COREML_DIR),
        "samples_per_cell": args.samples,
        "cells": {},
        "single_shot": {},
        "errors": [],
    }
    try:
        import wave
        with wave.open(str(audio), "rb") as w:
            result["audio_seconds"] = round(w.getnframes() / w.getframerate(), 1)
    except Exception:
        pass

    try:
        result["cells"]["idle"] = llm_ttft_cell(model, args.samples, "idle")
        fingerprint = result["cells"]["idle"].get("llm_system_fingerprint", "")

        for mode in ("metal", "coreml"):
            if (mode == "metal" and args.skip_metal) or (
                    mode == "coreml" and args.skip_coreml):
                continue
            load = WhisperLoad(mode, audio, args.threads or None)
            pid = load.start()
            load_meta[mode] = {"pid": pid, "log": str(load.log_path)}
            warm = load.wait_warm()
            if not warm:
                result["errors"].append(f"{mode}: 负载预热失败（见 {load.log_path}）")
            time.sleep(1.0)
            result["cells"][mode] = llm_ttft_cell(model, args.samples, mode)
            load.stop()
            time.sleep(3.0)  # GPU 排空冷却

        if not args.no_single:
            result["single_shot"] = single_shot(audio, runs=3)
    finally:
        # 兜底清理：任何残留负载进程按记录的 PID 进程组收掉（禁 pkill）
        try:
            if "load" in dir() and load.proc is not None:
                load.stop()
        except Exception:
            pass

    result["llm_system_fingerprint"] = fingerprint
    result["load_processes"] = load_meta
    result["finished_at"] = datetime.now().isoformat(timespec="seconds")
    result["prompt_chars"] = len(PROMPT_SYSTEM)

    (OUT_DIR / "ane-leg.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    (OUT_DIR / "ane-leg.md").write_text(render_md(result))
    print(f"[done] {OUT_DIR/'ane-leg.json'} + ane-leg.md")


def _git_rev() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=WHISPER_DIR,
            capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def render_md(r: dict) -> str:
    lines = [
        "# Whisper CoreML(ANE) 档 — LLM TTFT 三格对照",
        "",
        f"- 时间：{r['started_at']} → {r['finished_at']}",
        f"- LLM：`{r['llm_model']}` @ 127.0.0.1:1235（流式首 token，"
        f"≈2k token prompt≈{r.get('prompt_chars', '?')} 字）",
        f"- 转写负载：whisper.cpp `{r.get('whisper_commit', '?')}` "
        f"ggml-large-v3-turbo，素材 {r.get('audio_seconds', '?')}s 循环",
        f"- CoreML encoder：{r.get('coreml_encoder', '?')}"
        f"（CoreML 档=build/ 二进制自动加载；Metal 档=build-metal/ 无-CoreML 构建）",
        f"- GPU fingerprint：`{r.get('llm_system_fingerprint', '?')}`",
        "",
        "## 三格 TTFT（n=每格样本数）",
        "",
        "| 条件 | n | p50 (ms) | p95 (ms) | vs idle |",
        "|---|---|---|---|---|",
    ]
    idle = r["cells"].get("idle", {})
    idle_p50 = idle.get("p50_ms")
    for label in ("idle", "metal", "coreml"):
        c = r["cells"].get(label)
        if not c:
            lines.append(f"| {label} | — | — | — | — |")
            continue
        delta = ""
        if idle_p50 and c.get("p50_ms"):
            d = (c["p50_ms"] / idle_p50 - 1) * 100
            delta = f"{d:+.0f}%"
        lines.append(
            f"| {label} | {c.get('n', 0)} | {c.get('p50_ms')} | {c.get('p95_ms')} | {delta} |")
    ss = r.get("single_shot") or {}
    if ss:
        lines += [
            "",
            "## 单发转写延迟（whisper-cli，取中位）",
            "",
            "| 档 | wall (ms) | total (ms) | encode (ms) |",
            "|---|---|---|---|",
        ]
        for mode in ("metal", "coreml"):
            m = ss.get(mode)
            if m:
                lines.append(
                    f"| {mode} | {m['wall_ms_median']} | {m['total_ms_median']} "
                    f"| {m['encode_ms_median']} |")
    if r.get("errors"):
        lines += ["", "## 错误", ""] + [f"- {e}" for e in r["errors"]]
    lines += ["", "## 结论判读", ""]
    coreml, metal = r["cells"].get("coreml"), r["cells"].get("metal")
    if coreml and idle_p50 and coreml.get("p50_ms"):
        ratio = coreml["p50_ms"] / idle_p50
        verdict = ("第三格 ≈ 空闲格 ⇒ 假设成立（encoder 上 ANE 把 GPU 还给了 LLM）"
                   if ratio < 1.25 else
                   "第三格仍显著高于空闲格 ⇒ 假设不成立或 ANE 未生效（核对 load 日志是否 "
                   "『Core ML model loaded』）")
        lines.append(f"- coreml/idle p50 比 = {ratio:.2f}×：{verdict}")
    if coreml and metal and coreml.get("p50_ms") and metal.get("p50_ms"):
        lines.append(
            f"- coreml/metal p50 比 = {coreml['p50_ms'] / metal['p50_ms']:.2f}×")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
