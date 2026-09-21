#!/usr/bin/env python3
"""B 线 MT 语言校验器**实效探针**(E5 增补验收:量化「它到底多久才响一次」)。

问题:B 线 MT(Hy-MT2 :1236)出口的确定性语言校验器
(``bok_voice_core.mt_lang_check.looks_like_language``)是**安全网**还是真在治
一个现实问题?本探针拿一批真实形态的源句、走真 :1236、跑真判据,量:

  - ``fire_rate``      —— 真 MT 输出被判「不像目标语言」的比例(主读数);
  - ``retry_recovered``—— 响了的那些,强化 prompt 重试后是否回到目标语言;
  - ``hard_cases``     —— 故意挑的难例(源语=目标语恒等句/纯数字/中英夹杂/
                          普粤同形),量假阳(本来没问题却响);
  - ``synthetic``      —— **离线**对一批人工标注的错语言/正确语言串跑判据,
                          证明判据本身有牙(不是恒 True 的空网)——真 MT 上不响
                          不等于判据没用,只说明干净输入上没东西可逮。

**期望结论预告**:干净源句走真 MT,fire_rate 应当≈0。若如此,结论就是
「校验器是安全网,不是对已观测缺陷的修复」——探针会如实报告,不粉饰。

覆盖两方向语言对(zh→en / zh→cantonese / en→zh / en→cantonese / cantonese→zh /
cantonese→en)+ 6 条难例;默认生产采样(temp 0.7),temperature 可调。总调用数
≲30 次基线(+仅熄灭时的重试),远低于 :1236 的用量预算。

网络硬边界:所有请求过 ``guard_url``,只放行 http://127.0.0.1:{1235|1236|1237}。

用法:
  python scripts/probe_mt_lang_validator.py --dry-run     # 打印句集/精确 prompt/离线合成自检
  python scripts/probe_mt_lang_validator.py               # 真跑(:1236,temp 0.7)
  python scripts/probe_mt_lang_validator.py --temp 0      # 贪心确定性复跑
  python scripts/probe_mt_lang_validator.py --check-parity # 只校验 prompt 字面与源码一致
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "core"))

from bok_voice_core.mt_lang_check import (  # noqa: E402
    language_match_score,
    looks_like_language,
)


# ---------------------------------------------------------------------------
# 网络白名单守卫(逐字照 scripts/snippet_seed_mining.py 的 guard_url 模式)
# ---------------------------------------------------------------------------

class UrlGuardError(RuntimeError):
    """URL 未通过白名单守卫。"""


def guard_url(url: str, allowed: tuple[tuple[str, int], ...]) -> str:
    """校验 URL:只允许 http、host/port 命中白名单。返回原 URL,否则 raise。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http":
        raise UrlGuardError(f"scheme not allowed: {parsed.scheme!r}")
    host = parsed.hostname or ""
    port = parsed.port
    if (host, port) not in allowed:
        raise UrlGuardError(f"host/port not allowed: {host}:{port}")
    return url


LLM_HOST = "127.0.0.1"
ALLOWED_PORTS = (1235, 1236, 1237)


def llm_allow(port: int) -> tuple[tuple[str, int], ...]:
    if port not in ALLOWED_PORTS:
        raise UrlGuardError(f"LLM port not allowed: {port}")
    return ((LLM_HOST, port),)


def _post_json(url: str, payload: dict, timeout: int, allowed: tuple) -> dict:
    guard_url(url, allowed)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (guarded)
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# prompt 复刻(与 apps/agent/agent_runtime/providers/livekit_plugins.py `_mt_prompt`
#   逐字节同款)+ --check-parity 防两处漂移。
# ---------------------------------------------------------------------------

_MT_PROMPT_NAMES = {"zh": "中文", "cantonese": "粤语", "en": "英语"}


def mt_prompt(text: str, target_lang: str, glossary: str = "", retry: bool = False) -> str:
    """复刻 livekit_plugins._mt_prompt。"""
    name = _MT_PROMPT_NAMES.get(target_lang, target_lang)
    prefix = f"术语表（保持一致）：{glossary}\n\n" if glossary else ""
    if retry:
        body = (
            f"将以下文本翻译为 `{name}`（必须整句只用{name}输出，"
            f"不得保留原文），注意只需要输出翻译后的结果，不要额外解释"
        )
        return f"IMPORTANT RETRY: {prefix}{body}：\n\n`{text}`"
    return f"{prefix}将以下文本翻译为 `{name}`，注意只需要输出翻译后的结果，不要额外解释：\n\n`{text}`"


