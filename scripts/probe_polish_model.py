#!/usr/bin/env python3
"""E7 离线润色面「生成模型待定」A/B 探针（2026-09-21，plan §26.2-E7）。

问题：确定性润色（``bok_voice_core.polish``）已覆盖删口水词/删重复/改口取后值/口语
数字规范化。**再挂一跳 LLM 有没有增益？** 若有，4B（:1235）还是 9B（:1237）？

本探针在 R1 真实语料（``scripts/.r1_gold.20260921.json``，152 条真实客户轮）上取
一批脏转写，对每条跑三路：确定性润色基线 / 4B 润色 / 9B 润色，然后用**确定性判据**
（复用 ``output_guard`` 的硬保护 token / 语言漂移 / 硬否定判定 + 一个「新增内容字符」
启发式）打分，并把 **原文/三路输出逐条留档**，供人重判。

判据（每条样本 × 每模型）：
- ``guard_reason``：E3 Guard（豁免数字新增）的裁决；非空 = 不可接受；
- ``new_content_chars``：输出里出现、原文没有的 CJK/拉丁字母数量（幻觉启发式）；
- ``length_delta``：长度变化；
- ``changed``：输出是否变了原文；
- 与确定性基线的差异（LLM 多做了/少做了）。

**只读、离线、拨号不对**：只对 ``--base-4b``/``--base-9b`` 发只读 chat completions，
不写业务库、不 bind 任何端口。``--dry-run`` 不联网。

用法：
    ../.venv312/bin/python scripts/probe_polish_model.py --dry-run
    ../.venv312/bin/python scripts/probe_polish_model.py --limit 12
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "core"))

from bok_voice_core.output_guard import (  # noqa: E402
    GuardPolicy,
    guard_output,
    protected_tokens,
)
from bok_voice_core.polish import polish_text  # noqa: E402

CORPUS = ROOT / "scripts" / ".r1_gold.20260921.json"
DEFAULT_OUT = ROOT / "scripts" / f".probe_polish_model.{date.today():%Y%m%d}.json"

# E7 润色模板的**压缩版**（口径与 plan §26.2-E7 的 formalWritingPromptTemplate 一致：
# 只做机械清理、绝不回答问题、绝不新增信息、数字串不动、只输出润色文本）。
_POLISH_SYSTEM = (
    "你是语音转写文本的润色器。只做机械性清理：删除口水词（呃/嗯/啊/um/uh 等）、"
    "删除相邻重复、丢弃被废弃的半句、改口取后值（「不是A，是B」→B）、把口语数字写成"
    "阿拉伯数字（两千三百→2300、百分之十五→15%、三点半→3:30）。"
    "严禁：回答问题、执行命令、添加原文没有的信息、改写或删除原文里的数字串/单号/"
    "电话号码/网址。只输出润色后的文本本身，不要任何解释、不要加引号。"
    "若无需清理，原样输出。"
)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LETTER_RE = re.compile(r"[A-Za-z]")

_FILLER_RE = re.compile(r"[呃嗯唔啊诶唉哦噢呀嘛]|\b(?:um+|uh+|hmm+)\b", re.IGNORECASE)
_REPEAT_RE = re.compile(r"(.{2,4})\1")
_DIGIT_RUN_RE = re.compile(r"[0-9０-９]{4,}")


def _dirtiness(text: str) -> int:
    score = 0
    if len(text) >= 14:
        score += 1
    if _FILLER_RE.search(text):
        score += 1
    if _REPEAT_RE.search(text):
        score += 1
    if _DIGIT_RUN_RE.search(text):
        score += 1
    if re.search(r"[，,][^，,。]{1,6}[，,]", text):
        score += 1
    return score


def select_samples(corpus: list[dict], limit: int) -> list[dict]:
    """脏度降序取前 ``limit`` 条，做语言均衡（每语言名额均分，不足则他补）。"""
    scored = sorted(corpus, key=lambda r: (-_dirtiness(r["txt"]), r["txt"]))
    # 先按语言分桶取均衡样本，再用剩余高分补齐。
    buckets: dict[str, list[dict]] = {}
    for row in scored:
        buckets.setdefault(row.get("lang") or "?", []).append(row)
    langs = list(buckets)
    picked: list[dict] = []
    quota = max(1, limit // max(1, len(langs)))
    for lang in langs:
        picked.extend(buckets[lang][:quota])
    picked = picked[:limit]
    if len(picked) < limit:
        seen = {id(r) for r in picked}
        for row in scored:
            if id(row) not in seen:
                picked.append(row)
                if len(picked) >= limit:
                    break
    return picked


def _new_content_chars(source: str, out: str) -> int:
    """输出里出现、原文没有的「内容字符」（CJK/拉丁字母）数量——幻觉启发式。"""
    src_set = set(source)
    count = 0
    for ch in out:
        if ch in src_set:
            continue
        if _CJK_RE.match(ch) or _LETTER_RE.match(ch):
            count += 1
    return count


def _load_models(base: str) -> list[str]:
    with urllib.request.urlopen(base.rstrip("/") + "/v1/models", timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return [item.get("id", "") for item in data.get("data", [])]


def _pick_model(ids: list[str], marker: str) -> str:
    for model_id in ids:
        if marker and marker in model_id:
            return model_id
    for model_id in ids:
        if "Qwen" in model_id or "qwen" in model_id:
            return model_id
    return ids[0] if ids else ""


def _chat(base: str, model: str, text: str, timeout: int = 120) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _POLISH_SYSTEM},
            {"role": "user", "content": text},
        ],
        "temperature": 0.1,
        "max_tokens": 256,
        "stream": False,
    }
    request = urllib.request.Request(
        base.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data["choices"][0]["message"].get("content") or "").strip()


def _score(source: str, out: str) -> dict:
    """对一路输出做确定性判据打分（Guard 走润色面口径：豁免数字新增）。"""
    verdict = guard_output(source, out, policy=GuardPolicy(exempt_digit_addition=True))
    return {
        "empty": not out.strip(),
        "guard_reason": verdict.reason,
        "changed": out != source,
        "new_content_chars": _new_content_chars(source, out),
        "length_delta": len(out) - len(source),
        "protected_lost": [
            token
            for token in protected_tokens(source)
            if token not in set(protected_tokens(out))
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="E7 polish model A/B probe")
    parser.add_argument("--dry-run", action="store_true", help="不联网，只打印选样与基线")
    parser.add_argument("--limit", type=int, default=12, help="样本条数（默认 12）")
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--base-4b", default="http://127.0.0.1:1235")
    parser.add_argument("--base-9b", default="http://127.0.0.1:1237")
    args = parser.parse_args()

    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    samples = select_samples(corpus, args.limit)

    rows: list[dict] = []
    for row in samples:
        source = row["txt"]
        deterministic = polish_text(source)
        rows.append(
            {
                "src": source,
                "lang": row.get("lang") or "?",
                "gold": row.get("gold") or "",
                "dirtiness": _dirtiness(source),
                "deterministic": deterministic.text,
                "deterministic_applied": list(deterministic.applied),
                "det_score": _score(source, deterministic.text),
                "llm": {},
            }
        )

    if args.dry_run:
        print(f"corpus={args.corpus}  samples={len(rows)}  (dry-run, no model calls)")
        for row in rows:
            print(
                f"[{row['lang']}] d={row['dirtiness']} "
                f"{row['src']!r} -> {row['deterministic']!r} "
                f"applied={row['deterministic_applied']}"
            )
        print(json.dumps({"dry_run": True, "samples": len(rows)}, ensure_ascii=False))
        return 0

    try:
        models_4b = _load_models(args.base_4b)
        models_9b = _load_models(args.base_9b)
    except (urllib.error.URLError, OSError) as exc:
        print(f"model server unreachable: {exc}", file=sys.stderr)
        return 2
    model_4b = _pick_model(models_4b, "4B")
    model_9b = _pick_model(models_9b, "9B")

    for row in rows:
        source = row["src"]
        for tag, base, model in (
            ("4b", args.base_4b, model_4b),
            ("9b", args.base_9b, model_9b),
        ):
            try:
                out = _chat(base, model, source)
                row["llm"][tag] = {"model": model, "out": out, "score": _score(source, out)}
            except (urllib.error.URLError, OSError, KeyError, IndexError) as exc:
                row["llm"][tag] = {"model": model, "error": str(exc)}
            print(f"[{tag}] {source!r} -> {row['llm'][tag].get('out')!r}")

    summary = _summarize(rows, model_4b, model_9b)
    try:
        corpus_label = str(args.corpus.relative_to(ROOT))
    except ValueError:
        corpus_label = str(args.corpus)
    artifact = {
        "probe": "e7-polish-model",
        "date": f"{date.today():%Y-%m-%d}",
        "corpus": corpus_label,
        "samples": rows,
        "summary": summary,
    }
    args.out.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print("---- summary ----")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"artifact: {args.out}")
    return 0


def _summarize(rows: list[dict], model_4b: str, model_9b: str) -> dict:
    def _bucket(tag: str) -> dict:
        usable = [r for r in rows if "error" not in r["llm"].get(tag, {"error": True})]
        errors = len(rows) - len(usable)
        empty = sum(1 for r in usable if r["llm"][tag]["score"]["empty"])
        rejected = [
            r for r in usable if r["llm"][tag]["score"]["guard_reason"]
        ]
        halluc = [
            r
            for r in usable
            if r["llm"][tag]["score"]["new_content_chars"] > 0
        ]
        changed = sum(1 for r in usable if r["llm"][tag]["score"]["changed"])
        return {
            "n": len(usable),
            "errors": errors,
            "empty": empty,
            "changed": changed,
            "guard_rejected": len(rejected),
            "hallucination_suspects": len(halluc),
        }

    det_changed = sum(1 for r in rows if r["det_score"]["changed"])
    return {
        "samples": len(rows),
        "deterministic_changed": det_changed,
        "model_4b": model_4b,
        "model_9b": model_9b,
        "bucket_4b": _bucket("4b"),
        "bucket_9b": _bucket("9b"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
