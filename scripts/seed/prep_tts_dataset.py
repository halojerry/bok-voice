#!/usr/bin/env python3
"""原始客服录音 → Qwen3-TTS SFT 数据集流水线（docs/TTS-SFT-DATA-PREP.md 阶段 C 数据先行）。

契约（docs/TTS-SFT-PLAYBOOK.md §1，官方硬约束）：
- JSONL 三字段：{"audio": <相对路径>, "text": <转写>, "ref_audio": <同一参考 wav>}
- audio 与 ref_audio 必须 24kHz 单声道（官方 dataset.py `assert sr == 24000`）——
  本工具第 ① 步重采样即钉死 24k mono s16le，产物不再过第二次 resample。
- 全库共用同一条 ref_audio（8-10s 最干净一条；--pick-ref 物化为 <out>/ref.wav
  并回填全部行）。
- **单说话人假设：本工具不做说话人分离（diarization）**——源料须是单说话人（坐席）
  录音，或录音系统已分轨（先取坐席那轨再喂本工具）。多人混音请先人工分轨；
  刻意不引 pyannote（docs/TTS-SFT-DATA-PREP.md ②步属上游人工可选工序，非本工具职责）。
- 降噪同样不做（宁可轻微噪声不要处理伪影——伪影会烙进音色）。

步骤映射（docs/TTS-SFT-DATA-PREP.md 七步）：
  ① 重采样 24k mono s16 → out/audio24/        [本工具]
  ② diarization                                [不做，见上]
  ③ 降噪                                       [不做，见上]
  ④ VAD 切句 3-30s → out/segments/ + manifest  [本工具]
  ⑤ ASR 粗转写（本地 :8787 Qwen3-ASR）→ out/asr/ [本工具]
  ⑥ 人工校对                                    [人工；本工具产出即其输入]
  ⑦ 固定 ref.wav + 回填 → --pick-ref            [本工具]

用法：
  python scripts/seed/prep_tts_dataset.py --input RAW_DIR --out WORK_DIR \
      [--lang cantonese] [--vad silero|ffmpeg] [--asr-url http://127.0.0.1:8787] \
      [--skip resample,vad,asr,assemble] [--pick-ref]

幂等：产物目录即状态——重采样按文件名跳过已有；切句窗口冻结在
segments/manifest.json（改参数想重切=换干净 --out 或删 manifest）；转写按
asr/<片段>.json 逐段跳过；组装/报告每次全量重算（廉价确定）。

退出码：0 成功；1 输入/参数问题；2 ffmpeg 缺失；3 ASR sidecar 不可达
（仅当第 ⑤ 步真有待转写片段时才要求 sidecar 在场）。

验收口径（2026-09-26）：交付时仅离线单测（纯函数面，tests/test_prep_tts_dataset.py），
**未实弹跑 ASR 转写**——report.json 的 asr.verified 恒为 false，真栈实跑一次后人工翻正。

技术注意：一律 subprocess 调 ffmpeg（二进制缺失时人话报错退出码 2），**不用
audioread**——本机 audioread 有 segfault 前科（docs/TTS-SFT-DATA-PREP.md ①步明令）。
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
import array
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── 契约常量 ────────────────────────────────────────────────────────────────
TARGET_SR = 24000          # 官方硬断言：audio/ref_audio 均 24kHz
ASR_SR = 16000             # :8787 sidecar 的 PCM 进料采样率（raw s16le mono）
CLIP_THRESHOLD = 32000     # |sample| >= 32000 记削波（距满幅 32767 约 2% 余量）
SILENCE_AMP = 500          # |sample| < 500（约 -36dBFS）记静音，报告口径用
LANGS = ("zh", "cantonese", "en")
# sidecar /api/start 的 language 提示值：zh 通话也整场下发 Chinese（A 线同源惯例，
# 保 code-switching 词）；cantonese 原样；en 归一 English（sidecar 亦兜底归一）。
SIDECAR_LANG = {"zh": "Chinese", "cantonese": "cantonese", "en": "English"}
DEFAULT_ASR_URL = "http://127.0.0.1:8787"
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac"}
ASR_VERIFIED = False       # 交付验收未实弹跑 ASR；真栈实跑后人工翻正
ASR_NOTE = "工具交付验收未实弹（仅离线纯函数单测）；此位标记 ASR 链路是否被真栈实跑过"

# ── 出站 URL 护栏（Mimosa SSRF 收编，2026-10-04）────────────────────────────
# ASR sidecar 基址来自 --asr-url（默认环回）。请求前逐条过 _url_ok：scheme ∈
# {http,https}、URL 禁 userinfo 内嵌凭据；host 白名单=环回 ∪ BOK_PROBE_EXTRA_HOSTS
# （LAN ASR 机与 urlguard_gate 同一显式 opt-in 口，绝不静默把 PCM 外送）。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_EXTRA_HOSTS = frozenset(
    h.strip().lower()
    for h in os.environ.get("BOK_PROBE_EXTRA_HOSTS", "").split(",")
    if h.strip()
)


def _url_ok(url: str) -> bool:
    """出站 URL 是否允许（纯函数，离线可测）。"""
    parts = urllib.parse.urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    return (
        parts.scheme in ("http", "https")
        and (host in _LOOPBACK_HOSTS or host in _EXTRA_HOSTS)
        and not parts.username
        and not parts.password
    )


def _out_path(root: Path, name: str) -> Path:
    """输出路径护栏（数据驱动路径单点）：resolve 后必须落在 --out 根内。

    name 恒为本工具自建的常量（train_raw.jsonl），但产物目录与 manifest 可被
    外部改动——越出根（含 ../ 穿越、符号链接逃逸）一律拒写，绝不静默写外部路径。
    """
    root_r = root.resolve()
    target = (root_r / name).resolve()
    if target != root_r and root_r not in target.parents:
        raise SystemExit(f"错误：输出路径越出 --out 根目录：{target}")
    return target

# ── 纯函数面（离线可测；不碰 IO/子进程）────────────────────────────────────


def merge_windows(
    windows: list[tuple[float, float]],
    *,
    min_sec: float = 3.0,
    max_sec: float = 30.0,
    drop_frag: float = 1.0,
    gap_merge: float = 0.3,
    short_join_gap: float = 1.0,
) -> tuple[list[tuple[float, float]], dict[str, int]]:
    """VAD 语音窗 → 3-30s 片段窗。

    - <drop_frag 秒碎片先丢（计数 fragments_dropped）；
    - 贪心扫并：段已够长（>=min_sec）只跨 <=gap_merge 秒小缝续并（句间留 <=0.3s
      自然呼吸）；段还短（<min_sec）允许跨 <=short_join_gap 秒缝拉邻窗凑长；
    - **永不硬切**：并入下一窗会超 max_sec 时先把当前段封口（预闭合，切片不落在
      语音内部）；单条连续语音窗自身超长时整段保留，只计 oversized_kept 告警
      （切在语音中间破坏韵律，SFT 语料宁长勿断）；
    - 合并完仍 <min_sec 的孤段丢弃（计数 short_dropped）。

    返回 (片段窗列表, 统计)。
    """
    stats = {"fragments_dropped": 0, "short_dropped": 0, "oversized_kept": 0}
    kept: list[list[float]] = []
    for s, e in sorted((float(a), float(b)) for a, b in windows):
        if e - s < drop_frag:
            stats["fragments_dropped"] += 1
        else:
            kept.append([s, e])
    chunks: list[list[float]] = []
    cur: list[float] | None = None
    for s, e in kept:
        if cur is None:
            cur = [s, e]
            continue
        if e - cur[0] > max_sec:  # 预闭合：并入会超上限，宁开新段不硬切
            chunks.append(cur)
            cur = [s, e]
            continue
        cur_len = cur[1] - cur[0]
        allow = gap_merge if cur_len >= min_sec else short_join_gap
        if s - cur[1] > allow:
            chunks.append(cur)
            cur = [s, e]
        else:
            cur[1] = e
    if cur is not None:
        chunks.append(cur)
    out: list[tuple[float, float]] = []
    for c in chunks:
        ln = c[1] - c[0]
        if ln < min_sec:
            stats["short_dropped"] += 1
            continue
        if ln > max_sec:
            stats["oversized_kept"] += 1
        out.append((c[0], c[1]))
    return out, stats


_SIL_START_RE = re.compile(r"silence_start:\s*([0-9]+(?:\.[0-9]+)?)")
_SIL_END_RE = re.compile(r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)")


def parse_silencedetect(stderr_text: str, total_sec: float) -> list[tuple[float, float]]:
    """ffmpeg silencedetect stderr → 语音窗（静音区间取补集）。

    尾部未闭合的 silence_start 视为延伸到 total_sec；零长/倒置窗丢弃。
    """
    sil: list[list[float]] = []
    start: float | None = None
    for line in stderr_text.splitlines():
        if "silence_start" in line and start is None:
            m = _SIL_START_RE.search(line)
            if m:
                start = float(m.group(1))
        m = _SIL_END_RE.search(line)
        if m and start is not None:
            sil.append([start, float(m.group(1))])
            start = None
    if start is not None:
        sil.append([start, total_sec])
    windows: list[tuple[float, float]] = []
    pos = 0.0
    for s, e in sil:
        s = max(s, 0.0)
        if s > pos:
            windows.append((pos, min(s, total_sec)))
        pos = max(pos, e)
    if pos < total_sec:
        windows.append((pos, total_sec))
    return [(s, e) for s, e in windows if e > s]


def decode_s16(raw: bytes) -> array.array[int]:
    """s16le PCM bytes → array('h')（大端机 byteswap 纠正）。"""
    raw = raw[: len(raw) // 2 * 2]
    arr: array.array[int] = array.array("h")
    arr.frombytes(raw)
    if sys.byteorder == "big":
        arr.byteswap()
    return arr


def pcm_stats(samples: array.array[int]) -> dict[str, float | int]:
    """单遍削波/静音统计：|v|>=CLIP_THRESHOLD 记削波，|v|<SILENCE_AMP 记静音。"""
    clipped = 0
    silent = 0
    for v in samples:
        a = v if v >= 0 else -v
        if a >= CLIP_THRESHOLD:
            clipped += 1
        elif a < SILENCE_AMP:
            silent += 1
    total = len(samples)
    return {
        "clipped": clipped,
        "total_samples": total,
        "clip_ratio": (clipped / total) if total else 0.0,
        "silence_ratio": (silent / total) if total else 0.0,
    }


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    frac = pos - lo
    if lo + 1 >= len(sorted_vals):
        return float(sorted_vals[-1])
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[lo + 1] * frac


def duration_stats(durs: list[float]) -> dict[str, float | int]:
    if not durs:
        return {"count": 0, "total_sec": 0.0, "p50": 0.0, "p90": 0.0, "min": 0.0, "max": 0.0}
    sv = sorted(durs)
    return {
        "count": len(sv),
        "total_sec": round(sum(sv), 3),
        "p50": round(_percentile(sv, 0.50), 3),
        "p90": round(_percentile(sv, 0.90), 3),
        "min": round(sv[0], 3),
        "max": round(sv[-1], 3),
    }


def sidecar_language(lang: str) -> str:
    """CLI 语言规范值（zh/cantonese/en）→ sidecar /api/start 的 language 提示值。"""
    if lang not in LANGS:
        raise ValueError(f"语言只支持 {'/'.join(LANGS)}，收到 {lang!r}")
    return SIDECAR_LANG[lang]


def jsonl_line(row: dict[str, Any]) -> str:
    """官方契约行序列化：ensure_ascii=False 保中文原样（官方 prepare_data 直接读）。"""
    return json.dumps(row, ensure_ascii=False)


def parse_jsonl(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def apply_ref(rows: list[dict[str, Any]], ref_name: str = "ref.wav") -> list[dict[str, Any]]:
    """回填/统一全部行的 ref_audio（幂等：重复调用结果一致；不改其余字段）。"""
    return [{**row, "ref_audio": ref_name} for row in rows]


def choose_ref(
    cands: list[dict[str, Any]], lo: float = 8.0, hi: float = 10.0
) -> tuple[dict[str, Any] | None, bool]:
    """选全库共用 ref：优先 8-10s 内最干净（削波少→静音少→更长→名字稳定序）。

    区间内无人时回落「最接近区间中点的片段」并置 widened=True（报告/终端告警）。
    """
    if not cands:
        return None, False
    in_range = [c for c in cands if lo <= float(c["duration"]) <= hi]
    if in_range:
        best = sorted(
            in_range,
            key=lambda c: (float(c["clip_ratio"]), float(c["silence_ratio"]), -float(c["duration"]), str(c["name"])),
        )[0]
        return best, False
    mid = (lo + hi) / 2.0
    best = sorted(
        cands,
        key=lambda c: (abs(float(c["duration"]) - mid), float(c["clip_ratio"]), str(c["name"])),
    )[0]
    return best, True


# ── IO / 子进程面 ──────────────────────────────────────────────────────────


def require_ffmpeg() -> str:
    """ffmpeg 二进制在位性；缺失=人话报错退出码 2（顺带 audioread 禁令注释）。

    不走 audioread/librosa.load 兜底——本机 audioread 有 segfault 前科
    （docs/TTS-SFT-DATA-PREP.md ①步明令 subprocess ffmpeg）。
    """
    path = shutil.which("ffmpeg")
    if not path:
        print(
            "错误：找不到 ffmpeg。请先安装（macOS: brew install ffmpeg / "
            "Windows: winget install ffmpeg 或官网下载后加入 PATH）后重跑。"
            "本工具只用 subprocess 调 ffmpeg，不用 audioread（本机有 segfault 前科）。",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return path


def run_ffmpeg(args: list[str], *, capture_stderr: bool = False, stdin_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["ffmpeg", *args],
        input=stdin_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
        check=False,
    )


def resample_to_24k(src: Path, dst: Path) -> None:
    """① 升采样重排：任意输入 → 24kHz mono s16le wav（docs 契约命令同款）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = run_ffmpeg(["-y", "-v", "error", "-i", str(src), "-ac", "1", "-ar", str(TARGET_SR), "-c:a", "pcm_s16le", str(dst)])
    if proc.returncode != 0:
        raise SystemExit(f"错误：ffmpeg 重采样失败 {src.name}\n{proc.stderr.decode(errors='replace').strip()}")


