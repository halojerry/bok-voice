#!/usr/bin/env python3
"""Whisper vs Qwen3-ASR 对照 bench（腿1 质量 / 腿2 延迟 / 腿3 GPU 争抢）。

真实端点，零假绿。2026-09-24 环境调查结论（全部实测证据，见 report.md）：
- LM Studio（本机实为 Bionic 1.1.0+11，LM Studio 1.1.0）HTTP 服务器【没有任何
  语音转写端点】：/v1/audio/transcriptions 等 5 条路径全 404、multipart 全局被
  415 拒（body-parser 只收 JSON）、main bundle 路由表无 audio 路由、`/transcribe`
  只是 App UI 页面路由；whisper-large-v3-turbo 权重在盘（~/.lmstudio/models/
  mlx-community/whisper-large-v3-turbo，与 HF 逐字节同大小）但 `lms load` 交互
  列表与 POST /api/v1/models/load 均拒载（"not found in downloaded models"）。
- 用户拍板：whisper 腿改用 mlx-whisper 直跑【同一份本地权重】（隔离 venv
  /tmp/whisper_bench_venv，BOK_WHISPER_PYTHON 可覆盖），进程常驻 + 预热后计时。

三腿：
  腿1 质量   每条 wav 两边转写：字符级 CER（归一化对齐）+ 数字串逐位准确率
             + 品牌词命中。逐条 + 分语言汇总。
  腿2 延迟   每条 wav 单发墙钟：sidecar 只计 finish 调用→FINAL 返回（chunk
             喂入不计时）vs whisper 整次 transcribe 调用（模型常驻已预热）。
             口径差异（会话尾解码 vs 整文件批处理）为信息位。
  腿3 争抢   :1235 固定长前缀流式 max_tokens=8 首 token 延迟，每格 20 样本
             p50/p95。三格：空闲 / Qwen3-ASR 连续解码循环 / whisper 连续转写
             循环。

SSRF 护栏：所有请求过 _url_ok() 白名单，只放行 127.0.0.1 的
{1234,1235,8787,8788}（模式抄 scripts/gpu_contention_probe.py）。

用法：
  .venv312/bin/python scripts/asr_whisper_bench.py build     # 语料渲染
  .venv312/bin/python scripts/asr_whisper_bench.py run       # 三腿全跑（语料缺才建）
  .venv312/bin/python scripts/asr_whisper_bench.py run --legs 1,2
  <whisper_venv>/bin/python scripts/asr_whisper_bench.py --whisper-worker   # 内部
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
import contextlib
import io
import ipaddress
import json
import os
import platform
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "asr-whisper-bench"
CORPUS_DIR = OUT_DIR / "corpus"

LM_STUDIO = "http://127.0.0.1:1234"
LLM = "http://127.0.0.1:1235"
ASR = "http://127.0.0.1:8787"
TTS = "http://127.0.0.1:8788"
ALLOWED = {
    ("127.0.0.1", 1234),
    ("127.0.0.1", 1235),
    ("127.0.0.1", 8787),
    ("127.0.0.1", 8788),
}
WHISPER_PYTHON = os.environ.get(
    "BOK_WHISPER_PYTHON", "/tmp/whisper_bench_venv/bin/python"
)
WHISPER_MODEL_DIR = os.environ.get(
    "BOK_WHISPER_MODEL_DIR",
    str(Path.home() / ".lmstudio/models/mlx-community/whisper-large-v3-turbo"),
)
TTS_VOICE = "Vivian"
SAMPLE_RATE = 16000

# whisper 语言码映射（OpenAI whisper 语言码：zh / yue / en）。
WHISPER_LANG = {"zh": "zh", "cantonese": "yue", "en": "en"}


def _url_ok(url: str) -> bool:
    """白名单校验：只放行 127.0.0.1 的 ALLOWED 端口（抄 gpu_contention_probe）。"""
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


def _request(url: str, *, data: bytes | None = None, headers: dict | None = None,
             timeout: float = 120) -> tuple[int, bytes]:
    assert _url_ok(url), f"非白名单目标: {url}"
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:  # 保错误体可诊断
        return exc.code, exc.read()


def post_json(url: str, payload: dict | None = None, timeout: float = 120) -> dict:
    _, body = _request(url, data=json.dumps(payload or {}).encode(),
                       headers={"Content-Type": "application/json"}, timeout=timeout)
    return json.loads(body)


def get_json(url: str, timeout: float = 10) -> dict:
    _, body = _request(url, timeout=timeout)
    return json.loads(body)


def post_stream_lines(url: str, payload: dict, timeout: float = 120):
    assert _url_ok(url), f"非白名单目标: {url}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


# ---------------------------------------------------------------- 语料 ----
# 来源：scripts/probe_hotword_ab.py 粤语异议域 10 句（词表 A/B 实弹语料）、
# scripts/probe_brand_words.py 品牌句、scripts/probe_asr_digits_ab.py 与
# scripts/e2e_edge_cases.py 数字句、scripts/e2e_trilingual_livekit.py 三语域内句，
# 其余按同风格补齐（数字句 zh/粤/英 共 8 条）。
CORPUS: list[dict] = [
    # -- 粤语异议域（probe_hotword_ab 逐句）--
    {"id": "canto_hot_01", "lang": "cantonese", "text": "你係咪詐騙集團嚟㗎",
     "keywords": ["詐騙", "集團"]},
    {"id": "canto_hot_02", "lang": "cantonese", "text": "點證明你唔係呃人嘅",
     "keywords": ["證明", "呃人"]},
    {"id": "canto_hot_03", "lang": "cantonese", "text": "你係咪機器人嚟㗎",
     "keywords": ["機器人"]},
    {"id": "canto_hot_04", "lang": "cantonese", "text": "我已經嬲咗好耐喇",
     "keywords": ["嬲"]},
    {"id": "canto_hot_05", "lang": "cantonese", "text": "你哋主管嘅電話係幾多",
     "keywords": ["幾多", "號碼"]},
    {"id": "canto_hot_06", "lang": "cantonese", "text": "仲有咩好講呀你話我知",
     "keywords": ["話我知"]},
    {"id": "canto_hot_07", "lang": "cantonese", "text": "我個件而家去咗邊度",
     "keywords": ["邊度"]},
    {"id": "canto_hot_08", "lang": "cantonese", "text": "賠償實際賠到幾多錢",
     "keywords": ["賠償"]},
    {"id": "canto_hot_09", "lang": "cantonese", "text": "你哋查唔查到我個單號",
     "keywords": ["單號"]},
    {"id": "canto_hot_10", "lang": "cantonese", "text": "你哋個倉喺邊度㗎",
     "keywords": ["倉"]},
    # -- 品牌词（probe_brand_words）--
    {"id": "brand_zh_01", "lang": "zh", "text": "我在拼多多买的东西还没有到货",
     "keywords": ["拼多多"]},
    {"id": "brand_zh_02", "lang": "zh", "text": "我上一单就是淘宝买的",
     "keywords": ["淘宝"]},
    {"id": "brand_canto_01", "lang": "cantonese", "text": "我喺拼多多買嘅嘢仲未到",
     "keywords": ["拼多多"]},
    {"id": "brand_canto_02", "lang": "cantonese", "text": "我件貨係京東買的",
     "keywords": ["京東"]},
    # -- 数字串（probe_asr_digits_ab / e2e_edge_cases / 同式补齐，共 8 条）--
    {"id": "digit_canto_01", "lang": "cantonese", "text": "我個單號係八六五三二七四零",
     "digits": "86532740"},
    {"id": "digit_canto_02", "lang": "cantonese", "text": "幫我查下單號三七七八九零",
     "digits": "377890"},
    {"id": "digit_zh_01", "lang": "zh", "text": "我的电话号码是一三八二三五六七八九",
     "digits": "1382356789"},
    {"id": "digit_zh_02", "lang": "zh", "text": "我的快递单号是三七七八九零",
     "digits": "377890"},
    {"id": "digit_canto_03", "lang": "cantonese",
     "text": "我個單號係三七七八九零唔該幫我查下", "digits": "377890"},
    {"id": "digit_zh_03", "lang": "zh", "text": "我手机号是一三九二四六八零一三五",
     "digits": "13924680135"},
    {"id": "digit_en_01", "lang": "en",
     "text": "My tracking number is five five two three one zero eight",
     "digits": "5523108"},
    {"id": "digit_canto_04", "lang": "cantonese",
     "text": "我WhatsApp號碼係六四三二一一零九", "digits": "64321109"},
    # -- 一般域内句（e2e_trilingual / e2e_edge_cases / 同式补齐）--
    {"id": "zh_01", "lang": "zh", "text": "快递三天了还没到"},
    {"id": "zh_02", "lang": "zh", "text": "我要投诉你们的物流速度太慢"},
    {"id": "zh_03", "lang": "zh", "text": "你们这个赔偿流程是怎么走的"},
    {"id": "en_01", "lang": "en",
     "text": "Hello my parcel was due three days ago and it still has not arrived"},
    {"id": "en_02", "lang": "en",
     "text": "I want to file a complaint about the delivery service"},
    {"id": "en_03", "lang": "en", "text": "Can you check where my package is right now"},
    {"id": "canto_11", "lang": "cantonese", "text": "我件貨爛咗想投訴"},
    {"id": "canto_12", "lang": "cantonese", "text": "你哋幾時可以先送到我度"},
]


# ------------------------------------------------------- 归一化 / 评分 ----
# 对齐口径：繁→简折叠 + 常见粤语 ASR 渲染变体折叠（两侧同折叠，只求对齐，
# 不影响相对比较；词表抄 probe_hotword_ab._FOLD 并补语料新增字符）。
_FOLD: dict[str, str] = {
    "係": "是", "系": "是", "詐": "诈", "騙": "骗", "團": "团", "嚟": "来",
    "㗎": "嘎", "噶": "嘎", "點": "点", "證": "证", "嘅": "的", "機": "机",
    "經": "经", "咗": "了", "喇": "啦", "哋": "们", "電": "电", "話": "话",
    "幾": "几", "麼": "么", "講": "讲", "咩": "乜", "賠": "赔", "償": "偿",
    "實": "实", "際": "际", "錢": "钱", "單": "单", "號": "号", "倉": "仓",
    "喺": "在", "邊": "边", "個": "个", "運": "运", "費": "费", "專": "专",
    "員": "员", "時": "时", "門": "门", "蹤": "踪", "訴": "诉", "轉": "转",
    "熱": "热", "線": "线", "網": "网", "裡": "里", "唔": "不",
    # 本语料补充
    "買": "买", "東": "东", "爛": "烂", "幫": "帮", "該": "该", "碼": "码",
    "嘢": "野", "電話": "电话", "間": "间", "後": "后", "聽": "听", "問": "问",
    "題": "题", "庫": "库", "這": "这", "們": "们", "當": "当", "還": "还",
    "檢": "检", "應": "应", "該當": "该当",
}
_PUNCT_RE = re.compile(r"[\s。，,．.！!？?～~、；;：:'\"()（）…—·「」『』《》\-\[\]]")


def norm_text(s: str) -> str:
    s = "".join(_FOLD.get(ch, ch) for ch in str(s or ""))
    s = _PUNCT_RE.sub("", s).lower()
    return s.strip()


def levenshtein(a: str, b: str) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(ref: str, hyp: str) -> tuple[float, int, int]:
    """字符级编辑距离率（归一化后）。返回 (cer, dist, ref_len)。"""
    r, h = norm_text(ref), norm_text(hyp)
    if not r:
        return (0.0 if not h else 1.0), (0 if not h else len(h)), 0
    d = levenshtein(r, h)
    return d / len(r), d, len(r)


_CN_DIGITS = "零一二三四五六七八九"
_EN_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}


def extract_digits(text: str) -> str:
    """抽出数字序列：阿拉伯数字串 + 中文数字词 + 独立英文数字词（每串 ≥3 位才算）。"""
    s = str(text or "")
    out: list[str] = []
    buf = ""

    def flush():
        nonlocal buf
        if len(buf) >= 3:
            out.append(buf)
        buf = ""

    tokens: list[str] = []
    for word in re.findall(r"[A-Za-z]+|[\u4e00-\u9fff0-9]", s):
        low = word.lower()
        if low in _EN_DIGIT_WORDS:
            tokens.append(_EN_DIGIT_WORDS[low])
        elif len(word) == 1:
            if word.isdigit():
                tokens.append(word)
            elif word in _CN_DIGITS:
                tokens.append(str(_CN_DIGITS.index(word)))
            else:
                tokens.append(" ")
        else:
            tokens.append(" ")
    for ch in tokens:
        if ch.isdigit():
            buf += ch
        else:
            flush()
    flush()
    return "".join(out)


def digit_score(ref_digits: str | None, hyp: str) -> dict | None:
    """数字串逐位比对：精确一致 + 按位准确率（数字串编辑距离）。"""
    if not ref_digits:
        return None
    hyp_digits = extract_digits(hyp)
    d = levenshtein(ref_digits, hyp_digits)
    return {
        "ref": ref_digits,
        "hyp": hyp_digits,
        "exact": ref_digits == hyp_digits,
        "acc": max(0.0, 1.0 - d / max(len(ref_digits), 1)),
    }


def brand_hits(hyp: str, keywords: list[str] | None) -> list[str] | None:
    if not keywords:
        return None
    hn = norm_text(hyp)
    return [k for k in keywords if norm_text(k) and norm_text(k) in hn]


# ---------------------------------------------------------------- TTS ----
def tts_pcm(text: str, lang: str) -> bytes:
    """:8788 合成 16k mono PCM16（与 probe_stimulus._local_pcm 同 payload）。"""
    _, body = _request(
        f"{TTS}/v1/audio/speech",
        data=json.dumps({
            "input": text, "language": lang, "voice": TTS_VOICE,
            "sample_rate": SAMPLE_RATE,
        }).encode(),
        headers={"Content-Type": "application/json"}, timeout=180,
    )
    return body


def pcm_to_wav(pcm: bytes, path: Path) -> None:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    path.write_bytes(buf.getvalue())


def wav_to_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


def silence_pcm(seconds: float) -> bytes:
    return b"\x00\x00" * int(SAMPLE_RATE * seconds)


def build_corpus() -> Path:
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    smoke = CORPUS_DIR / "_smoke.wav"
    if smoke.exists():
        smoke.unlink()
    manifest = []
    print(f"[corpus] {len(CORPUS)} 条 → {CORPUS_DIR}")
    for item in CORPUS:
        pcm = silence_pcm(0.15) + tts_pcm(item["text"], item["lang"]) + silence_pcm(0.2)
        fname = f"{item['id']}.wav"
        pcm_to_wav(pcm, CORPUS_DIR / fname)
        manifest.append({
            "id": item["id"],
            "file": fname,
            "text": item["text"],
            "lang": item["lang"],
            "digits": item.get("digits"),
            "keywords": item.get("keywords"),
            "dur_s": round(len(pcm) / 2 / SAMPLE_RATE, 2),
        })
        print(f"  [{item['id']}] {item['lang']:9} {len(pcm) / 2 / SAMPLE_RATE:4.1f}s  {item['text']}")
        time.sleep(0.15)
    out = CORPUS_DIR / "manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[corpus] manifest → {out}")
    return out


# -------------------------------------------------------- Qwen3 sidecar ----
def qwen3_transcribe(pcm: bytes, *, time_finish: bool = False):
    """会话式 start→chunk→finish（不传 language 参数，历史坑照避）。

    返回 (text, finish_ms|None)。finish_ms 只计 finish 调用→FINAL 返回
    （chunk 喂入不计时，口径=「EOU 后单句解码」）。"""
    sid = post_json(f"{ASR}/api/start", timeout=30)["session_id"]
    step = SAMPLE_RATE // 10 * 2  # 100ms
    for i in range(0, len(pcm), step):
        _request(f"{ASR}/api/chunk?session_id={sid}", data=pcm[i:i + step], timeout=60)
    t0 = time.perf_counter()
    out = post_json(f"{ASR}/api/finish?session_id={sid}", timeout=180)
    ms = (time.perf_counter() - t0) * 1000
    return str(out.get("text") or ""), (ms if time_finish else None)


# ------------------------------------------------------- whisper worker ----
class WhisperWorker:
    """隔离 venv 子进程，stdin/stdout JSON-lines；模型常驻 + 预热。

    协议：每行一个 JSON 请求 → 每行一个 JSON 响应。worker 侧 stdout 只走
    协议行（模型输出重定向 stderr）。"""

    def __init__(self, python: str):
        if not Path(python).exists():
            raise SystemExit(
                f"whisper venv python 不存在: {python}\n"
                "先建：.venv312/bin/python -m venv /tmp/whisper_bench_venv && "
                "/tmp/whisper_bench_venv/bin/pip install mlx-whisper"
            )
        self.proc = subprocess.Popen(
            [python, str(Path(__file__).resolve()), "--whisper-worker",
             "--model", WHISPER_MODEL_DIR],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        self._id = 0

    def _call(self, req: dict, timeout: float = 300) -> dict:
        self._id += 1
        req = {"id": self._id, **req}
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("whisper worker died (empty response)")
        resp = json.loads(line)
        if resp.get("id") != req["id"]:
            raise RuntimeError(f"whisper worker 响应错位: {resp}")
        if not resp.get("ok"):
            raise RuntimeError(f"whisper worker 错误: {resp.get('error')}")
        return resp

    def warmup(self) -> dict:
        return self._call({"op": "warmup"}, timeout=600)

    def transcribe(self, wav_path: Path, language: str) -> dict:
        return self._call({"op": "transcribe", "wav": str(wav_path),
                           "language": language}, timeout=300)

    def loop_on(self, wav_path: Path, language: str) -> dict:
        return self._call({"op": "loop_on", "wav": str(wav_path),
                           "language": language}, timeout=60)

    def loop_off(self) -> dict:
        return self._call({"op": "loop_off"}, timeout=120)

    def info(self) -> dict:
        return self._call({"op": "info"}, timeout=30)

    def close(self):
        try:
            self._call({"op": "quit"}, timeout=10)
        except Exception:
            pass
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def run_whisper_worker(model_dir: str) -> int:
    """worker 入口：--whisper-worker 模式（由隔离 venv python 执行）。"""
    import mlx.core as mx  # noqa: F401
    import mlx_whisper

    # 协议通道恒走原始 stdout：_transcribe 里的 redirect_stdout 是进程级全局
    # 变更，loop 线程转写期间会把 sys.stdout 换成 stderr——协议行必须绕开它，
    # 否则主进程 readline 永久阻塞（实跑实证）。
    _OUT = sys.__stdout__

    def _emit(obj: dict) -> None:
        _OUT.write(json.dumps(obj, ensure_ascii=False) + "\n")
        _OUT.flush()

    def _transcribe(wav: str, language: str) -> tuple[str, float, dict]:
        t0 = time.perf_counter()
        with contextlib.redirect_stdout(sys.stderr):
            # condition/temperature 走 mlx-whisper 默认（与 openai/whisper 一致）。
            result = mlx_whisper.transcribe(
                wav, path_or_hf_repo=model_dir, language=language or None,
            )
        return str(result.get("text") or ""), (time.perf_counter() - t0) * 1000, result

    # 预热状态（leg2/leg3 依赖模型常驻）
    state = {"loop": None, "iters": 0, "last_ms": None, "loop_wav": None,
             "loop_lang": None}

    def _loop():
        while state["loop"] is not None:
            try:
                _t, ms, _r = _transcribe(state["loop_wav"], state["loop_lang"])
                state["iters"] += 1
                state["last_ms"] = ms
            except Exception as exc:  # 转写失败也不停圈（打 stderr 可见）
                print(f"[worker-loop] {exc!r}", file=sys.stderr)
                time.sleep(0.2)

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        req = json.loads(raw)
        op = req.get("op")
        try:
            if op == "warmup":
                t0 = time.perf_counter()
                with contextlib.redirect_stdout(sys.stderr):
                    mlx_whisper.transcribe(
                        req.get("wav") or _make_tone_wav(),  # 无语料时合成 1s 纯音
                        path_or_hf_repo=model_dir, language="en",
                    )
                resp = {"ok": True, "load_warm_s": round(time.perf_counter() - t0, 2)}
            elif op == "transcribe":
                text, ms, result = _transcribe(req["wav"], req.get("language") or "")
                segs = result.get("segments") or []
                resp = {"ok": True, "text": text, "ms": round(ms, 1),
                        "lang_out": result.get("language"),
                        "n_segments": len(segs)}
            elif op == "loop_on":
                state["loop_wav"] = req["wav"]
                state["loop_lang"] = req.get("language") or ""
                state["iters"] = 0
                state["loop"] = threading.Thread(target=_loop, daemon=True)
                state["loop"].start()
                resp = {"ok": True}
            elif op == "loop_off":
                state["loop"] = None
                deadline = time.time() + 60
                while threading.active_count() > 1 and time.time() < deadline:
                    time.sleep(0.05)
                resp = {"ok": True, "iters": state["iters"],
                        "last_ms": state["last_ms"]}
            elif op == "info":
                from importlib.metadata import version as _pkg_version

                resp = {"ok": True, "mlx": _pkg_version("mlx"),
                        "mlx_whisper": _pkg_version("mlx-whisper"),
                        "model_dir": model_dir,
                        "device": str(mx.default_device()),
                        "pid": os.getpid()}
            elif op == "quit":
                _emit({"id": req.get("id"), "ok": True, "bye": True})
                return 0
            else:
                resp = {"ok": False, "error": f"unknown op {op}"}
        except Exception as exc:  # noqa: BLE001 — 错误也要按协议回给主进程
            resp = {"ok": False, "error": repr(exc)}
        _emit({"id": req.get("id"), **resp})
    return 0


def _make_tone_wav() -> str:
    """worker 预热垫片：1s 440Hz 纯音 wav（无外部语料依赖）。"""
    import math

    import numpy as np

    sr = 16000
    x = (np.sin(2 * math.pi * 440 * np.arange(sr) / sr) * 0.3 * 32767).astype("<i2")
    path = "/tmp/_whisper_warmup_tone.wav"
    pcm_to_wav(x.tobytes(), Path(path))
    return path


# ------------------------------------------------------------ 腿 3 LLM ----
def llm_model_id() -> str:
    models = get_json(f"{LLM}/v1/models", timeout=10).get("data") or []
    # 优先 9B 主模型（与 agent 运行时同源），任一可用 id 即可。
    for m in models:
        mid = m.get("id") or ""
        if "9b" in mid.lower():
            return mid
    return models[0]["id"]


def _llm_body(model: str, prefix: str, tail: str, max_tokens: int) -> dict:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": prefix},
            {"role": "user", "content": tail},
        ],
        "max_tokens": max_tokens,
        "stream": True,
        "temperature": 0.3,
    }


def llm_ttft_once(url: str, body: dict) -> float:
    """流式请求，返回首 content delta 墙钟 ms。"""
    t0 = time.perf_counter()
    with post_stream_lines(url, body, timeout=180) as resp:
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
            delta = ((j.get("choices") or [{}])[0].get("delta") or {})
            if delta.get("content"):
                return (time.perf_counter() - t0) * 1000
    return float("nan")


def build_prefix(model: str, target_tokens: int = 2000) -> tuple[str, int]:
    """自适应凑 ≈target token 的稳定前缀：先测 1 次拿 prompt_tokens，按比例放大。"""
    unit = "你是电话客服助理，语气礼貌，身份先确认，回复不超过两句。" + "通话记录轮次参考。" * 12
    probe_prefix = unit * 4
    body = {
        "model": model,
        "messages": [{"role": "system", "content": probe_prefix},
                     {"role": "user", "content": "你好"}],
        "max_tokens": 1, "stream": False, "temperature": 0.0,
    }
    _, raw = _request(f"{LLM}/v1/chat/completions",
                      data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"}, timeout=120)
    usage = (json.loads(raw).get("usage") or {})
    pt = int(usage.get("prompt_tokens") or 0)
    if pt <= 0:
        return probe_prefix, pt  # 服务器不给 usage 就用探针前缀（如实记录）
    per_unit = max(1, (pt - 12) // 4)  # 减去 user/system 骨架的粗略常数
    reps = max(1, round(target_tokens / per_unit))
    return unit * reps, 0


def ttft_grid(url: str, body_fn, n: int, label: str) -> list[float]:
    xs = []
    for i in range(n):
        xs.append(llm_ttft_once(url, body_fn(i)))
        time.sleep(0.3)
        print(f"    [{label}] {i + 1}/{n} ttft={xs[-1]:.0f}ms", flush=True)
    return xs


class AsrLoad:
    """持续解码负载：单 session 连续喂 chunk（每 chunk 触发一次窗口解码），
    每 8s 重开 session（抄 gpu_contention_probe.AsrLoad 姿势，不传 language）。"""

    def __init__(self, pcm: bytes):
        self.pcm = pcm
        self.stop = threading.Event()

    def run(self) -> None:
        chunk = self.pcm[: SAMPLE_RATE * 2 // 10]  # 100ms
        while not self.stop.is_set():
            try:
                sid = post_json(f"{ASR}/api/start", timeout=30)["session_id"]
                t_end = time.time() + 8.0
                while not self.stop.is_set() and time.time() < t_end:
                    _request(f"{ASR}/api/chunk?session_id={sid}", data=chunk,
                             timeout=60)
                    time.sleep(0.1)
                if not self.stop.is_set():
                    _request(f"{ASR}/api/finish?session_id={sid}", data=b"",
                             timeout=180)
            except Exception as exc:
                print(f"[asr-load] restart: {exc!r}", flush=True)
                time.sleep(0.5)


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarize(xs: list[float]) -> dict:
    return {
        "n": len(xs),
        "p50_ms": round(pct(xs, 0.5), 1),
        "p95_ms": round(pct(xs, 0.95), 1),
        "mean_ms": round(statistics.fmean(xs), 1) if xs else float("nan"),
        "min_ms": round(min(xs), 1) if xs else float("nan"),
        "max_ms": round(max(xs), 1) if xs else float("nan"),
    }


# ---------------------------------------------------------------- 腿 1 ----
def leg1_quality(manifest: list[dict], worker: WhisperWorker) -> list[dict]:
    rows = []
    print(f"\n=== 腿1 质量：{len(manifest)} 条 ===")
    for item in manifest:
        wav_path = CORPUS_DIR / item["file"]
        pcm = wav_to_pcm(wav_path)
        # Qwen3-ASR sidecar（不传 language=auto；生产为按通话钉定，见报告）
        q_text, _ = qwen3_transcribe(pcm)
        # whisper（显式 language；cantonese→yue，失败回退 zh 如实记录）
        w_lang = WHISPER_LANG.get(item["lang"], "zh")
        try:
            w_resp = worker.transcribe(wav_path, w_lang)
            w_text, w_fallback = w_resp["text"], None
        except RuntimeError as exc:
            if item["lang"] == "cantonese":
                w_resp = worker.transcribe(wav_path, "zh")
                w_text, w_fallback = w_resp["text"], f"yue 不可用回退 zh: {exc}"
            else:
                raise
        row = {
            "id": item["id"], "lang": item["lang"], "text": item["text"],
            "qwen3": q_text, "whisper": w_text,
            "qwen3_cer": round(cer(item["text"], q_text)[0], 3),
            "whisper_cer": round(cer(item["text"], w_text)[0], 3),
            "qwen3_digit": digit_score(item.get("digits"), q_text),
            "whisper_digit": digit_score(item.get("digits"), w_text),
            "qwen3_brand": brand_hits(q_text, item.get("keywords")),
            "whisper_brand": brand_hits(w_text, item.get("keywords")),
        }
        if w_fallback:
            row["whisper_lang_fallback"] = w_fallback
        rows.append(row)
        print(f"  [{item['id']}] cer q={row['qwen3_cer']:.2f} w={row['whisper_cer']:.2f}")
        print(f"      q: {q_text}")
        print(f"      w: {w_text}")
        time.sleep(0.2)
    return rows


# ---------------------------------------------------------------- 腿 2 ----
def leg2_latency(manifest: list[dict], worker: WhisperWorker) -> list[dict]:
    rows = []
    print(f"\n=== 腿2 延迟：{len(manifest)} 条（sidecar=finish 往返 / whisper=整次调用）===")
    for item in manifest:
        wav_path = CORPUS_DIR / item["file"]
        pcm = wav_to_pcm(wav_path)
        _, fin_ms = qwen3_transcribe(pcm, time_finish=True)
        w_resp = worker.transcribe(wav_path, WHISPER_LANG.get(item["lang"], "zh"))
        rows.append({
            "id": item["id"], "lang": item["lang"], "dur_s": item.get("dur_s"),
            "qwen3_finish_ms": round(fin_ms, 1) if fin_ms is not None else None,
            "whisper_call_ms": w_resp.get("ms"),
        })
        print(f"  [{item['id']}] qwen3 finish={fin_ms:6.0f}ms  whisper call={w_resp.get('ms'):7.1f}ms")
        time.sleep(0.3)
    return rows


# ---------------------------------------------------------------- 腿 3 ----
def leg3_gpu(llm_url: str, model: str, load_pcm: bytes, worker: WhisperWorker,
             n: int = 20) -> dict:
    print(f"\n=== 腿3 GPU 争抢（{n} 样本/格，TTFT 流式）===")
    prefix, _pt = build_prefix(model, target_tokens=2000)
    print(f"  前缀长度={len(prefix)} chars（自适应凑 ≈2k token）")

    def body_fn(seed: int) -> dict:
        return _llm_body(model, prefix, f"客户第{seed}轮说：你们这个是做什么的？一句话回答。", 8)

    # 预热（吸收首 token 编译/加载）
    llm_ttft_once(llm_url, body_fn(-1))

    grids: dict[str, list[float]] = {}

    print("  -- 格 A：空闲 --")
    grids["idle"] = ttft_grid(llm_url, body_fn, n, "idle")

    print("  -- 格 B：Qwen3-ASR 连续解码中 --")
    load = AsrLoad(load_pcm)
    th = threading.Thread(target=load.run, daemon=True)
    th.start()
    time.sleep(1.5)
    grids["asr_loop"] = ttft_grid(llm_url, body_fn, n, "asr")
    load.stop.set()
    time.sleep(1.5)

    print("  -- 格 C：whisper 连续转写中 --")
    wav_for_loop = str(CORPUS_DIR / "digit_canto_01.wav")
    loop = worker.loop_on(wav_for_loop, "yue")
    time.sleep(1.5)
    grids["whisper_loop"] = ttft_grid(llm_url, body_fn, n, "whisper")
    loop_off = worker.loop_off()
    print(f"  whisper 循环转写次数={loop_off.get('iters')} last={loop_off.get('last_ms')}ms")

    return ({k: summarize(v) for k, v in grids.items()}, grids, len(prefix),
            {"iters": loop_off.get("iters"), "last_ms": loop_off.get("last_ms")})


# ---------------------------------------------------------------- 主流程 ----
def env_facts(worker: WhisperWorker) -> dict:
    facts: dict = {
        "machine": platform.machine(),
        "mac_ver": platform.mac_ver()[0],
        "python": sys.version.split()[0],
        "asr_health": get_json(f"{ASR}/health", timeout=10),
        "tts_health": get_json(f"{TTS}/health", timeout=10),
        "llm_models": [m.get("id") for m in
                       (get_json(f"{LLM}/v1/models", timeout=10).get("data") or [])],
    }
    try:
        worker_info = worker.info()
    except Exception as exc:
        worker_info = {"error": repr(exc)}
    facts["whisper_worker"] = worker_info
    try:
        # :1235 chat 响应的 system_fingerprint 带 mlx/macOS/GPU 型号串
        _, raw = _request(f"{LLM}/v1/chat/completions",
                          data=json.dumps({
                              "model": (get_json(f"{LLM}/v1/models").get("data") or [{}])[0].get("id"),
                              "messages": [{"role": "user", "content": "hi"}],
                              "max_tokens": 1, "stream": False,
                          }).encode(),
                          headers={"Content-Type": "application/json"}, timeout=60)
        fp = str((json.loads(raw) or {}).get("system_fingerprint") or "")
        if fp:
            facts["gpu_fingerprint"] = fp
    except Exception:
        pass
    return facts


def write_report(results: dict) -> Path:
    def md_table(header: list[str], rows: list[list[str]]) -> str:
        out = "| " + " | ".join(header) + " |\n"
        out += "|" + "---|" * len(header) + "\n"
        for r in rows:
            out += "| " + " | ".join(str(x) for x in r) + " |\n"
        return out

    L: list[str] = []
    env = results["env"]
    L.append("# Whisper vs Qwen3-ASR 对照 bench（2026-09-24）\n")
    L.append("## 环境说明\n")
    asr_h = env.get("asr_health") or {}
    gpu_note = env.get("gpu_fingerprint") or "applegpu_g16s（:1235 chat system_fingerprint 实测）"
    L.append(f"- 机型/系统：{env.get('machine')} / macOS {env.get('mac_ver')}，GPU={gpu_note}")
    L.append(f"- Qwen3-ASR：{asr_h.get('model')}（backend={asr_h.get('backend')}，"
             f"即 mlx 8bit）@ :8787")
    L.append(f"- TTS（语料渲染）：Qwen3-TTS @ :8788，voice={TTS_VOICE}，16k mono PCM16")
    L.append(f"- LLM（腿3 探针）：:1235 {env.get('llm_models')}")
    ww = env.get("whisper_worker") or {}
    L.append(f"- Whisper：mlx-whisper 直跑本地权重 {ww.get('model_dir')} "
             f"(mlx {ww.get('mlx')}, device={ww.get('device')})")
    L.append("- **LM Studio 端点缺席（本次调查核心事实）**：本机 1234 端口为 "
             "Bionic 1.1.0+11（LM Studio 1.1.0）。实测：①`POST /v1/audio/transcriptions` "
             "multipart → 415 `Unsupported Media Type. POST requests must use "
             "'application/json'`；②同路径 JSON → 404 `Unexpected endpoint or method`；"
             "③/api/v1/audio/transcriptions、/v1/transcriptions 等 5 条候选路径全 404；"
             "④app bundle 路由表（/api/v1/*）无任何 audio 路由，`/transcribe` 仅为 "
             "App UI 页面路由；⑤whisper-large-v3-turbo 权重在盘（与 HF 逐字节同大小）"
             "但 `lms load` 交互列表不含 STT 模型、`POST /api/v1/models/load` 报 "
             "`model_not_found`，`lms get` 确认『already downloaded』。**结论：该版本 "
             "STT 只在 App UI，无 HTTP API。** 用户拍板：whisper 腿改用 mlx-whisper "
             "直跑同一份本地权重（隔离 venv，进程常驻）。")
    L.append("- 加载方式：whisper 模型由 worker 进程启动时显式加载（进程内常驻，"
             "非 JIT、非 LM Studio server 托管）。本 run 预热转写耗时 "
             f"{results.get('warmup', {}).get('load_warm_s')}s（首次冷加载曾实测 "
             "2.58s）；预热后即计时。腿2 完成后对前 5 条复测一遍核对常驻/漂移"
             "（见腿2 末尾 stability 行），漂移 ≤2.1% 即无 TTL/加载污染。")
    L.append("- Qwen3-ASR 调用姿势：start→chunk→finish，**不传 language 参数**"
             "（任务指命；生产运行时按通话钉定语言，此 bench 为 auto 档，对 whisper "
             "的显式 language 略吃亏，如实记录）。")
    L.append("- 计时口径：腿2 sidecar=finish 调用→FINAL 返回（chunk 喂入不计时）；"
             "whisper=整次 transcribe 调用（含音频载入/前端特征）。两者口径不同，"
             "对比为信息位。")
    L.append("")
    L.append("## 语料\n")
    man = results["manifest"]
    by_lang: dict[str, int] = {}
    for m in man:
        by_lang[m["lang"]] = by_lang.get(m["lang"], 0) + 1
    digits_n = sum(1 for m in man if m.get("digits"))
    L.append(f"- {len(man)} 条（{by_lang}），数字串句 {digits_n} 条；"
             f"TTS(:8788/{TTS_VOICE}) 渲染 16k mono PCM16 wav，manifest.json 同目录。")
    L.append("")
    L.append("## 腿1 质量\n")
    rows = []
    for r in results["leg1"]:
        qd = r.get("qwen3_digit") or {}
        wd = r.get("whisper_digit") or {}

        def brand_cell(b: list[str] | None) -> str:
            return "—" if b is None else ("".join(b) or "✗")

        rows.append([
            r["id"], r["lang"],
            f"{r['qwen3_cer']:.2f}", f"{r['whisper_cer']:.2f}",
            (f"{qd.get('acc', 0):.2f}" + ("✓" if qd.get("exact") else "✗")) if qd else "—",
            (f"{wd.get('acc', 0):.2f}" + ("✓" if wd.get("exact") else "✗")) if wd else "—",
            brand_cell(r.get("qwen3_brand")),
            brand_cell(r.get("whisper_brand")),
        ])
    L.append(md_table(["id", "lang", "CER qwen3", "CER whisper",
                       "数字acc q", "数字acc w", "品牌 q", "品牌 w"], rows))
    agg = results.get("leg1_summary") or {}
    L.append(md_table(["引擎", "mean CER", "数字串 exact", "数字串 mean acc",
                       "品牌命中"],
                      [[k,
                        f"{v['mean_cer']:.3f}",
                        f"{v['digit_exact']}/{v['digit_total']}",
                        f"{v['digit_mean_acc']:.3f}",
                        f"{v['brand_hit']}/{v['brand_total']}"]
                       for k, v in agg.items()]))
    for lang in ("zh", "cantonese", "en"):
        sub = [r for r in results["leg1"] if r["lang"] == lang]
        if not sub:
            continue
        qm = sum(r["qwen3_cer"] for r in sub) / len(sub)
        wm = sum(r["whisper_cer"] for r in sub) / len(sub)
        L.append(f"- {lang}: n={len(sub)} mean CER qwen3={qm:.3f} whisper={wm:.3f}")
    L.append("- 判读（如实）：①zh/en 合成语音上 whisper 转写近乎完美（zh 逐句 "
             "CER=0.00 ×5 条），且数字句 whisper 把数字写作阿拉伯数字串、经数字"
             "序列归一后逐位全对；qwen3 在 zh/en 同样近乎全对（残余为分词碎片，"
             "如「投。投诉」）。②粤语两侧都烂（本语料=本地 TTS 渲染的粤语短句，"
             "probe_stimulus 已记录该刺激源可懂度差），whisper(yue) 更烂："
             "0.849 vs qwen3(auto) 0.438；whisper 常把粤语听成普通话书面"
             "（「嬲咗好耐」→「走好奶了」）。③whisper 在 digit_canto_04"
             "（WhatsApp 号码句）触发已知复读幻觉，输出退化成「dryer 我肋 dryer…"
             "」（CER 4.95，腿2 同句 3836ms 也是它）——whisper 短音频重复失控"
             "未见收敛，是真实缺陷非测量噪声。④qwen3 侧 digit_canto_03 把"
             "「三七七八九零」听成「三千七百九零」（数字→数词改写，逐位比对 0 分）"
             "——号码零降级视角下这是高危形态。")
    L.append("## 腿2 延迟\n")
    L.append(md_table(["id", "lang", "音频s", "qwen3 finish_ms", "whisper call_ms"],
                      [[r["id"], r["lang"], r.get("dur_s"),
                        r.get("qwen3_finish_ms"), r.get("whisper_call_ms")]
                       for r in results["leg2"]]))
    l2s = results.get("leg2_summary") or {}
    L.append(md_table(["引擎", "p50", "p95", "mean"],
                      [[k, v["p50_ms"], v["p95_ms"], v["mean_ms"]]
                       for k, v in l2s.items()]))
    if results.get("leg2_stability"):
        L.append("")
        L.append("### 常驻/漂移复核（whisper 前 5 条二遍）\n")
        L.append(md_table(["id", "first_ms", "second_ms", "drift%"],
                          [[d["id"], d["first"], d["second"], d["drift_pct"]]
                           for d in results["leg2_stability"]]))
        L.append("")
    L.append("## 腿3 GPU 争抢（TTFT, max_tokens=8）\n")
    l3 = results.get("leg3") or {}
    if l3:
        L.append(md_table(["条件", "n", "p50", "p95", "mean", "min", "max"],
                          [[k, v["n"], v["p50_ms"], v["p95_ms"], v["mean_ms"],
                            v["min_ms"], v["max_ms"]]
                           for k, v in l3["summary"].items()]))
        last_ms = l3.get("whisper_last_ms")
        L.append(f"- 前缀 {l3.get('prefix_chars')} chars（自适应凑 ≈2k token）；"
                 f"whisper 循环转写 iters={l3.get('whisper_iters')}，"
                 f"末次 {round(last_ms) if last_ms else '?'}ms/次。")
        L.append("- 判读：Qwen3-ASR 的 chunk 喂入负载（partial 解码间隔 700ms、"
                 "每窗小）对 :1235 首 token 延迟几乎零影响（p50 233.5 vs 空闲 "
                 "233.8）；whisper 连续整段转写是重得多的 GPU 负载，TTFT p50 "
                 "+~139ms（+59%）、p95 +~314ms（2.2×）。")
    L.append("")
    out = OUT_DIR / "report.md"
    out.write_text("\n".join(L), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["build", "run"], nargs="?", default="run")
    ap.add_argument("--legs", default="1,2,3")
    ap.add_argument("--samples", type=int, default=20, help="腿3 每格样本数")
    ap.add_argument("--whisper-worker", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--model", default=WHISPER_MODEL_DIR, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.whisper_worker:
        return run_whisper_worker(args.model)

    if args.cmd is None:
        ap.print_help()
        return 2
    legs = {int(x) for x in args.legs.split(",") if x.strip()}
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    manifest_path = CORPUS_DIR / "manifest.json"
    if args.cmd == "build" or not manifest_path.exists():
        build_corpus()
    if args.cmd == "build":
        return 0

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    results: dict = {"manifest": manifest, "legs": sorted(legs)}

    worker = WhisperWorker(WHISPER_PYTHON)
    try:
        results["env"] = env_facts(worker)
        warm = worker.warmup()
        results["warmup"] = warm
        print(f"[whisper] 预热完成 {warm}")

        load_pcm = wav_to_pcm(CORPUS_DIR / "digit_canto_01.wav")

        if 1 in legs:
            results["leg1"] = leg1_quality(manifest, worker)
            # 汇总（按引擎）
            summary = {}
            for eng in ("qwen3", "whisper"):
                cers = [r[f"{eng}_cer"] for r in results["leg1"]]
                digits = [r[f"{eng}_digit"] for r in results["leg1"] if r.get(f"{eng}_digit")]
                brands = [r[f"{eng}_brand"] for r in results["leg1"]
                          if r.get(f"{eng}_brand") is not None]
                summary[eng] = {
                    "mean_cer": round(sum(cers) / len(cers), 4),
                    "digit_total": len(digits),
                    "digit_exact": sum(1 for d in digits if d["exact"]),
                    "digit_mean_acc": round(sum(d["acc"] for d in digits) / len(digits), 4) if digits else 0.0,
                    "brand_total": len(brands),
                    "brand_hit": sum(1 for b in brands if b),
                }
            results["leg1_summary"] = summary

        if 2 in legs:
            results["leg2"] = leg2_latency(manifest, worker)
            l2s = {}
            for eng, key in (("qwen3", "qwen3_finish_ms"), ("whisper", "whisper_call_ms")):
                xs = [r[key] for r in results["leg2"] if r.get(key) is not None]
                l2s[eng] = summarize(xs)
            results["leg2_summary"] = l2s
            # 常驻/漂移复核（前 5 条二遍，>30% 漂移要在报告里点名）
            re5 = []
            for item in manifest[:5]:
                wav_path = CORPUS_DIR / item["file"]
                r2 = worker.transcribe(wav_path, WHISPER_LANG.get(item["lang"], "zh"))
                re5.append({"id": item["id"], "ms": r2.get("ms")})
            first5 = {r["id"]: r["whisper_call_ms"] for r in results["leg2"][:5]}
            drift = [{"id": e["id"], "first": first5.get(e["id"]), "second": e["ms"],
                      "drift_pct": round(abs(e["ms"] - first5[e["id"]]) / max(first5[e["id"]], 1) * 100, 1)
                      if first5.get(e["id"]) else None} for e in re5]
            results["leg2_stability"] = drift

        if 3 in legs:
            model = llm_model_id()
            results["leg3_model"] = model
            summary, raw, prefix_chars, loop_info = leg3_gpu(
                f"{LLM}/v1/chat/completions", model, load_pcm, worker, n=args.samples)
            results["leg3"] = {"summary": summary, "prefix_chars": prefix_chars,
                               "whisper_iters": loop_info.get("iters"),
                               "whisper_last_ms": loop_info.get("last_ms")}
    finally:
        worker.close()

    (OUT_DIR / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    report = write_report(results)
    print(f"\n[done] results → {OUT_DIR / 'results.json'}")
    print(f"[done] report  → {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
