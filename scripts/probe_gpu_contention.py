"""GPU 争用探针：谁在抢 4B 的 prefill（2026-09-22，TTFT 归因用）。

背景
----
真栈 300 轮 `LLM_TTFT_MS` 回归：TTFT ≈ **465ms 固定头 + 2.21ms × 未命中 token**；
只留「生成吞吐正常」的轮（tps≥15）是 **402ms + 1.58ms/token**，而疑似被抢的轮
（tps<15）是 **795ms + 2.00ms/token**——**固定头多 ~390ms**。本探针直接量这句
「被抢」是谁抢的：在本机 4B(:1235) 上发 max_tokens=1 的 prefill 测往返，
分别在三档条件下重复，取中位数比差：

  idle    无其它负载（基线）
  judge   :1237 的 9B 正在跑一段判据级长 prefill（后台 judge 的真实形状）
  asr     :8787 正在解一段真实语音（用户说话/句末 finish 的真实形状）

判据：Δ 中位数（ms）。这不是 pass/fail 探针，是**归因读数**——它决定「争用那条
腿」该往哪修（9B 错峰 vs ASR 侧抑制），以及值不值得修。

安全边界：本探针的唯一用途是打**本机侧车**，故所有端点先过 `_endpoint()`
（只收 http/https + 回环主机，其余拒绝），且请求不跟随重定向——探针没有任何
访问外网/内网其它主机的理由，这条边界把 SSRF 面整个关掉。

用法：
  .venv312/bin/python scripts/probe_gpu_contention.py            # 默认 5 轮/档
  PROBE_REPEATS=8 .venv312/bin/python scripts/probe_gpu_contention.py
前置：bok serve 在跑（:1235 / :1237；:8787 只 asr 档需要，缺则跳过该档）。
"""
from __future__ import annotations

import json
import os
import statistics as stats
import sys
import threading
import time
import urllib.parse
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENDPOINTS = {
    "main": os.environ.get("PROBE_MAIN_URL", "http://127.0.0.1:1235/v1"),
    "judge": os.environ.get("PROBE_JUDGE_URL", "http://127.0.0.1:1237/v1"),
    "asr": os.environ.get("PROBE_ASR_URL", "http://127.0.0.1:8787"),
}
REPEATS = int(os.environ.get("PROBE_REPEATS", "5") or 5)
WAV = ROOT / "tests" / "fixtures" / "audio" / "cantonese.wav"

# 回环白名单：本探针只与同机侧车通信，别的目标一律拒（含用户显式传的 env）。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _endpoint(name: str) -> str:
    """校验并归一化端点：http/https + 回环主机，否则抛错。返回可安全请求的基址。"""
    raw = str(ENDPOINTS.get(name) or "").strip()
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"{name}: 只允许 http/https，收到 {parts.scheme!r}")
    host = (parts.hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"{name}: 只允许本机回环主机，收到 {host!r}")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """探针不跟随重定向：目标固定为本机侧车，跳转只会是异常状态。"""

    def redirect_request(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)

# 真实形状：主 LLM 每轮未命中 ≈ 尾块（实测空尾 321 tok / 晚通话真实态 997 tok），
# 取 ~500 字的代表档；judge 那边是判据集+转写（AGENTS.md 记 9B prefill 慢、timeout 抬到 20s）。
_TAIL_LINE = "客户讲：平台係拼多多，单号尾号7890，上个礼拜三已经寄出，要求用WhatsApp联络。\n"
MAIN_PROMPT = _TAIL_LINE * 12
JUDGE_PROMPT = ("你是通话流程判定器。候选意图与判据如下：\n" + _TAIL_LINE * 40 +
                "\n请只输出命中的 id 或 NONE。")


def _fresh(prompt: str, tag: str) -> str:
    """给 prompt 尾上换一个 nonce。

    关键：同一份 prompt 重复请求会走 **前缀缓存**（实测同一判据 prompt 从 4878ms
    掉到 301ms），量出来的是「cache hit + 1 token」——真实链路每轮都是**新 token**
    （转写/尾块在变），所以必须让每次请求都带一段新文本，否则读数系统性偏乐观
    （第一版就是这么得出「没有争用」的假结论）。
    """
    return f"{prompt}【{tag}-{time.time_ns()}】"