def read_s16_mono(path: Path) -> tuple[array.array[int], int]:
    """读本工具自产的 24k mono s16 wav（容错多声道=均混）；非 s16 wav 报人话错。"""
    with wave.open(str(path), "rb") as w:
        nch = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise SystemExit(f"错误：{path.name} 不是 16bit wav（工作区产物应恒为 s16，请勿手工替换）。")
    arr = decode_s16(raw)
    if nch > 1:
        mono: array.array[int] = array.array("h")
        for i in range(0, len(arr) - nch + 1, nch):
            acc = 0
            for j in range(nch):
                acc += arr[i + j]
            mono.append(int(acc / nch))
        return mono, rate
    return arr, rate


def speech_windows_ffmpeg(path: Path, total_sec: float) -> list[tuple[float, float]]:
    """silencedetect 粗切：-35dB、最短静音 0.3s（与句间呼吸档同源），取补集为语音窗。"""
    proc = run_ffmpeg(
        ["-nostats", "-i", str(path), "-af", "silencedetect=noise=-35dB:d=0.3", "-f", "null", "-"],
        capture_stderr=True,
    )
    return parse_silencedetect(proc.stderr.decode(errors="replace"), total_sec)


def speech_windows_silero(path: Path) -> list[tuple[float, float]] | None:
    """silero-vad 路径（16k 输入；未安装返回 None 由调用方回落 ffmpeg）。

    16k PCM 走 ffmpeg 管道直出（s16le mono 16000），不落中间文件。
    """
    try:
        import numpy as np  # type: ignore
        import torch  # type: ignore
        from silero_vad import get_speech_timestamps, load_silero_vad  # type: ignore
    except ImportError:
        return None
    proc = run_ffmpeg(
        ["-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", str(ASR_SR), "-"],
        stdin_bytes=b"",
    )
    if proc.returncode != 0:
        raise SystemExit(f"错误：ffmpeg 提取 16k PCM 失败 {path.name}\n{proc.stderr.decode(errors='replace').strip()}")
    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    model = load_silero_vad()
    ts = get_speech_timestamps(torch.from_numpy(pcm), model, sampling_rate=ASR_SR)
    return [(t["start"] / ASR_SR, t["end"] / ASR_SR) for t in ts]


