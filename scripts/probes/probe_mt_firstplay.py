#!/usr/bin/env python3
"""W8-A2 MT 首可播块台架（2026-10-09,LATENCY_BUDGETS §5a 的 MT 腿测量仪器）。

问题：<700ms 实时感带唯一路径=spec HIT——spec 开火后到「译文第一个可合成块」
到底多快?本探针把 MT 腿拆到「首可播分句」粒度:

  首可播块判据(镜像 B 线流式交付语义):译文累计流里第一段
  「句末标点收尾(≥2 字)」或「逗号界收尾(≥12 内容字,lite 意群档定档)」的
  前缀——墙钟=该前缀凑齐时刻(=可喂 TTS 起合成时刻,先于整句排干)。

  对照列=spec 稳定判据触发时刻(仿真):源句按确定性合成 interim 流(200ms/字
  语速、400ms interim 节拍)喂镜像 interpret._SpecMtDetector 判据的状态机
  (边界标点候选+≥6 字+跨 ≥2 次目击或 ≥500ms+数字 run 否决),得到「若走
  spec 路,MT 请求会在源语开始后第几毫秒发出」。spec 路 e2e 预估=
  spec 开火 + 首可播块(同口径 MT 腿) + TTS TTFB(§5a 表 420-870ms)。

题集/姿势:复用 probe_mt_lane_ttft.py 的 6 句×两向(cantonese/en)×两轮
(冷=前缀缓存冷/热=同题重放);取钥=设置库 model_routing_json mt 车道 →
env 兜底(同源姿势);出站唯一 chokepoint 过 https 白名单闸。

用法:
  .venv312/bin/python scripts/probes/probe_mt_firstplay.py [--rounds 2]
  （报告写 reports/w8a/mt-firstplay.md+.json;凭据绝不打印）
"""
from __future__ import annotations
# --- scripts import bootstrap (G1) ---
import sys as _sys, pathlib as _pathlib
_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))

import argparse
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DB = Path.home() / "Library/Application Support/BokVoice/bok_voice.db"
REPORT_DIR = _S.parent / "reports" / "w8a"
# 精确主机白名单(单域,无通配/后缀):本探针只打现役 DeepSeek 官方 mt 车道。
_ALLOWED_HOSTS = frozenset({"api.deepseek.com"})
_UA = "OpenAI/Python 1.99.1 bok-probe/1.0"

# 同声传译员契约(两向同判;镜像 probe_mt_lane_ttft,共享指引=标准书面中文)。
SYSTEMS = {
    "cantonese": (
        "你是粤语同声传译译员。把用户消息即时翻译成地道粤语口语。"
        "只输出译文,不解释不回答;保留原文标点;"
        "专有名词按术语表翻译:顺丰=SF Express;绝不虚构内容。"
    ),
    "en": (
        "You are an English simultaneous interpreter. Translate the user message "
        "into natural spoken English. Output only the translation, keep original "
        "punctuation, never explain; proper nouns follow the glossary: 顺丰=SF Express."
    ),
}
SENTS = [
    ("业务18字", "我们这批货的验货报告今天下午会发给您。"),
    ("业务22字", "麻烦您今天下班前把出货明细表发到我们这边核对。"),
    ("业务22字b", "这批订单的尾款需要在发货之前结清，请您安排处理。"),
    ("术语顺丰", "顺丰的单号麻烦发我一下。"),
    ("长句36字", "如果这批货物在运输途中出现破损或短缺，请第一时间联系我们的售后同事处理登记。"),
    ("短句8字", "今天下午能到货吗？"),
]
WARMUP_SENT = "好的，明白了。"
_CANTO_MARKS = "唔嘅哋咗喺冇啲嚟俾咁嗰佢咩嘢啱攞瞓"

# ---- 首可播块判据（句末标点 或 ≥12 字逗号界；纯函数） ----
# 全角+半角都收（en 译文的句末是半角句号；镜像 interpret._SPEC_BOUNDARY 清单）。
_STRONG_PUNCT = "。！？!?."
_WEAK_PUNCT = "，、；,;,"
_MIN_STRONG_CHARS = 2   # 句末标点界最小段长（防孤标点/语气残渣）
_MIN_WEAK_CHARS = 12    # 逗号界意群档（lite QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS 定档）


