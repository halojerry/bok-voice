#!/usr/bin/env python3
"""W7-P5 MT 车道 TTFT 竞测探针（2026-10-09,用户指令:贴着说话出声选译道）。

同题同判 × 三云译道逐 delta 计时,回答「稳态滞后 ≤1.5s(目标 1.1-1.3s)谁当家」:
  1. deepseek 官方（现役 mt 车道,设置库 model_routing_json→env 兜底;
     生产契约 thinking{type:disabled},chat/completions 流式）
  2. 火山方舟 doubao-seed-2.0-lite（env ARK_API_KEY;chat/completions 先试,
     拒认即降 /responses——先例:doubao-seed-translation 只认 Responses,
     seed-2.0-lite 是普通 LLM 两态都试,哪个通用哪个;实测 chat 通。
     ⚠模型名勘误:Ark 只认带版本 ID,裸名 doubao-seed-2.0-lite 两态全 404
     InvalidEndpointOrModel.NotFound,缺省钉 doubao-seed-2-0-lite-260428,
     ARK_MODEL 可换 doubao-seed-2-1-lite-260915 更新一档）
  3. qwen-flash（DashScope compatible-mode;厂商 .env QWEN_API_KEY→
     ~/.bok_dev_qianwen_key 兜底;enable_thinking:false 先试,拒认即裸请求）

题集:6 条 zh 源句(3 业务 15-25 字 + 1 术语句「顺丰的单号麻烦发我一下。」
+ 1 条 36 字长句 + 1 条 8 字短句) × 目标语 cantonese/en 两向 × 两轮
(轮 1=同臂首遇=前缀缓存冷;轮 2=同题重放=各家隐式前缀缓存热——TTFT 差
即缓存真收益)。每臂先打一发暖机(吸收 TCP/TLS/路由冷启动,单独记录)。

每请求收:TTFT(首个内容 delta,=喂 TTS 解锁时刻)、token 间隔 p50/p90、
整条耗时、completion tok/s、cached_tokens(响应 usage,各家字段归一)、
译文质量抽查(术语 SF Express 落地/粤译口语标记/英译 ASCII 比;人工眼测兜底)。

凭据纪律:全部服务端/env/本机点位文件读取,绝不打印值、绝不写进代码或
报告;出站唯一 chokepoint=_post_sse:https 白名单三域精确匹配
(api.deepseek.com / ark.cn-beijing.volces.com / dashscope.aliyuncs.com,
拒端口/凭据注入),不过闸即 SystemExit。语言值只写 cantonese/zh/en。

用法:
  ARK_API_KEY=... .venv312/bin/python scripts/probes/probe_mt_lane_ttft.py
  （--only deepseek,ark,qwen 选臂复跑;结果写 reports/w7probe/p5-mt-ttft.md+.json）
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
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DB = Path.home() / "Library/Application Support/BokVoice/bok_voice.db"
VENDOR_ENV = Path("/Users/halo/Documents/m芯片/.env")
QIANWEN_KEY_FILE = Path.home() / ".bok_dev_qianwen_key"
REPORT_DIR = _S.parent / "reports" / "w7probe"
# 精确主机白名单(三域,无通配/后缀):DeepSeek 官方+方舟+DashScope 通用域。
_ALLOWED_HOSTS = frozenset(
    {
        "api.deepseek.com",
        "ark.cn-beijing.volces.com",
        "dashscope.aliyuncs.com",
    }
)
_UA = "OpenAI/Python 1.99.1 bok-probe/1.0"

# 同声传译员契约(两向同判;术语表单点)。共享指引=标准书面中文(prompt 纯度纪律)。
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


def _url_ok(url: str) -> bool:
    """SSRF 白名单(probe_mt_matrix 同款纪律):https+三域精确匹配。"""
    parts = urllib.parse.urlsplit(str(url or ""))
    return (
        parts.scheme == "https"
        and (parts.hostname or "").lower() in _ALLOWED_HOSTS
        and parts.port in (None, 443)
        and not parts.username
        and not parts.password
    )


def _load_deepseek_lane() -> tuple[str, str, str]:
    """设置库 model_routing_json mt 车道 → env 兜底(probe_deepseek_stream 同款)。"""
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


def _load_vendor_env() -> dict:
    """厂商 .env(QWEN_*,m芯片同传还原);只回字典,绝不打印值。"""
    out: dict[str, str] = {}
    try:
        for line in VENDOR_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001
        pass
    return out


def _load_qwen_key() -> str:
    """厂商 .env QWEN_API_KEY → ~/.bok_dev_qianwen_key(本机既有点位)兜底。"""
    key = _load_vendor_env().get("QWEN_API_KEY", "").strip()
    if key:
        return key
    try:
        return QIANWEN_KEY_FILE.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        return ""


def _post_raw(url: str, key: str, body: dict):
    """唯一出站 chokepoint:URL 先过白名单闸再 POST(SSE 流返回)。"""
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


def _norm_usage(u: dict) -> dict:
    """各家 usage 字段归一(prompt/completion/cached)。"""
    cached = (
        u.get("prompt_cache_hit_tokens")
        or ((u.get("prompt_tokens_details") or {}).get("cached_tokens"))
        or ((u.get("input_tokens_details") or {}).get("cached_tokens"))
        or 0
    )
    return {
        "prompt": u.get("prompt_tokens") or u.get("input_tokens") or 0,
        "completion": u.get("completion_tokens") or u.get("output_tokens") or 0,
        "cached": cached,
    }


def _stream_chat(url: str, key: str, body: dict) -> dict:
    """chat/completions 流式:逐 delta 计时。err 非空=失败(code/first160)。"""
    out: dict = {"ttft": None, "span": None, "total": None, "deltas": 0,
                 "gaps": [], "text": "", "usage": {}, "err": ""}
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
            except Exception:  # noqa: BLE001
                continue
            if isinstance(j.get("usage"), dict) and j["usage"]:
                out["usage"] = _norm_usage(j["usage"])
            for ch in j.get("choices") or []:
                piece = ((ch.get("delta") or {}).get("content")) or ""
                if piece:
                    times.append(time.perf_counter() - t0)
                    out["deltas"] += 1
                    out["text"] += piece
    except urllib.error.HTTPError as e:
        out["err"] = f"HTTP{e.code}:{e.read()[:160]!r}"
        return out
    except Exception as exc:  # noqa: BLE001
        out["err"] = repr(exc)[:160]
        return out
    out["total"] = time.perf_counter() - t0
    if times:
        out["ttft"] = times[0]
        out["span"] = times[-1] - times[0]
        out["gaps"] = [(times[i + 1] - times[i]) * 1000 for i in range(len(times) - 1)]
    return out


def _stream_responses(url: str, key: str, body: dict) -> dict:
    """/responses 流式(response.output_text.delta 事件)。"""
    out: dict = {"ttft": None, "span": None, "total": None, "deltas": 0,
                 "gaps": [], "text": "", "usage": {}, "err": ""}
    t0 = time.perf_counter()
    times: list[float] = []
    try:
        resp = _post_raw(url, key, body)
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                j = json.loads(line[6:])
            except Exception:  # noqa: BLE001
                continue
            et = str(j.get("type") or "")
            if et == "response.output_text.delta" and j.get("delta"):
                times.append(time.perf_counter() - t0)
                out["deltas"] += 1
                out["text"] += str(j["delta"])
            elif et == "response.completed":
                out["usage"] = _norm_usage((j.get("response") or {}).get("usage") or {})
            elif et in ("response.failed", "error") and not out["text"]:
                out["err"] = f"SSE:{et}:{json.dumps(j, ensure_ascii=False)[:140]}"
    except urllib.error.HTTPError as e:
        out["err"] = f"HTTP{e.code}:{e.read()[:160]!r}"
        return out
    except Exception as exc:  # noqa: BLE001
        out["err"] = repr(exc)[:160]
        return out
    out["total"] = time.perf_counter() - t0
    if times:
        out["ttft"] = times[0]
        out["span"] = times[-1] - times[0]
        out["gaps"] = [(times[i + 1] - times[i]) * 1000 for i in range(len(times) - 1)]
    return out


def _chat_bodies(arm: dict, system: str, sent: str) -> list[tuple[str, dict]]:
    """chat 请求体候选序列(extras 拒认逐级降级:思考开关+stream_options→裸);
    首键=档位名(full/mid/bare),胜出档由 request_once 锁存,后续请求不再试错。"""
    base = {
        "model": arm["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": sent}],
        "stream": True, "max_tokens": 256, "temperature": 0.3,
    }
    return [
        ("full", dict(base, **arm["chat_extras_full"])),
        ("mid", dict(base, **arm["chat_extras_mid"])),
        ("bare", base),
    ]


def _quality(target: str, text: str, term_needed: bool) -> str:
    """轻量质量抽查(自动化部分;人工眼测兜底)。"""
    marks: list[str] = []
    if term_needed:
        low = text.lower()
        if "sf express" in low:
            marks.append("term=SF✓")
        elif "顺丰" in text:
            marks.append("term=顺丰保留")
        else:
            marks.append("term=MISS")
    if target == "cantonese":
        n = sum(1 for ch in text if ch in _CANTO_MARKS)
        marks.append(f"canto标记={n}")
        if n == 0 and any(ch in text for ch in "的吗这"):
            marks.append("疑似普通话")
    elif target == "en":
        letters = sum(1 for ch in text if ch.isascii() and ch.isalpha())
        nonspace = max(1, sum(1 for ch in text if not ch.isspace()))
        marks.append(f"ascii={100 * letters // nonspace}%")
    return " ".join(marks) if marks else "-"


def _pct(vals: list[float], q: float) -> float:
    if not vals:
        return float("nan")
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, round(q / 100 * (len(s) - 1))))]


def request_once(arm: dict, system: str, sent: str) -> dict:
    """单请求(按臂锁定 shape/extras);Ark chat 拒认自动降 responses 并锁存。"""
    if arm["shape"] == "responses":
        body = {
            "model": arm["model"], "instructions": system,
            "input": [{"role": "user", "content": sent}],
            "stream": True, "max_output_tokens": 256,
        }
        r = _stream_responses(arm["base"] + "/responses", arm["key"], body)
        r["shape"] = "responses"
        return r
    tier = arm.get("locked_tier") or ""
    last: dict = {}
    for name, body in _chat_bodies(arm, system, sent):
        if tier and name != tier:
            continue  # 已锁档,只跑锁存档
        r = _stream_chat(arm["base"] + "/chat/completions", arm["key"], body)
        if not r["err"]:
            arm["locked_tier"] = name
            r["shape"] = "chat"
            return r
        last = r
        if "HTTP401" in r["err"] or "HTTP403" in r["err"]:
            break  # 鉴权死,别硬绕
    # chat 全候选拒认 → 方舟两态都试:降 /responses(锁存,后续恒走)。
    if arm.get("responses_fallback"):
        arm["shape"] = "responses"
        last["note"] = "chat拒认→降responses"
        return request_once(arm, system, sent)
    last["shape"] = "chat"
    return last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="逗号分隔臂名 deepseek,ark,qwen")
    ap.add_argument("--rounds", type=int, default=2)
    args = ap.parse_args()
    stem = os.environ.get("P5_REPORT_STEM", "p5-mt-ttft")

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    ds_base, ds_model, ds_key = _load_deepseek_lane()
    ark_key = os.environ.get("ARK_API_KEY", "").strip()
    qwen_key = _load_qwen_key()
    qwen_base = os.environ.get(
        "QWEN_FLASH_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")

    arms: dict[str, dict] = {}
    arms["deepseek"] = {
        "name": ds_model or "deepseek-flash", "base": ds_base, "model": ds_model, "key": ds_key,
        "shape": "chat", "responses_fallback": False,
        "chat_extras_full": {"thinking": {"type": "disabled"},
                             "stream_options": {"include_usage": True}},
        "chat_extras_mid": {"stream_options": {"include_usage": True}},
        "src": "设置库mt车道" if ds_key else "",
    }
    arms["ark"] = {
        "name": os.environ.get("ARK_MODEL", "doubao-seed-2-0-lite-260428"),
        "base": os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/"),
        "model": os.environ.get("ARK_MODEL", "doubao-seed-2-0-lite-260428"),
        "key": ark_key, "shape": "chat", "responses_fallback": True,
        "chat_extras_full": {"thinking": {"type": "disabled"},
                             "stream_options": {"include_usage": True}},
        "chat_extras_mid": {"stream_options": {"include_usage": True}},
        "src": "env ARK_API_KEY",
    }
    arms["qwen"] = {
        "name": os.environ.get("QWEN_FLASH_MODEL", "qwen-flash"),
        "base": qwen_base, "model": os.environ.get("QWEN_FLASH_MODEL", "qwen-flash"),
        "key": qwen_key, "shape": "chat", "responses_fallback": False,
        "chat_extras_full": {"enable_thinking": False,
                             "stream_options": {"include_usage": True}},
        "chat_extras_mid": {"stream_options": {"include_usage": True}},
        "src": "厂商.env QWEN_API_KEY" if qwen_key else "",
    }
    for name, arm in list(arms.items()):
        if not arm["key"]:
            arm["dead"] = f"no key ({arm.get('src') or '无凭据源'})"
        if only and name not in only:
            arm["dead"] = "skipped (--only)"
    active = {k: v for k, v in arms.items() if not v.get("dead")}

    print("arms: " + ", ".join(
        f"{k}({v['name']}@{urllib.parse.urlsplit(v['base']).hostname},key={'✓' if v['key'] else '✗'})"
        for k, v in arms.items()), flush=True)
    for k, v in arms.items():
        if v.get("dead"):
            print(f"  {k}: {v['dead']}", flush=True)

    records: list[dict] = []

    # 暖机:每臂一发(吸收 TCP/TLS/路由冷启动,数字单独记录,不进冷热轮汇总)。
    print(f"\n== warmup (吸收连接冷启动) @ {time.strftime('%H:%M:%S')} ==", flush=True)
    for name, arm in active.items():
        r = request_once(arm, SYSTEMS["cantonese"], WARMUP_SENT)
        r.update(arm=name, target="cantonese", rnd=0, tag=WARMUP_SENT)
        records.append(r)
        tt = f"{r['ttft']*1000:.0f}ms" if r["ttft"] is not None else "-"
        print(f"  {name}: warmup ttft={tt} err={r['err'][:80] or '-'} shape={r['shape']}", flush=True)
        if r["err"] and ("HTTP401" in r["err"] or "HTTP403" in r["err"]):
            arm["dead"] = f"鉴权失败: {r['err'][:90]}"

    for k in [k for k, v in active.items() if v.get("dead")]:
        print(f"  {k}: 暖机失败出局 — {active[k]['dead']}", flush=True)
    live = {k: v for k, v in active.items() if not v.get("dead")}

    for target in ("cantonese", "en"):
        for rnd in range(1, args.rounds + 1):
            print(f"\n== target={target} round={rnd} @ {time.strftime('%H:%M:%S')} ==", flush=True)
            for tag, sent in SENTS:
                for name, arm in live.items():
                    r = request_once(arm, SYSTEMS[target], sent)
                    r.update(arm=name, target=target, rnd=rnd, tag=tag)
                    records.append(r)
                    tt = f"{r['ttft']*1000:.0f}ms" if r["ttft"] is not None else "-"
                    sp = f"{r['span']*1000:.0f}" if r["span"] is not None else "-"
                    gp = (f"{_pct(r['gaps'], 50):.0f}/{_pct(r['gaps'], 90):.0f}"
                          if r["gaps"] else "-/-")
                    u = r["usage"]
                    term = tag == "术语顺丰"
                    q = _quality(target, r["text"], term) if not r["err"] else "-"
                    print(
                        f"  {name:9s} ttft={tt:>6s} span={sp:>5s}ms gaps={gp}ms "
                        f"cached={u.get('cached', 0)}/{u.get('prompt', 0)} {q} "
                        f"{r['text'][:30]}{'…' if len(r['text']) > 30 else ''}"
                        f"{' ERR=' + r['err'][:70] if r['err'] else ''}",
                        flush=True)

    # 汇总(暖机轮除外)。
    agg: dict[str, list[dict]] = {}
    for r in records:
        if r.get("rnd", 0) == 0 or r.get("err") or r["ttft"] is None:
            continue
        agg.setdefault(r["arm"], []).append(r)

    lines: list[str] = []
    lines.append(f"# W7-P5 MT 车道 TTFT 竞测({stem})\n")
    lines.append(f"- 时间: {time.strftime('%Y-%m-%d %H:%M')}  运行目录: work-session-20261009-091609-w7probe")
    lines.append("- 判据: 贴着说话出声稳态滞后 ≤1.5s(目标 1.1-1.3s);TTFT=首个内容 delta(=喂 TTS 解锁时刻)")
    lines.append("- 轮 1=同臂首遇(前缀缓存冷) 轮 2=同题重放(各家隐式前缀缓存热);每臂先 1 发暖机吸收连接冷启动\n")
    lines.append("## 每臂汇总(两向合并 24 请求/臂)\n")
    lines.append("| 臂 | shape | TTFT p50 | p90 | max | span p50 | tok/s p50 "
                 "| gaps p50/p90 | cached(冷/热) | 术语 | 粤标记 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")

    def _rows_for(arm_name: str, rs: list[dict]) -> str:
        arm = arms[arm_name]
        tt = [r["ttft"] for r in rs]
        spans = [r["span"] for r in rs]
        gaps = [g for r in rs for g in r["gaps"]]
        tps = [r["usage"]["completion"] / r["span"] for r in rs
               if r["span"] and r["usage"].get("completion")]
        cold = [r for r in rs if r["rnd"] == 1]
        hot = [r for r in rs if r["rnd"] == 2]
        cd = sum(r["usage"].get("cached", 0) for r in cold)
        cp = sum(r["usage"].get("prompt", 0) for r in cold)
        hd = sum(r["usage"].get("cached", 0) for r in hot)
        hp = sum(r["usage"].get("prompt", 0) for r in hot)
        term = [r for r in rs if r["tag"] == "术语顺丰"]
        term_ok = sum(1 for r in term if "term=MISS" not in _quality(r["target"], r["text"], True))
        canto = [r for r in rs if r["target"] == "cantonese"]
        canto_n = sum(1 for r in canto if sum(1 for ch in r["text"] if ch in _CANTO_MARKS) > 0)
        tps_s = f"{_pct(tps, 50):.1f}" if tps else "-"
        return (
            f"| {arm_name} ({arm['name']}) | {rs[0].get('shape', 'chat')} "
            f"| {_pct(tt, 50)*1000:.0f}ms | {_pct(tt, 90)*1000:.0f}ms | {max(tt)*1000:.0f}ms "
            f"| {_pct(spans, 50)*1000:.0f}ms "
            f"| {tps_s} "
            f"| {_pct(gaps, 50):.0f}/{_pct(gaps, 90):.0f}ms "
            f"| {cd}/{cp} vs {hd}/{hp} "
            f"| {term_ok}/{len(term)} | {canto_n}/{len(canto)} |"
        )

    for name in ("deepseek", "ark", "qwen"):
        rs = agg.get(name) or []
        if not rs:
            lines.append(f"| {name} | - | - | - | - | - | - | - | - | - | - |")
            continue
        lines.append(_rows_for(name, rs))
    lines.append("")

    lines.append("## 冷/热两轮 TTFT 对照(前缀缓存真收益)\n")
    lines.append("| 臂 | 向 | 轮1(冷) p50 | 轮2(热) p50 | Δ |")
    lines.append("|---|---|---|---|---|")
    for name in ("deepseek", "ark", "qwen"):
        for target in ("cantonese", "en"):
            rs = [r for r in (agg.get(name) or []) if r["target"] == target]
            if not rs:
                continue
            c1 = [r["ttft"] for r in rs if r["rnd"] == 1]
            c2 = [r["ttft"] for r in rs if r["rnd"] == 2]
            if c1 and c2:
                a, b = _pct(c1, 50) * 1000, _pct(c2, 50) * 1000
                lines.append(f"| {name} | {target} | {a:.0f}ms | {b:.0f}ms | {b-a:+.0f}ms |")
    lines.append("")

    lines.append("## 逐请求明细\n")
    for r in records:
        u = r["usage"]
        tt = f"{r['ttft']*1000:.0f}" if r["ttft"] is not None else "-"
        sp = f"{r['span']*1000:.0f}" if r["span"] is not None else "-"
        term = r["tag"] == "术语顺丰"
        q = _quality(r["target"], r["text"], term) if (not r["err"] and r.get("rnd", 0) != 0) else "-"
        lines.append(
            f"- [{r['target']} r{r.get('rnd', 0)}] {r['arm']:9s} {r['tag']:7s} "
            f"ttft={tt}ms span={sp}ms deltas={r['deltas']} "
            f"cached={u.get('cached', 0)}/{u.get('prompt', 0)} {q} "
            f":: {r['text']}" + (f" :: ERR {r['err']}" if r["err"] else "")
        )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    md = "\n".join(lines) + "\n"
    (REPORT_DIR / f"{stem}.md").write_text(md, encoding="utf-8")
    (REPORT_DIR / f"{stem}.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\nreport -> {REPORT_DIR / (stem + '.md')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