def pcm_16k(path: Path) -> bytes:
    """片段 wav → 16k s16le mono PCM（sidecar 裸 body 的进料形状）。"""
    proc = run_ffmpeg(
        ["-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", str(ASR_SR), "-"],
        stdin_bytes=b"",
    )
    if proc.returncode != 0:
        raise SystemExit(f"错误：ffmpeg 提取 16k PCM 失败 {path.name}\n{proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def extract_segment(src: Path, dst: Path, start: float, dur: float) -> None:
    """按窗切片：输出侧 -ss/-t（PCM 无压缩帧，seek 样本级准），保持 24k mono s16。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = run_ffmpeg(
        ["-y", "-v", "error", "-i", str(src), "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
         "-ac", "1", "-ar", str(TARGET_SR), "-c:a", "pcm_s16le", str(dst)],
    )
    if proc.returncode != 0:
        raise SystemExit(f"错误：ffmpeg 切片失败 {src.name} @{start:.2f}s\n{proc.stderr.decode(errors='replace').strip()}")


def _http_json(req: urllib.request.Request, timeout: float) -> dict[str, Any]:
    target = req.full_url if isinstance(req, urllib.request.Request) else str(req)
    if not _url_ok(target):
        raise PermissionError(f"ASR sidecar URL 未过护栏（拒发）: {target}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def sidecar_health(base_url: str) -> dict[str, Any]:
    req = urllib.request.Request(base_url.rstrip("/") + "/health")
    return _http_json(req, timeout=5.0)


def sidecar_transcribe(base_url: str, pcm: bytes, language_hint: str) -> dict[str, Any]:
    """一次性转写：POST /api/start（language 提示）→ /api/finish 整包 body。

    sidecar /api/finish 兼容「整包 PCM 直接放 finish body」（app.py 注释明示），
    比 chunk 分片少几十次 HTTP。
    """
    q = urllib.parse.urlencode({"language": language_hint})
    start_req = urllib.request.Request(base_url.rstrip("/") + "/api/start?" + q, data=b"", method="POST")
    sess = _http_json(start_req, timeout=10.0)
    session_id = str(sess.get("session_id") or "")
    if not session_id:
        raise RuntimeError(f"sidecar /api/start 未返回 session_id: {sess}")
    q2 = urllib.parse.urlencode({"session_id": session_id})
    fin_req = urllib.request.Request(
        base_url.rstrip("/") + "/api/finish?" + q2, data=pcm, method="POST"
    )
    return _http_json(fin_req, timeout=180.0)


# ── 流水线编排 ─────────────────────────────────────────────────────────────


def discover_inputs(input_dir: Path) -> list[Path]:
    if not input_dir.is_dir():
        raise SystemExit(f"错误：--input 目录不存在：{input_dir}")
    files = sorted(p for p in input_dir.iterdir() if p.suffix.lower() in AUDIO_EXTS and p.is_file())
    if not files:
        raise SystemExit(f"错误：--input 目录里没有音频文件（支持 {'/'.join(sorted(AUDIO_EXTS))}）：{input_dir}")
    return files


def step_resample(files: list[Path], audio24: Path) -> dict[str, int]:
    require_ffmpeg()
    audio24.mkdir(parents=True, exist_ok=True)
    produced = 0
    for src in files:
        dst = audio24 / (src.stem + ".wav")
        if dst.exists():
            continue
        resample_to_24k(src, dst)
        produced += 1
    return {"inputs": len(files), "produced": produced, "skipped": len(files) - produced}


def step_vad(files: list[Path], audio24: Path, segments_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    """④ 切句：窗口冻结进 manifest（幂等=重跑复用）；缺片段文件只补切片。"""
    require_ffmpeg()
    segments_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = segments_dir / "manifest.json"
    manifest: dict[str, Any] = {"version": 1, "sources": {}}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sources: dict[str, Any] = manifest.setdefault("sources", {})
    vad_backend_used = manifest.get("vad_backend") or ""
    silero_fallback_noted = False
    for src in files:
        resampled = audio24 / (src.stem + ".wav")
        if not resampled.exists():
            raise SystemExit(f"错误：{resampled.name} 缺失——先跑重采样步（去掉 --skip resample）。")
        if src.name in sources:
            continue
        samples, rate = read_s16_mono(resampled)
        total_sec = len(samples) / rate if rate else 0.0
        windows: list[tuple[float, float]] | None = None
        backend = "ffmpeg"
        if args.vad == "silero":
            windows = speech_windows_silero(resampled)
            if windows is None:
                backend = "ffmpeg"
                if not silero_fallback_noted:
                    silero_fallback_noted = True
                    print("提示：silero-vad 未安装（pip install silero-vad，需 torch），回落 ffmpeg silencedetect。")
            else:
                backend = "silero"
        if windows is None:
            windows = speech_windows_ffmpeg(resampled, total_sec)
        chunks, mstats = merge_windows(
            windows,
            min_sec=args.min_seg,
            max_sec=args.max_seg,
            drop_frag=args.drop_frag,
            gap_merge=args.gap_merge,
            short_join_gap=args.short_join_gap,
        )
        seg_entries: list[dict[str, Any]] = []
        for idx, (s, e) in enumerate(chunks, start=1):
            name = f"{src.stem}__seg{idx:03d}.wav"
            seg_path = segments_dir / name
            if not seg_path.exists():
                extract_segment(resampled, seg_path, s, e - s)
            seg_samples, _ = read_s16_mono(seg_path)
            st = pcm_stats(seg_samples)
            seg_entries.append(
                {
                    "name": name,
                    "source": src.name,
                    "start": round(s, 3),
                    "end": round(e, 3),
                    "duration": round(len(seg_samples) / rate, 3),
                    "clip_ratio": round(float(st["clip_ratio"]), 6),
                    "silence_ratio": round(float(st["silence_ratio"]), 4),
                }
            )
        sources[src.name] = {"vad_backend": backend, "merge_stats": mstats, "segments": seg_entries}
        vad_backend_used = vad_backend_used or backend
    manifest["vad_backend"] = vad_backend_used or args.vad
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def step_asr(manifest: dict[str, Any], segments_dir: Path, asr_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    """⑤ ASR 粗转写：逐段跳过已有 asr/<name>.json；sidecar 不通=人话报错退出码 3。"""
    asr_dir.mkdir(parents=True, exist_ok=True)
    all_segs = [seg for src in manifest["sources"].values() for seg in src["segments"]]
    pending = [s for s in all_segs if not (asr_dir / (s["name"] + ".json")).exists()]
    if not pending:
        return {"done": len(all_segs), "pending": 0, "skipped": False}
    require_ffmpeg()
    try:
        health = sidecar_health(args.asr_url)
    except Exception as exc:  # urllib 连不上系列
        print(
            f"错误：ASR sidecar 不可达（{args.asr_url}）：{exc}。请先起本地 ASR"
            "（python tools/bok.py serve 会拉起 :8787），或 --skip asr 跳过转写步。",
            file=sys.stderr,
        )
        raise SystemExit(3) from exc
    print(f"ASR sidecar 就绪：backend={health.get('backend')} model={Path(str(health.get('model') or '')).name}")
    hint = sidecar_language(args.lang)
    done = 0
    for seg in all_segs:
        out_path = asr_dir / (seg["name"] + ".json")
        if out_path.exists():
            continue
        pcm = pcm_16k(segments_dir / seg["name"])
        try:
            result = sidecar_transcribe(args.asr_url, pcm, hint)
        except Exception as exc:
            print(
                f"错误：转写 {seg['name']} 时 sidecar 请求失败：{exc}。已完成 {done} 段"
                "已落盘，重跑本工具会从断点续转。",
                file=sys.stderr,
            )
            raise SystemExit(3) from exc
        out_path.write_text(
            json.dumps(
                {"text": str(result.get("text") or ""), "language": str(result.get("language") or "")},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        done += 1
        if done % 10 == 0:
            print(f"  已转写 {done}/{len(pending)} …")
    return {"done": len(all_segs), "pending": 0, "skipped": False, "transcribed_now": done}


def step_assemble(out_dir: Path, manifest: dict[str, Any], asr_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    """④' 组装：train_raw.jsonl（官方契约）+ report.json（统计/质检旗/逐片段转写表）。"""
    rows: list[dict[str, Any]] = []
    table: list[dict[str, Any]] = []
    durs: list[float] = []
    done = 0
    pending = 0
    for src in manifest["sources"].values():
        for seg in src["segments"]:
            tpath = asr_dir / (seg["name"] + ".json")
            text = ""
            if tpath.exists():
                text = str(json.loads(tpath.read_text(encoding="utf-8")).get("text") or "")
                done += 1
            else:
                pending += 1
            durs.append(float(seg["duration"]))
            table.append(
                {
                    "name": seg["name"],
                    "source": seg["source"],
                    "start": seg["start"],
                    "end": seg["end"],
                    "duration": seg["duration"],
                    "clip_ratio": seg["clip_ratio"],
                    "silence_ratio": seg["silence_ratio"],
                    "transcribed": tpath.exists(),
                    "text": text,
                }
            )
            if tpath.exists():
                rows.append({"audio": f"segments/{seg['name']}", "text": text, "ref_audio": "ref.wav"})
    jsonl_path = _out_path(out_dir, "train_raw.jsonl")
    with jsonl_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(jsonl_line(row) + "\n")
    stats = duration_stats(durs)
    clip_ratio = (sum(t["clip_ratio"] for t in table) / len(table)) if table else 0.0
    silence_ratio = (sum(t["silence_ratio"] for t in table) / len(table)) if table else 0.0
    mstats_all = [src.get("merge_stats") or {} for src in manifest["sources"].values()]
    merged_stats = {
        key: sum(int(m.get(key, 0)) for m in mstats_all)
        for key in ("fragments_dropped", "short_dropped", "oversized_kept")
    }
    flags: list[str] = []
    if clip_ratio > 0.001:
        flags.append(f"clipping: 整体削波占比 {clip_ratio * 100:.2f}% 偏高，检查源录音增益")
    if merged_stats["oversized_kept"]:
        flags.append(f"oversized_segments: {merged_stats['oversized_kept']} 条超 {args.max_seg:.0f}s（连续语音未硬切），建议人工听检")
    if stats["p50"] and stats["p50"] < 4.0:
        flags.append(f"segments_short: 中位时长 {stats['p50']}s 低于目标 ~8s，检查 VAD 参数")
    if silence_ratio > 0.6:
        flags.append(f"silence_heavy: 静音占比 {silence_ratio * 100:.0f}% 偏高")
    if rows and not (out_dir / "ref.wav").exists():
        flags.append("ref_missing: 尚未选 ref（跑一次 --pick-ref）")
    report = {
        "tool": "prep_tts_dataset",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_dir": str(args.input),
        "lang": args.lang,
        "contract": {
            "jsonl_fields": ["audio", "text", "ref_audio"],
            "sample_rate": TARGET_SR,
            "ref_shared": True,
            "note": "24kHz 硬断言在官方 dataset.py；本工具产物恒 24kHz mono s16le",
        },
        "diarization": "不做（单说话人假设：源料单说话人或已分轨；多人混音先人工分轨）",
        "resample": getattr(args, "_resample_stats", None),
        "vad": {"backend": manifest.get("vad_backend"), **merged_stats},
        "segments": {**stats, "target": "3-30s（目标中位 ~8s）"},
        "qc": {"clip_ratio": round(clip_ratio, 6), "silence_ratio": round(silence_ratio, 4), "flags": flags},
        "asr": {
            "url": args.asr_url,
            "language": args.lang,
            "done": done,
            "pending": pending,
            "skipped": "asr" in (args.skip or "").split(","),
            "verified": ASR_VERIFIED,
            "note": ASR_NOTE,
        },
        "ref": getattr(args, "_ref_info", None),
        "segments_table": table,
    }
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"rows": len(rows), "report": report}


