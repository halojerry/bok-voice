#!/usr/bin/env python3
"""豆包 SAUC definite utterance 时序探针（2026-10-08 协议定案取证）。

问题：豆包（火山引擎）SAUC 流式 ASR 在**说话中**（客户端未发末包）会不会把
``result.utterances[]`` 条目标成 ``definite=true`` 增量下发，还是只在整段末包
（负 seq）时一次性给？生产实现 apps/agent/agent_runtime/providers/doubao_asr.py
当前只消费 result.text（单调累积、utterances.definite 被丢弃）——本探针把
utterances 原样落 JSONL，为「云侧 definite 能否当子句级提交边界」提供协议事实。

姿势：
- 凭据：env DOUBAO_API_KEY（或 DOUBAO_APP_ID+DOUBAO_ACCESS_TOKEN）优先；缺省回读
  本机 CP ``GET /api/settings?internal=1`` 的 asr 段（probe_cloud_asr.py 同源纪律：
  脚本零字面量凭据；输出只打掩码，JSONL 不落任何凭据材料）。
- 音频：macOS ``say -v Tingting`` 现场合成普通话 16k mono PCM16（免费本地手段），
  按变体拼接 + 静音隙：pause400 / pause800 / continuous / long30（>30s 长会话）。
- 喂入：200ms 分包实时节奏（正 seq），**音频喂完前绝不发负 seq 末包**；整段喂完后
  保持会话 --hold 秒（不喂音频、不发 EOS）观察服务端是否自发 definite/is_last，
  之后才发末包定稿（--no-eos 可全程不发）。帧格式/解析单源=probe_cloud_asr.py。
- 产物：reports/doubao-utterances/<ts>-<variant>.jsonl（逐帧）+ <ts>-summary.json
  + <ts>-<variant>-input.wav（输入留档，reports/ 已 gitignore）。

用法：
  .venv312/bin/python scripts/probes/probe_doubao_utterances.py --variant all
  .venv312/bin/python scripts/probes/probe_doubao_utterances.py --variant pause800
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
import subprocess
import tempfile
import time
import uuid
import wave
from pathlib import Path

import httpx
import websockets

# 协议帧格式/解析单源 = probe_cloud_asr.py（生产镜像 apps/agent .../providers/doubao_asr.py 同语义）
from probe_cloud_asr import (
    DOUBAO_WS_DEFAULT,
    MSG_ERROR,
    frame_audio,
    frame_full_client_request,
    parse_server_frame,
)

ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "reports" / "doubao-utterances"


# ---- SSRF 护栏（语义镜像生产 providers/doubao_asr.py 的 _ws_host_ok；探针单源
# 在 probe_cloud_asr，此处为本地诊断工具的等价护栏，不 import 生产件=免拖 livekit）----
def _ws_host_ok(url: str) -> bool:
    """仅放行 wss 公网端点（拒环回/私有/保留/明文；IP 字面量按 is_global）。"""
    import ipaddress
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(str(url or ""))
    except Exception:  # noqa: BLE001
        return False
    if parsed.scheme != "wss":
        return False
    host = (parsed.hostname or "").lower().strip(".")
    if not host or host == "localhost" or host.endswith((".local", ".internal", ".lan", ".localhost")):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # 域名放行（连接层解析）
    return bool(ip.is_global)


def _cp_base_ok(cp_base: str) -> bool:
    """CP 面护栏（SSRF）：仅放行**环回字面量**（127.0.0.1/localhost/::1）+
    http/https——本探针的设计目标只有本机 CP；内网段/云元数据/任意域名一律拒。"""
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(str(cp_base or ""))
    except Exception:  # noqa: BLE001
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip().lower()
    return host in ("127.0.0.1", "localhost", "::1", "[::1]")
DOUBAO_RESOURCE_DEFAULT = "volc.seedasr.sauc.duration"  # 生产装配缺省（seedasr 2.0）
PACKET_BYTES = 16000 * 2 * 200 // 1000  # 200ms@16k mono PCM16 = 6400B
BYTES_PER_MS = 32.0  # 16k*2B/1000ms

SENTS_4 = [
    "您好，我是平台客服专员。",
    "你的订单我们这边已经收到了。",
    "请问方便核对一下收货地址吗？",
    "稍后会有专员跟您联系。",
]
SENTS_10 = SENTS_4 + [
    "系统显示包裹明天就能送达。",
    "如果有问题您可以随时联系我们。",
    "我们会尽快帮您处理这个事情。",
    "请您保持手机畅通就可以了。",
    "感谢您的理解和耐心等待。",
    "祝您生活愉快，再见。",
]
VARIANTS = {
    "pause400": {"sentences": SENTS_4, "gap_s": 0.4, "tail_silence_s": 0.0},
    "pause800": {"sentences": SENTS_4, "gap_s": 0.8, "tail_silence_s": 0.0},
    "continuous": {"sentences": SENTS_4, "gap_s": 0.0, "tail_silence_s": 0.0},
    "long30": {"sentences": SENTS_10, "gap_s": 0.8, "tail_silence_s": 2.0},
    # 尾部 8s 静音持续喂入（配 --no-eos）：测服务端 end_window 在「有静音帧流」时
    # 多久自发 definite/is_last（短臂停喂=墙钟静默不触发，已实证）。
    "tail8": {"sentences": SENTS_4, "gap_s": 0.8, "tail_silence_s": 8.0},
}


# ---- 音频合成（macOS say → 16k mono PCM16）----
def _say_pcm(text: str, voice: str, workdir: Path, idx: int) -> bytes:
    raw = workdir / f"utt_{idx:02d}.wav"
    subprocess.run(
        ["say", "-v", voice, "--data-format=LEI16@16000", "-o", str(raw), text],
        check=True, capture_output=True,
    )
    path = raw
    with wave.open(str(raw), "rb") as w:
        if (w.getframerate(), w.getsampwidth(), w.getnchannels()) != (16000, 2, 1):
            path = workdir / f"utt_{idx:02d}_16k.wav"
            subprocess.run(
                ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(raw), str(path)],
                check=True, capture_output=True,
            )
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


def build_pcm(variant: str, voice: str, workdir: Path) -> tuple[bytes, dict]:
    """句子 PCM 按变体拼接（句间静音隙 + 尾部静音），补齐到 200ms 包整除。"""
    spec = VARIANTS[variant]
    parts: list[bytes] = []
    for i, text in enumerate(spec["sentences"]):
        parts.append(_say_pcm(text, voice, workdir, i))
        if i < len(spec["sentences"]) - 1 and spec["gap_s"] > 0:
            parts.append(b"\x00" * int(spec["gap_s"] * 1000 * BYTES_PER_MS))
    if spec["tail_silence_s"] > 0:
        parts.append(b"\x00" * int(spec["tail_silence_s"] * 1000 * BYTES_PER_MS))
    pcm = b"".join(parts)
    pad = (-len(pcm)) % PACKET_BYTES
    if pad:
        pcm += b"\x00" * pad
    return pcm, spec


# ---- 凭据（env 优先 → CP 设置面 asr 段；只掩码输出）----
def _mask(v: str) -> str:
    v = str(v or "")
    if not v:
        return "(空)"
    return f"{v[:3]}***{v[-2:]}" if len(v) > 8 else "***"


def _cp_asr_segment(cp_base: str) -> dict:
    r = httpx.get(f"{cp_base.rstrip('/')}/api/settings?internal=1", timeout=5.0)
    r.raise_for_status()
    d = r.json() or {}
    seg = d.get("asr") or d.get("asr_json") or {}
    return seg if isinstance(seg, dict) else {}


def resolve_creds(cp_base: str | None, ws_url_override: str) -> tuple[dict, str, str, str]:
    """返回 (headers, ws_url, cred_kind, 描述串)。描述串只含掩码材料。"""
    api_key = os.environ.get("DOUBAO_API_KEY", "").strip()
    app_id = os.environ.get("DOUBAO_APP_ID", "").strip()
    token = os.environ.get("DOUBAO_ACCESS_TOKEN", "").strip()
    resource_id = os.environ.get("DOUBAO_RESOURCE_ID", "").strip()
    ws_url = ws_url_override or os.environ.get("DOUBAO_ENDPOINT", "").strip() or DOUBAO_WS_DEFAULT
    src = "env"
    if not (api_key or (app_id and token)):
        seg = _cp_asr_segment(cp_base) if cp_base else {}
        provider = str(seg.get("provider") or "")
        api_key = str(seg.get("api_key") or "").strip()
        app_id = str(seg.get("app_id") or "").strip()
        token = str(seg.get("access_token") or "").strip()
        resource_id = str(seg.get("resource_id") or "").strip()
        seg_url = str(seg.get("ws_url") or seg.get("base_url") or "").strip()
        if not ws_url_override and seg_url.startswith("wss"):
            ws_url = seg_url
        src = f"cp({cp_base}) provider={provider or '(未设)'}"
    if not (api_key or (app_id and token)):
        raise SystemExit(
            "凭据缺失：设 DOUBAO_API_KEY（或 DOUBAO_APP_ID+DOUBAO_ACCESS_TOKEN），"
            "或给 --cp 指向在跑的 CP（读设置面 asr 段）"
        )
    headers = {
        "X-Api-Resource-Id": resource_id or DOUBAO_RESOURCE_DEFAULT,
        "X-Api-Connect-Id": str(uuid.uuid4()),
        # 官方文档列为必选（任务ID，随机 UUID）；与生产 DoubaoSTT._headers() 同构
        "X-Api-Request-Id": str(uuid.uuid4()),
    }
    if api_key:
        headers["X-Api-Key"] = api_key
        cred_kind = "api_key"
        desc = f"{src} api_key={_mask(api_key)}"
    else:
        headers["X-Api-App-Key"] = app_id
        headers["X-Api-Access-Key"] = token
        cred_kind = "app_key+access_key"
        desc = f"{src} app_id={_mask(app_id)} token={_mask(token)}"
    return headers, ws_url, cred_kind, desc


# ---- 会话（喂入 + 观察 + 定稿，逐帧 JSONL）----
async def run_session(
    url: str, headers: dict, cfg: dict, pcm: bytes, *, pace_s: float, hold_s: float,
    eos: bool, final_timeout_s: float, meta: dict, fh,
) -> dict:
    res: dict = {
        "first_audio_t": None, "fed_bytes": 0, "fed_done_rel_ms": None,
        "eos_rel_ms": None, "is_last": False, "is_last_rel_ms": None,
        "is_last_source": None, "error": None, "n_frames": 0, "texts": [],
        "first_text_rel_ms": None, "text_regressions": 0,
        "definite": [], "definite_keys": set(),
    }
    eos_sent = {"v": False}

    def log(obj: dict) -> None:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        fh.flush()

    def rel_ms() -> float | None:
        if res["first_audio_t"] is None:
            return None
        return round((time.perf_counter() - res["first_audio_t"]) * 1000, 1)

    log({"evt": "meta", **meta, "config": cfg})

    if not _ws_host_ok(url):
        raise SystemExit(f"WS 端点非法（仅 wss 公网；拒环回/私有/明文）: {url!r}")
    async with websockets.connect(
        url, additional_headers=headers, open_timeout=10, max_size=20_000_000
    ) as ws:
        await ws.send(frame_full_client_request(cfg))

        async def receiver() -> None:
            try:
                while True:
                    raw = await ws.recv()
                    if isinstance(raw, str):
                        continue
                    fr = parse_server_frame(bytes(raw))
                    now_rel = rel_ms()
                    res["n_frames"] += 1
                    rec: dict = {
                        "evt": "frame", "wall": round(time.time(), 3), "rel_ms": now_rel,
                        "fed_ms": round(res["fed_bytes"] / BYTES_PER_MS, 1),
                        "type": fr["type"], "seq": fr["seq"], "is_last": fr["is_last"],
                    }
                    if fr["type"] == MSG_ERROR:
                        res["error"] = f"server_error:{fr.get('code')}"
                    payload = fr["payload"]
                    if payload:
                        try:
                            j = json.loads(payload)
                        except Exception:  # noqa: BLE001 - 非 JSON 载荷只记帧头
                            j = None
                        if isinstance(j, dict):
                            r = j.get("result") or {}
                            text = str(r.get("text") or "")
                            rec["text"] = text
                            if text:
                                if res["texts"] and len(text) < len(res["texts"][-1]):
                                    res["text_regressions"] += 1
                                res["texts"].append(text)
                                if res["first_text_rel_ms"] is None:
                                    res["first_text_rel_ms"] = now_rel
                            new_defs = []
                            for u in (r.get("utterances") or []):
                                if not u.get("definite"):
                                    continue
                                key = (u.get("start_time"), u.get("end_time"), u.get("text"))
                                if key in res["definite_keys"]:
                                    continue
                                res["definite_keys"].add(key)
                                new_defs.append(u)
                            if new_defs:
                                rec["new_definite"] = new_defs
                                phase = "eos" if eos_sent["v"] else "pre_eos"
                                for u in new_defs:
                                    end_ms = u.get("end_time") or 0
                                    res["definite"].append({
                                        "text": u.get("text"),
                                        "start_time": u.get("start_time"),
                                        "end_time": u.get("end_time"),
                                        "rel_ms": now_rel, "fed_ms": rec["fed_ms"],
                                        "lag_after_end_ms": (
                                            None if now_rel is None else round(now_rel - end_ms, 1)
                                        ),
                                        # 音频域滞后=发包位置已越过子句末多少 ms（消实时发包抖动）
                                        "lag_audio_ms": round(rec["fed_ms"] - end_ms, 1),
                                        "phase": phase,
                                    })
                            if r.get("utterances"):
                                rec["utts"] = r["utterances"]
                    log(rec)
                    if fr["is_last"]:
                        res["is_last"] = True
                        res["is_last_rel_ms"] = now_rel
                        res["is_last_source"] = "eos" if eos_sent["v"] else "autonomous"
                        return
                    if fr["type"] == MSG_ERROR:
                        return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 连接关/超时归档后返回
                res["error"] = res["error"] or f"recv:{exc!r}"

        recv_task = asyncio.create_task(receiver())

        async def sender() -> None:
            seq = 2
            chunks = [pcm[i:i + PACKET_BYTES] for i in range(0, len(pcm), PACKET_BYTES)]
            for i, ch in enumerate(chunks):
                await ws.send(frame_audio(seq, ch, last=False))
                seq += 1
                if res["first_audio_t"] is None:
                    res["first_audio_t"] = time.perf_counter()
                res["fed_bytes"] += len(ch)
                if pace_s > 0 and i < len(chunks) - 1:
                    await asyncio.sleep(pace_s)
            res["fed_done_rel_ms"] = rel_ms()
            res["next_seq"] = seq

        await sender()
        # ---- hold 观察窗：不喂音频、不发 EOS——服务端能否自发 definite/is_last ----
        await asyncio.sleep(hold_s)
        if eos and not res["is_last"]:
            seq = res.get("next_seq") or 2  # 末包负 seq=延续计数（生产 sender 同姿势；乱序大数会被 45000000 拒）
            await ws.send(frame_audio(seq, b"", last=True))
            eos_sent["v"] = True
            res["eos_rel_ms"] = rel_ms()
            log({"evt": "eos", "rel_ms": res["eos_rel_ms"], "fed_ms": round(res["fed_bytes"] / BYTES_PER_MS, 1),
                 "neg_seq": -seq})
        try:
            await asyncio.wait_for(asyncio.shield(recv_task), timeout=final_timeout_s)
        except asyncio.TimeoutError:
            res["error"] = res["error"] or "final_timeout"
    if not recv_task.done():
        recv_task.cancel()
    try:
        await recv_task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 收尾归档
        pass
    res["final_text"] = max(res["texts"], key=len) if res["texts"] else ""
    return res


def summarize(variant: str, res: dict, audio_s: float) -> dict:
    return {
        "variant": variant,
        "audio_s": round(audio_s, 2),
        "n_frames": res["n_frames"],
        "first_text_rel_ms": res.get("first_text_rel_ms"),
        "fed_done_rel_ms": res["fed_done_rel_ms"],
        "eos_rel_ms": res["eos_rel_ms"],
        "definite_events": res["definite"],
        "definite_pre_eos": sum(1 for d in res["definite"] if d["phase"] == "pre_eos"),
        "is_last": res["is_last"],
        "is_last_source": res["is_last_source"],
        "is_last_rel_ms": res["is_last_rel_ms"],
        "final_text": res["final_text"],
        "text_regressions": res["text_regressions"],
        "error": res["error"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="all", choices=[*VARIANTS, "all"])
    ap.add_argument("--cp", default="http://127.0.0.1:8000",
                    help="CP base URL（凭据回退源；传空串禁用）")
    ap.add_argument("--voice", default="Tingting")
    ap.add_argument("--pace", type=float, default=0.2, help="发包节奏秒（0.2=实时 200ms 分包）")
    ap.add_argument("--hold", type=float, default=5.0, help="喂完后静默观察窗秒（不发 EOS）")
    ap.add_argument("--final-timeout", type=float, default=8.0)
    ap.add_argument("--no-eos", action="store_true", help="全程不发负 seq 末包（纯观察自发行为）")
    ap.add_argument("--ws-url", default="", help="覆盖 WS 端点（缺省 env/设置面/官方缺省）")
    ap.add_argument("--outdir", default=str(REPORT_DIR))
    args = ap.parse_args()

    variants = list(VARIANTS) if args.variant == "all" else [args.variant]
    headers, ws_url, cred_kind, cred_desc = resolve_creds(args.cp or None, args.ws_url)
    cfg = {  # 与生产 DoubaoSTT._config() 同语义（show_utterances=True 是本探针前提）
        "user": {"uid": "bok-probe-utt"},
        "audio": {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1},
        "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True,
                    "show_utterances": True, "result_type": "full", "enable_lid": True},
    }
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    print(f"[cred] {cred_desc} resource={headers['X-Api-Resource-Id']}", flush=True)
    print(f"[ws] {ws_url} pace={args.pace}s hold={args.hold}s eos={not args.no_eos}", flush=True)

    summaries = []
    with tempfile.TemporaryDirectory(prefix="doubao-utt-") as td:
        for variant in variants:
            pcm, spec = build_pcm(variant, args.voice, Path(td))
            audio_s = len(pcm) / (BYTES_PER_MS * 1000)
            jsonl_path = outdir / f"{ts}-{variant}.jsonl"
            print(f"\n== {variant} audio={audio_s:.1f}s gap={spec['gap_s']}s "
                  f"tail_sil={spec['tail_silence_s']}s -> {jsonl_path.name}", flush=True)
            with jsonl_path.open("w", encoding="utf-8") as fh:
                res = asyncio.run(run_session(
                    ws_url, headers, cfg, pcm, pace_s=args.pace, hold_s=args.hold,
                    eos=not args.no_eos, final_timeout_s=args.final_timeout,
                    meta={
                        "variant": variant, "audio_s": round(audio_s, 2),
                        "gap_s": spec["gap_s"], "tail_silence_s": spec["tail_silence_s"],
                        "pace_s": args.pace, "hold_s": args.hold, "eos": not args.no_eos,
                        "sentences": spec["sentences"], "cred_kind": cred_kind,
                        "resource_id": headers["X-Api-Resource-Id"], "ws_url": ws_url,
                    }, fh=fh,
                ))
            wav_path = outdir / f"{ts}-{variant}-input.wav"
            with wave.open(str(wav_path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(pcm)
            summary = summarize(variant, res, audio_s)
            summary["jsonl"] = str(jsonl_path)
            summaries.append(summary)
            defs = summary["definite_events"]
            print(f"[{variant}] frames={summary['n_frames']} definite={len(defs)}"
                  f"(pre_eos={summary['definite_pre_eos']}) "
                  f"lag_audio_ms={[d['lag_audio_ms'] for d in defs]} "
                  f"is_last={summary['is_last_source']}@{summary['is_last_rel_ms']}ms "
                  f"text_regressions={summary['text_regressions']} err={summary['error']}",
                  flush=True)
            print(f"    final={summary['final_text'][:80]!r}", flush=True)

    path = outdir / f"{ts}-summary.json"
    path.write_text(
        json.dumps({"ts": ts, "ws_url": ws_url,
                    "resource_id": headers["X-Api-Resource-Id"],
                    "pace_s": args.pace, "hold_s": args.hold, "eos": not args.no_eos,
                    "summaries": summaries}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[report] {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