def first_playable_prefix(text: str) -> str | None:
    """译文累计流中第一个可合成分句前缀（纯函数；None=尚不可播）。"""
    i = 0
    while i < len(text):
        strong = text[i] in _STRONG_PUNCT
        weak = text[i] in _WEAK_PUNCT
        if strong or weak:
            j = i + 1
            while j < len(text) and text[j] in (_STRONG_PUNCT if strong else _WEAK_PUNCT):
                j += 1
            seg = text[:j]
            if len(seg) >= (_MIN_STRONG_CHARS if strong else _MIN_WEAK_CHARS):
                return seg
            i = j
            continue
        i += 1
    return None


# ---- spec 稳定判据仿真（镜像 interpret._SpecMtDetector 判据；确定性合成流） ----
_SPEC_BOUNDARY = "，、；,;。？！.!?"
_SPEC_DIGIT_RUN_RE = re.compile(r"[0-9一二三四五六七八九零]{4,}")
_SPEC_MIN_CLAUSE_CHARS = 6      # len(cand) 含边界标点（interpret 同口径）
_SPEC_STABLE_SIGHTINGS = 2
_SPEC_STABLE_S = 0.5
# 合成 interim 流参数：中文语速 ~5 字/s（200ms/字）、豆包 interim 节拍 400ms。
_SPEC_MS_PER_CHAR = 200
_SPEC_INTERIM_MS = 400


class _SpecSim:
    """spec 检测器判据镜像（纯同步状态机；判据常量单点对齐 interpret）。"""

    def __init__(self) -> None:
        self._cand = ""
        self._seen = 0
        self._since = 0.0
        self._last_spec = ""
        self._fires = 0

    def feed(self, text: str, t: float) -> str | None:
        cand = _spec_clause_prefix(text)
        if cand is None or len(cand) < _SPEC_MIN_CLAUSE_CHARS:
            return None
        if _SPEC_DIGIT_RUN_RE.search(cand):
            return None
        if cand != self._cand:
            self._cand, self._seen, self._since = cand, 1, t
        else:
            self._seen += 1
        if self._seen < _SPEC_STABLE_SIGHTINGS and (t - self._since) < _SPEC_STABLE_S:
            return None
        if self._fires >= 2:
            return None
        if len(cand) - len(self._last_spec) < 6:
            return None
        self._last_spec = cand
        self._fires += 1
        return cand


def _spec_clause_prefix(text: str) -> str | None:
    cut = max((text.rfind(ch) for ch in _SPEC_BOUNDARY), default=-1)
    return None if cut < 0 else text[: cut + 1]


def spec_fire_ms(sentence: str) -> int | None:
    """确定性合成 interim 流下的 spec 开火时刻（ms，自源语开始；None=整段零开火）。

    节拍确定性（无弱随机）：每 400ms 一条累计前缀，前缀字数按 200ms/字线性增长；
    末条=整句（含句末标点）。"""
    n = len(sentence)
    duration = n * _SPEC_MS_PER_CHAR / 1000
    sim = _SpecSim()
    t = _SPEC_INTERIM_MS / 1000
    while t < duration + 1e-9:
        chars = min(n, max(1, int(t * 1000 / _SPEC_MS_PER_CHAR)))
        if sim.feed(sentence[:chars], t) is not None:
            return int(t * 1000)
        t += _SPEC_INTERIM_MS / 1000
    if sim.feed(sentence, duration) is not None:
        return int(duration * 1000)
    return None


# ---- DeepSeek 车道载入（设置库 → env 兜底；镜像 probe_mt_lane_ttft） ----
def _load_deepseek_lane() -> tuple[str, str, str]:
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").strip().rstrip("/")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        row = conn.execute("SELECT model_routing_json FROM global_settings LIMIT 1").fetchone()
        conn.close()
        entry = ((json.loads(row[0]) if row else {}).get("lanes") or {}).get("mt") or {}
        if str(entry.get("provider") or "") == "openai":
            b = str(entry.get("base_url") or "").strip().rstrip("/")
            m = str(entry.get("model") or "").strip()
            k = str(entry.get("api_key") or "").strip()
            if b and m and k:
                return b, m, k
    except Exception:  # noqa: BLE001 - 库缺失/损坏回 env 缺省
        pass
    return base, model, key


# ---- 出站 chokepoint（唯一 sink；URL 先过白名单闸再 POST） ----
def _url_ok(url: str) -> bool:
    parts = urllib.parse.urlsplit(str(url or ""))
    return (
        parts.scheme == "https"
        and (parts.hostname or "").lower() in _ALLOWED_HOSTS
        and parts.port in (None, 443)
        and not parts.username
        and not parts.password
    )


