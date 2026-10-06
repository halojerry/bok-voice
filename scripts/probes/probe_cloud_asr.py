#!/usr/bin/env python3
"""云 ASR 直连探针（A线优化总计划 · 批次0.6b）。

在同一批语料（reports/asr-whisper-bench/corpus，16k mono PCM16 wav +
manifest.json 参考文本）上直连评估两家云 ASR，并与本地 Qwen3-ASR 基线对照：

  豆包（火山 SAUC bigmodel）  wss://openspeech.bytedance.com/api/v3/sauc/bigmodel
   MiniMax  asr-1.0           POST https://api.minimax.cn/v1/speech_to_text

凭据只从环境变量 / 本地设置库读，脚本零字面量：
  豆包：DOUBAO_APP_ID + DOUBAO_ACCESS_TOKEN（+ 可选 DOUBAO_RESOURCE_ID，
        默认 volc.bigasr.sauc.duration；DOUBAO_ENDPOINT 可换端点变体）
  MiniMax：MINIMAX_API_KEY，缺省回读 bok_voice.db global_settings.tts_json.api_key
        （bench_minimax_bidi.py 先例）

指标：CER（zh/粤/ja 字符级、en/de/fr/pt 词级；中文数字与全角数字归一到 ASCII 数字后比）、
四语折叠判定（W2c：de/fr/pt casefold+去变音符+空白归一、ja 剥空白/标点，精确或
difflib≥0.8 折过；三语既有口径不动）、首结果延迟（首条非空文本，自首个音频包发出起算）、
最终延迟、数字串逐位正确率、关键词命中（原字样）。报告写 reports/cloud-asr/<ts>.json。

用法：
  DOUBAO_API_KEY=... \
    .venv312/bin/python scripts/probes/probe_cloud_asr.py \
    --engine doubao|minimax|local|all --endpoint bigmodel \
    [--corpus <dir>] [--langs cantonese,zh,en,de,fr,ja,pt] [--limit N] [--pace fast|realtime]
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
import difflib
import gzip
import json
import os
import re
import sqlite3
import struct
import time
import unicodedata
import uuid
import wave
from pathlib import Path

import httpx
import websockets

# 繁简+粤语渲染变体折叠表（口径单源=asr_whisper_bench._FOLD，与历史基线同表；
# 直跑姿势 scripts/ 在 sys.path 恒可导入）
from asr_whisper_bench import _FOLD as _CANTO_FOLD

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "reports" / "asr-whisper-bench" / "corpus"
MANIFEST = CORPUS / "manifest.json"
REPORT_DIR = ROOT / "reports" / "cloud-asr"
SETTINGS_DB = Path.home() / "Library" / "Application Support" / "BokVoice" / "bok_voice.db"

DOUBAO_WS_DEFAULT = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
MINIMAX_STT_URL = "https://api.minimax.cn/v1/speech_to_text"
LOCAL_ASR_URL = "http://127.0.0.1:8787"
# 厂商语言标签（外部接口真字面量，术语铁律**边界映射单点**——内部语言字段一律
# cantonese；yue/yue-CN 仅作 MiniMax BCP-47 头与火山 SAUC language 参数出现）。
_VENDOR_LANG = {
    "cantonese": {"minimax": "yue", "volc": "yue-CN"},
    "zh": {"minimax": "zh", "volc": "zh-CN"},
    "en": {"minimax": "en", "volc": "en-US"},
    # 2026-10-06 W2c 四语实测扩（de/fr/ja/pt；标签只进厂商接口，语料=
    # reports/asr-4lang-corpus，渲染见 scripts/seed/render_asr_corpus_4lang.py）
    "de": {"minimax": "de", "volc": "de-DE"},
    "fr": {"minimax": "fr", "volc": "fr-FR"},
    "ja": {"minimax": "ja", "volc": "ja-JP"},
    "pt": {"minimax": "pt", "volc": "pt-BR"},
}
# 四语折叠判定语种集（norm/fold/cer 新分支只吃这四语，三语既有口径零触碰）。
_LANGS_4 = ("de", "fr", "ja", "pt")
_FOLD_RATIO_MIN = 0.8  # 精确不中时 difflib 相似度放行门（de/fr/pt/ja 同阈值）

# ---- 火山 SAUC 二进制帧（V3 协议族；官方 demo protocol.py 语义） ----
MSG_FULL_CLIENT_REQ = 0b0001
MSG_AUDIO_ONLY_REQ = 0b0010
MSG_FULL_SERVER_RESP = 0b1001
MSG_SERVER_ACK = 0b1011
MSG_ERROR = 0b1111

FLAG_NO_SEQ = 0b0000
FLAG_POS_SEQ = 0b0001
FLAG_LAST_NO_SEQ = 0b0010
FLAG_NEG_SEQ = 0b0011


def _header(msg_type: int, flags: int, serialization: int, compression: int) -> bytes:
    return bytes([
        (1 << 4) | 1,                      # version=1, header_size=1 → 4 字节头
        (msg_type << 4) | flags,
        (serialization << 4) | compression,
        0x00,
    ])


def frame_full_client_request(payload: dict) -> bytes:
    body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    buf = _header(MSG_FULL_CLIENT_REQ, FLAG_POS_SEQ, serialization=1, compression=1)
    buf += struct.pack(">i", 1)            # seq=1
    buf += struct.pack(">I", len(body)) + body
    return buf


def frame_audio(seq: int, chunk: bytes, *, last: bool) -> bytes:
    body = gzip.compress(chunk)
    if last:
        buf = _header(MSG_AUDIO_ONLY_REQ, FLAG_NEG_SEQ, serialization=0, compression=1)
        buf += struct.pack(">i", -seq)
    else:
        buf = _header(MSG_AUDIO_ONLY_REQ, FLAG_POS_SEQ, serialization=0, compression=1)
        buf += struct.pack(">i", seq)
    buf += struct.pack(">I", len(body)) + body
    return buf


def parse_server_frame(data: bytes) -> dict:
    """宽容解析（官方 demo 语义）：flags&1=带 seq、&2=末包、&4=带 event。"""
    b0, b1, b2 = data[0], data[1], data[2]
    msg_type = b1 >> 4
    flags = b1 & 0x0F
    compression = b2 & 0x0F
    pos = 4 * (b0 & 0x0F)
    out: dict = {"type": msg_type, "flags": flags, "is_last": bool(flags & 0x02),
                 "seq": None, "event": None, "payload": b"", "code": None}
    if flags & 0x01:
        out["seq"] = struct.unpack(">i", data[pos:pos + 4])[0]
        pos += 4
    if flags & 0x04:
        out["event"] = struct.unpack(">i", data[pos:pos + 4])[0]
        pos += 4
    if msg_type == MSG_ERROR:
        out["code"] = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
    if msg_type in (MSG_FULL_SERVER_RESP, MSG_SERVER_ACK, MSG_ERROR):
        if pos + 4 <= len(data):
            size = struct.unpack(">I", data[pos:pos + 4])[0]
            pos += 4
            payload = data[pos:pos + size]
            if compression == 1 and payload:
                try:
                    payload = gzip.decompress(payload)
                except Exception:  # noqa: BLE001 - 解析失败留原始
                    pass
            out["payload"] = payload
    return out


# ---- 音频读写 ----
def read_wav_16k(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000 and w.getsampwidth() == 2, f"非 16k/16bit: {path}"
        assert w.getnchannels() == 1, f"非单声道: {path}"
        return w.readframes(w.getnframes())


# ---- 打分 ----
_CJK_PUNCT = re.compile(r"[\s，。！？、,.!?;:；：'\"“”‘’（）()【】\[\]—…·]")
_CN_DIGITS = {"零": "0", "〇": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
              "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
_EN_DIGIT_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
                   "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
# 四语数字词归一（casefold+去变音符后的形态；fünf→funf、três→tres）。W2c。
_DIGIT_WORDS_4 = {
    "de": {"null": "0", "eins": "1", "zwei": "2", "drei": "3", "vier": "4",
           "funf": "5", "sechs": "6", "sieben": "7", "acht": "8", "neun": "9"},
    "fr": {"zero": "0", "un": "1", "deux": "2", "trois": "3", "quatre": "4",
           "cinq": "5", "six": "6", "sept": "7", "huit": "8", "neuf": "9"},
    "pt": {"zero": "0", "um": "1", "dois": "2", "tres": "3", "quatro": "4",
           "cinco": "5", "seis": "6", "sete": "7", "oito": "8", "nove": "9"},
}
# 日语标点/空白（折句读；长音符 ー 是词的一部分，不剥）。
_JA_NOISE_RE = re.compile(r"[\s、。！？，．：；「」『』（）・…―～〜“”‘’]")


def _deaccent_fold(s: str) -> str:
    """去变音符+casefold（NFKD 分解后剥组合符；é→e、ü→u、ß→ss）。"""
    return "".join(ch for ch in unicodedata.normalize("NFKD", s.casefold())
                   if not unicodedata.combining(ch))


def norm_text_4(s: str, lang: str) -> str:
    """四语归一（W2c 折叠口径）：de/fr/pt=casefold+去变音符+非字母数字折空白+
    数字词归一+空白归一；ja=空白/标点剥离+汉字·全角数字归 ASCII。双侧同构。"""
    s = (s or "").translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if lang == "ja":
        s = "".join(_CN_DIGITS.get(ch, ch) for ch in s)
        return _JA_NOISE_RE.sub("", s)
    s = _deaccent_fold(s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    words = _DIGIT_WORDS_4[lang]
    s = re.sub(r"\b(" + "|".join(words) + r")\b", lambda m: words[m.group(1)], s)
    return re.sub(r"\s+", " ", s).strip()


def fold4_hit(ref: str, hyp: str, lang: str) -> tuple[bool, float]:
    """四语折叠判定：归一后精确，或 difflib 相似度 ≥0.8 折过。返回 (hit, ratio)。"""
    r, h = norm_text_4(ref, lang), norm_text_4(hyp, lang)
    if not r:
        return (not h), 0.0
    ratio = 1.0 if r == h else difflib.SequenceMatcher(None, r, h).ratio()
    return ratio >= _FOLD_RATIO_MIN, round(ratio, 3)


def norm_text(s: str, lang: str) -> str:
    s = (s or "").strip().translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    # 繁简+粤语渲染变体折叠（口径单源=asr_whisper_bench._FOLD）
    s = "".join(_CANTO_FOLD.get(ch, ch) for ch in s)
    if lang in ("zh", "cantonese"):
        s = "".join(_CN_DIGITS.get(ch, ch) for ch in s)
        return _CJK_PUNCT.sub("", s)
    if lang in _LANGS_4:
        return norm_text_4(s, lang)  # W2c 四语（de/fr/pt 折变音符+数字词、ja 剥空白）
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    # 数字词归一（five five two… ↔ 552…），CER 与数字串比对同步受益
    s = re.sub(r"\b(zero|oh|one|two|three|four|five|six|seven|eight|nine)\b",
               lambda m: _EN_DIGIT_WORDS[m.group(1)], s)
    return re.sub(r"\s+", " ", s).strip()


def levenshtein(a: list, b: list) -> int:
    """序列级编辑距离（list[char] 或 list[word] 通用）。"""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(ref: str, hyp: str, lang: str) -> float:
    """zh/粤/ja=字符级 CER；en/de/fr/pt=词级 WER（四语先折变音符/数字词再比）。"""
    r, h = norm_text(ref, lang), norm_text(hyp, lang)
    if not r:
        return 0.0 if not h else 1.0
    if lang in ("en", "de", "fr", "pt"):
        ru = r.split(" ")
        hu = h.split(" ") if h else []
        return levenshtein(ru, hu) / len(ru)
    return levenshtein(list(r), list(h)) / len(r)


def digit_acc(hyp: str, target: str, lang: str) -> float:
    norm = norm_text(hyp, lang)
    runs = re.findall(r"\d+", norm)
    if not runs:
        return 0.0
    best = 0.0
    for run in runs:
        common = min(len(run), len(target))
        matches = sum(1 for i in range(common) if run[i] == target[i])
        best = max(best, matches / max(1, len(target)))
    return round(best, 3)


def digit_seq_acc(hyp: str, target: str, lang: str) -> float:
    """四语数字序列分（W2c）：归一后剥空白取全部数字按序连串与目标逐位比。

    逐位口播场景（de/fr/pt 数字词、ja 顿号逐位）若 ASR 不做连写 ITN，run 级
    digit_acc 只认单连串会系统性低估（"8 4 1 …" 每串 1 位 → ≤1/len）；本口径
    只要求全部数字按序正确。三语行不产此键，既有口径不受扰。
    """
    seq = "".join(re.findall(r"\d+", re.sub(r"\s+", "", norm_text(hyp, lang))))
    if not seq:
        return 0.0
    common = min(len(seq), len(target))
    matches = sum(1 for i in range(common) if seq[i] == target[i])
    return matches / max(1, len(target))


# ---- 豆包直连 ----
async def doubao_once(url: str, headers: dict, pcm: bytes, *, pace: str,
                      endpoint_kind: str, lang_tag: str, timeout_s: float) -> dict:
    out: dict = {"first_text_ms": None, "final_ms": None, "text": "", "updates": 0,
                 "definite_seen": 0, "error": None, "err_code": None}
    cfg = {
        "user": {"uid": "bok-probe"},
        "audio": {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1},
        "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True,
                    "show_utterances": True, "result_type": "full",
                    # 方言识别开关：官方文档「启用中英文及方言识别」（含粤语）。
                    # 缺省 false 时粤语被按普通话音系硬转（2026-10-03 实测），故常开。
                    "enable_lid": True},
    }
    if endpoint_kind == "bigmodel_nostream":
        cfg["audio"]["language"] = lang_tag
    if endpoint_kind == "bigmodel_async":
        cfg["request"]["enable_nonstream"] = True

    chunk_bytes = 16000 * 2 * 200 // 1000  # 200ms
    chunks = [pcm[i:i + chunk_bytes] for i in range(0, len(pcm), chunk_bytes)]
    t_audio0: list[float] = []
    done = asyncio.Event()

    async with websockets.connect(url, additional_headers=headers, open_timeout=10,
                                  max_size=20_000_000) as ws:
        await ws.send(frame_full_client_request(cfg))

        async def _sender() -> None:
            seq = 2
            for i, ch in enumerate(chunks):
                last = i == len(chunks) - 1
                await ws.send(frame_audio(seq, ch, last=last))
                if not t_audio0:
                    t_audio0.append(time.perf_counter())
                seq += 1
                if pace == "realtime" and not last:
                    await asyncio.sleep(0.2)

        sender = asyncio.create_task(_sender())
        t0_deadline = time.perf_counter() + timeout_s
        try:
            while time.perf_counter() < t0_deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.2, t0_deadline - time.perf_counter()))
                if isinstance(raw, str):
                    continue
                fr = parse_server_frame(bytes(raw))
                if fr["type"] == MSG_ERROR:
                    out["error"] = "server_error"
                    out["err_code"] = fr.get("code")
                    break
                if fr["type"] in (MSG_FULL_SERVER_RESP, MSG_SERVER_ACK) and fr["payload"]:
                    try:
                        j = json.loads(fr["payload"])
                    except Exception:  # noqa: BLE001
                        continue
                    text = str((j.get("result") or {}).get("text") or "")
                    utt = (j.get("result") or {}).get("utterances") or []
                    out["definite_seen"] += sum(1 for u in utt if u.get("definite"))
                    if text:
                        out["updates"] += 1
                        if out["first_text_ms"] is None and t_audio0:
                            out["first_text_ms"] = round((time.perf_counter() - t_audio0[0]) * 1000, 1)
                        if len(text) >= len(out["text"]):
                            out["text"] = text
                if fr["is_last"]:
                    break
        except asyncio.TimeoutError:
            out["error"] = out["error"] or "timeout"
        finally:
            if not sender.done():
                sender.cancel()
        if t_audio0:
            out["final_ms"] = round((time.perf_counter() - t_audio0[0]) * 1000, 1)
    return out


# ---- MiniMax ----
def minimax_key() -> str:
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if key:
        return key
    try:
        con = sqlite3.connect(f"file:{SETTINGS_DB}?mode=ro", uri=True)
        raw = con.execute("select tts_json from global_settings").fetchone()[0]
        con.close()
        return str(json.loads(raw).get("api_key") or "")
    except Exception:  # noqa: BLE001
        return ""


def minimax_once(key: str, wav_path: Path, lang_tag: str, timeout_s: float) -> dict:
    out: dict = {"first_text_ms": None, "final_ms": None, "text": "", "updates": 0,
                 "error": None}
    t0 = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout_s) as client:
            with open(wav_path, "rb") as f:
                files = {"file": (wav_path.name, f, "audio/wav")}
                data = {"model": "asr-1.0", "stream": "true"}
                headers = {"Authorization": f"Bearer {key}", "language": lang_tag}
                with client.stream("POST", MINIMAX_STT_URL, headers=headers, data=data, files=files) as resp:
                    resp.raise_for_status()
                    parts: list[str] = []
                    finished = False
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        try:
                            ev = json.loads(line[len("data:"):].strip())
                        except json.JSONDecodeError:
                            continue
                        out["n_events"] = out.get("n_events", 0) + 1
                        out["last_event"] = ev
                        delta = ev.get("delta") or ""
                        if delta:
                            out["updates"] += 1
                            if out["first_text_ms"] is None:
                                out["first_text_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                            parts.append(delta)
                        if ev.get("finish"):
                            finished = True
                            out["finish_duration"] = ev.get("duration")
                            break
                    out["text"] = "".join(parts)
                    if not finished:
                        out["stream_ended_early"] = True
    except Exception as exc:  # noqa: BLE001
        out["error"] = repr(exc)
    out["final_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


# ---- 本地 Qwen3-ASR（对照臂；finish 往返口径 = asr_whisper_bench 的 qwen3_finish_ms）----
def local_once(pcm: bytes, timeout_s: float) -> dict:
    """start→chunk→finish（不传 language，与 asr_whisper_bench 口径一致）。

    first_text_ms 恒 None（本臂只测 finish 往返）；final_ms=finish 调用→FINAL 返回。
    """
    out: dict = {"first_text_ms": None, "final_ms": None, "text": "", "error": None}
    try:
        with httpx.Client(timeout=timeout_s) as client:
            sid = client.post(f"{LOCAL_ASR_URL}/api/start").json()["session_id"]
            step = 16000 // 10 * 2  # 100ms
            for i in range(0, len(pcm), step):
                client.post(f"{LOCAL_ASR_URL}/api/chunk", params={"session_id": sid},
                            content=pcm[i:i + step])
            t0 = time.perf_counter()
            r = client.post(f"{LOCAL_ASR_URL}/api/finish", params={"session_id": sid}).json()
            out["final_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            out["text"] = str(r.get("text") or "")
    except Exception as exc:  # noqa: BLE001
        out["error"] = repr(exc)
    return out


# ---- 主流程 ----
def load_corpus(corpus_dir: Path, langs: list[str], limit: int | None, only: list[str]) -> list[dict]:
    items = json.loads((corpus_dir / "manifest.json").read_text(encoding="utf-8"))
    if langs:
        items = [it for it in items if it.get("lang") in langs]
    if only:
        items = [it for it in items if it.get("id") in only]
    if limit:
        items = items[:limit]
    return items


def agg(rows: list[dict], key: str | None) -> dict:
    got = [r for r in rows if (key is None or r.get("lang") == key)
           and not r.get("error") and "cer" in r]
    if not got:
        return {}
    digits = [r for r in got if r.get("digits")]
    kws = [r for r in got if r.get("keywords")]
    folds = [r for r in got if "fold_hit" in r]  # W2c 四语折叠判定（三语行无此键不受影响）
    firsts = sorted(r["first_text_ms"] for r in got if r.get("first_text_ms"))
    out = {
        "n": len(got),
        "mean_cer": round(sum(r["cer"] for r in got) / len(got), 3),
        "digit_exact": f"{sum(1 for r in digits if r.get('digit_acc') == 1.0)}/{len(digits)}" if digits else "-",
        "keyword_hits": f"{sum(1 for r in kws if r.get('keyword_hit'))}/{len(kws)}" if kws else "-",
        "first_ms_p50": firsts[len(firsts) // 2] if firsts else None,
    }
    if folds:
        out["fold_hits"] = f"{sum(1 for r in folds if r['fold_hit'])}/{len(folds)}"
    seqs = [r for r in got if "digit_seq_acc" in r]
    if seqs:
        out["digit_seq_exact"] = f"{sum(1 for r in seqs if r['digit_seq_acc'] == 1.0)}/{len(seqs)}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["doubao", "minimax", "local", "both", "all"],
                    default="both")
    ap.add_argument("--endpoint", choices=["bigmodel", "bigmodel_nostream", "bigmodel_async"],
                    default="bigmodel")
    ap.add_argument("--langs", default="", help="逗号分隔 cantonese,zh,en；空=全部")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only", default="", help="逗号分隔 id")
    ap.add_argument("--corpus", default=str(CORPUS), help="语料目录（含 manifest.json）")
    ap.add_argument("--pace", choices=["realtime", "fast"], default="realtime")
    ap.add_argument("--timeout", type=float, default=20.0)
    args = ap.parse_args()

    langs = [x.strip() for x in args.langs.split(",") if x.strip()]
    only = [x.strip() for x in args.only.split(",") if x.strip()]
    corpus_dir = Path(args.corpus)
    items = load_corpus(corpus_dir, langs, args.limit, only)
    if not items:
        print("no corpus items selected")
        return 1

    rows: list[dict] = []
    ts = int(time.time())
    run: dict = {"ts": ts, "endpoint": args.endpoint, "pace": args.pace, "rows": rows}

    do_doubao = args.engine in ("doubao", "both", "all")
    do_local = args.engine in ("local", "all")
    app_id = os.environ.get("DOUBAO_APP_ID", "").strip()
    token = os.environ.get("DOUBAO_ACCESS_TOKEN", "").strip()
    dd_api_key = os.environ.get("DOUBAO_API_KEY", "").strip()
    if do_doubao and not ((app_id and token) or dd_api_key):
        print("DOUBAO_APP_ID / DOUBAO_ACCESS_TOKEN（或 DOUBAO_API_KEY）未设置", flush=True)
        return 2
    if dd_api_key:
        # 新版控制台单 Key 姿势（官方 demo RequestBuilder.new_auth_headers 先例）
        dd_headers = {
            "X-Api-Key": dd_api_key,
            "X-Api-Resource-Id": os.environ.get("DOUBAO_RESOURCE_ID", "volc.bigasr.sauc.duration"),
            "X-Api-Connect-Id": str(uuid.uuid4()),
            # 官方文档列为必选（任务ID，随机 UUID）；社区协议只有 Connect-Id，双发无害
            "X-Api-Request-Id": str(uuid.uuid4()),
        }
    else:
        dd_headers = {
            "X-Api-App-Key": app_id,
            "X-Api-Access-Key": token,
            "X-Api-Resource-Id": os.environ.get("DOUBAO_RESOURCE_ID", "volc.bigasr.sauc.duration"),
            "X-Api-Connect-Id": str(uuid.uuid4()),
            "X-Api-Request-Id": str(uuid.uuid4()),
        }
    dd_url = os.environ.get("DOUBAO_ENDPOINT", DOUBAO_WS_DEFAULT)

    mm_key = minimax_key() if args.engine in ("minimax", "both", "all") else ""

    for it in items:
        wav_path = corpus_dir / it["file"]
        row: dict = {"id": it["id"], "lang": it["lang"], "ref": it["text"],
                     "digits": it.get("digits"), "keywords": it.get("keywords")}
        pcm = read_wav_16k(wav_path) if (do_doubao or do_local) else None
        if do_doubao:
            assert pcm is not None
            lang_tag = _VENDOR_LANG.get(it["lang"], {}).get("volc", "")
            t0 = time.perf_counter()
            try:
                res = asyncio.run(doubao_once(dd_url, dd_headers, pcm, pace=args.pace,
                                              endpoint_kind=args.endpoint, lang_tag=lang_tag,
                                              timeout_s=max(args.timeout, it["dur_s"] * 3)))
            except Exception as exc:  # noqa: BLE001
                msg = repr(exc)
                resp = getattr(exc, "response", None)
                body = getattr(resp, "body", None)
                if body is not None:
                    try:
                        msg = f"HTTP {getattr(resp, 'status_code', '?')}: " + bytes(body).decode("utf-8", "replace")
                    except Exception:  # noqa: BLE001
                        pass
                res = {"error": msg[:300], "text": "", "first_text_ms": None, "final_ms": None}
            res["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            row["doubao"] = res
        if mm_key:
            mm_lang = _VENDOR_LANG.get(it["lang"], {}).get("minimax", "")
            try:
                row["minimax"] = minimax_once(mm_key, wav_path, mm_lang, max(args.timeout, it["dur_s"] * 3))
            except Exception as exc:  # noqa: BLE001
                row["minimax"] = {"error": repr(exc), "text": "", "first_text_ms": None, "final_ms": None}
        if do_local:
            assert pcm is not None
            try:
                row["local"] = local_once(pcm, max(args.timeout, it["dur_s"] * 3))
            except Exception as exc:  # noqa: BLE001
                row["local"] = {"error": repr(exc), "text": "", "first_text_ms": None, "final_ms": None}
        # 打分
        for eng in ("doubao", "minimax", "local"):
            r = row.get(eng)
            if not r:
                continue
            if r.get("text"):
                r["cer"] = round(cer(it["text"], r["text"], it["lang"]), 3)
                if it["lang"] in _LANGS_4:
                    # W2c 四语折叠判定（精确或 difflib≥0.8）；三语既有口径不加此键
                    r["fold_hit"], r["fold_ratio"] = fold4_hit(it["text"], r["text"], it["lang"])
                if it.get("digits"):
                    r["digit_acc"] = digit_acc(r["text"], it["digits"], it["lang"])
                    if it["lang"] in _LANGS_4:
                        r["digit_seq_acc"] = round(digit_seq_acc(r["text"], it["digits"], it["lang"]), 3)
                if it.get("keywords"):
                    hn = norm_text(r["text"], it["lang"])
                    r["keyword_hit"] = all(norm_text(k, it["lang"]) in hn
                                           for k in it["keywords"])
        rows.append(row)
        # 逐条打印
        brief = " | ".join(
            f"{eng}: cer={r.get('cer')}"
            + (f" fold={r.get('fold_hit')}({r.get('fold_ratio')})" if r.get("fold_hit") is not None else "")
            + f" first={r.get('first_text_ms')}ms final={r.get('final_ms')}ms"
            f"{' ERR=' + str(r.get('error') or r.get('err_code')) if r.get('error') or r.get('err_code') else ''}"
            for eng, r in (("dd", row.get("doubao")), ("mm", row.get("minimax")),
                           ("lc", row.get("local"))) if r
        )
        preview = (row.get("doubao") or row.get("minimax") or row.get("local") or {}).get("text", "")[:48]
        print(f"[{it['id']}] {brief} | {preview!r}", flush=True)

    # 汇总（三语键序恒在前=旧报告逐字节稳定；四语等新语种按首现序追加）
    summary: dict = {}
    for eng in ("doubao", "minimax", "local"):
        eng_rows = [{**r, **r[eng]} for r in rows if r.get(eng)]
        if not eng_rows:
            continue
        lang_keys = ["cantonese", "zh", "en"]
        lang_keys += [lg for lg in dict.fromkeys(r.get("lang") for r in eng_rows)
                      if lg not in lang_keys]
        summary[eng] = {lg: agg(eng_rows, lg) for lg in lang_keys}
        summary[eng]["all"] = agg(eng_rows, None)
    run["summary"] = summary
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{ts}-cloud-asr-{args.engine}-{args.endpoint}.json"
    path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n== summary ==", flush=True)
    for eng, per in summary.items():
        for lg, s in per.items():
            print(f"[{eng}][{lg}] {s}", flush=True)
    print(f"[report] {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
