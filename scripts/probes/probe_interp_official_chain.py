#!/usr/bin/env python3
"""B 线同传官方化裸链探针:DeepSeek 流式 token 逐个直发 MiniMax t2a_v2_bidi。

问题(W7 probe P3,2026-10-08):官方栈方案=LLM 边生成边把每个 delta 原样喂
MiniMax bidi(服务端攒句合成),验证裸链物理:

  C1 token 节奏(zh→en):首 token 延迟 / delta 间隔 p50 p90 max / token 总数 /
      整条耗时——delta 节奏够不够密,bidi 攒句会不会被饿着。
  C2 裸链 EVS:t0=发起 DeepSeek 请求,每个 delta 立即 task_continue 直发
      (一 delta 一条,全标点保留);记 t_first_audio / t_sentence1_end /
      t_all_audio。三关键数:t0→首音频(链路 EVS)/ 首句音频时长 / 整句音频
      总时长。方向:zh→en(English 音色)与 zh→cantonese(Cantonese_GentleLady)。
  C3 投机预热:同一 utterance 先发「前缀请求」(前 10 字,max_tokens=1,纯预热
      吃上下文缓存)紧接完整请求测 TTFT;对照无预热的冷启 TTFT。
  C4 峰值观察:本时段全部请求的 TTFT 分布 + 尾部 3 连发重复请求(时段效应)。

协议(镜像 providers/livekit_plugins.py _MiniMaxBidiSession,最小客户端):
  connect → connected_success → task_start{model,voice_setting,audio_setting,
  language_boost} → task_started → task_continue{text} → task_continued
  (data.audio hex;extra_info.audio_length 毫秒) → sentence_end → 收尾
  task_flush → task_flushed(空闲排干) → task_finish。
  B 线合成档=speech-2.8-turbo(_resolve_minimax_model 同缺省);语速 zh/粤 1.2、
  en 1.0(minimax_speed_for 同源);音频 24k mono pcm。

凭据(env 优先,缺省回设置库只读,绝不打印 key):
  DeepSeek  DEEPSEEK_API_KEY → global_settings.model_routing_json 的
            mt/a_reply 车道(provider=openai,base_url/model/api_key;模型用车道
            解析结果,现役 deepseek-flash;thinking 缺省关)。
  MiniMax   MINIMAX_API_KEY → global_settings.tts_json.api_key。
出站 allowlist(https/wss only,frozenset 钉死):api.deepseek.com +
api.minimax.cn + api.minimax.chat(MINIMAX_REGION=intl 时用 .chat)。

用法:
  .venv312/bin/python scripts/probes/probe_interp_official_chain.py \
      [--report reports/w7probe/p3-chain.md] [--skip c1|c3|c4 ...]
报告另存 reports/w7probe/p3-chain.md(reports/ 已 gitignore,本地证据)。
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
import asyncio
import json
import math
import os
import sqlite3
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import httpx
import websockets

DB = Path(os.environ.get("BOK_PROBE_DB", "")) if os.environ.get("BOK_PROBE_DB") else (
    Path.home() / "Library/Application Support/BokVoice/bok_voice.db"
)
_ALLOWED_HOSTS = frozenset({"api.deepseek.com", "api.minimax.cn", "api.minimax.chat"})

# 测量语料(15-25 字,句末标点):产品场景代表性口吻。
UTTERANCES = [
    "我们这批货的验货报告今天下午会发给您。",
    "你先把合同里的违约条款再确认一遍。",
    "下周三之前必须完成清关不然赶不上船期。",
]

# 音色/boost/语速(镜像 interpret.py boost_map 与 minimax_speed_for;音色 id 是
# MiniMax API 外部标识符)。zh 档列出备查(C2 只跑 en/cantonese 两个译文方向)。
_VOICES = {
    "zh": "Chinese (Mandarin)_News_Anchor",
    "en": "English_magnetic_voiced_man",
    "cantonese": "Cantonese_GentleLady",
}
_BOOST = {"zh": "Chinese", "en": "English", "cantonese": "Chinese,Yue"}
_SPEED = {"zh": 1.2, "en": 1.0, "cantonese": 1.2}
_TARGET = {"en": "标准英语", "cantonese": "地道粤语"}
C2_DIRECTIONS = ("en", "cantonese")
SAMPLE_RATE = 24000
BYTES_PER_MS = SAMPLE_RATE * 2 / 1000.0  # pcm16 mono


def _out_ok(url: str, scheme: str) -> bool:
    """出站白名单:钉死 scheme + 官方域,拒端口/凭据注入。"""
    parts = urllib.parse.urlsplit(str(url or ""))
    return (
        parts.scheme == scheme
        and (parts.hostname or "").lower() in _ALLOWED_HOSTS
        and parts.port in (None, 443)
        and not parts.username
        and not parts.password
    )


def _load_llm_lane() -> tuple[str, str, str]:
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


def _load_minimax_key() -> str:
    """MiniMax key:env MINIMAX_API_KEY 优先,缺省回设置面 tts 段 api_key。"""
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if key:
        return key
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        row = conn.execute("SELECT tts_json FROM global_settings LIMIT 1").fetchone()
        conn.close()
        return str(((json.loads(row[0]) if row else {}) or {}).get("api_key") or "").strip()
    except Exception as exc:  # noqa: BLE001
        print(f"[probe] tts settings unavailable ({exc!r})", flush=True)
        return ""


def _minimax_model() -> str:
    """B 线合成档缺省 speech-2.8-turbo(interpret._resolve_minimax_model 同缺省)。"""
    return os.environ.get("MINIMAX_MODEL", "").strip() or "speech-2.8-turbo"


def _minimax_ws_endpoint() -> str:
    region = os.environ.get("MINIMAX_REGION", "cn").strip().lower()
    host = "api.minimax.chat" if region in {"intl", "global", "chat"} else "api.minimax.cn"
    return f"wss://{host}/ws/v1/t2a_v2_bidi"


def _system_for(target: str) -> str:
    return f"你是同声传译员，把用户的话翻成{_TARGET[target]}，只输出译文，保留标点，不解释。"


def _cached_of(usage: dict) -> str:
    if not usage:
        return "?"
    hit = usage.get("prompt_cache_hit_tokens")
    total = usage.get("prompt_tokens")
    if hit is None:
        return "?"
    return f"{hit}/{total}"


def pct(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = max(0, min(len(s) - 1, math.ceil(q / 100 * len(s)) - 1))
    return s[idx]


# ---- DeepSeek 流式最小客户端(镜像 probe_deepseek_stream:thinking 缺省关) ----

async def ds_stream(
    client: httpx.AsyncClient,
    url: str,
    key: str,
    model: str,
    messages: list[dict],
    *,
    max_tokens: int = 256,
    on_delta=None,
) -> dict:
    """POST /chat/completions stream=true;逐 delta 记到达时刻。返回结果 dict。"""
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "thinking": {"type": "disabled"},  # DeepSeek 思考缺省开,小预算会静默哑火
    }
    t0 = time.monotonic()
    res: dict = {
        "t0": t0, "ttft_ms": None, "total_ms": None, "deltas": [], "text": "",
        "usage": {}, "error": None,
    }
    try:
        async with client.stream(
            "POST", url, json=body, headers={"Authorization": f"Bearer {key}"}
        ) as resp:
            if resp.status_code != 200:
                raw = await resp.aread()
                res["error"] = f"http {resp.status_code}: {raw[:200]!r}"
                return res
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    j = json.loads(payload)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(j.get("usage"), dict):
                    res["usage"] = j["usage"]
                for ch in j.get("choices") or []:
                    piece = ((ch.get("delta") or {}).get("content")) or ""
                    if piece:
                        t = time.monotonic()
                        res["deltas"].append((t, piece))
                        res["text"] += piece
                        if res["ttft_ms"] is None:
                            res["ttft_ms"] = (t - t0) * 1000
                        if on_delta is not None:
                            await on_delta(piece)
    except Exception as exc:  # noqa: BLE001
        res["error"] = repr(exc)
        return res
    res["total_ms"] = (time.monotonic() - t0) * 1000
    return res


async def ds_prefix_warm(
    client: httpx.AsyncClient, url: str, key: str, model: str, messages: list[dict]
) -> tuple[float, str]:
    """前缀预热请求(max_tokens=1,非流式)。返回 (耗时 ms, 错误)。"""
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "max_tokens": 1,
        "temperature": 0.3,
        "thinking": {"type": "disabled"},
    }
    t0 = time.monotonic()
    try:
        resp = await client.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        if resp.status_code != 200:
            return (time.monotonic() - t0) * 1000, f"http {resp.status_code}: {resp.text[:120]!r}"
        return (time.monotonic() - t0) * 1000, ""
    except Exception as exc:  # noqa: BLE001
        return (time.monotonic() - t0) * 1000, repr(exc)


# ---- MiniMax t2a_v2_bidi 最小客户端 ----

class BidiTts:
    """一链路一连接(生产 prewarm 姿势:先连好,t0 只从 LLM 请求起算)。"""

    def __init__(self, ws_url: str, key: str, voice: str, boost: str, speed: float):
        self.ws_url = ws_url
        self.key = key
        self.voice = voice
        self.boost = boost
        self.speed = speed
        self.ws = None
        self.connect_ms = 0.0
        self.start_ms = 0.0

    def _task_start(self) -> dict:
        start = {
            "event": "task_start",
            "model": _minimax_model(),
            "voice_setting": {"voice_id": self.voice, "speed": self.speed, "vol": 1.0, "pitch": 0},
            "audio_setting": {"sample_rate": SAMPLE_RATE, "format": "pcm", "channel": 1},
            "stream_options": {"exclude_aggregated_audio": True},
        }
        if self.boost:
            start["language_boost"] = self.boost
        return start

    async def connect(self) -> None:
        t = time.monotonic()
        self.ws = await websockets.connect(
            self.ws_url,
            additional_headers={"Authorization": f"Bearer {self.key}"},
            open_timeout=10,
            max_size=20_000_000,
            ping_interval=None,  # 官方:服务端永不 ping;短跑探针免自管 ping
        )
        self.connect_ms = (time.monotonic() - t) * 1000
        try:  # connected_success:丢帧不致命(同 provider 语义)
            await asyncio.wait_for(self.ws.recv(), timeout=10)
        except Exception:  # noqa: BLE001
            pass
        await self.ws.send(json.dumps(self._task_start()))
        resp = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=15))
        if resp.get("event") != "task_started":
            raise RuntimeError(f"task_start failed: {str(resp)[:200]}")
        self.start_ms = (time.monotonic() - t) * 1000

    async def close(self) -> None:
        if self.ws is None:
            return
        try:
            await self.ws.close()
        except Exception:  # noqa: BLE001
            pass
        self.ws = None


# ---- C2:单条裸链跑(DeepSeek delta 直发 bidi) ----

async def chain_run(
    client: httpx.AsyncClient, ds_url: str, ds_key: str, ds_model: str,
    utt: str, target: str, ws_url: str, mm_key: str,
) -> dict:
    """t0=发起 DeepSeek 请求;每 delta 即时 task_continue;收 flush 排干。"""
    tts = BidiTts(ws_url, mm_key, _VOICES[target], _BOOST[target], _SPEED[target])
    await tts.connect()  # 预热连接(生产姿势),connect/start 耗时单列
    audio = bytearray()
    st: dict = {
        "first_audio": None, "sentence1_end": None, "bytes_at_s1": 0,
        "sentences": 0, "extra_len_ms": 0.0, "last_audio": None,
        "flushed": False, "error": None,
    }
    t0 = 0.0

    async def send_delta(piece: str) -> None:
        await tts.ws.send(json.dumps({"event": "task_continue", "text": piece}))

    async def recv_loop() -> None:
        while True:
            timeout = 0.8 if st["flushed"] else 30.0
            try:
                raw = await asyncio.wait_for(tts.ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                if st["flushed"]:
                    return  # 尾巴排干
                continue
            except Exception as exc:  # noqa: BLE001
                st["error"] = f"ws recv: {exc!r}"
                return
            t = time.monotonic()
            try:
                msg = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                continue
            event = msg.get("event") or ""
            data = msg.get("data") or {}
            audio_hex = data.get("audio") or ""
            if audio_hex:
                chunk = bytes.fromhex(audio_hex)
                audio.extend(chunk)
                if st["first_audio"] is None:
                    st["first_audio"] = t
                st["last_audio"] = t
            extra = msg.get("extra_info") or data.get("extra_info") or {}
            alen = extra.get("audio_length")
            if isinstance(alen, (int, float)):
                st["extra_len_ms"] += float(alen)
            if event == "sentence_end":
                st["sentences"] += 1
                if st["sentence1_end"] is None:
                    st["sentence1_end"] = t
                    st["bytes_at_s1"] = len(audio)
            if event == "task_flushed":
                st["flushed"] = True
            if event == "task_failed":
                st["error"] = f"task_failed: {str(msg)[:200]}"
                return

    recv_task = asyncio.create_task(recv_loop())
    messages = [{"role": "system", "content": _system_for(target)}, {"role": "user", "content": utt}]
    ds = await ds_stream(client, ds_url, ds_key, ds_model, messages, on_delta=send_delta)
    t0 = ds["t0"]
    try:
        await tts.ws.send(json.dumps({"event": "task_flush"}))
    except Exception as exc:  # noqa: BLE001
        st["error"] = st["error"] or f"flush send: {exc!r}"
    await recv_task
    try:  # 关连接(官方:task_finish 后服务端吐完剩余关)
        await tts.ws.send(json.dumps({"event": "task_finish"}))
        while True:
            raw = await asyncio.wait_for(tts.ws.recv(), timeout=1.0)
            if json.loads(raw).get("event") == "task_finished":
                break
    except Exception:  # noqa: BLE001
        pass
    await tts.close()

    total_audio_ms = len(audio) / BYTES_PER_MS
    return {
        "target": target, "utt": utt, "ds": ds, "t0": t0,
        "connect_ms": tts.connect_ms, "start_ms": tts.start_ms,
        "evs_ms": (st["first_audio"] - t0) * 1000 if st["first_audio"] else None,
        "sentence1_ms": (st["sentence1_end"] - t0) * 1000 if st["sentence1_end"] else None,
        "s1_audio_ms": st["bytes_at_s1"] / BYTES_PER_MS,
        "total_audio_ms": total_audio_ms, "extra_len_ms": st["extra_len_ms"],
        "sentences": st["sentences"], "error": ds["error"] or st["error"],
        "text": ds["text"],
    }


# ---- 报告 ----

class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, line: str = "") -> None:
        self.lines.append(line)
        print(line, flush=True)

    def table(self, headers: list[str], rows: list[list]) -> None:
        self.add("| " + " | ".join(headers) + " |")
        self.add("|" + "|".join(["---"] * len(headers)) + "|")
        for row in rows:
            self.add("| " + " | ".join(str(c) for c in row) + " |")


def gap_stats(deltas: list[tuple[float, str]]) -> tuple[float, float, float]:
    gaps = [(deltas[i + 1][0] - deltas[i][0]) * 1000 for i in range(len(deltas) - 1)]
    if not gaps:
        return 0.0, 0.0, 0.0
    return statistics.median(gaps), pct(gaps, 90), max(gaps)


async def net_baseline(client: httpx.AsyncClient, base_root: str) -> float:
    """TTFB 基线(无鉴权 POST,401=已到达;顺带把连接池焐热,冷启臂不吃 TLS 钱)。"""
    t0 = time.monotonic()
    try:
        resp = await client.post(base_root + "/v1/chat/completions", json={})
        ms = (time.monotonic() - t0) * 1000
        print(f"[probe] net baseline (unauth POST): http={resp.status_code} ttfb={ms:.0f}ms", flush=True)
        return ms
    except Exception as exc:  # noqa: BLE001
        print(f"[probe] net baseline FAILED {exc!r}", flush=True)
        return -1.0


async def run_all(args, ds_base: str, ds_model: str, ds_key: str, mm_key: str, rep: Report) -> int:
    ds_url = ds_base + "/chat/completions"
    ws_url = _minimax_ws_endpoint()
    for u, sch in ((ds_url, "https"), (ws_url, "wss")):
        if not _out_ok(u, sch):
            rep.add(f"[probe] endpoint failed allowlist: {u}")
            return 2
    all_full: list[tuple[str, float]] = []  # (phase 标签, TTFT ms) — C4 全量
    failures: list[str] = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        rep.add(f"- time: {datetime.now().isoformat(timespec='seconds')}")
        rep.add(f"- deepseek: base={ds_base} model={ds_model} key={'present' if ds_key else 'MISSING'}")
        rep.add(f"- minimax: ws={ws_url} model={_minimax_model()} key={'present' if mm_key else 'MISSING'}")
        await net_baseline(client, ds_base[: -len("/v1")] if ds_base.endswith("/v1") else ds_base)

        # ---- C3 投机预热(先跑:冷启臂要在任何缓存灼烧前取样) ----
        if "c3" not in args.skip:
            rep.add("", )
            rep.add("## C3 投机预热(冷启 TTFT vs 前 10 字 max_tokens=1 预热后 TTFT)")
            rep.table(
                ["#", "utterance", "cold TTFT ms", "prefix req ms", "warm TTFT ms", "Δms",
                 "cold cached", "warm cached", "冷启译文首 12 字"],
                [],
            )
            for i, utt in enumerate(UTTERANCES, 1):
                sysmsg = _system_for("en")
                cold = await ds_stream(client, ds_url, ds_key, ds_model,
                                       [{"role": "system", "content": sysmsg}, {"role": "user", "content": utt}])
                if cold["error"]:
                    failures.append(f"C3#{i} cold: {cold['error']}")
                pre_ms, pre_err = await ds_prefix_warm(
                    client, ds_url, ds_key, ds_model,
                    [{"role": "system", "content": sysmsg}, {"role": "user", "content": utt[:10]}],
                )
                if pre_err:
                    failures.append(f"C3#{i} prefix: {pre_err}")
                warm = await ds_stream(client, ds_url, ds_key, ds_model,
                                       [{"role": "system", "content": sysmsg}, {"role": "user", "content": utt}])
                if warm["error"]:
                    failures.append(f"C3#{i} warm: {warm['error']}")
                cold_t, warm_t = cold["ttft_ms"], warm["ttft_ms"]
                if cold_t is not None:
                    all_full.append(("c3-cold", cold_t))
                if warm_t is not None:
                    all_full.append(("c3-warm", warm_t))
                delta = (warm_t - cold_t) if (cold_t is not None and warm_t is not None) else None
                rep.add(
                    f"| {i} | {utt} | {cold_t and round(cold_t)} | {round(pre_ms)} "
                    f"| {warm_t and round(warm_t)} | {delta is not None and round(delta)} "
                    f"| {_cached_of(cold['usage'])} | {_cached_of(warm['usage'])} "
                    f"| {cold['text'][:12]} |"
                )
                await asyncio.sleep(0.3)

        # ---- C1 token 节奏(zh→en 流式,无 TTS) ----
        if "c1" not in args.skip:
            rep.add("")
            rep.add("## C1 token 节奏(zh→en,DeepSeek 流式)")
            rep.table(
                ["#", "utterance", "首 token ms", "间隔 p50/p90/max ms", "deltas", "字数",
                 "整条 ms", "cached"],
                [],
            )
            for i, utt in enumerate(UTTERANCES, 1):
                ds = await ds_stream(client, ds_url, ds_key, ds_model,
                                     [{"role": "system", "content": _system_for("en")},
                                      {"role": "user", "content": utt}])
                if ds["error"]:
                    failures.append(f"C1#{i}: {ds['error']}")
                if ds["ttft_ms"] is not None:
                    all_full.append(("c1", ds["ttft_ms"]))
                g50, g90, gmax = gap_stats(ds["deltas"])
                rep.add(
                    f"| {i} | {utt} | {ds['ttft_ms'] and round(ds['ttft_ms'])} "
                    f"| {g50:.0f}/{g90:.0f}/{gmax:.0f} | {len(ds['deltas'])} "
                    f"| {len(ds['text'])} | {ds['total_ms'] and round(ds['total_ms'])} "
                    f"| {_cached_of(ds['usage'])} |"
                )

        # ---- C2 裸链 EVS(DeepSeek delta 直发 bidi) ----
        if "c2" not in args.skip:
            if not mm_key:
                failures.append("C2 skipped: no MiniMax key (env MINIMAX_API_KEY / settings tts.api_key 均空)")
            else:
                for target in C2_DIRECTIONS:
                    rep.add("")
                    rep.add(f"## C2 裸链 EVS(zh→{target},voice={_VOICES[target]},boost={_BOOST[target]})")
                    rep.table(
                        ["#", "utterance", "EVS t0→首audio ms", "LLM TTFT ms", "首δ→audio ms",
                         "句数", "首句 audio ms", "总 audio ms", "connect/start ms", "状态"],
                        [],
                    )
                    for i, utt in enumerate(UTTERANCES, 1):
                        try:
                            r = await chain_run(client, ds_url, ds_key, ds_model,
                                                utt, target, ws_url, mm_key)
                        except Exception as exc:  # noqa: BLE001
                            failures.append(f"C2 {target}#{i}: {exc!r}")
                            rep.add(f"| {i} | {utt} | - | - | - | - | - | - | - | FAIL {exc!r} |")
                            continue
                        if r["ds"]["ttft_ms"] is not None:
                            all_full.append((f"c2-{target}", r["ds"]["ttft_ms"]))
                        if r["error"]:
                            failures.append(f"C2 {target}#{i}: {r['error']}")
                        d2a = (r["evs_ms"] - r["ds"]["ttft_ms"]) if (
                            r["evs_ms"] is not None and r["ds"]["ttft_ms"] is not None
                        ) else None
                        rep.add(
                            f"| {i} | {utt} | {r['evs_ms'] is not None and round(r['evs_ms'])} "
                            f"| {r['ds']['ttft_ms'] and round(r['ds']['ttft_ms'])} "
                            f"| {d2a is not None and round(d2a)} | {r['sentences']} "
                            f"| {round(r['s1_audio_ms'])} | {round(r['total_audio_ms'])} "
                            f"| {round(r['connect_ms'])}/{round(r['start_ms'])} "
                            f"| {'FAIL ' + r['error'] if r['error'] else 'ok'} |"
                        )
                        if r["text"]:
                            rep.add(f"    译文: {r['text'][:80]}")

        # ---- C4 峰值观察(3 连发重复请求 + 全时段分布) ----
        if "c4" not in args.skip:
            rep.add("")
            rep.add("## C4 峰值观察")
            rep.add("3 连发(同一 utterance zh→en 背靠背,看时段/限流效应):")
            tail: list[float] = []
            for k in range(3):
                ds = await ds_stream(client, ds_url, ds_key, ds_model,
                                     [{"role": "system", "content": _system_for("en")},
                                      {"role": "user", "content": UTTERANCES[0]}])
                if ds["error"]:
                    failures.append(f"C4#{k + 1}: {ds['error']}")
                if ds["ttft_ms"] is not None:
                    tail.append(ds["ttft_ms"])
                    all_full.append(("c4-tail", ds["ttft_ms"]))
                rep.add(
                    f"  #{k + 1}: ttft={ds['ttft_ms'] and round(ds['ttft_ms'])}ms "
                    f"total={ds['total_ms'] and round(ds['total_ms'])}ms "
                    f"cached={_cached_of(ds['usage'])}"
                )

    # ---- C4 分布汇总 ----
    rep.add("")
    rep.add("### 本时段全部请求 TTFT 分布(C3 两臂 + C1 + C2 + C4 尾部)")
    if all_full:
        vals = [v for _, v in all_full]
        rep.add(f"- n={len(vals)} p50={pct(vals, 50):.0f}ms p90={pct(vals, 90):.0f}ms "
                f"max={max(vals):.0f}ms min={min(vals):.0f}ms")
        by_phase: dict[str, list[float]] = {}
        for ph, v in all_full:
            by_phase.setdefault(ph, []).append(v)
        for ph, vv in by_phase.items():
            rep.add(f"- {ph}: n={len(vv)} p50={pct(vv, 50):.0f} max={max(vv):.0f}")

    rep.add("")
    rep.add("## 失败/异常")
    if failures:
        for f in failures:
            rep.add(f"- {f}")
    else:
        rep.add("- 无")

    # ---- 总结论 ----
    rep.add("")
    rep.add("## 判读")
    rep.add("- C2 EVS = t0(发 DeepSeek 请求)→首个 task_continued 音频块;bidi 连接已预连"
            "(生产 prewarm 姿势),connect/start 单列可加总成冷链。")
    rep.add("- C1 间隔 p90 大 → bidi 攒句被饿(次级标点/空闲兜底才切);小 → delta 节奏喂得饱。")
    rep.add("- C3 Δ=warm−cold,负值=前缀预热有效;cached 列看上下文缓存实命中率。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--report", default="reports/w7probe/p3-chain.md")
    ap.add_argument("--skip", nargs="*", default=[], help="c1|c2|c3|c4 任选")
    args = ap.parse_args()

    ds_base, ds_model, ds_key = _load_llm_lane()
    mm_key = _load_minimax_key()
    if not ds_key:
        print("no DeepSeek key (env DEEPSEEK_API_KEY + settings mt/a_reply lane both empty)", flush=True)
        return 2
    rep = Report()
    rep.add("# W7 probe P3: DeepSeek 流式 token 直发 MiniMax bidi 裸链")
    rep.add(f"- utterances: {len(UTTERANCES)} 条 / directions: {'+'.join(C2_DIRECTIONS)}")
    rep.add(f"- db: {DB} (read-only)")
    try:
        code = asyncio.run(run_all(args, ds_base, ds_model, ds_key, mm_key, rep))
    finally:
        out = Path(__file__).resolve().parents[2] / args.report
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(rep.lines) + "\n", encoding="utf-8")
        print(f"[probe] report → {out}", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
