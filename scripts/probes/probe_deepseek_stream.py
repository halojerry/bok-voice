"""DeepSeek 流式真伪归因探针（2026-10-08 晚波,用户指令:先归因再动刀）。

问题:B 线 MT 腿(DeepSeek chat.completions, stream=true)实测 8/8 请求
first_ms≈mt_ms——疑似服务端整包缓冲(假流式),「LLM 边生成边喂 TTS」被卡死。
用户贴出 Responses API 文档(response.output_text.delta 事件流)——本探针
对同一 payload 三形状逐 delta 计时(SSE 逐行到达时刻),回答:

  1. chat.completions 流式对「翻译长度输出」是否真流(delta 逐个到 or 同毫秒 burst)?
  2. Responses API(/responses)流式是否真流?首 delta 何时到?
  3. 网络腿 vs 生成腿:TTFB 基线(无鉴权请求)多少?
  4. 上下文缓存命中:同前缀重复请求 cached_tokens(prompt_cache_hit_tokens /
     input_tokens_details.cached_tokens)是否命中(投机翻译重复 span 的账单面)。

判读口径:
  burst_factor = 总时长/(首 delta 到末 delta 跨度):≈1=整包缓冲;真间隔
  (>30ms)数多=真流。first_delta 相对请求发出=喂 TTS 的解锁时刻。

用法:
  .venv312/bin/python scripts/probes/probe_deepseek_stream.py
env:
  DEEPSEEK_MODEL     缺省 deepseek-flash(用户定档)
凭据:设置库 model_routing_json mt/a_reply 车道(服务端只读,绝不打印;对齐
probe_voice_style_gate 的 live 面:base_url 过 SSRF 白名单后才发请求)。
出站:仅 https://api.deepseek.com(白名单单点,拒端口/凭据注入)。
"""
from __future__ import annotations
# --- scripts import bootstrap (G1) ---
import sys as _sys, pathlib as _pathlib
_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))

import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DB = Path.home() / "Library/Application Support/BokVoice/bok_voice.db"
_ALLOWED_LIVE_HOSTS = frozenset({"api.deepseek.com"})


def _url_ok(url: str) -> bool:
    """SSRF 白名单(probe_voice_style_gate 同款单点):https + 钉死官方域。"""
    parts = urllib.parse.urlsplit(str(url or ""))
    return (
        parts.scheme == "https"
        and (parts.hostname or "").lower() in _ALLOWED_LIVE_HOSTS
        and parts.port in (None, 443)
        and not parts.username
        and not parts.password
    )


# MT 生产形状的代表性 payload(镜像 _build_mt_context:系统指令+滚动对+当前句;
# 长度对齐生产,内容为代表性翻译指令——计时归因不需要逐字节同产线)。
SYSTEM = (
    "你是粤语同声传译译员。把用户提供的普通话口语即时翻译成地道粤语口语。"
    "要求:只输出译文,不解释不回答;ASR 转写可能含同音误听,结合上下文修正明显"
    "误识后翻译;绝不虚构内容;语气词照译;专有名词按术语表。术语表:顺丰=SF Express。"
)
PAIRS = [
    ("你好呀我想问一下,你们这个集运怎么收费?", "你好呀,我想问下你哋呢个集运点收费?"),
    ("上个月那批货到广州要几天?", "上个月嗰批货到广州要几日?"),
]
SENTS = [
    "我们坐高铁过去拿,顺便看看那边的仓库环境。",
    "如果数量比较大的话,运费能不能再便宜一点点,毕竟我们是长期合作的老客户了。",
]


def _load_lane() -> tuple[str, str, str]:
    """设置库 mt/a_reply 车道读 (base_url, model, key);缺库回 env 缺省。"""
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").strip().rstrip("/")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT model_routing_json FROM global_settings LIMIT 1"
        ).fetchone()
        conn.close()
        lanes = (json.loads(row[0]) if row else {}).get("lanes") or {}
        for lane in ("mt", "a_reply"):
            entry = lanes.get(lane) or {}
            if str(entry.get("provider") or "") != "openai":
                continue
            b = str(entry.get("base_url") or "").strip().rstrip("/")
            m = str(entry.get("model") or "").strip()
            k = str(entry.get("api_key") or "").strip()
            if b and m and k:
                return b, m, k
    except Exception as exc:  # noqa: BLE001 - 库缺失/损坏回 env 缺省
        print(f"[probe] settings db unavailable ({exc!r}) — env fallback", flush=True)
    return base, model, key


def _messages(sent: str):
    msgs = [{"role": "system", "content": SYSTEM}]
    for src, dst in PAIRS:
        msgs.append({"role": "user", "content": src})
        msgs.append({"role": "assistant", "content": dst})
    msgs.append({"role": "user", "content": sent})
    return msgs


