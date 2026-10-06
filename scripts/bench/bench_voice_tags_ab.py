#!/usr/bin/env python3
"""A 线语气标记 ±对照真合成 A/B 台架（2026-10-06，用户已授权真合成）。

对固定句集做「同句 ±标记」MiniMax t2a_v2 真合成，物化到 reports/voice-tags-ab/
（reports/ 全目录 gitignore），供人耳 A/B——台架只物化+计时，不打分。

句集（3 语 × 2 句：一句共情安抚向、一句查询向）× 5 变体：
  plain / +(breath) / +(emm) / +(sighs) / +停顿 <#0.3#>
  —— sighs **不在** A 线白名单（voice_style.VOICE_TAG_WHITELIST 现役六标记），
  正是要听它是不是官方真标记、值不值得扩白名单；台架直发原始文本（不经
  sanitize），合成了什么就听什么。

模型：speech-2.8-hd 全矩阵（3×2×5=30 条）+ speech-2.8-turbo 抽样（zh 共情句
5 变体=5 条），共 35 条 ≤36 预算。音色：zh=Chinese (Mandarin)_Warm_Bestie、
canto=Cantonese_GentleLady、en=English_Trustworth_Man；语速与生产同规则
（zh/粤 1.2、en 1.0，minimax_speed_for 同源）。

计时：stream=true SSE 首音频块时刻=first_audio_ms；时长优先 extra_info
（缺席按 128kbps CBR 估）。失败自动回退非流式（first_audio_ms 记 null）。

凭据：env MINIMAX_API_KEY 优先，缺省只读打开 settings DB（global_settings.
tts_json.api_key）——**绝不打印 key**（同 cache_minimax_auditions.py 取法）。
SSRF 护栏同源：https + host 白名单 {api.minimax.cn, api.minimax.chat}。

用法：
  .venv312/bin/python scripts/bench/bench_voice_tags_ab.py --dry-run  # 只列计划
  .venv312/bin/python scripts/bench/bench_voice_tags_ab.py            # 全量 35 条
  .venv312/bin/python scripts/bench/bench_voice_tags_ab.py --model hd  # 只跑 hd
产出：reports/voice-tags-ab/{hd,turbo}/*.mp3 + manifest.jsonl + REPORT.md
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
import hashlib
import json
import os
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "reports" / "voice-tags-ab"
SETTINGS_DB = Path.home() / "Library" / "Application Support" / "BokVoice" / "bok_voice.db"

T2A_PATH = "/v1/t2a_v2"
CN_BASE = "https://api.minimax.cn"
INTL_BASE = "https://api.minimax.chat"
ALLOWED_HOSTS = frozenset({"api.minimax.cn", "api.minimax.chat"})

SAMPLE_RATE = 32000   # mp3 合法档（同 cache_minimax_auditions.py）
BITRATE = 128000      # 显式 CBR——extra_info 缺席时时长可估
TIMEOUT_S = 60
RETRIES = 3
PACE_S = 0.5          # 逐条小睡，给 RPM 限频留余量

VOICES = {
    "zh": "Chinese (Mandarin)_Warm_Bestie",
    "cantonese": "Cantonese_GentleLady",
    "en": "English_Trustworth_Man",
}
SPEEDS = {"zh": 1.2, "cantonese": 1.2, "en": 1.0}  # 生产同规则（minimax_speed_for 同源）

# 每句拆两小句——变体只动句界（句号后插标记/停顿），保证 A/B 只差一个 token。
SENTENCES = {
    ("zh", "empathy"): ("您先别急，这件事我们一定会负责到底", "我马上帮您登记处理，尽快给您答复"),
    ("zh", "query"): ("我帮您查一下包裹的到仓状态", "麻烦您提供一下运单号，我这边马上核实"),
    ("cantonese", "empathy"): ("你唔使急，呢件事我哋一定會負責到底", "我即刻幫你登記處理，盡快答覆你"),
    ("cantonese", "query"): ("我幫你查下件包裹到咗未", "唔該畀個運單號我，我即刻幫你核實"),
    ("en", "empathy"): (
        "I completely understand your concern",
        "We will take full responsibility and make this right for you",
    ),
    ("en", "query"): (
        "Let me check the arrival status for your package",
        "Could you please give me the tracking number",
    ),
}

# (variant, 句界插值)——en 侧句号自动换 ". "；joiner 里 {c1}/{c2} 为两小句。
VARIANTS = [
    ("plain", "{c1}. {c2}."),
    ("breath", "{c1}. (breath){c2}."),
    ("emm", "{c1}. (emm){c2}."),
    ("sighs", "{c1}. (sighs){c2}."),
    ("pause", "{c1}. <#0.3#>{c2}."),
]
# 中文句界：全角句号、标记后不加空格（生产 TTS 文本形态）
JOINERS_ZH = {
    "plain": "{c1}。{c2}。",
    "breath": "{c1}。(breath){c2}。",
    "emm": "{c1}。(emm){c2}。",
    "sighs": "{c1}。(sighs){c2}。",
    "pause": "{c1}。<#0.3#>{c2}。",
}

MODEL_IDS = {"hd": "speech-2.8-hd", "turbo": "speech-2.8-turbo"}
# turbo 抽样：只跑 zh 共情句全变体（模型 A/B 锚点句）
TURBO_SAMPLE = [("zh", "empathy")]


def build_plan(models: list[str]) -> list[dict]:
    plan: list[dict] = []
    for mkey in models:
        model = MODEL_IDS[mkey]
        pairs = list(SENTENCES.items()) if mkey == "hd" else [(k, SENTENCES[k]) for k in TURBO_SAMPLE]
        for (lang, sent_id), (c1, c2) in pairs:
            for variant, joiner_en in VARIANTS:
                joiner = joiner_en if lang == "en" else JOINERS_ZH[variant]
                text = joiner.format(c1=c1, c2=c2)
                plan.append(
                    {
                        "model_key": mkey,
                        "model": model,
                        "lang": lang,
                        "sentence_id": sent_id,
                        "variant": variant,
                        "voice": VOICES[lang],
                        "speed": SPEEDS[lang],
                        "text": text,
                        "file": f"{mkey}/{lang}-{sent_id}-{variant}.mp3",
                    }
                )
    return plan


# ---- SSRF 护栏（同 cache_minimax_auditions.py：云端目标钉死白名单） ----
def _url_ok(url: str) -> bool:
    parts = urllib.parse.urlsplit(str(url or ""))
    return (
        parts.scheme == "https"
        and (parts.hostname or "").lower() in ALLOWED_HOSTS
        and parts.port in (None, 443)
        and not parts.username
        and not parts.password
    )


def endpoint() -> str:
    base = os.environ.get("MINIMAX_BASE_URL", "").strip().rstrip("/")
    if not base:
        region = os.environ.get("MINIMAX_REGION", "cn").strip().lower()
        base = INTL_BASE if region in {"intl", "global", "chat"} else CN_BASE
    return base if base.endswith(T2A_PATH) else f"{base}{T2A_PATH}"


def load_api_key() -> tuple[str, str]:
    """返回 (key, 来源说明)；key 绝不回显。env 优先，缺省读 settings DB。"""
    raw = os.environ.get("MINIMAX_API_KEY", "").strip()
    if raw:
        return raw, "env MINIMAX_API_KEY"
    if not SETTINGS_DB.exists():
        print(f"FAIL: 找不到设置库 {SETTINGS_DB}，也没有 MINIMAX_API_KEY env", file=sys.stderr)
        sys.exit(2)
    try:
        con = sqlite3.connect(f"file:{SETTINGS_DB}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT tts_json FROM global_settings").fetchone()
        finally:
            con.close()
        key = str((json.loads(row[0]) if row else {}).get("api_key") or "").strip()
    except (sqlite3.Error, ValueError, TypeError) as exc:
        print(f"FAIL: 读设置库失败（{exc}）；可改用 MINIMAX_API_KEY env", file=sys.stderr)
        sys.exit(2)
    if not key:
        print("FAIL: 设置库 tts_json.api_key 为空（设置 → TTS 语音合成 填 Key）", file=sys.stderr)
        sys.exit(2)
    return key, "settings DB tts.api_key"


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # pragma: no cover
        return ssl.create_default_context()


def _request_body(item: dict, stream: bool) -> dict:
    return {
        "model": item["model"],
        "text": item["text"],
        "stream": stream,
        "voice_setting": {"voice_id": item["voice"], "speed": float(item["speed"]), "vol": 1, "pitch": 0},
        "audio_setting": {
            "sample_rate": SAMPLE_RATE,
            "bitrate": BITRATE,
            "format": "mp3",
            "channel": 1,
        },
    }


def _post(key: str, url: str, body: dict, timeout: float):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())


def _duration_ms(audio_bytes: int) -> int:
    """时长=bytes×8/显式码率（CBR，请求里钉死 128000）。

    实测校准（2026-10-07，afconvert 解 PCM 逐样本核）：MiniMax SSE
    extra_info.audio_length **恒 ≈ 真实时长一半**（单位口径不明，2960 vs 实测
    6190ms），不可信；bytes/码率估计与 PCM 解码差 <0.5%，听感台架足够。"""
    return int(audio_bytes * 8 / BITRATE * 1000)


def synth_stream(key: str, url: str, item: dict) -> tuple[bytes, float | None]:
    """t2a_v2 stream=true SSE：首音频块时刻=first_audio_ms。

    返回 (mp3_bytes, first_audio_ms)。时长不取 extra_info（口径少半，见
    _duration_ms）；SSE 零音频/解析失败抛 RuntimeError（调用方回退非流式）。"""
    t0 = time.perf_counter()
    first_ms: float | None = None
    audio = bytearray()
    with _post(key, url, _request_body(item, stream=True), TIMEOUT_S) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[len("data:"):].strip())
            except ValueError:
                continue
            base_resp = ev.get("base_resp") or {}
            if int(base_resp.get("status_code", 0)) != 0:
                raise RuntimeError(f"MiniMax base_resp={base_resp}")
            hexchunk = ((ev.get("data") or {}).get("audio") or "")
            if hexchunk:
                if first_ms is None:
                    first_ms = (time.perf_counter() - t0) * 1000
                audio.extend(bytes.fromhex(hexchunk))
    if not audio:
        raise RuntimeError("stream=true 零音频")
    return bytes(audio), first_ms


def synth_plain(key: str, url: str, item: dict) -> tuple[bytes, float | None]:
    """非流式回退：first_audio_ms 不可测（None），时长由 _duration_ms 按 bytes 算。"""
    t0 = time.perf_counter()
    with _post(key, url, _request_body(item, stream=False), TIMEOUT_S) as resp:
        payload = json.loads(resp.read().decode())
    base_resp = payload.get("base_resp") or {}
    code = int(base_resp.get("status_code", -1))
    if code != 0:
        raise RuntimeError(f"MiniMax base_resp={base_resp}")
    audio_hex = (payload.get("data") or {}).get("audio") or ""
    if not audio_hex:
        raise RuntimeError("MiniMax 返回空音频")
    return bytes.fromhex(audio_hex), (time.perf_counter() - t0) * 1000  # 首包≈总包，仅参考


def synth_one(key: str, url: str, item: dict) -> tuple[bytes, dict]:
    """单条合成：stream 优先、非流式回退；429/传输抖动退避重试。

    返回 (mp3_bytes, meta)；meta.first_audio_ms 仅 stream 档可测（plain=None）。"""
    last = ""
    for attempt in range(RETRIES):
        if not _url_ok(url):
            raise RuntimeError(f"SSRF 护栏拒绝非白名单目标: {url}")
        try:
            try:
                audio, first_ms = synth_stream(key, url, item)
                mode = "stream"
            except RuntimeError as exc:
                # 流式协议层失败 → 回退非流式；确定性音色错（2054）不空转
                if "2054" in str(exc) or "voice-not-exist" in str(exc):
                    raise
                audio, first_ms = synth_plain(key, url, item)
                mode = "plain"
                first_ms = None  # 非流式测不了首音频
            if len(audio) < 512:
                raise RuntimeError(f"音频过短（{len(audio)} 字节）")
            if not (audio[:3] == b"ID3" or (audio[0] == 0xFF and (audio[1] & 0xE0) == 0xE0)):
                raise RuntimeError(f"音频头非 mp3 帧（前 4 字节 {audio[:4].hex()}）")
            meta = {
                "bytes": len(audio),
                "duration_ms": _duration_ms(len(audio)),
                "first_audio_ms": round(first_ms) if first_ms is not None else None,
                "mode": mode,
            }
            return audio, meta
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code == 401:
                raise RuntimeError("MiniMax 鉴权失败（key 过期/无效，检查设置页 TTS API Key）") from exc
            if exc.code in (429, 500, 502, 503, 504) and attempt < RETRIES - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"MiniMax {last}: {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = repr(exc)
            if attempt < RETRIES - 1:
                time.sleep(3 * (attempt + 1))
                continue
    raise RuntimeError(f"重试耗尽: {last}")


def write_report(rows: list[dict], out_dir: Path, failed: list[str]) -> None:
    """人耳 A/B 报告：清单表 + 听什么指引（台架不打分）。"""
    by_lang = {"zh": "普通话", "cantonese": "粤语", "en": "英语"}
    lines = [
        "# A 线语气标记 ±对照试听（voice tags A/B）",
        "",
        f"生成：{time.strftime('%Y-%m-%d %H:%M:%S')}；共 {len(rows)} 条真合成"
        + (f"；失败 {len(failed)} 条" if failed else ""),
        "",
        "## 听什么",
        "",
        "- 同句五变体只差一个 token：plain（素）/ (breath) 换气 / (emm) 迟疑 /",
        "  (sighs) 叹气 / <#0.3#> 0.3s 停顿。",
        "- **(sighs) 不在 A 线白名单**（现役六标记 clear-throat/inhale/breath/coughs/emm/exhale）——",
        "  重点听它是否被渲染成真叹气声（官方真标记→值不值得扩白名单）还是被当文本照念。",
        "- hd 目录=全矩阵（3 语×2 句×5 变体）；turbo 目录=zh 共情句 5 变体（模型档 A/B）。",
        "- 语速与生产同规则（zh/粤 1.2、en 1.0）。",
        "",
        "## 清单",
        "",
        "| file | lang | sentence | variant | model | voice | duration_ms | first_audio_ms |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['file']} | {by_lang.get(r['lang'], r['lang'])} | {r['sentence_id']} | {r['variant']} "
            f"| {r['model']} | {r['voice']} | {r['duration_ms']} | {r['first_audio_ms']} |"
        )
    if failed:
        lines += ["", "## 失败", ""] + [f"- {f}" for f in failed]
    (out_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="A 线语气标记 ±对照真合成 A/B 台架（人耳测）")
    ap.add_argument("--model", default="both", choices=["hd", "turbo", "both"], help="合成档（默认两档）")
    ap.add_argument("--out", default=str(OUT_DIR), help="产物目录（默认 reports/voice-tags-ab/）")
    ap.add_argument("--force", action="store_true", help="已存在也重合成（默认幂等跳过）")
    ap.add_argument(
        "--refit",
        action="store_true",
        help="不调 API：按 manifest 里 bytes 重算时长并重写 manifest+REPORT（修 extra_info 口径史数据）",
    )
    ap.add_argument("--dry-run", action="store_true", help="只列计划，不调 API")
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    if args.refit:
        manifest_path = out_dir / "manifest.jsonl"
        if not manifest_path.exists():
            print(f"FAIL: 无 manifest 可修: {manifest_path}", file=sys.stderr)
            return 2
        fixed: list[dict] = []
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row.get("bytes"), int) and row["bytes"] > 0:
                row["duration_ms"] = _duration_ms(row["bytes"])
                row["duration_source"] = "bytes/bitrate"
            fixed.append(row)
        manifest_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in fixed), encoding="utf-8")
        write_report(fixed, out_dir, [])
        print(f"[refit] {len(fixed)} 行时长重算 → {manifest_path} / REPORT.md")
        return 0

    models = ["hd"] if args.model == "hd" else (["turbo"] if args.model == "turbo" else ["hd", "turbo"])
    plan = build_plan(models)
    if args.dry_run:
        for it in plan:
            print(f"{it['model']:>16} {it['lang']:>9} {it['sentence_id']:>7} {it['variant']:>6} {it['file']}")
        print(f"[dry-run] {len(plan)} 条 → {args.out}")
        return 0

    url = endpoint()
    if not _url_ok(url):
        print(f"FAIL: 端点未过 SSRF 白名单: {url}", file=sys.stderr)
        return 2
    key, key_src = load_api_key()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[env] endpoint={urllib.parse.urlsplit(url).netloc}{T2A_PATH} key={key_src} plan={len(plan)} 条")

    manifest_path = out_dir / "manifest.jsonl"
    done_rows: list[dict] = []
    failed: list[str] = []
    planned_files = [it["file"] for it in plan]
    with manifest_path.open("a", encoding="utf-8") as mf:
        for idx, item in enumerate(plan):
            path = out_dir / item["file"]
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and not args.force:
                print(f"[skip] {item['file']} 已存在")
                continue
            t0 = time.perf_counter()
            try:
                audio, meta = synth_one(key, url, item)
            except RuntimeError as exc:
                print(f"[fail] {item['file']}: {exc}", file=sys.stderr)
                failed.append(f"{item['file']}: {exc}")
                continue
            tmp = path.with_suffix(".mp3.tmp")
            tmp.write_bytes(audio)
            os.replace(tmp, path)  # 原子落盘
            row = {
                **item,
                "duration_ms": meta["duration_ms"],
                "first_audio_ms": meta["first_audio_ms"],
                "bytes": meta["bytes"],
                "synth_mode": meta["mode"],
                "wall_s": round(time.perf_counter() - t0, 2),
                "sha1": hashlib.sha1(item["text"].encode()).hexdigest()[:10],
            }
            mf.write(json.dumps(row, ensure_ascii=False) + "\n")
            mf.flush()
            done_rows.append(row)
            print(
                f"[ok] {item['file']} {meta['duration_ms']}ms first_audio={meta['first_audio_ms']}ms"
                f" {meta['bytes']}B ({row['wall_s']}s, {meta['mode']})"
            )
            if idx != len(plan) - 1:
                time.sleep(PACE_S)

    # 报告表=本计划面全量（本跑新合成 + 历史 manifest 里同计划名的行）
    if manifest_path.exists():
        seen = {r["file"] for r in done_rows}
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            if len(done_rows) >= len(planned_files) and not seen:
                break
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("file") in planned_files and row.get("file") not in seen:
                done_rows.append(row)
                seen.add(row["file"])
    write_report(done_rows, out_dir, failed)
    print(f"[done] ok={len(done_rows)} fail={len(failed)} → {out_dir / 'REPORT.md'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
