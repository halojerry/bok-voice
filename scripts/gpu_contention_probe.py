#!/usr/bin/env python3
"""GPU 同卡争抢微基准（B 机 CUDA 节点，2026-09-24 D 项归因）。

问题：A 线一通轮次里 ASR(transformers, :8787) 与 LLM(llama-server, :1235)
共卡，首声 p50 2184ms（Mac mlx 同栈 927ms）。本探针隔离量化两个方向：

  腿1  LLM TTFT：空闲 vs ASR 持续解码中（同 prompt 形状，中位对比=争抢代价）
  腿1b 云端 MiniMax(经 shim :1236) 同对照（纯网络，预期与 GPU 争抢无关）
  腿2  ASR finish 往返：空闲 vs LLM 长生成中（反向争抢）

探针只打本机回环固定端口（SSRF 护栏：host/port 白名单，见 _url_ok）。
用法（B 机 ASR sidecar venv，需 soundfile/librosa）：
  /root/bok/services/qwen3-asr-sidecar/.venv/bin/python gpu_contention_probe.py
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


import ipaddress
import json
import os
import socket
import statistics
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

LLM_LOCAL = "http://127.0.0.1:1235/v1/chat/completions"
LLM_CLOUD = "http://127.0.0.1:1236/v1/chat/completions"
ASR = "http://127.0.0.1:8787"
ALLOWED = {
    ("127.0.0.1", 1235),
    ("127.0.0.1", 1236),
    ("127.0.0.1", 8787),
}
WAV_CANDIDATES = [
    os.environ.get("PROBE_WAV", ""),
    "/root/bok/assets/fillers",
    "/root/.local/share/BokVoice/tts_cache",
    "/root/FireRedTTS3/outputs",
    "/root/FireRedTTS3/funasr_models/paraformer-zh/example",
]


def _url_ok(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http":
        return False
    host = parsed.hostname or ""
    if (host, parsed.port) not in ALLOWED:
        return False
    for info in socket.getaddrinfo(host, parsed.port):
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_loopback:
            return False
    return True


def post(url: str, payload: dict | None = None, content: bytes | None = None,
         timeout: float = 60) -> dict:
    assert _url_ok(url), f"非白名单目标: {url}"
    body = content if content is not None else json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def post_stream(url: str, payload: dict, timeout: float = 60):
    assert _url_ok(url), f"非白名单目标: {url}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def load_pcm() -> bytes:
    """找一段真实语音（filler 资产/tts_cache 罐头），转 16k s16le PCM。"""
    import numpy as np
    import soundfile as sf

    for d in WAV_CANDIDATES:
        base = Path(d)
        if not base.exists():
            continue
        wavs = sorted(base.rglob("*.wav"))
        if wavs:
            data, sr = sf.read(str(wavs[0]), dtype="float32", always_2d=False)
            if data.ndim > 1:
                data = data.mean(axis=1)
            if sr != 16000:
                import librosa

                data = librosa.resample(data, orig_sr=sr, target_sr=16000)
            pcm = (data * 32767).astype("<i2").tobytes()
            print(f"[probe] 语音源 {wavs[0]} {len(pcm)/32000.0:.1f}s@16k")
            return pcm
    raise SystemExit("找不到 wav（filler 资产/tts_cache 均空）")


def llm_ttft_once(base_url: str, seed: int) -> float:
    """真实轮次形状：长静态前缀 + 变尾（cache-reuse 命中前缀，增量 prefill 尾巴）。"""
    prefix = (
        "你是电话客服助理。请遵守：身份先确认，语气礼貌，每轮回复不超过两句话。"
        "背景：客户张先生购买了家电延保服务，本次来电是服务回访邀约。"
        + "通话记录轮次参考。" * 40
    )
    tail = f"客户第{seed}轮说：你们这个是做什么的？请用一句话回答。"
    body = {
        "model": "probe",
        "messages": [
            {"role": "system", "content": prefix},
            {"role": "user", "content": tail},
        ],
        "max_tokens": 40,
        "stream": True,
        "temperature": 0.35,
    }
    t0 = time.time()
    with post_stream(base_url, body) as resp:
        for line in resp:
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                j = json.loads(payload)
            except Exception:
                continue
            d = ((j.get("choices") or [{}])[0].get("delta") or {})
            if d.get("content"):
                return (time.time() - t0) * 1000
    return float("nan")


def llm_ttft(base_url: str, n: int, label: str) -> list[float]:
    xs = []
    for i in range(n):
        xs.append(llm_ttft_once(base_url, i))
        time.sleep(0.4)
    print(f"[leg1] {label}: n={n} p50={statistics.median(xs):.0f}ms "
          f"all={[round(x) for x in xs]}")
    return xs


class AsrLoad:
    """持续解码负载：单 session 连续喂 chunk（每 chunk 触发一次窗口解码），
    每 8s 重开 session 防窗口上限。"""

    def __init__(self, pcm: bytes):
        self.pcm = pcm
        self.stop = threading.Event()

    def run(self) -> None:
        chunk = self.pcm[: 16000 * 2 // 10]  # 100ms
        while not self.stop.is_set():
            try:
                sid = post(f"{ASR}/api/start", content=b"")["session_id"]
                t_end = time.time() + 8.0
                while not self.stop.is_set() and time.time() < t_end:
                    post(f"{ASR}/api/chunk?session_id={sid}", content=chunk, timeout=30)
                    time.sleep(0.1)
                if not self.stop.is_set():
                    post(f"{ASR}/api/finish?session_id={sid}", content=b"")
            except Exception as exc:
                print(f"[asr-load] restart: {exc!r}")
                time.sleep(0.5)


def asr_finish_ms(pcm: bytes) -> float:
    """start + 全量 chunk + finish；返回 finish 调用本身的往返毫秒。"""
    sid = post(f"{ASR}/api/start", content=b"")["session_id"]
    for i in range(0, len(pcm), 3200):
        post(f"{ASR}/api/chunk?session_id={sid}", content=pcm[i : i + 3200])
    t_fin = time.time()
    post(f"{ASR}/api/finish?session_id={sid}", content=b"")
    return (time.time() - t_fin) * 1000


def llm_long_generation() -> threading.Event:
    """让 llama 持续解码（500 token 上限），返回可 wait 的结束闸。"""
    done = threading.Event()

    def _run() -> None:
        body = {
            "model": "probe",
            "messages": [
                {"role": "system", "content": "你是客服。"},
                {"role": "user", "content": "请把今天的服务邀约流程详细讲一遍，尽量长。"},
            ],
            "max_tokens": 500,
            "stream": False,
        }
        try:
            post(LLM_LOCAL, body, timeout=120)
        except Exception:
            pass
        done.set()

    threading.Thread(target=_run, daemon=True).start()
    return done


def cloud_shim_up() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:1236/health", timeout=3) as r:
            return bool(json.loads(r.read()).get("ok"))
    except Exception:
        return False


def main() -> None:
    pcm = load_pcm()

    print("=== 腿1: LLM TTFT 本地 llama :1235 ===")
    llm_ttft(LLM_LOCAL, 6, "空闲基线")
    load = AsrLoad(pcm)
    th = threading.Thread(target=load.run, daemon=True)
    th.start()
    time.sleep(1.5)  # 让 ASR 解码转起来
    llm_ttft(LLM_LOCAL, 6, "ASR 持续解码中")
    load.stop.set()
    time.sleep(1.0)

    if cloud_shim_up():
        print("=== 腿1b: 云端 MiniMax abab6.5s-chat（经 shim）===")
        llm_ttft(LLM_CLOUD, 4, "云端-空闲")
        load2 = AsrLoad(pcm)
        th2 = threading.Thread(target=load2.run, daemon=True)
        th2.start()
        time.sleep(1.5)
        llm_ttft(LLM_CLOUD, 4, "云端-ASR解码中(预期无差=纯网络)")
        load2.stop.set()
        time.sleep(1.0)
    else:
        print("[leg1b] shim 不在线，跳过云端对照")

    print("=== 腿2: ASR finish 往返 ===")
    print(f"[leg2] 空闲 finish: {asr_finish_ms(pcm):.0f}ms")
    gen_done = llm_long_generation()
    time.sleep(0.8)
    print(f"[leg2] LLM 长生成中 finish: {asr_finish_ms(pcm):.0f}ms")
    gen_done.wait(timeout=90)


if __name__ == "__main__":
    main()