def step_pick_ref(out_dir: Path, report: dict[str, Any], args: argparse.Namespace) -> dict[str, Any] | None:
    """⑦ ref 选取：8-10s 最干净一条 → <out>/ref.wav（24k mono，契约达标）+ 回填全部行。"""
    table = report.get("segments_table") or []
    picked, widened = choose_ref(table)
    if picked is None:
        print("提示：没有任何片段可选 ref（先跑前面的步骤）。")
        return None
    src = out_dir / "segments" / str(picked["name"])
    dst = out_dir / "ref.wav"
    shutil.copyfile(src, dst)
    jsonl_path = _out_path(out_dir, "train_raw.jsonl")
    if jsonl_path.exists():
        rows = parse_jsonl(jsonl_path.read_text(encoding="utf-8"))
        rows = apply_ref(rows, dst.name)
        with jsonl_path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(jsonl_line(row) + "\n")
    if widened:
        print(f"警告：没有 8-10s 片段，ref 回落为最接近的 {picked['name']}（{picked['duration']}s）。")
    print(f"ref 已选：{picked['name']}（{picked['duration']}s，削波 {float(picked['clip_ratio']) * 100:.3f}%）→ ref.wav；全部行已回填。")
    return {"picked": picked["name"], "duration": picked["duration"], "widened": widened}