def _post_raw(url: str, key: str, body: dict):
    if not _url_ok(url):
        raise SystemExit(f"[probe] endpoint failed SSRF allowlist: {url}")
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": _UA,
        },
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=60)


def request_once(base: str, model: str, key: str, system: str, sent: str) -> dict:
    """单请求（chat 流式；thinking 关闭档先行，拒认降裸请求并锁存形状）。"""
    bodies = [
        ("full", {"thinking": {"type": "disabled"}, "stream_options": {"include_usage": True}}),
        ("bare", {}),
    ]
    out: dict = {"ttft": None, "first_play_ms": None, "first_play_text": "",
                 "span": None, "text": "", "err": "", "shape": "chat"}
    last_err = ""
    for tier, extras in bodies:
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": sent}],
            "stream": True, "max_tokens": 256, "temperature": 0.3, **extras,
        }
        r = _stream_chat(base + "/chat/completions", key, body)
        if not r["err"]:
            r["shape"] = tier
            return r
        last_err = r["err"]
        if "HTTP401" in last_err or "HTTP403" in last_err:
            break
    out["err"] = last_err
    return out


def _stream_chat(url: str, key: str, body: dict) -> dict:
    """chat/completions 流式：TTFT=首 delta、first_play_ms=首可播块凑齐时刻。"""
    out: dict = {"ttft": None, "first_play_ms": None, "first_play_text": "",
                 "span": None, "text": "", "err": ""}
    t0 = time.perf_counter()
    times: list[float] = []
    try:
        resp = _post_raw(url, key, body)
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                j = json.loads(line[6:])
            except Exception:  # noqa: BLE001 - 非 JSON 心跳行跳过
                continue
            for ch in j.get("choices") or []:
                piece = ((ch.get("delta") or {}).get("content")) or ""
                if not piece:
                    continue
                now = time.perf_counter() - t0
                times.append(now)
                out["text"] += piece
                if out["ttft"] is None:
                    out["ttft"] = now
                if out["first_play_ms"] is None:
                    head = first_playable_prefix(out["text"])
                    if head is not None:
                        out["first_play_ms"] = now * 1000
                        out["first_play_text"] = head
    except urllib.error.HTTPError as e:
        out["err"] = f"HTTP{e.code}:{e.read()[:160]!r}"
        return out
    except Exception as exc:  # noqa: BLE001 - 传输层异常记账不抛
        out["err"] = repr(exc)[:160]
        return out
    if times:
        out["span"] = times[-1] - times[0]
    return out


# ---- 本地表格渲染（tabulate 非项目依赖；本地极简对齐实现） ----
def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def _fmt(cells: list[str]) -> str:
        return "|" + "|".join(f" {c!s:<{widths[i]}} " for i, c in enumerate(cells)) + "|"

    lines = [sep, _fmt(headers), sep]
    lines += [_fmt([str(c) for c in row]) for row in rows]
    lines.append(sep)
    return "\n".join(lines)