def check_parity() -> list[str]:
    """读源码断言 prompt 字面量与本地复刻一致(防两处漂移)。返回问题列表。"""
    problems: list[str] = []
    plugins = REPO / "apps/agent/agent_runtime/providers/livekit_plugins.py"
    pl_src = plugins.read_text(encoding="utf-8") if plugins.exists() else ""
    normal_escaped = (
        "return f\"{prefix}将以下文本翻译为 `{name}`，注意只需要输出翻译后的结果，不要额外解释：\\n\\n`{text}`\""
    )
    if normal_escaped not in pl_src:
        problems.append("normal MT prompt literal not found in livekit_plugins.py")
    if "_MT_PROMPT_NAMES = {\"zh\": \"中文\", \"cantonese\": \"粤语\", \"en\": \"英语\"}" not in pl_src:
        problems.append("MT target-name dict literal not found in livekit_plugins.py")
    if "IMPORTANT RETRY:" not in pl_src:
        problems.append("retry marker 'IMPORTANT RETRY:' not found in livekit_plugins.py")
    if "不得保留原文" not in pl_src:
        problems.append("retry in-template constraint '不得保留原文' not found in livekit_plugins.py")
    return problems


# ---------------------------------------------------------------------------
# 句集(按源语言;真实同传形态)+ 难例 + 离线合成自检
# ---------------------------------------------------------------------------

ZH_SOURCE = [
    "你好，请问是张先生吗？",
    "我的快递今天下午能送到吗？",
    "麻烦你帮我查一下这个单号。",
    "好的，谢谢，我明白了。",
]
EN_SOURCE = [
    "Hello, is this Mr. Zhang?",
    "Can you check the tracking number for me?",
    "Yes, that is correct, thank you.",
]
CANTONESE_SOURCE = [
    "你好，請問係張先生嗎？",
    "我個件今日下晝可唔可以送到？",
    "可唔可以幫我查下個單號？",
]

# (源语言, 目标语言, 源句集)
PAIRS: list[tuple[str, str, list[str]]] = [
    ("zh", "en", ZH_SOURCE),
    ("zh", "cantonese", ZH_SOURCE),
    ("en", "zh", EN_SOURCE),
    ("en", "cantonese", EN_SOURCE),
    ("cantonese", "zh", CANTONESE_SOURCE),
    ("cantonese", "en", CANTONESE_SOURCE),
]

# 难例(假阳压力面):恒等句/纯数字/中英夹杂/普粤同形短句。
# (标签, 源语言, 目标语言, 源句)
HARD_CASES: list[tuple[str, str, str, str]] = [
    ("identity-en", "en", "en", "Yes, that is correct, thank you."),
    ("identity-zh-short", "zh", "zh", "好的，谢谢。"),
    ("digits-only", "zh", "en", "12345"),
    ("code-switch-zh", "zh", "zh", "好的，OK，马上安排。"),
    ("identity-cantonese", "cantonese", "cantonese", "唔該晒，我聽日再覆你。"),
    ("en-source-to-zh", "en", "zh", "OK, I will send it to you."),
]

# 真 MT 上的「错语言模拟」牙证:prompt 用 A 语言、声明目标用 B 语言(制造「MT
# 输出了 B 目标不认的语言」),量 ①判据是否当场响 ②强化重试是否把它拉回 B 目标。
# 这是**受控模拟**(真实干净输入上不该发生),目的是证明「安全网不是空网、
# 重试口真的能恢复」——与 synthetic 的离线牙证互补(那份不动服务器)。
# (标签, 源句, prompt 语言, 声明目标语言)
SIMULATED_MISMATCH: list[tuple[str, str, str, str]] = [
    ("declared-zh-got-english", "Can you check the tracking number for me?", "en", "zh"),
    ("declared-en-got-chinese", "麻烦你帮我查一下这个单号。", "zh", "en"),
]

# 离线合成自检(不烧网络):(文本, 目标语言, 期望判据结果)
SYNTHETIC: list[tuple[str, str, bool]] = [
    ("This is a plain English sentence.", "zh", False),      # en 输出配 zh 目标 → 应响
    ("麻烦你帮我查一下这个单号。", "en", False),               # zh 输出配 en 目标 → 应响
    ("I will check it right now.", "en", True),
    ("麻烦你帮我查一下这个单号。", "zh", True),
    ("好的", "en", True),                                     # 证据不足 → 放行
    ("12345", "zh", True),                                   # 纯数字 → 放行
    ("唔該幫我查下個單號嘅物流。", "cantonese", True),
]


# ---------------------------------------------------------------------------
# 单次 MT 调用
# ---------------------------------------------------------------------------