SKIP_CHOICES = ("resample", "vad", "asr", "assemble")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    def _lang(v: str) -> str:
        if v not in LANGS:
            raise argparse.ArgumentTypeError(f"语言只支持 {'/'.join(LANGS)}")
        return v

    def _skip(v: str) -> str:
        parts = [p.strip() for p in v.split(",") if p.strip()]
        bad = [p for p in parts if p not in SKIP_CHOICES]
        if bad:
            raise argparse.ArgumentTypeError(f"--skip 只支持 {','.join(SKIP_CHOICES)}，收到 {bad}")
        return ",".join(parts)

    ap = argparse.ArgumentParser(
        description="原始录音 → Qwen3-TTS SFT 数据集（24kHz mono / 全库同一 ref / 单说话人假设）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "契约：JSONL {audio,text,ref_audio}；audio 与 ref_audio 均 24kHz mono（官方 dataset.py 硬断言）；\n"
            "全库共用一条 ref（8-10s 最干净一条）。diarization/降噪不做：源料须单说话人（或已分轨），\n"
            "多人混音先人工分轨。退出码：0 成功 / 1 输入问题 / 2 ffmpeg 缺失 / 3 ASR sidecar 不可达。\n"
            "本工具只用 subprocess 调 ffmpeg，不用 audioread（本机有 segfault 前科）。"
        ),
    )
    ap.add_argument("--input", type=Path, required=True, help="原始录音目录（wav/mp3/m4a/flac/ogg/aac）")
    ap.add_argument("--out", type=Path, required=True, help="工作区目录（产物即状态，幂等可重跑）")
    ap.add_argument("--lang", type=_lang, default="cantonese", help="转写语言提示（默认 cantonese）")
    ap.add_argument("--vad", choices=("silero", "ffmpeg"), default="silero", help="VAD 后端；silero 未安装自动回落 ffmpeg silencedetect")
    ap.add_argument("--asr-url", default=DEFAULT_ASR_URL, help="本地 ASR sidecar 地址（默认 :8787）")
    ap.add_argument("--skip", type=_skip, default="", help="跳过的步骤，逗号分隔（resample,vad,asr,assemble）")
    ap.add_argument("--pick-ref", action="store_true", help="选 8-10s 最干净片段为全库 ref.wav 并回填全部行")
    ap.add_argument("--min-seg", type=float, default=3.0, help="最短片段秒数（更短丢弃）")
    ap.add_argument("--max-seg", type=float, default=30.0, help="最长片段秒数（超长不硬切只告警）")
    ap.add_argument("--drop-frag", type=float, default=1.0, help="VAD 碎片丢弃门槛秒数")
    ap.add_argument("--gap-merge", type=float, default=0.3, help="段够长后允许续并的最大缝隙秒数")
    ap.add_argument("--short-join-gap", type=float, default=1.0, help="段未达最短时长时允许跨缝拉邻窗的最大缝隙秒数")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    audio24 = out_dir / "audio24"
    segments_dir = out_dir / "segments"
    asr_dir = out_dir / "asr"
    skip = set((args.skip or "").split(",")) - {""}

    files: list[Path] = []
    if "resample" not in skip or "vad" not in skip:
        # 源清单恒按 --input 原始名（manifest 键）；--skip resample 时 audio24
        # 缺产物会在切句步报人话错，静默兜底只会掩盖半成品工作区。
        files = discover_inputs(args.input)

    if "resample" not in skip:
        stats = step_resample(files, audio24)
        args._resample_stats = stats  # type: ignore[attr-defined]
        print(f"① 重采样：新产 {stats['produced']}，跳过已有 {stats['skipped']}。")
    else:
        args._resample_stats = None  # type: ignore[attr-defined]
        print("① 重采样：按 --skip 跳过。")

    manifest: dict[str, Any]
    if "vad" in skip:
        manifest_path = segments_dir / "manifest.json"
        if not manifest_path.exists():
            raise SystemExit("错误：--skip vad 但 segments/manifest.json 不存在（首次必须跑切句步）。")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        print("② VAD 切句：按 --skip 跳过，复用既有 manifest。")
    else:
        manifest = step_vad(files, audio24, segments_dir, args)
        n_segs = sum(len(s["segments"]) for s in manifest["sources"].values())
        print(f"④ VAD 切句：{len(manifest['sources'])} 个源 → {n_segs} 个片段（manifest 冻结窗口）。")

    if "asr" in skip:
        args_asr = {"done": 0, "pending": 0, "skipped": True}
        print("⑤ ASR 转写：按 --skip 跳过。")
    else:
        args_asr = step_asr(manifest, segments_dir, asr_dir, args)  # type: ignore[assignment]
        print(f"⑤ ASR 转写：在库 {args_asr['done']} 段，待转写 {args_asr['pending']}。")  # type: ignore[index]

    args._ref_info = None  # type: ignore[attr-defined]
    if "assemble" not in skip:
        built = step_assemble(out_dir, manifest, asr_dir, args)
        print(f"组装：train_raw.jsonl {built['rows']} 行 + report.json 已写。")
        if args.pick_ref:
            args._ref_info = step_pick_ref(out_dir, built["report"], args)  # type: ignore[attr-defined]
            if args._ref_info:  # type: ignore[attr-defined]
                built = step_assemble(out_dir, manifest, asr_dir, args)  # 回填后重算报告（含 ref 段）
    else:
        print("组装：按 --skip 跳过。")
        if args.pick_ref:
            print("提示：--pick-ref 需要组装步产出 report.json，--skip assemble 下被忽略。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
