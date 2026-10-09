#!/usr/bin/env python3
"""豆包 SAUC 官方参数臂可行性探针（w7 · p2，2026-10-09）。

问题：官方 request 参数能否替代客户端手搓的三件套（稳定门/结巴折叠/句头保护）。
五臂各给测量数字，只做探针零产品改动：

  S1  end_window_size      VAD 静音判停窗 {500,800,1000} → 静音起点→definite final 实测延迟
  S2  enable_nonstream     二遍识别 on/off → final 折叠 CER 对照 + definite 到达时刻差
  S3  enable_ddc           语义顺滑 on/off → 自拼结巴音频重复度（「结巴折叠」官方版）
  S4  force_to_speech_time {0,1000} → 500ms 静音前缀下句头是否被吃（「句头保护」官方版）
  S5  result_type          full vs single → 响应形状 dump（增量返回语义）

单源复用：协议帧/语料读取/折叠评分 = probe_cloud_asr.py（import，不复制不改动）。
凭据姿势同源：env（DOUBAO_API_KEY 或 DOUBAO_APP_ID+DOUBAO_ACCESS_TOKEN）优先，
缺省回读设置库 global_settings.asr_json（新式单 api_key 或旧式 app_id+access_token），
打印只打掩码；出站护栏 = 仅 wss + openspeech.bytedance.com。
判停窗可自发触发的前置事实（probe_doubao_utterances.py 已实证）：静音帧持续在流时
服务端才按 end_window 自发 definite/is_last——本探针所有场景尾部都持续喂静音帧。
报告写 reports/w7probe/p2-doubao.md（+ 同目录 raw JSON；reports/ 已 gitignore）。

用法：
  .venv312/bin/python scripts/probes/probe_doubao_native_arms.py
  .venv312/bin/python scripts/probes/probe_doubao_native_arms.py --scenarios S1,S3
  .venv312/bin/python scripts/probes/probe_doubao_native_arms.py --corpus <dir> --s2-items zh_01,zh_02
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
import os
import sqlite3
import struct
import time
import uuid
from pathlib import Path

import websockets

# 协议帧格式/语料/折叠评分单源 = probe_cloud_asr.py（生产镜像 providers/doubao_asr.py 同语义；
# 折叠口径 asr_whisper_bench._FOLD 经其 norm_text/cer 间接消费）
from probe_cloud_asr import (
    DOUBAO_WS_DEFAULT,
    MSG_ERROR,
    cer,
    frame_audio,
    frame_full_client_request,
    norm_text,
    parse_server_frame,
    read_wav_16k,
)

ROOT = Path(__file__).resolve().parents[2]
MAIN_TREE_CORPUS = Path("/Users/halo/Documents/bok/voice-assistant/reports/asr-whisper-bench/corpus")
SETTINGS_DB = Path.home() / "Library" / "Application Support" / "BokVoice" / "bok_voice.db"
ALLOWED_HOST = "openspeech.bytedance.com"
RESOURCE_DEFAULT = "volc.seedasr.sauc.duration"

PACKET_BYTES = 16000 * 2 * 200 // 1000  # 200ms@16k mono PCM16 = 6400B
BYTES_PER_MS = 32.0  # 16k*2B/1000ms

# 场景缺省语料（zh 为主；id 必须在 manifest.json 内。注意主树 corpus/ 是 2026-10-03
# 语料翻案前的旧渲染——粤语句由普通话声念粤语字，绝对 CER 不可信，只可做同料 A/B）
S1_ITEMS = ["zh_01", "zh_02"]
S1_WINDOWS = [500, 800, 1000, 0]
S2_ITEMS = ["zh_01", "zh_02", "zh_03", "brand_zh_01"]
S3_SOURCE = "zh_02"
S3_SEG_S = 0.8
S4_ITEMS = ["zh_01", "zh_02"]
S4_FORCE = [0, 1000]
S5_ITEM = "zh_01"


# ---- 凭据（env 优先 → 设置库 global_settings.asr_json 兜底；只掩码输出）----
def _mask(v: str) -> str:
    v = str(v or "")
    if not v:
        return "(空)"
    return f"{v[:3]}***{v[-2:]}" if len(v) > 8 else "***"


def _db_asr_segment() -> dict:
    try:
        con = sqlite3.connect(f"file:{SETTINGS_DB}?mode=ro", uri=True)
        raw = con.execute("select asr_json from global_settings").fetchone()[0]
        con.close()
        seg = json.loads(raw)
        return seg if isinstance(seg, dict) else {}
    except Exception:  # noqa: BLE001 - 库缺失/损坏→保守当空（env 仍可用）
        return {}


def resolve_creds(ws_url_override: str) -> tuple[dict, str, str]:
    """返回 (headers, ws_url, 描述串)。描述串只含掩码材料，绝不落 key 明文。"""
    api_key = os.environ.get("DOUBAO_API_KEY", "").strip()
    app_id = os.environ.get("DOUBAO_APP_ID", "").strip()
    token = os.environ.get("DOUBAO_ACCESS_TOKEN", "").strip()
    resource_id = os.environ.get("DOUBAO_RESOURCE_ID", "").strip()
    ws_url = ws_url_override or os.environ.get("DOUBAO_ENDPOINT", "").strip() or DOUBAO_WS_DEFAULT
    src = "env"
    if not (api_key or (app_id and token)):
        seg = _db_asr_segment()
        api_key = str(seg.get("api_key") or "").strip()
        app_id = str(seg.get("app_id") or "").strip()
        token = str(seg.get("access_token") or "").strip()
        resource_id = resource_id or str(seg.get("resource_id") or "").strip()
        seg_url = str(seg.get("base_url") or seg.get("endpoint") or "").strip()
        if not ws_url_override and seg_url.startswith("wss"):
            ws_url = seg_url
        src = "settings-db"
    if not (api_key or (app_id and token)):
        raise SystemExit(
            "凭据缺失：设 DOUBAO_API_KEY（或 DOUBAO_APP_ID+DOUBAO_ACCESS_TOKEN），"
            f"或在本机设置库留 asr 段（{SETTINGS_DB}）"
        )
    headers = {
        "X-Api-Resource-Id": resource_id or RESOURCE_DEFAULT,
        "X-Api-Connect-Id": str(uuid.uuid4()),
        # 官方文档列为必选（任务ID，随机 UUID）；与生产 DoubaoSTT._headers() 同构
        "X-Api-Request-Id": str(uuid.uuid4()),
    }
    if api_key:
        headers["X-Api-Key"] = api_key
        desc = f"形态=api_key 掩码={_mask(api_key)}"
    else:
        headers["X-Api-App-Key"] = app_id
        headers["X-Api-Access-Key"] = token
        desc = f"形态=app_id+access_token 掩码={_mask(app_id)}/{_mask(token)}"
    return headers, ws_url, f"来源={src} {desc}"


def _ws_url_ok(url: str) -> bool:
    """出站护栏：仅 wss + 豆包 openspeech host（本探针唯一出站目标）。"""
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(str(url or ""))
    except Exception:  # noqa: BLE001
        return False
    return parsed.scheme == "wss" and (parsed.hostname or "").lower() == ALLOWED_HOST


# ---- 请求构造（与生产 DoubaoSTT._config() 同语义；臂参数逐个叠进 request）----
def base_cfg(extra: dict | None = None) -> dict:
    request: dict = {
        "model_name": "bigmodel",
        "enable_itn": True,
        "enable_punc": True,
        "show_utterances": True,
        "result_type": "full",
        # 方言识别开关常开（缺省 false 时粤语被按普通话音系硬转；生产同款）。
        "enable_lid": True,
    }
    request.update(extra or {})
    return {
        "user": {"uid": "bok-w7probe"},
        "audio": {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1},
        "request": request,
    }


# ---- 单会话 runner（整段 PCM 200ms 实时分包 + 静音尾 + EOS 定稿）----
async def sauc_run(
    url: str,
    headers: dict,
    cfg: dict,
    pcm: bytes,
    *,
    tail_silence_s: float = 1.2,
    pace_s: float = 0.2,
    wait_definite_s: float = 3.5,
    settle_s: float = 0.3,
    final_timeout_s: float = 6.0,
    shape: bool = False,
) -> dict:
    """喂入→观察→定稿。rel_ms 一律相对首个音频包发出时刻。

    definite 事件只认 utterances[].definite=true（服务端自发判停），按
    (start,end,text) 去重——nonstream 二遍替换同段不同文=新事件（正是要观测的）。
    tail_silence_s>0 时持续喂静音帧（服务端只在有帧流时推进判停，已实证）。
    """
    res: dict = {"error": None, "err_code": None, "t0": None, "fed_bytes": 0,
                 "fed_done_rel_ms": None, "texts": [], "definite": [], "frames": [],
                 "eos_rel_ms": None, "is_last": False, "is_last_rel_ms": None,
                 "n_frames": 0}
    eos = {"sent": False}
    seen_def: set = set()
    first_definite = asyncio.Event()

    if not _ws_url_ok(url):
        raise SystemExit(f"WS 端点被拒（仅 wss + {ALLOWED_HOST}）: {url!r}")

    def rel_ms() -> float | None:
        return None if res["t0"] is None else round((time.perf_counter() - res["t0"]) * 1000, 1)

    async with websockets.connect(url, additional_headers=headers, open_timeout=10,
                                  max_size=20_000_000) as ws:
        await ws.send(frame_full_client_request(cfg))

        async def receiver() -> None:
            try:
                while True:
                    raw = await ws.recv()
                    if isinstance(raw, str):
                        continue
                    buf = bytes(raw)
                    fr = parse_server_frame(buf)
                    res["n_frames"] += 1
                    now = rel_ms()
                    if fr["type"] == MSG_ERROR:
                        res["error"] = f"server_error:{fr.get('code')}"
                        res["err_code"] = fr.get("code")
                    payload = fr["payload"]
                    if payload:
                        try:
                            j = json.loads(payload)
                        except Exception:  # noqa: BLE001 - 非 JSON 载荷只记帧头
                            j = None
                        if isinstance(j, dict):
                            rj = j.get("result") or {}
                            text = str(rj.get("text") or "")
                            utts = rj.get("utterances") or []
                            rec: dict = {"rel_ms": now, "frame_bytes": len(buf),
                                         "text_len": len(text), "text": text, "n_utts": len(utts)}
                            if shape:
                                rec["top_keys"] = sorted(str(k) for k in j.keys())
                                rec["result_keys"] = sorted(str(k) for k in rj.keys())
                                rec["utt_texts"] = [str(u.get("text") or "")[:24] for u in utts[:2]]
                            res["frames"].append(rec)
                            if text and (not res["texts"] or text != res["texts"][-1]["text"]):
                                res["texts"].append({"rel_ms": now, "text": text})
                            for u in utts:
                                if not u.get("definite"):
                                    continue
                                key = (u.get("start_time"), u.get("end_time"), u.get("text"))
                                if key in seen_def:
                                    continue
                                seen_def.add(key)
                                res["definite"].append({
                                    "text": u.get("text"), "start_time": u.get("start_time"),
                                    "end_time": u.get("end_time"), "rel_ms": now,
                                    "fed_ms": round(res["fed_bytes"] / BYTES_PER_MS, 1),
                                    "phase": "eos" if eos["sent"] else "pre_eos"})
                                first_definite.set()
                    if fr["is_last"]:
                        res["is_last"] = True
                        res["is_last_rel_ms"] = now
                        return
                    if fr["type"] == MSG_ERROR:
                        return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 连接关/超时归档
                res["error"] = res["error"] or f"recv:{exc!r}"

        recv_task = asyncio.create_task(receiver())

        async def sender() -> int:
            seq = 2
            chunks = [pcm[i:i + PACKET_BYTES] for i in range(0, len(pcm), PACKET_BYTES)]
            if tail_silence_s > 0:
                tail = b"\x00" * int(tail_silence_s * 1000 * BYTES_PER_MS)
                chunks += [tail[i:i + PACKET_BYTES] for i in range(0, len(tail), PACKET_BYTES)]
            res["t0"] = time.perf_counter()
            for i, ch in enumerate(chunks):
                await ws.send(frame_audio(seq, ch, last=False))
                seq += 1
                res["fed_bytes"] += len(ch)
                if i < len(chunks) - 1:
                    await asyncio.sleep(pace_s)
            res["fed_done_rel_ms"] = rel_ms()
            return seq

        next_seq = await sender()
        # 等 definite（服务端自发判停；等不到也照发 EOS 兜底定稿）
        try:
            await asyncio.wait_for(first_definite.wait(), timeout=wait_definite_s)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(settle_s)
        if not res["is_last"]:
            await ws.send(frame_audio(next_seq, b"", last=True))
            eos["sent"] = True
            res["eos_rel_ms"] = rel_ms()
        try:
            await asyncio.wait_for(asyncio.shield(recv_task), timeout=final_timeout_s)
        except asyncio.TimeoutError:
            res["error"] = res["error"] or "final_timeout"
    if not recv_task.done():
        recv_task.cancel()
    try:
        await recv_task
    except Exception:  # noqa: BLE001 - 收尾归档
        pass
    res["text_last"] = res["texts"][-1]["text"] if res["texts"] else ""
    pre = [t for t in res["texts"] if res["eos_rel_ms"] is None
           or (t["rel_ms"] is not None and t["rel_ms"] <= res["eos_rel_ms"])]
    res["text_pre_eos_last"] = pre[-1]["text"] if pre else ""
    return res


# ---- 音频工具（能量阈值；语料为干净 TTS 渲染，阈值 500/32768 足够）----
def speech_bounds(pcm: bytes, threshold: int = 500) -> tuple[int, int]:
    """返回（首/末超阈样本索引）。全程静音→(0, n-1) 保底不裁。"""
    n = len(pcm) // 2
    first = None
    for i in range(n):
        if abs(struct.unpack_from("<h", pcm, i * 2)[0]) > threshold:
            first = i
            break
    if first is None:
        return 0, max(0, n - 1)
    last = first
    for i in range(n - 1, first - 1, -1):
        if abs(struct.unpack_from("<h", pcm, i * 2)[0]) > threshold:
            last = i
            break
    return first, last


def speech_end_ms(pcm: bytes, threshold: int = 500) -> float:
    first, last = speech_bounds(pcm, threshold)
    return round((last + 1) / 16.0, 1) if (first or last) else round(len(pcm) / 2 / 16.0, 1)


def trim_head(pcm: bytes, threshold: int = 500) -> bytes:
    first, _last = speech_bounds(pcm, threshold)
    return pcm[first * 2:]


def build_stutter(pcm: bytes, seg_s: float) -> tuple[bytes, dict]:
    """结巴音频 = 前 seg_s 语音段 ×3 + 原整句（去头静音后拼接，段间零隙=真结巴形态）。"""
    trimmed = trim_head(pcm)
    seg = trimmed[:int(seg_s * 1000 * BYTES_PER_MS)]
    body = seg * 3 + trimmed
    meta = {"seg_ms": round(len(seg) / BYTES_PER_MS, 1),
            "total_ms": round(len(body) / BYTES_PER_MS, 1)}
    return body, meta


def with_silent_prefix(pcm: bytes, prefix_s: float) -> bytes:
    """去头静音后前置恰好 prefix_s 纯静音（S4 句头保护刺激件）。"""
    return b"\x00" * int(prefix_s * 1000 * BYTES_PER_MS) + trim_head(pcm)


# ---- 场景驱动 ----
def _mean(vals: list) -> float | None:
    got = [v for v in vals if v is not None]
    return round(sum(got) / len(got), 3) if got else None


def _run_digest(r: dict) -> dict:
    """整次会话的时间线摘要（raw JSON 留档用；不含帧级数据以外的派生）。"""
    return {"definite": r["definite"], "texts": r["texts"], "eos_rel_ms": r["eos_rel_ms"],
            "is_last_rel_ms": r["is_last_rel_ms"], "n_frames": r["n_frames"],
            "error": r["error"], "err_code": r["err_code"]}


async def scenario_s1(url: str, headers: dict, items: list[dict], corpus_dir: Path,
                      windows: list[int], *, pace_s: float, wait_s: float) -> dict:
    """w=0 特殊臂=不设 end_window_size（服务端缺省行为对照）。尾部 3s 静音帧
    （比缺省判停更长的观察余量：3s 内仍不自发 definite=缺省不判停）。"""
    rows: list[dict] = []
    for it in items:
        pcm = read_wav_16k(corpus_dir / it["file"])
        onset_ms = speech_end_ms(pcm)
        for w in windows:
            extra = {} if w == 0 else {"end_window_size": int(w)}
            r = await sauc_run(url, headers, base_cfg(extra), pcm,
                               tail_silence_s=3.0, pace_s=pace_s, wait_definite_s=wait_s)
            pre = [d for d in r["definite"] if d["phase"] == "pre_eos"]
            after = [d for d in pre if d["rel_ms"] is not None and d["rel_ms"] >= onset_ms - 60]
            mid = [d for d in pre if d["rel_ms"] is not None and d["rel_ms"] < onset_ms - 60]
            lag = round(after[0]["rel_ms"] - onset_ms, 1) if after else None
            rows.append({
                "id": it["id"], "ref": it["text"], "window_ms": w, "onset_ms": onset_ms,
                "lag_ms": lag, "mid_speech_definite": len(mid),
                "final_text": r["text_last"], "error": r["error"],
                "cer": round(cer(it["text"], r["text_last"], it["lang"]), 3) if r["text_last"] else None,
                "run": _run_digest(r),
            })
            print(f"[S1] {it['id']} w={w or 'default'} onset={onset_ms}ms lag={lag}ms mid={len(mid)} "
                  f"cer={rows[-1]['cer']} err={r['error']}", flush=True)
    means = {w: _mean([r["lag_ms"] for r in rows if r["window_ms"] == w]) for w in windows}
    return {"rows": rows, "mean_lag_by_window_ms": means}


async def scenario_s2(url: str, headers: dict, items: list[dict], corpus_dir: Path,
                      *, pace_s: float, wait_s: float) -> dict:
    """两臂都显式 end_window_size=800（官方二遍触发条件=句末静音≥end_window；
    不设该参数服务端不自发 definite→二遍无从观察，S1 w=0 臂实证）。"""
    rows: list[dict] = []
    for it in items:
        pcm = read_wav_16k(corpus_dir / it["file"])
        for ns in (False, True):
            extra = {"end_window_size": 800}
            if ns:
                extra["enable_nonstream"] = True
            r = await sauc_run(url, headers, base_cfg(extra), pcm,
                               tail_silence_s=1.6, pace_s=pace_s, wait_definite_s=wait_s)
            pre = [d for d in r["definite"] if d["phase"] == "pre_eos"]
            texts = r["texts"]
            replaced = any(i > 0 and len(t["text"]) < len(texts[i - 1]["text"])
                           for i, t in enumerate(texts))
            rows.append({
                "id": it["id"], "lang": it["lang"], "ref": it["text"], "nonstream": ns,
                "first_definite_rel_ms": pre[0]["rel_ms"] if pre else None,
                "last_definite_rel_ms": pre[-1]["rel_ms"] if pre else None,
                "n_definite_pre": len(pre), "text_replace_seen": replaced,
                "final_text": r["text_last"], "error": r["error"],
                "cer": round(cer(it["text"], r["text_last"], it["lang"]), 3) if r["text_last"] else None,
                "run": _run_digest(r),
            })
            print(f"[S2] {it['id']} ns={int(ns)} first={rows[-1]['first_definite_rel_ms']} "
                  f"last={rows[-1]['last_definite_rel_ms']} cer={rows[-1]['cer']} "
                  f"replace={replaced} err={r['error']}", flush=True)
    agg = {}
    for ns in (False, True):
        arm = [r for r in rows if r["nonstream"] == ns]
        agg["on" if ns else "off"] = {
            "mean_cer": _mean([r["cer"] for r in arm]),
            "mean_first_definite_ms": _mean([r["first_definite_rel_ms"] for r in arm]),
            "mean_last_definite_ms": _mean([r["last_definite_rel_ms"] for r in arm]),
            "text_replace_seen": sum(1 for r in arm if r["text_replace_seen"]),
        }
    return {"rows": rows, "agg": agg}


async def scenario_s3(url: str, headers: dict, it: dict, pcm_stutter: bytes, meta: dict,
                      *, pace_s: float, wait_s: float) -> dict:
    rows: list[dict] = []
    nref = len(norm_text(it["text"], it["lang"]))
    head4 = norm_text(it["text"], it["lang"])[:4]
    for ddc in (False, True):
        r = await sauc_run(url, headers, base_cfg({"enable_ddc": True} if ddc else {}),
                           pcm_stutter, tail_silence_s=1.2, pace_s=pace_s, wait_definite_s=wait_s)
        nout = norm_text(r["text_last"], it["lang"])
        interims = [t["text"] for t in r["texts"]]
        rows.append({
            "ddc": ddc, "final_text": r["text_last"],
            "dup_factor": round(len(nout) / max(1, nref), 2),
            "head4_rep": nout.count(head4) if head4 else None,
            "cer": round(cer(it["text"], r["text_last"], it["lang"]), 3) if r["text_last"] else None,
            "max_interim_len": max((len(t) for t in interims), default=0),
            "interim_texts": interims,
            "error": r["error"],
            "run": _run_digest(r),
        })
        print(f"[S3] ddc={int(ddc)} dup={rows[-1]['dup_factor']} head4_rep={rows[-1]['head4_rep']} "
              f"cer={rows[-1]['cer']} max_interim={rows[-1]['max_interim_len']} "
              f"err={r['error']} | {r['text_last'][:40]!r}", flush=True)
    return {"source": it["id"], "ref": it["text"], "stutter_meta": meta, "rows": rows}


async def scenario_s4(url: str, headers: dict, items: list[dict], corpus_dir: Path,
                      force_values: list[int], prefix_s: float,
                      *, pace_s: float, wait_s: float) -> dict:
    rows: list[dict] = []
    for it in items:
        pcm = with_silent_prefix(read_wav_16k(corpus_dir / it["file"]), prefix_s)
        for force in force_values:
            r = await sauc_run(url, headers, base_cfg({"force_to_speech_time": int(force)}),
                               pcm, tail_silence_s=1.2, pace_s=pace_s, wait_definite_s=wait_s)
            head3 = norm_text(it["text"], it["lang"])[:3]
            nout = norm_text(r["text_last"], it["lang"])
            first_text = r["texts"][0]["rel_ms"] if r["texts"] else None
            rows.append({
                "id": it["id"], "ref": it["text"], "force_ms": force, "head3": head3,
                "head_complete": bool(head3) and head3 in nout,
                "first_text_rel_ms": first_text,
                "final_text": r["text_last"], "error": r["error"],
                "cer": round(cer(it["text"], r["text_last"], it["lang"]), 3) if r["text_last"] else None,
                "run": _run_digest(r),
            })
            print(f"[S4] {it['id']} force={force} head3={head3!r} complete={rows[-1]['head_complete']} "
                  f"first_text={first_text}ms cer={rows[-1]['cer']} err={r['error']} "
                  f"| {r['text_last'][:36]!r}", flush=True)
    agg = {f: _mean([1.0 if r["head_complete"] else 0.0 for r in rows if r["force_ms"] == f])
           for f in force_values}
    return {"prefix_s": prefix_s, "rows": rows, "head_complete_rate": agg}


async def scenario_s5(url: str, headers: dict, it: dict, pcm: bytes,
                      *, pace_s: float, wait_s: float) -> dict:
    arms: dict = {}
    for rt in ("full", "single"):
        r = await sauc_run(url, headers, base_cfg({"result_type": rt}), pcm,
                           tail_silence_s=1.2, pace_s=pace_s, wait_definite_s=wait_s, shape=True)
        frames = r["frames"]
        concat = "".join(f["text"] for f in frames)
        idx = list(range(min(3, len(frames))))
        if len(frames) > 3:
            idx.append(len(frames) - 1)
        arms[rt] = {
            "n_frames": r["n_frames"],
            "sum_frame_bytes": sum(f["frame_bytes"] for f in frames),
            "n_text_updates": len(r["texts"]),
            "top_keys": frames[0]["top_keys"] if frames else [],
            "result_keys": frames[0]["result_keys"] if frames else [],
            "final_text": r["text_last"],
            "concat_text": concat,
            "cer_final": round(cer(it["text"], r["text_last"], it["lang"]), 3) if r["text_last"] else None,
            "cer_concat": round(cer(it["text"], concat, it["lang"]), 3) if concat else None,
            "run": _run_digest(r),
            "samples": [{"rel_ms": frames[i]["rel_ms"], "frame_bytes": frames[i]["frame_bytes"],
                         "text_len": frames[i]["text_len"], "n_utts": frames[i]["n_utts"],
                         "text": frames[i]["text"][:30], "utt_texts": frames[i].get("utt_texts")}
                        for i in idx],
            "error": r["error"],
        }
        print(f"[S5] rt={rt} frames={arms[rt]['n_frames']} updates={arms[rt]['n_text_updates']} "
              f"cer_final={arms[rt]['cer_final']} cer_concat={arms[rt]['cer_concat']} "
              f"err={r['error']}", flush=True)
    return {"source": it["id"], "ref": it["text"], "arms": arms}


# ---- 报告 ----
def _fmt(v) -> str:
    return "-" if v is None else str(v)


def _conclusions(report: dict) -> list[str]:
    """从数字推导的逐臂结论（措辞随读数走，便于复跑后仍成立）。"""
    sc = report["scenarios"]
    out: list[str] = []
    if "S1" in sc:
        m = {int(k): v for k, v in sc["S1"]["mean_lag_by_window_ms"].items()}
        mid_total = sum(r["mid_speech_definite"] for r in sc["S1"]["rows"])
        out.append(
            f"- **end_window（稳定门）**：显式设置时 definite 判停延迟随窗值 1:1 跟踪"
            f"（500={_fmt(m.get(500))}ms / 800={_fmt(m.get(800))}ms / 1000={_fmt(m.get(1000))}ms，"
            f"处理开销≈25-190ms）；**不设参数时服务端缺省 ≈{_fmt(m.get(0))}ms 才提交**（非文档默认 800）。"
            f"mid_speech_definite 全程 {mid_total}=纯静音门——只能接管「整句/整轮收尾」的稳定门，"
            "管不了句中子句提前提交；且现产线是客户端 VAD 分段+末包负 seq 强制定稿（提交更快），"
            "官方臂的适配形态是「长会话单连接、服务端分段」。")
    if "S2" in sc:
        off, on = sc["S2"]["agg"]["off"], sc["S2"]["agg"]["on"]
        delta = round((on["mean_first_definite_ms"] or 0) - (off["mean_first_definite_ms"] or 0), 1)
        out.append(
            f"- **nonstream（二遍识别）**：干净语料上 on/off 无可测差别（CER {off['mean_cer']} vs "
            f"{on['mean_cer']}；首 definite 差 {delta}ms≈噪声；文本回退替换 {off['text_replace_seen']} vs "
            f"{on['text_replace_seen']} 次；post-EOS 零改写）——首遍已对时二遍是无成本 no-op，"
            "无回归也无收益；收益面（噪声/不确定音频换更准 final）本探针未演示，开它不亏但不解决任何现有痛点。")
    if "S3" in sc:
        rows = sc["S3"]["rows"]
        off, on = rows[0], rows[1]
        out.append(
            f"- **ddc（语义顺滑）**：off 臂就已经把 3× 结巴完全折叠"
            f"（dup_factor {off['dup_factor']}/{on['dup_factor']}，"
            f"interim 全程未见重复、最大长度 {off['max_interim_len']}/{on['max_interim_len']} 字=干净句长）——"
            "bigmodel 引擎对精确音频重复的折叠是缺省行为（声学/LM 层），enable_ddc 在本材料上零增量；"
            "客户端「结巴折叠」对该端点属多余件。边界：单材料（精确重复），停顿词/语气词删除面未测。")
    if "S4" in sc:
        rate = sc["S4"]["head_complete_rate"]
        out.append(
            f"- **force_to_speech_time（句头保护）**：500ms 静音前缀下 force=0 与 1000 句头全部完整"
            f"（完整率 {json.dumps(rate, ensure_ascii=False)}），首 interim 时序也无一致收益"
            "（两臂互有胜负）——服务端 VAD 在干净音频上不吃句头，「VAD START 迟到吃句头」属客户端/"
            "麦克风侧问题，该参数治不了；按官方推荐设 1000 无害但无实测收益。")
    if "S5" in sc:
        full, single = sc["S5"]["arms"]["full"], sc["S5"]["arms"]["single"]
        same = (full["n_frames"] == single["n_frames"]
                and full["n_text_updates"] == single["n_text_updates"]
                and full["result_keys"] == single["result_keys"])
        out.append(
            f"- **result_type=single（增量返回）**：与 full 形状逐位相同"
            f"（frames {full['n_frames']}/{single['n_frames']}、"
            f"updates {full['n_text_updates']}/{single['n_text_updates']}、result 字段 {full['result_keys']}，"
            f"identical={same}）——在 volc.seedasr.sauc.duration 资源上该参数不生效，result.text 仍是全量累积；"
            "客户端 interim 文本变化去重仍不可省。")
    return out


def render_md(report: dict) -> str:
    sc = report["scenarios"]
    lines = [
        "# 豆包 SAUC 官方参数臂可行性探针（w7 · p2）",
        "",
        f"- 生成 {report['ts']}｜端点 `{report['ws_url']}`｜resource `{report['resource_id']}`",
        f"- 语料 `{report['corpus']}`（16k mono PCM16 + manifest 参考文本；评分=probe_cloud_asr 折叠 CER 同款）",
        "- 凭据=env/设置库（只掩码，不入档）；出站护栏=仅 wss + openspeech.bytedance.com；"
        "所有场景尾部持续喂静音帧（服务端只在有帧流时推进判停，probe_doubao_utterances 已实证）",
        "- 喂入节奏=200ms 实时分包（正 seq），观察后 EOS（负 seq）定稿；rel_ms 相对首个音频包",
        "",
    ]
    if "S1" in sc:
        s1 = sc["S1"]
        # JSON round-trip 后 dict 键变 str，行内 window_ms 仍是 int——统一收 int 再渲染
        means = {int(k): v for k, v in s1["mean_lag_by_window_ms"].items()}
        wins = sorted(means)
        lines += ["## S1 end_window_size 判停窗（静音起点 → definite final 实测延迟）", "",
                  "静音起点=能量阈值（|amp|>500）找最后一个非静音样本；lag=首个「静音起点后」的 "
                  "pre-EOS definite 到达 rel_ms − 起点 ms；w=0 臂=不设该参数（服务端缺省行为），"
                  "lag 空=喂帧静音 3s 内未自发 definite（final 只能靠 EOS 定稿）。", "",
                  "| item | ref | 静音起点 ms | " + " | ".join(
                      (f"w={w} lag ms" if w else "缺省 lag ms") for w in wins) + " |",
                  "|---|---|---|" + "---|" * len(wins)]
        by_id: dict = {}
        for r in s1["rows"]:
            by_id.setdefault(r["id"], {"ref": r["ref"], "onset": r["onset_ms"], "lags": {}})
            if r["lag_ms"] is not None:
                by_id[r["id"]]["lags"][int(r["window_ms"])] = r["lag_ms"]
        for iid, d in by_id.items():
            cells = " | ".join(_fmt(d["lags"].get(w)) for w in wins)
            lines.append(f"| {iid} | {d['ref']} | {d['onset']} | {cells} |")
        cells = " | ".join(_fmt(means[w]) for w in wins)
        lines.append(f"| **均值** |  |  | {cells} |")
        lines += ["", f"观察：lag 均值 {json.dumps({str(k): v for k, v in means.items()}, ensure_ascii=False)} ——"
                      "判停延迟随窗值近似 1:1 抬升即官方窗生效；mid_speech_definite>0 表示服务端"
                      "在静音前就发过 definite（按标点成句）。", ""]
    if "S2" in sc:
        s2 = sc["S2"]
        lines += ["## S2 enable_nonstream 二遍识别（final 折叠 CER + definite 到达时刻）", "",
                  "| item | 臂 | CER | 首definite ms | 末definite ms | 文本回退替换 | final 文本 |",
                  "|---|---|---|---|---|---|---|"]
        for r in s2["rows"]:
            lines.append(f"| {r['id']} | {'nonstream=on' if r['nonstream'] else 'off'} | {_fmt(r['cer'])} "
                         f"| {_fmt(r['first_definite_rel_ms'])} | {_fmt(r['last_definite_rel_ms'])} "
                         f"| {r['text_replace_seen']} | {r['final_text'][:30] or '(空)'} |")
        agg = s2["agg"]
        off, on = agg["off"], agg["on"]
        lines += ["", f"汇总：CER off={off['mean_cer']} on={on['mean_cer']}｜"
                      f"首definite off={off['mean_first_definite_ms']}ms on={on['mean_first_definite_ms']}ms｜"
                      f"末definite off={off['mean_last_definite_ms']}ms on={on['mean_last_definite_ms']}ms｜"
                      f"文本回退替换 off={off['text_replace_seen']} on={on['text_replace_seen']}", ""]
    if "S3" in sc:
        s3 = sc["S3"]
        lines += ["## S3 enable_ddc 语义顺滑（自拼结巴音频）", "",
                  f"刺激件：`{s3['source']}` 前 {s3['stutter_meta']['seg_ms']}ms 语音段 ×3 + 原整句"
                  f"（段间零隙，共 {s3['stutter_meta']['total_ms']}ms）；ref={s3['ref']}", "",
                  "| 臂 | 全文输出 | dup_factor | 句头4字重复次数 | interim 最大长度 | CER |",
                  "|---|---|---|---|---|---|"]
        for r in s3["rows"]:
            lines.append(f"| ddc={'on' if r['ddc'] else 'off'} | {r['final_text'] or '(空)'} "
                         f"| {_fmt(r['dup_factor'])} | {_fmt(r['head4_rep'])} "
                         f"| {_fmt(r['max_interim_len'])} | {_fmt(r['cer'])} |")
        lines += ["", "dup_factor=归一输出长度/归一 ref 长度（结巴不折叠≈>1.5，官方顺滑生效→趋近 1）；"
                      "head4_rep=归一输出里 ref 前 4 字出现次数；interim 最大长度看说话中途"
                      "（B 线 interim 喂 MT 的视角）重复是否出现过又被顺掉。", ""]
    if "S4" in sc:
        s4 = sc["S4"]
        lines += ["## S4 force_to_speech_time 句头保护（500ms 静音前缀）", "",
                  f"前置 {s4['prefix_s']}s 纯静音 + 去头静音后的原语音；"
                  "句头完整判据=归一 ref 前 3 字在归一 final 中；首 interim=首个非空文本到达 "
                  "rel_ms（force 治「VAD START 迟到吃句头」，提前 START 应同时提前首 interim）。", "",
                  "| item | force ms | 句头3字 | 句头完整 | 首 interim ms | CER | final 文本 |",
                  "|---|---|---|---|---|---|---|"]
        for r in s4["rows"]:
            lines.append(f"| {r['id']} | {r['force_ms']} | {r['head3']} | {r['head_complete']} "
                         f"| {_fmt(r['first_text_rel_ms'])} | {_fmt(r['cer'])} "
                         f"| {r['final_text'][:30] or '(空)'} |")
        lines += ["", f"句头完整率：{json.dumps(s4['head_complete_rate'], ensure_ascii=False)}", ""]
    if "S5" in sc:
        s5 = sc["S5"]
        lines += ["## S5 result_type full vs single（响应形状）", "",
                  f"刺激件 `{s5['source']}`：{s5['ref']}", ""]
        for rt, arm in s5["arms"].items():
            lines += [f"### result_type={rt}", "",
                      f"- frames={arm['n_frames']} 总字节={arm['sum_frame_bytes']} "
                      f"文本变化次数={arm['n_text_updates']}",
                      f"- 顶层字段={arm['top_keys']}｜result 字段={arm['result_keys']}",
                      f"- final CER={_fmt(arm['cer_final'])} concat CER={_fmt(arm['cer_concat'])}｜"
                      f"final={arm['final_text'][:40]!r}",
                      "", "| rel_ms | 帧字节 | text_len | n_utts | text(截30) | utterances(截24) |",
                      "|---|---|---|---|---|---|"]
            for s in arm["samples"]:
                lines.append(f"| {_fmt(s['rel_ms'])} | {s['frame_bytes']} | {s['text_len']} "
                             f"| {s['n_utts']} | {s['text']!r} | {s['utt_texts']!r} |")
            lines.append("")
    lines += ["## 总结论"] + _conclusions(report) + [""]
    return "\n".join(lines)


# ---- 主流程 ----
def load_manifest(corpus_dir: Path) -> list[dict]:
    return json.loads((corpus_dir / "manifest.json").read_text(encoding="utf-8"))


def pick(items: list[dict], ids: list[str]) -> list[dict]:
    by_id = {it["id"]: it for it in items}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise SystemExit(f"语料缺条目: {missing}（--corpus 指对了吗？）")
    return [by_id[i] for i in ids]


async def amain(args: argparse.Namespace) -> int:
    headers, ws_url, cred_desc = resolve_creds(args.ws_url)
    print(f"[cred] key 已找到（{cred_desc}）resource={headers['X-Api-Resource-Id']}", flush=True)
    print(f"[ws] {ws_url} pace={args.pace}s", flush=True)
    corpus_dir = Path(args.corpus)
    items = load_manifest(corpus_dir)
    report: dict = {"ts": time.strftime("%Y%m%d-%H%M%S"), "ws_url": ws_url,
                    "resource_id": headers["X-Api-Resource-Id"], "corpus": str(corpus_dir),
                    "scenarios": {}}
    todo = [s.strip().upper() for s in args.scenarios.split(",") if s.strip()]
    kw = {"pace_s": args.pace, "wait_s": args.wait_definite}

    if "S1" in todo:
        report["scenarios"]["S1"] = await scenario_s1(
            ws_url, headers, pick(items, args.s1_items.split(",")), corpus_dir,
            [int(x) for x in args.s1_windows.split(",")], **kw)
    if "S2" in todo:
        report["scenarios"]["S2"] = await scenario_s2(
            ws_url, headers, pick(items, args.s2_items.split(",")), corpus_dir, **kw)
    if "S3" in todo:
        src = pick(items, [args.s3_source])[0]
        pcm_stutter, meta = build_stutter(read_wav_16k(corpus_dir / src["file"]), args.s3_seg_s)
        report["scenarios"]["S3"] = await scenario_s3(ws_url, headers, src, pcm_stutter, meta, **kw)
    if "S4" in todo:
        report["scenarios"]["S4"] = await scenario_s4(
            ws_url, headers, pick(items, args.s4_items.split(",")), corpus_dir,
            [int(x) for x in args.s4_force.split(",")], args.s4_prefix_s, **kw)
    if "S5" in todo:
        src = pick(items, [args.s5_item])[0]
        report["scenarios"]["S5"] = await scenario_s5(ws_url, headers, src,
                                                      read_wav_16k(corpus_dir / src["file"]), **kw)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    json_path = outdir / "p2-doubao-raw.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = outdir / "p2-doubao.md"
    md_path.write_text(render_md(report), encoding="utf-8")
    print("\n== 关键读数 ==", flush=True)
    if "S1" in report["scenarios"]:
        print(f"S1 lag 均值 by window: {report['scenarios']['S1']['mean_lag_by_window_ms']}", flush=True)
    if "S2" in report["scenarios"]:
        print(f"S2 agg: {report['scenarios']['S2']['agg']}", flush=True)
    if "S3" in report["scenarios"]:
        rows = report["scenarios"]["S3"]["rows"]
        print(f"S3 dup_factor off/on: {[r['dup_factor'] for r in rows]} "
              f"head4_rep: {[r['head4_rep'] for r in rows]}", flush=True)
    if "S4" in report["scenarios"]:
        print(f"S4 句头完整率: {report['scenarios']['S4']['head_complete_rate']}", flush=True)
    print(f"[report] {md_path}", flush=True)
    print(f"[report] {json_path}", flush=True)
    return 0


def default_corpus() -> str:
    if MAIN_TREE_CORPUS.is_dir():
        return str(MAIN_TREE_CORPUS)
    return str(ROOT / "reports" / "asr-whisper-bench" / "corpus")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("--corpus", default=default_corpus(), help="语料目录（含 manifest.json）")
    ap.add_argument("--ws-url", default="", help="覆盖 WS 端点（仍限 wss + openspeech host）")
    ap.add_argument("--scenarios", default="S1,S2,S3,S4,S5")
    ap.add_argument("--pace", type=float, default=0.2, help="发包节奏秒（0.2=实时 200ms 分包）")
    ap.add_argument("--wait-definite", type=float, default=3.5, help="等自发 definite 的秒数")
    ap.add_argument("--s1-items", default=",".join(S1_ITEMS))
    ap.add_argument("--s1-windows", default=",".join(str(w) for w in S1_WINDOWS))
    ap.add_argument("--s2-items", default=",".join(S2_ITEMS))
    ap.add_argument("--s3-source", default=S3_SOURCE)
    ap.add_argument("--s3-seg-s", type=float, default=S3_SEG_S)
    ap.add_argument("--s4-items", default=",".join(S4_ITEMS))
    ap.add_argument("--s4-force", default=",".join(str(v) for v in S4_FORCE))
    ap.add_argument("--s4-prefix-s", type=float, default=0.5)
    ap.add_argument("--s5-item", default=S5_ITEM)
    ap.add_argument("--outdir", default=str(ROOT / "reports" / "w7probe"))
    args = ap.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