def strip_mt_quotes(text: str) -> str:
    """剥 MT 输出包裹引号(镜像 _StripMTQuoteStream:首尾引号类字符)。"""
    s = str(text or "").strip()
    pairs = [("\"", "\""), ("“", "”"), ("「", "」"), ("『", "』"), ("'", "'"), ("`", "`")]
    changed = True
    while changed and s:
        changed = False
        for op, cl in pairs:
            if len(s) >= 2 and s[0] == op and s[-1] == cl:
                s = s[1:-1].strip()
                changed = True
    return s


def call_mt(url: str, model: str, prompt: str, port: int, temp: float, timeout: int) -> tuple[str, float]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temp,
        "max_tokens": 512,
        # 生产档(_build_llm_provider:329-332)
        "top_p": 0.6,
        "top_k": 20,
        "repetition_penalty": 1.05,
    }
    allowed = llm_allow(port)
    t0 = time.perf_counter()
    resp = _post_json(url, payload, timeout=timeout, allowed=allowed)
    dt = time.perf_counter() - t0
    content = resp["choices"][0]["message"]["content"]
    return strip_mt_quotes(content), dt


def _synthetic_report() -> dict:
    """离线跑合成集,证明判据有牙(不依赖服务器)。"""
    rows = []
    flagged = 0
    mismatches = 0
    for text, target, expected in SYNTHETIC:
        got = looks_like_language(text, target)
        rows.append({
            "text": text, "target": target, "expected": expected, "got": got,
            "score": round(language_match_score(text, target), 3),
        })
        if not got:
            flagged += 1
        if got != expected:
            mismatches += 1
    return {"checked": len(SYNTHETIC), "flagged_mismatch": flagged,
            "expectation_mismatches": mismatches, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=1236, help="MT server port (1235/1236/1237)")
    ap.add_argument("--model", default="mlx-community/Hy-MT2-1.8B-Abliterated-8bit")
    ap.add_argument("--temp", type=float, default=0.7, help="采样温度(默认 0.7=生产档;0=贪心确定性)")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--dry-run", action="store_true", help="只打印句集/精确 prompt/离线合成自检,不调服务器")
    ap.add_argument("--check-parity", action="store_true", help="只校验 prompt 字面与源码一致")
    ap.add_argument("--only-pairs", default="", help="只跑某些语言对,逗号分隔(如 zh→en,en→zh)")
    ap.add_argument("--out", default="", help="产物 JSON 路径(默认 scripts/.probe_mt_lang_validator.<date>.json)")
    args = ap.parse_args()

    problems = check_parity()
    if problems:
        print("[PARITY] MISMATCH:")
        for p in problems:
            print("  -", p)
    else:
        print("[PARITY] ok: local MT prompt literals match livekit_plugins.py `_mt_prompt`")
    if args.check_parity:
        return 1 if problems else 0

    syn = _synthetic_report()
    print(f"[synthetic] checked={syn['checked']} flagged={syn['flagged_mismatch']} "
          f"expectation_mismatches={syn['expectation_mismatches']}")
    for r in syn["rows"]:
        mark = "OK" if r["got"] == r["expected"] else "!!"
        print(f"  {mark} target={r['target']:9s} got={str(r['got']):5s} score={r['score']:.2f} {r['text']!r}")

    pairs = PAIRS
    if args.only_pairs:
        keep = {p.strip() for p in args.only_pairs.split(",") if p.strip()}
        pairs = [p for p in PAIRS if f"{p[0]}→{p[1]}" in keep]

    if args.dry_run:
        n = sum(len(s) for _s, _t, s in pairs) + len(HARD_CASES) + len(SIMULATED_MISMATCH)
        print(f"\n=== planned baseline calls: {n} (retries only on fire, at most +1 each) ===")
        for src, tgt, sents in pairs:
            for sent in sents:
                print(f"--- {src}->{tgt} sent={sent!r}")
                print(f"    normal= {mt_prompt(sent, tgt)!r}")
                print(f"    retry = {mt_prompt(sent, tgt, retry=True)!r}")
        for label, src, tgt, sent in HARD_CASES:
            print(f"--- HARD {label} {src}->{tgt} sent={sent!r}")
            print(f"    normal= {mt_prompt(sent, tgt)!r}")
        for label, sent, prompt_lang, declared_tgt in SIMULATED_MISMATCH:
            print(f"--- SIM {label}: prompt={prompt_lang!r} declared_target={declared_tgt!r} sent={sent!r}")
            print(f"    gen_prompt = {mt_prompt(sent, prompt_lang)!r}")
            print(f"    retry_prompt(declared) = {mt_prompt(sent, declared_tgt, retry=True)!r}")
        return 0

    url = f"http://{LLM_HOST}:{args.port}/v1/chat/completions"
    guard_url(url, llm_allow(args.port))
    print(f"\n[run] {url} model={args.model} temp={args.temp}")

    results: list[dict] = []
    n_calls = 0
    fires = 0
    recovered = 0

    def _run_one(direction: str, label: str, src: str, tgt: str, sent: str,
                 prompt_lang: str = "") -> None:
        """生成用 ``prompt_lang``(缺省=声明目标 ``tgt``);判据/重试一律按声明目标 ``tgt``。

        SIM 臂借 ``prompt_lang != tgt`` 制造「声明目标 ≠ MT 实际输出语言」的受控错语言。
        """
        nonlocal n_calls, fires, recovered
        gen_lang = prompt_lang or tgt
        try:
            out, dt = call_mt(url, args.model, mt_prompt(sent, gen_lang), args.port, args.temp, args.timeout)
        except (urllib.error.URLError, UrlGuardError, KeyError, IndexError, ValueError) as exc:
            out, dt = f"<<ERROR {type(exc).__name__}: {exc}>>", float("nan")
        n_calls += 1
        ok = looks_like_language(out, tgt)
        score = language_match_score(out, tgt)
        fired = not ok
        retry_out = ""
        retry_ok = None
        if fired:
            fires += 1
            try:
                retry_out, _ = call_mt(url, args.model, mt_prompt(sent, tgt, retry=True),
                                       args.port, args.temp, args.timeout)
            except (urllib.error.URLError, UrlGuardError, KeyError, IndexError, ValueError) as exc:
                retry_out = f"<<ERROR {type(exc).__name__}: {exc}>>"
            n_calls += 1
            retry_ok = looks_like_language(retry_out, tgt)
            recovered += int(bool(retry_ok))
        row = {
            "direction": direction, "kind": label, "source_lang": src, "target_lang": tgt,
            "sentence": sent, "output": out, "latency_s": round(dt, 3),
            "match": ok, "score": round(score, 3), "fired": fired,
            "retry_output": retry_out, "retry_match": retry_ok,
        }
        results.append(row)
        tag = f" score={score:.2f} FIRE retry_ok={retry_ok}" if fired else f" score={score:.2f} ok"
        print(f"{direction:18s} {label:16s} -> {out!r} ({dt:.2f}s){tag}")

    for src, tgt, sents in pairs:
        for sent in sents:
            _run_one(f"{src}->{tgt}", "pair", src, tgt, sent)
    for label, src, tgt, sent in HARD_CASES:
        _run_one(f"{src}->{tgt}", f"HARD:{label}", src, tgt, sent)
    # 真 MT 错语言模拟(受控):用 prompt 语言生成、按声明目标判据——量判据响应与重试恢复。
    sim_rows: list[dict] = []
    for label, sent, prompt_lang, declared_tgt in SIMULATED_MISMATCH:
        _run_one(f"SIM:{prompt_lang}->{declared_tgt}", f"SIM:{label}",
                 prompt_lang, declared_tgt, sent, prompt_lang=prompt_lang)
        sim_rows.append(results[-1])

    clean_calls = sum(len(s) for _s, _t, s in pairs) + len(HARD_CASES)
    clean_fires = sum(1 for r in results if not r["kind"].startswith("SIM:") and r["fired"])
    sim_fires = sum(1 for r in sim_rows if r["fired"])
    sim_recovered = sum(1 for r in sim_rows if r["retry_match"] is True)
    fire_rate = (clean_fires / clean_calls) if clean_calls else 0.0
    summary = {
        "total_mt_calls": n_calls,
        "baseline_sentences": clean_calls,
        "validator_fires": clean_fires,
        "fire_rate": round(fire_rate, 4),
        "retry_recovered": recovered,
        "retry_still_wrong": fires - recovered,
        "simulated_mismatch": {
            "cases": len(sim_rows),
            "fires": sim_fires,
            "retry_recovered": sim_recovered,
        },
        "synthetic": {k: v for k, v in syn.items() if k != "rows"},
    }
    artifact = {
        "probe": "probe_mt_lang_validator",
        "date": date.today().isoformat(),
        "server": url,
        "model": args.model,
        "sampling": {"temperature": args.temp, "top_p": 0.6, "top_k": 20,
                     "repetition_penalty": 1.05, "max_tokens": 512},
        "predicate": "bok_voice_core.mt_lang_check.looks_like_language (script-family only; "
                     "cannot separate zh vs cantonese — see module docstring)",
        "prompt_parity_problems": problems,
        "synthetic_rows": syn["rows"],
        "simulated_mismatch_rows": sim_rows,
        "summary": summary,
        "results": results,
    }
    out_path = Path(args.out) if args.out else (
        REPO / "scripts" / f".probe_mt_lang_validator.{date.today().isoformat()}.json"
    )
    out_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[summary] {json.dumps(summary, ensure_ascii=False, indent=2)}")
    print(f"\n[written] {out_path}  (calls={n_calls})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
