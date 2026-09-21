#!/usr/bin/env python3
"""B 线术语表 A/B/C 三臂实证探针(E5 定案用)。

问题(plan doc E5):B 线会话级术语表当前以 **MT prompt 术语槽**注入
(`_mt_prompt` 前置「术语表（保持一致）：…」行)。它到底有没有效?还是
**确定性 pre-MT 归一**(源句里已知源形→已知目标形替换后再送 MT)更可靠?

三臂(同一固定句集、同一 :1236 Hy-MT2):
  A  no-glossary      : prompt 无术语槽(基线);
  B  glossary-in-prompt: 术语槽填满(prompt 形态 = 当前生产行为);
  C  pre-MT normalize : prompt 无术语槽,但源句里该句术语的**源形字面**先
                        替换成**目标形**再送 MT。

指标:
  (i)  term fidelity —— 期望目标形是否出现在输出(确定性字符串包含,主判据;
       附 loose 变体=多词目标形的全部词各自出现);
  (ii) control cleanliness —— 无术语对照句是否被「硬塞术语」(检查有没有
       任何术语目标形泄漏进对照句输出);
  (iii) latency per call。

局限(探针自带 1 条 alias 句/direction 显式演示):**臂 C 只能对「源形字面
出现在源句里」的术语生效**——被替换的是字面目击者;同义词/改写(如术语表
写「快递」而句子说「包裹」)臂 C 结构性地无能为力,这正是它相对臂 B 的代价。

网络硬边界:所有请求过 `guard_url`,只放行 http://127.0.0.1:{1235|1236|1237}。

用法:
  python scripts/probe_mt_glossary_ab.py --dry-run          # 只打印句集+精确 prompt
  python scripts/probe_mt_glossary_ab.py                    # 真跑(:1236, temp=0)
  python scripts/probe_mt_glossary_ab.py --temp 0.7         # 生产采样档复跑
  python scripts/probe_mt_glossary_ab.py --check-parity     # 只校验 prompt 字面与源码一致
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

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
# prompt 复刻(与 apps/agent/agent_runtime/providers/livekit_plugins.py:833
#   `_mt_prompt` + interpret.py:166 `glossary_block` 逐字节同款)
# ---------------------------------------------------------------------------
# 机器上无 livekit 依赖(无法 import 真模块),故本地复刻 + --check-parity
# 直接读源码文件断言字面量仍在(防两处漂移)。

# 注意:MT 快路(_mt_prompt)用的是 livekit_plugins.py:808 的**短名**字典
# {"zh":"中文","cantonese":"粤语","en":"英语"}——不是 interpret.py:52 的
# `_translation_instructions` 长名(那只是回退通用 LLM 路径)。评审曾踩此坑。
_MT_PROMPT_NAMES = {"zh": "中文", "cantonese": "粤语", "en": "英语"}

_MT_GLOSS_PREFIX_TMPL = "术语表（保持一致）：{glossary}\n\n"
_MT_BODY_TMPL = "将以下文本翻译为 `{name}`，注意只需要输出翻译后的结果，不要额外解释：\n\n`{text}`"

_GLOSSARY_SPLIT_RE = re.compile(r"[,，、;；\n]+")
_GLOSSARY_MAX_CHARS = 400


def mt_prompt(text: str, target_lang: str, glossary: str = "") -> str:
    """复刻 livekit_plugins._mt_prompt:833。"""
    name = _MT_PROMPT_NAMES.get(target_lang, target_lang)
    prefix = _MT_GLOSS_PREFIX_TMPL.format(glossary=glossary) if glossary else ""
    return prefix + _MT_BODY_TMPL.format(name=name, text=text)


def parse_glossary(raw: str) -> tuple[tuple[str, str], ...]:
    """复刻 interpret.parse_glossary:145。"""
    pairs: list[tuple[str, str]] = []
    for part in _GLOSSARY_SPLIT_RE.split(str(raw or "")):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            src, _, tgt = part.partition("=")
            src, tgt = src.strip(), tgt.strip()
            if src:
                pairs.append((src, tgt))
        else:
            pairs.append((part, ""))
    return tuple(pairs)


def glossary_block(pairs) -> str:
    """复刻 interpret.glossary_block:166。"""
    if not pairs:
        return ""
    parts: list[str] = []
    total = 0
    for src, tgt in pairs:
        piece = f"{src}={tgt}" if tgt else src
        if total + len(piece) + 1 > _GLOSSARY_MAX_CHARS:
            break
        parts.append(piece)
        total += len(piece) + 1
    return "；".join(parts)


def check_parity() -> list[str]:
    """读源码断言 prompt 字面量与本地复刻一致(防两处漂移)。返回问题列表。"""
    problems: list[str] = []
    plugins = REPO / "apps/agent/agent_runtime/providers/livekit_plugins.py"
    interp = REPO / "apps/agent/agent_runtime/interpret.py"
    pl_src = plugins.read_text(encoding="utf-8") if plugins.exists() else ""
    ip_src = interp.read_text(encoding="utf-8") if interp.exists() else ""
    # 源码里 \n 是转义序列,比对时须换成文件里的字面反斜杠-n
    body_escaped = _MT_BODY_TMPL.replace("\n", "\\n")
    if body_escaped not in pl_src:
        problems.append("MT body template literal not found in livekit_plugins.py")
    if '_MT_PROMPT_NAMES = {"zh": "中文", "cantonese": "粤语", "en": "英语"}' not in pl_src:
        problems.append("MT target-name dict literal not found in livekit_plugins.py")
    if 'prefix = f"术语表（保持一致）：{glossary}\\n\\n" if glossary else ""' not in pl_src:
        problems.append("glossary prefix literal not found in livekit_plugins.py")
    if '"；".join(parts)' not in ip_src:
        problems.append("glossary_block join literal not found in interpret.py")
    if "_GLOSSARY_MAX_CHARS = 400" not in ip_src:
        problems.append("_GLOSSARY_MAX_CHARS = 400 not found in interpret.py")
    return problems


# ---------------------------------------------------------------------------
# 句集(zh→en / zh→cantonese)。每句 1 个术语,期望目标形写死供确定性判定。
# ---------------------------------------------------------------------------

# (源形, 目标形, 源句) —— 源形**字面出现**在源句里(臂 C 可替换)
EN_TERM_SENTENCES: list[tuple[str, str, str]] = [
    ("快递", "parcel", "我的快递今天下午能送到吗？"),
    ("单号", "waybill", "麻烦你帮我查一下这个单号。"),
    ("退款", "reimbursement", "请问退款大概几天能到账？"),
    ("运费", "postage", "这个运费是怎么算的？"),
    ("保价", "declared value", "我想给这个快递做保价。"),
    ("上门取件", "doorstep pickup", "能不能安排上门取件？"),
    ("客服", "support agent", "我要转人工客服。"),
    ("时效", "turnaround", "你们的时效一般要多久？"),
]
# 别名句:术语表写「快递」,句子说「包裹」——源形不字面出现,臂 C 结构性失效
EN_ALIAS_SENTENCE: tuple[str, str, str] = ("快递", "parcel", "这个包裹什么时候能到？")
EN_CONTROLS: list[str] = [
    "今天天气很好，我们下午三点见面吧。",
    "麻烦你稍等一下，我马上回来。",
]

CANTONESE_TERM_SENTENCES: list[tuple[str, str, str]] = [
    ("快递", "速遞", "我的快递今天下午能送到吗？"),
    ("取件", "攞件", "我明天上午过来取件可以吗？"),
    ("什么时候", "幾時", "你什么时候方便收货？"),
    ("多少", "幾多", "这个运费多少钱？"),
    ("这个", "呢個", "这个单号我记下了。"),
    ("退款", "退錢", "麻烦你尽快帮我退款。"),
    ("上门", "上門", "你们可以安排上门收件吗？"),
    ("保价", "保價", "我想给这个快递做保价。"),
]
CANTONESE_ALIAS_SENTENCE: tuple[str, str, str] = ("快递", "速遞", "我的包裹到哪了？")
CANTONESE_CONTROLS: list[str] = [
    "谢谢你，我回头再回复你。",
    "请问你现在方便说话吗？",
]


def build_glossary_text(term_sentences) -> str:
    """整场术语表文本(所有术语,会话级常量)——臂 B 用。"""
    return "，".join(f"{src}={tgt}" for src, tgt, _ in term_sentences)


# ---------------------------------------------------------------------------
# 判定纯函数
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


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


def term_hit(output: str, expected: str) -> tuple[bool, bool]:
    """返回 (strict, loose)。strict=目标形逐字子串;loose=多词目标每词都在。"""
    out = _norm(output)
    exp = _norm(expected)
    strict = bool(exp) and exp in out
    words = [w for w in exp.split(" ") if w]
    loose = bool(words) and all(w in out for w in words)
    return strict, loose


# 术语目标形的「语言正确」粗判(供对照句 clean 判定用:目标形不应无端出现)
def _glossary_targets(term_sentences) -> list[str]:
    return [tgt for _src, tgt, _ in term_sentences]


# ---------------------------------------------------------------------------
# 单次 MT 调用
# ---------------------------------------------------------------------------

def call_mt(url: str, model: str, prompt: str, port: int, temp: float, timeout: int) -> tuple[str, float]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temp,
        "max_tokens": 512,
        # 生产档(_build_llm_provider:329-332);temp=0 时 top_p/top_k 不影响贪心
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


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def build_plan(glossary_by_direction: dict[str, str]) -> list[dict]:
    """构造三臂调用计划(句 × 臂),同时可 dry-run 打印。

    臂 C 对控制句 = 臂 A 逐字节同 prompt,故复用臂 A 结果(不重复烧调用)。
    """
    plan: list[dict] = []

    def add_group(direction: str, term_sentences, alias, controls):
        for src, tgt, sent in list(term_sentences) + [alias]:
            plan.append({"direction": direction, "kind": "term", "src_term": src,
                         "expected": tgt, "sentence": sent, "arm": "A"})
            plan.append({"direction": direction, "kind": "term", "src_term": src,
                         "expected": tgt, "sentence": sent, "arm": "B"})
            plan.append({"direction": direction, "kind": "term", "src_term": src,
                         "expected": tgt, "sentence": sent, "arm": "C"})
        for sent in controls:
            plan.append({"direction": direction, "kind": "control", "src_term": "",
                         "expected": "", "sentence": sent, "arm": "A"})
            plan.append({"direction": direction, "kind": "control", "src_term": "",
                         "expected": "", "sentence": sent, "arm": "B"})
    add_group("en", EN_TERM_SENTENCES, EN_ALIAS_SENTENCE, EN_CONTROLS)
    add_group("cantonese", CANTONESE_TERM_SENTENCES, CANTONESE_ALIAS_SENTENCE, CANTONESE_CONTROLS)
    return plan


def prompt_for(arm: str, sentence: str, direction: str, glossary_by_direction: dict[str, str],
               src_term: str, expected: str) -> str:
    if arm == "B":
        return mt_prompt(sentence, direction, glossary_by_direction.get(direction, ""))
    if arm == "C":
        normalized = sentence.replace(src_term, expected) if src_term else sentence
        return mt_prompt(normalized, direction, "")
    return mt_prompt(sentence, direction, "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=1236, help="MT server port (1235/1236/1237)")
    ap.add_argument("--model", default="mlx-community/Hy-MT2-1.8B-Abliterated-8bit")
    ap.add_argument("--temp", type=float, default=0.0, help="采样温度(默认 0=确定性;生产为 0.7)")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--dry-run", action="store_true", help="只打印句集与精确 prompt,不调服务器")
    ap.add_argument("--check-parity", action="store_true", help="只校验 prompt 字面与源码一致")
    ap.add_argument("--only-direction", default="", help="只跑某方向(en/cantonese)")
    ap.add_argument("--only-arms", default="", help="只跑某些臂,逗号分隔(如 A,B)")
    ap.add_argument("--out", default="", help="产物 JSON 路径(默认 scripts/.probe_mt_glossary_ab.<date>.json)")
    args = ap.parse_args()

    problems = check_parity()
    if problems:
        print("[PARITY] MISMATCH:")
        for p in problems:
            print("  -", p)
    else:
        print("[PARITY] ok: local prompt literals match source (livekit_plugins.py:833 / interpret.py:166)")
    if args.check_parity:
        return 1 if problems else 0

    # 术语表按 direction 各一份(生产上术语表是整场一份、只对一个目标语);
    # 两方向合并会产生重复源形+冲突目标形(如 退款=reimbursement 与 退款=退錢),
    # 不是生产形态,故分开。
    glossary_by_direction = {
        "en": glossary_block(parse_glossary(build_glossary_text(EN_TERM_SENTENCES))),
        "cantonese": glossary_block(parse_glossary(build_glossary_text(CANTONESE_TERM_SENTENCES))),
    }
    plan = build_plan(glossary_by_direction)
    if args.only_direction:
        plan = [p for p in plan if p["direction"] == args.only_direction]
    if args.only_arms:
        keep = {a.strip() for a in args.only_arms.split(",") if a.strip()}
        plan = [p for p in plan if p["arm"] in keep]

    if args.dry_run:
        print(f"\n=== sentence set: {sum(1 for p in plan if p['arm'] == 'A')} unique sentences, "
              f"{len(plan)} planned calls ===")
        for d, g in glossary_by_direction.items():
            print(f"glossary[{d}] (arm B slot) = {g!r}")
        print()
        for p in plan:
            norm = p["sentence"].replace(p["src_term"], p["expected"]) if (p["arm"] == "C" and p["src_term"]) else p["sentence"]
            pr = prompt_for(p["arm"], p["sentence"], p["direction"], glossary_by_direction, p["src_term"], p["expected"])
            print(f"--- {p['direction']} arm={p['arm']} kind={p['kind']} "
                  f"src={p['src_term']!r} exp={p['expected']!r}")
            print(f"    sentence(in)= {norm!r}")
            print(f"    prompt= {pr!r}")
        return 0

    url = f"http://{LLM_HOST}:{args.port}/v1/chat/completions"
    guard_url(url, llm_allow(args.port))
    print(f"[run] {url} model={args.model} temp={args.temp}")
    for d, g in glossary_by_direction.items():
        print(f"[run] glossary slot[{d}] = {g!r}")
    print()

    results: list[dict] = []
    arm_a_control_cache: dict[tuple[str, str], dict] = {}
    n_calls = 0
    for p in plan:
        key = (p["direction"], p["sentence"])
        # 臂 C 控制句 == 臂 A 控制句(逐字节同 prompt)→ 复用,不重复烧调用
        if p["arm"] == "C" and p["kind"] == "control" and key in arm_a_control_cache:
            src = arm_a_control_cache[key]
            row = {**p, "output": src["output"], "latency_s": src["latency_s"], "reused_from": "A"}
            results.append(row)
            continue
        prompt = prompt_for(p["arm"], p["sentence"], p["direction"], glossary_by_direction,
                            p["src_term"], p["expected"])
        try:
            out, dt = call_mt(url, args.model, prompt, args.port, args.temp, args.timeout)
        except (urllib.error.URLError, UrlGuardError, KeyError, IndexError, ValueError) as exc:
            out, dt = f"<<ERROR {type(exc).__name__}: {exc}>>", float("nan")
        n_calls += 1
        row = {**p, "output": out, "latency_s": round(dt, 3), "reused_from": ""}
        results.append(row)
        if p["arm"] == "A" and p["kind"] == "control":
            arm_a_control_cache[key] = row
        tag = ""
        if p["kind"] == "term":
            strict, loose = term_hit(out, p["expected"])
            tag = f" strict={strict} loose={loose}"
        print(f"{p['direction']:9s} arm={p['arm']} {p['kind']:7s} "
              f"{p['src_term'] or '-':6s} -> {out!r} ({dt:.2f}s){tag}")

    # ---- 汇总 ----
    def arm_rows(direction: str, arm: str, kind: str) -> list[dict]:
        return [r for r in results
                if r["direction"] == direction and r["arm"] == arm and r["kind"] == kind]

    summary: dict = {"by_direction": {}}
    targets_by_direction = {
        "en": _glossary_targets(EN_TERM_SENTENCES),
        "cantonese": _glossary_targets(CANTONESE_TERM_SENTENCES),
    }
    for direction in ("en", "cantonese"):
        all_targets = targets_by_direction[direction]
        dsum: dict = {}
        for arm in ("A", "B", "C"):
            rows = arm_rows(direction, arm, "term")
            strict_hits = 0
            loose_hits = 0
            lats: list[float] = []
            for r in rows:
                s, lo = term_hit(r["output"], r["expected"])
                strict_hits += int(s)
                loose_hits += int(lo)
                if r["latency_s"] == r["latency_s"]:
                    lats.append(r["latency_s"])
            dsum[f"arm_{arm}"] = {
                "term_n": len(rows),
                "term_strict_hits": strict_hits,
                "term_loose_hits": loose_hits,
                "term_strict_rate": round(strict_hits / len(rows), 3) if rows else None,
                "latency_mean_s": round(statistics.mean(lats), 3) if lats else None,
                "latency_p50_s": round(statistics.median(lats), 3) if lats else None,
                "latency_max_s": round(max(lats), 3) if lats else None,
            }
            ctrl_rows = arm_rows(direction, arm, "control")
            leak = 0
            for r in ctrl_rows:
                out = _norm(r["output"])
                if any(_norm(t) in out for t in all_targets if t):
                    leak += 1
            dsum[f"arm_{arm}"]["control_n"] = len(ctrl_rows)
            dsum[f"arm_{arm}"]["control_term_leak"] = leak
        summary["by_direction"][direction] = dsum

    # A vs B 输出是否逐字同(术语槽是否连"改变输出"都做不到)
    identical_ab = 0
    term_rows_a = {(r["direction"], r["sentence"]): r for r in results if r["arm"] == "A" and r["kind"] == "term"}
    for r in results:
        if r["arm"] == "B" and r["kind"] == "term":
            a = term_rows_a.get((r["direction"], r["sentence"]))
            if a and _norm(a["output"]) == _norm(r["output"]):
                identical_ab += 1
    summary["arm_A_B_identical_outputs"] = identical_ab
    summary["arm_A_B_term_total"] = len(term_rows_a)

    artifact = {
        "probe": "probe_mt_glossary_ab",
        "date": date.today().isoformat(),
        "server": url,
        "model": args.model,
        "sampling": {"temperature": args.temp, "top_p": 0.6, "top_k": 20,
                     "repetition_penalty": 1.05, "max_tokens": 512},
        "production_sampling_note": "生产 _build_llm_provider 用 temperature=0.7;本探针默认 0=确定性对比",
        "glossary_slot_arm_b_by_direction": glossary_by_direction,
        "arms": {
            "A": "no glossary slot (baseline)",
            "B": "glossary slot populated (current production prompt)",
            "C": "no glossary slot; source term literal replaced by target form pre-MT",
        },
        "caveat": "arm C only applies where the source form literally appears in the sentence; "
                  "alias sentence demonstrates the structural limit",
        "n_calls": n_calls,
        "prompt_parity_problems": problems,
        "summary": summary,
        "results": results,
    }
    out_path = Path(args.out) if args.out else (REPO / "scripts" / f".probe_mt_glossary_ab.{date.today().isoformat()}.json")
    out_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[summary] {json.dumps(summary, ensure_ascii=False, indent=2)}")
    print(f"\n[written] {out_path}  (calls={n_calls})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