def _get_json(base: str, path: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(f"{base}{path}", method="GET")
    with _OPENER.open(req, timeout=timeout) as r:  # noqa: S310 - base 已过回环白名单
        return json.loads(r.read().decode())


def _models(base: str, timeout: float = 15.0) -> str:
    ids = [str(m.get("id") or "") for m in _get_json(base, "/models", timeout).get("data", [])]
    return next((i for i in ids if i.startswith("/")), ids[0] if ids else "")


def _chat_ms(base: str, model: str, prompt: str, *, max_tokens: int, timeout: float = 120.0) -> float:
    """一次 chat 往返墙钟（ms）。max_tokens=1 时≈该 prompt 的 prefill + 首 token。"""
    body = json.dumps({"model": model, "stream": False, "temperature": 0,
                       "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(f"{base}/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.monotonic()
    with _OPENER.open(req, timeout=timeout) as r:  # noqa: S310 - base 已过回环白名单
        json.loads(r.read().decode())
    return (time.monotonic() - t0) * 1000


def _asr_pcm(seconds: float = 4.0) -> bytes:
    with wave.open(str(WAV), "rb") as w:
        n = int(min(w.getnframes(), w.getframerate() * seconds))
        return w.readframes(n)


def _asr_load(base: str, stop: threading.Event, counter: list[int]) -> None:
    """真实用户话音形状：开一个 ASR 会话并持续灌 chunk（partial 解码在跑）。"""
    import httpx

    pcm = _asr_pcm()
    try:
        with httpx.Client(timeout=30) as c:
            sid = c.post(f"{base}/api/start", params={"language": "cantonese"}).json().get("session_id")
            step = 16000 * 2 // 2  # 0.5s
            while not stop.is_set():
                for i in range(0, len(pcm), step):
                    if stop.is_set():
                        break
                    c.post(f"{base}/api/chunk", params={"session_id": sid},
                           content=pcm[i:i + step])
                    time.sleep(0.05)
                counter[0] += 1  # 一整段音频推完算一次
            c.post(f"{base}/api/finish", params={"session_id": sid}, content=pcm[-step:])
    except Exception as exc:  # noqa: BLE001 - 负载侧失败只记，不污染读数
        print(f"  （asr 负载提前退出：{type(exc).__name__}）")


def _judge_load(base: str, stop: threading.Event, counter: list[int]) -> None:
    """后台 judge 形状：9B 反复跑长 prefill + 短输出，直到 stop。

    每次换 nonce（`_fresh`）——真实 judge 的输入是当轮转写，永远是新的；
    复用同一份 prompt 会全走缓存，等于没造负载。
    """
    try:
        model = _models(base)
        while not stop.is_set():
            _chat_ms(base, model, _fresh(JUDGE_PROMPT, "judge"), max_tokens=32)
            counter[0] += 1
    except Exception as exc:  # noqa: BLE001
        print(f"  （judge 负载提前退出：{type(exc).__name__}）")


def _phase(label: str, load, main_base: str, main_model: str, repeats: int) -> list[float]:
    """量 repeats 次 4B prefill 往返；load 为 (stop, counter) -> None 的负载函数（None=裸测）。

    每次请求都换 nonce：不换就会全走前缀缓存，量不到 prefill（见 `_fresh`）。
    """
    stop = threading.Event()
    counter = [0]
    th = None
    if load is not None:
        th = threading.Thread(target=load, args=(stop, counter), daemon=True)
        th.start()
        time.sleep(1.5)  # 让负载先真跑起来，别量到它的冷启动
    samples: list[float] = []
    try:
        for i in range(repeats):
            samples.append(_chat_ms(main_base, main_model, _fresh(MAIN_PROMPT, f"m{i}"), max_tokens=1))
            time.sleep(0.3)
    finally:
        stop.set()
        if th is not None:
            th.join(timeout=30)
    med = stats.median(samples)
    load_note = f" 负载完成 {counter[0]} 次" if load is not None else ""
    print(f"  {label:<22} n={len(samples)} 中位 {med:>7.0f}ms{load_note}  样本 {[round(s) for s in samples]}")
    return samples


def main() -> int:
    main_base, judge_base, asr_base = _endpoint("main"), _endpoint("judge"), _endpoint("asr")
    main_model = _models(main_base)
    print(f"[contention] 4B={Path(main_model).name} rounds={REPEATS} 主 prompt≈{len(MAIN_PROMPT)}字")

    judge_ms = None
    try:
        judge_model = _models(judge_base)
        judge_ms = _chat_ms(judge_base, judge_model, _fresh(JUDGE_PROMPT, "single"), max_tokens=32)
        print(f"  （9B 单跑同形状判据请求：{judge_ms:.0f}ms —— 这就是它每次占 GPU 的时长）")
    except Exception as exc:  # noqa: BLE001
        print(f"  （:1237 不可用，跳过 judge 档：{type(exc).__name__}）")

    idle = _phase("idle（基线）", None, main_base, main_model, REPEATS)
    out: dict[str, float] = {"idle_ms": stats.median(idle)}
    if judge_ms is not None:
        j = _phase("judge 9B 抢", lambda s, c: _judge_load(judge_base, s, c),
                   main_base, main_model, REPEATS)
        out["judge_ms"] = stats.median(j)
        out["judge_delta_ms"] = out["judge_ms"] - out["idle_ms"]
    if WAV.exists():
        a = _phase("asr 解码抢", lambda s, c: _asr_load(asr_base, s, c),
                   main_base, main_model, REPEATS)
        out["asr_ms"] = stats.median(a)
        out["asr_delta_ms"] = out["asr_ms"] - out["idle_ms"]
    else:
        print(f"  （缺 {WAV.name}，跳过 asr 档）")

    print("\n[归因]")
    for key in ("judge_delta_ms", "asr_delta_ms"):
        if key in out:
            who = "9B judge" if key.startswith("judge") else "ASR 解码"
            print(f"  {who:<10} 抢走 {out[key]:>+7.0f}ms（同一份 4B prefill 往返）")
    if out.get("judge_delta_ms", 0) > 200 and out.get("judge_delta_ms", 0) > out.get("asr_delta_ms", 0):
        print("  → 主嫌=9B：修法方向是「judge 只在 4B 空闲窗开火」（错峰），不是动 prompt。")
    elif out.get("asr_delta_ms", 0) > 200:
        print("  → 主嫌=ASR：修法方向在 ASR 侧（partial 抑制/句末 finish 错峰）。")
    else:
        print("  → 两者都不显著：+390ms 固定头另有来源（排队/首 token 开销），换方向查。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