def _pct(vals: list[float], q: float) -> float:
    if not vals:
        return float("nan")
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, round(q / 100 * (len(s) - 1))))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--only-target", default="", choices=["", "cantonese", "en"])
    args = ap.parse_args()
    base, model, key = _load_deepseek_lane()
    if not key:
        print("[probe] no mt lane key（设置库 mt 车道与 DEEPSEEK_API_KEY 均空）——不跑", flush=True)
        return 1
    host = urllib.parse.urlsplit(base).hostname or ""
    print(f"arm: {model}@{host} key=✓（值不打印）", flush=True)

    targets = [t for t in ("cantonese", "en") if not args.only_target or t == args.only_target]
    # 暖机一发（吸收 TCP/TLS 冷启动，数字不进汇总）。
    warm = request_once(base, model, key, SYSTEMS["cantonese"], WARMUP_SENT)
    wt = f"{warm['ttft'] * 1000:.0f}ms" if warm["ttft"] is not None else "-"
    print(f"warmup ttft={wt} err={warm['err'][:80] or '-'}", flush=True)
    if warm["err"] and ("HTTP401" in warm["err"] or "HTTP403" in warm["err"]):
        print("[probe] 鉴权失败出局", flush=True)
        return 1

    spec_ms = {tag: spec_fire_ms(sent) for tag, sent in SENTS}
    records: list[dict] = []
    for target in targets:
        for rnd in range(1, args.rounds + 1):
            print(f"\n== target={target} round={rnd} @ {time.strftime('%H:%M:%S')} ==", flush=True)
            for tag, sent in SENTS:
                r = request_once(base, model, key, SYSTEMS[target], sent)
                r.update(target=target, rnd=rnd, tag=tag, src_chars=len(sent),
                         spec_fire_ms=spec_ms[tag])
                records.append(r)
                fp = f"{r['first_play_ms']:.0f}" if r["first_play_ms"] is not None else "-"
                tt = f"{r['ttft'] * 1000:.0f}" if r["ttft"] is not None else "-"
                sp = spec_ms[tag]
                marks = sum(1 for c in r["text"] if c in _CANTO_MARKS) if target == "cantonese" else 0
                print(
                    f"  {tag:8s} src={len(sent):2d}字 spec={sp if sp is not None else '-':>5} "
                    f"first_play={fp:>6}ms ttft={tt:>6}ms canto标记={marks} "
                    f"{r['first_play_text'][:24] or r['text'][:24]}"
                    f"{' ERR=' + r['err'][:70] if r['err'] else ''}",
                    flush=True,
                )

    # 汇总表（暖机除外）：首可播块墙钟分布 × spec 仿真对照。
    ok = [r for r in records if not r["err"] and r["first_play_ms"] is not None]
    rows: list[list[str]] = []
    for target in targets:
        for tag, sent in SENTS:
            rs = [r for r in ok if r["target"] == target and r["tag"] == tag]
            if not rs:
                continue
            fps = [r["first_play_ms"] for r in rs]
            tts = [r["ttft"] * 1000 for r in rs if r["ttft"] is not None]
            sp = spec_ms[tag]
            spec_est = (sp + _pct(fps, 50)) if sp is not None else float("nan")
            rows.append([
                target, tag, str(len(sent)),
                f"{sp}" if sp is not None else "未开火",
                f"{_pct(fps, 50):.0f} / {_pct(fps, 90):.0f}",
                f"{_pct(tts, 50):.0f}" if tts else "-",
                f"{spec_est:.0f}" if spec_est == spec_est else "-",  # NaN 哨兵
                rs[0]["first_play_text"][:18],
            ])
    all_fps = [r["first_play_ms"] for r in ok]
    print("\n== 首可播块墙钟分布（p50/p90 ms；对照列=spec 稳定判据仿真触发时刻）==", flush=True)
    table = _table(
        ["向", "句", "源字", "spec开火ms", "首可播 p50/p90", "TTFT p50", "spec路出声ms", "首块"],
        rows,
    )
    print(table, flush=True)
    if all_fps:
        print(
            f"\n整体 first_play p50={_pct(all_fps, 50):.0f}ms p90={_pct(all_fps, 90):.0f}ms "
            f"n={len(all_fps)}（SLO 对照：MT TTFT<250 / 首可播与 TTS TTFB<300 同权重；"
            f"spec路出声=spec开火+首可播 p50（自源语起算的绝对时刻），"
            f"e2e(说完→出声)≈max(0, 该时刻−源语时长[源字×200ms])+TTS TTFB 420-870ms——"
            f"出声早于源语结束=spec HIT 可「final 即声」）",
            flush=True,
        )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    md = [
        f"# MT 首可播块台架（{time.strftime('%Y-%m-%d %H:%M')}，{model}@{host}）\n",
        "- 判据：首可播=句末标点(≥2字)或逗号界(≥12字)前缀凑齐时刻；spec开火=确定性合成 "
        "interim 流（200ms/字、400ms 节拍）喂 interpret._SpecMtDetector 判据镜像。",
        "- spec路出声=spec开火+首可播 p50（自源语起算）；e2e(说完→出声)≈max(0, 该时刻−源语时长)+TTS TTFB(420-870ms)；"
        "业界 SLO 对照见 docs/LATENCY_BUDGETS.md §5a。\n",
        table,
        "",
        "## 逐请求明细",
    ]
    md += [
        f"- [{r['target']} r{r['rnd']}] {r['tag']}: src={r['src_chars']}字 "
        f"spec={r['spec_fire_ms']} first_play={r['first_play_ms']}ms "
        f"ttft={None if r['ttft'] is None else int(r['ttft'] * 1000)}ms :: {r['text']}"
        + (f" :: ERR {r['err']}" if r["err"] else "")
        for r in records
    ]
    (REPORT_DIR / "mt-firstplay.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (REPORT_DIR / "mt-firstplay.json").write_text(
        json.dumps({"model": model, "host": host, "records": records}, ensure_ascii=False,
                   indent=1, default=float),
        encoding="utf-8",
    )
    print(f"\nreport -> {REPORT_DIR / 'mt-firstplay.md'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