def _post_sse(url: str, key: str, body: dict):
    """POST + SSE 逐行迭代(yield (到达时刻, 当前行))。url 已过白名单。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=60)
    return resp


def _profile(name: str, deltas: list[tuple[float, str]], total_s: float, usage: dict) -> None:
    n = len(deltas)
    if n == 0:
        print(f"  {name}: NO DELTAS total={total_s*1000:.0f}ms usage={usage}", flush=True)
        return
    first, last = deltas[0][0], deltas[-1][0]
    span = max(last - first, 1e-9)
    burst = total_s / span
    gaps = [deltas[i + 1][0] - deltas[i][0] for i in range(n - 1)] or [0.0]
    spread = sum(1 for g in gaps if g > 0.03)  # >30ms 的真间隔数
    cached = usage.get("prompt_cache_hit_tokens") or usage.get("cached_tokens") or 0
    total_tok = usage.get("prompt_tokens") or usage.get("input_tokens") or "?"
    print(
        f"  {name}: deltas={n} first={first*1000:.0f}ms span={span*1000:.0f}ms "
        f"total={total_s*1000:.0f}ms burst={burst:.2f} spread30ms={spread} "
        f"cached={cached}/{total_tok} text={sum(len(t) for _, t in deltas)}字",
        flush=True,
    )


def run_chat_stream(base: str, key: str, model: str, sent: str) -> None:
    t0 = time.perf_counter()
    deltas: list[tuple[float, str]] = []
    usage: dict = {}
    resp = _post_sse(
        base + "/chat/completions", key,
        {
            "model": model, "messages": _messages(sent), "stream": True,
            "max_tokens": 256, "temperature": 0.3,
            "thinking": {"type": "disabled"},
        },
    )
    for raw in resp:
        t = time.perf_counter() - t0
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        try:
            j = json.loads(line[6:])
        except Exception:  # noqa: BLE001
            continue
        if isinstance(j.get("usage"), dict):
            usage = {
                "prompt_tokens": j["usage"].get("prompt_tokens", 0),
                "prompt_cache_hit_tokens": j["usage"].get("prompt_cache_hit_tokens", 0),
            }
        for ch in j.get("choices") or []:
            piece = (((ch.get("delta") or {}).get("content")) or "")
            if piece:
                deltas.append((t, piece))
    _profile("chat_stream   ", deltas, time.perf_counter() - t0, usage)


def run_responses_stream(base_root: str, key: str, model: str, sent: str) -> None:
    """base_root=https://api.deepseek.com(去 /v1 的根);/responses 端点。"""
    t0 = time.perf_counter()
    deltas: list[tuple[float, str]] = []
    usage: dict = {}
    items = [{"role": m["role"], "content": m["content"]} for m in _messages(sent)][1:]
    resp = _post_sse(
        base_root + "/responses", key,
        {
            "model": model, "instructions": SYSTEM, "input": items,
            "stream": True, "max_output_tokens": 256, "temperature": 0.3,
            # Responses API 思考开关=reasoning.effort("none"=关;不传默认思考全开
            # 烧光 max_output_tokens=纯 reasoning_text.delta 零 output_text)。
            "reasoning": {"effort": "none"},
        },
    )
    for raw in resp:
        t = time.perf_counter() - t0
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data: "):
            continue
        try:
            j = json.loads(line[6:])
        except Exception:  # noqa: BLE001
            continue
        et = str(j.get("type") or "")
        if et == "response.output_text.delta" and j.get("delta"):
            deltas.append((t, str(j["delta"])))
        elif et == "response.completed":
            u = (j.get("response") or {}).get("usage") or {}
            usage = {
                "input_tokens": u.get("input_tokens", 0),
                "cached_tokens": ((u.get("input_tokens_details") or {}).get("cached_tokens", 0)),
            }
    _profile("responses_str ", deltas, time.perf_counter() - t0, usage)


def run_chat_nonstream(base: str, key: str, model: str, sent: str) -> None:
    t0 = time.perf_counter()
    resp = _post_sse(
        base + "/chat/completions", key,
        {
            "model": model, "messages": _messages(sent), "stream": False,
            "max_tokens": 256, "temperature": 0.3,
            "thinking": {"type": "disabled"},
        },
    )
    j = json.loads(resp.read().decode("utf-8", "replace"))
    total = time.perf_counter() - t0
    u = j.get("usage") or {}
    text = ""
    for ch in j.get("choices") or []:
        text += (((ch.get("message") or {}).get("content")) or "")
    print(
        f"  chat_nonstrm : total={total*1000:.0f}ms "
        f"cached={u.get('prompt_cache_hit_tokens', 0)}/{u.get('prompt_tokens', '?')} "
        f"text={len(text)}字",
        flush=True,
    )


def network_baseline(base_root: str, key: str) -> None:
    """TTFB 基线:无鉴权 POST(401=已到达)。URL 同白名单域。"""
    t0 = time.perf_counter()
    req = urllib.request.Request(
        base_root + "/v1/chat/completions", data=b"{}",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        code = resp.status
        resp.read()
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as exc:  # noqa: BLE001
        print(f"  net_baseline  : FAILED {exc!r}", flush=True)
        return
    print(f"  net_baseline  : http={code} ttfb={(time.perf_counter()-t0)*1000:.0f}ms(DNS+TCP+TLS+server)", flush=True)


def main() -> int:
    base, model, key = _load_lane()
    base_root = base[: -len("/v1")] if base.endswith("/v1") else base
    for u in (base, base_root):
        if not _url_ok(u + "/chat/completions") and not _url_ok(u):
            print(f"[probe] endpoint failed SSRF allowlist: {u}", flush=True)
            return 2
    if not key:
        print("no api key (settings db + env both empty)", flush=True)
        return 2
    print(f"base={base_root} model={model}", flush=True)
    network_baseline(base_root, key)
    for sent in SENTS:
        print(f"payload: {sent}", flush=True)
        run_chat_stream(base, key, model, sent)
        run_responses_stream(base_root, key, model, sent)
        run_chat_nonstream(base, key, model, sent)
        print("  (repeat same payload → cache-hit arm)", flush=True)
        run_chat_stream(base, key, model, sent)
        run_responses_stream(base_root, key, model, sent)
    print(
        "\n判读: burst≈1 且 spread30ms=0 → 整包缓冲(假流式);responses_str 的"
        " first=LLM→TTS 可解锁时刻;cached 高位=前缀缓存命中。",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
