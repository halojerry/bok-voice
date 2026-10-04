#!/usr/bin/env python3
"""QA 匹配器离线标定 harness（golden 集驱动,零网络零真库零 LLM）。

对 ``tests/qa_golden_set.json`` 建迷你 QaIndex,逐条跑召回 ``rank()`` 与快道
``match()``,打人类可读标定表到 stdout（不写文件）:

- [1] 索引概况:entries/positives/negatives 计数与语言分布;
- [2] 正样本逐条:kind / 期望条目在 top-k 中的位次与得分 / 0.90 快道是否命中;
- [3] 负样本逐条:闸 reason / best 召回得分 / 0.90 快道误命中;
- [4] floor sweep:各 floor 档的召回率 / 同音族召回 / adjacent 入侵率（Laya
     floor 与 VectorQ 每词条阈值的标定原料）。

用法::

    .venv312/bin/python scripts/pipeline/qa_match_report.py
    .venv312/bin/python scripts/pipeline/qa_match_report.py --k 8 --floor-sweep 0.40,0.55,0.70
    .venv312/bin/python scripts/pipeline/qa_match_report.py --lang cantonese

分数分解列:若 rank 返回行携带第三元素（dict,如 lex/bidir/pinyin 分量）则逐列
打印;当前契约只回 (score, entry_id) 时打总分。rank 未就位（并行实现中）→
明确错误 + 退出码 2,不抛 traceback。
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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "agent"))
sys.path.insert(0, str(ROOT / "packages" / "core"))

from agent_runtime.qa_gate import (  # noqa: E402
    QaIndex,
    qa_exclude_reason,
    qa_threshold,
)

GOLDEN_DEFAULT = ROOT / "tests" / "qa_golden_set.json"
FASTLANE_FALLBACK = 0.90


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="QA 匹配器 golden 离线标定报告")
    ap.add_argument("--k", type=int, default=8, help="召回 top-k（默认 8,与 rank 契约一致）")
    ap.add_argument(
        "--floor-sweep",
        default="0.40,0.50,0.55,0.60,0.65,0.70",
        help="逗号分隔的 floor 档位列表（默认 0.40-0.70 六档）",
    )
    ap.add_argument("--golden", default=str(GOLDEN_DEFAULT), help="golden json 路径")
    ap.add_argument("--lang", default="", help="只标定该语言（空=全部）")
    return ap.parse_args()


def _normalize_rank_row(row: tuple) -> tuple[float, str, dict]:
    """(score, entry_id[, detail]) → 归一三元组;倒置元组与缺 detail 都容错。"""
    score, eid = row[0], row[1]
    if isinstance(score, str) and not isinstance(eid, str):
        score, eid = eid, score
    detail = row[2] if len(row) >= 3 and isinstance(row[2], dict) else {}
    return float(score), str(eid), detail


def _rank(idx: QaIndex, query: str, lang: str, k: int, floor: float) -> list[tuple[float, str, dict]]:
    rows = idx.rank(query, k=k, floor=floor, lang=(lang or None))
    return [_normalize_rank_row(r) for r in rows]


def _match_id(idx: QaIndex, query: str, lang: str) -> tuple[str | None, float]:
    entry, score = idx.match(query, lang=lang, threshold=qa_threshold())
    return (None if entry is None else str(entry.get("id") or "")), float(score)


def _fmt_score(x: float) -> str:
    return f"{x:.3f}"


def _lang_counts(rows: list[dict], key: str) -> str:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.get(key, "") or "-"] = counts.get(r.get(key, "") or "-", 0) + 1
    return " ".join(f"{k}={v}" for k, v in sorted(counts.items()))


def main() -> int:
    args = _parse_args()
    try:
        golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: 无法读取 golden 集 {args.golden}: {exc}", file=sys.stderr)
        return 2

    entries: list[dict] = golden["entries"]
    positives: list[dict] = golden["positives"]
    negatives: list[dict] = golden["negatives"]
    if args.lang:
        positives = [p for p in positives if p.get("lang") == args.lang]
        negatives = [n for n in negatives if n.get("lang", args.lang) == args.lang]

    if not (callable(getattr(QaIndex, "rank", None))):
        print("ERROR: QaIndex.rank 尚未就位——召回车道并行实现中,本工具无法标定。", file=sys.stderr)
        print("  需要契约: rank(query, *, k, floor, lang=None, step_index=None) -> list[(score, entry_id)]", file=sys.stderr)
        print("  就位后重跑本脚本即可（快道 0.90 现状可先看 tests/test_qa_golden.py 的 fastlane 汇总）。", file=sys.stderr)
        return 2

    idx = QaIndex(entries)
    lang = args.lang
    k = args.k
    thr = qa_threshold()
    entry_lang = {str(e["id"]): str(e.get("lang") or "") for e in entries}

    print(f"QA MATCH REPORT  golden={Path(args.golden).name}  k={k}  fastlane_thr={thr:.2f}"
          + (f"  lang={lang}" if lang else ""))
    print("=" * 100)

    # ---- [1] 索引概况 ----
    print(f"\n[1] 索引概况  entries={len(idx)}  positives={len(positives)}  negatives={len(negatives)}")
    print(f"    entries 语言的分布: {_lang_counts(entries, 'lang')}")
    print(f"    positives kind 分布: {_lang_counts(positives, 'kind')}")

    # ---- [2] 正样本逐条 ----
    print(f"\n[2] positives 逐条（floor=0 全排位:期望条目位次/得分;fastlane=0.90 现状）")
    print(f"    {'#':<4} {'kind':<10} {'lang':<9} {'rank_pos':<8} {'score':<7} {'fastlane':<16} query / expect")
    recall_hits = 0
    kind_stats: dict[str, list[int]] = {}
    detail_keys: list[str] = []
    for i, p in enumerate(positives, 1):
        rows = _rank(idx, p["query"], p.get("lang", ""), k, 0.0)  # floor=0: 看全排位
        pos_in = next((j for j, (_s, eid, _d) in enumerate(rows, 1) if eid == p["expect"]), 0)
        score_at = next((s for s, eid, _d in rows if eid == p["expect"]), 0.0)
        if pos_in:
            detail = next((d for _s, eid, d in rows if eid == p["expect"]), {})
            for dk in detail:
                if dk not in detail_keys:
                    detail_keys.append(dk)
        hit_id, hit_score = _match_id(idx, p["query"], p.get("lang", ""))
        lane = "hit " + (hit_id if hit_id == p["expect"] else f"WRONG {hit_id}") if hit_id else "miss"
        recall_hits += int(bool(pos_in))
        kind_stats.setdefault(p["kind"], []).append(int(bool(pos_in)))
        print(f"    {i:<4} {p['kind']:<10} {p['lang']:<9} "
              f"{(pos_in if pos_in else f'>{k}'):<8} {_fmt_score(score_at):<7} {lane:<16} "
              f"{p['query']} / {p['expect']}")
    print(f"    -> floor=0 召回位次 top{k} 内: {recall_hits}/{len(positives)} = {recall_hits / max(1, len(positives)):.0%}"
          f"（过 0.55 floor 档的达标数见 [4];本表看排位与得分）"
          + (f"（分数分解列可用: {','.join(detail_keys)}）" if detail_keys else "（rank 只回总分,无分解列）"))
    for kind, flags in sorted(kind_stats.items()):
        print(f"       {kind:<10} {sum(flags)}/{len(flags)} = {sum(flags) / len(flags):.0%}")

    # ---- [3] 负样本逐条 ----
    print(f"\n[3] negatives 逐条（闸 reason / best 召回得分 / 0.90 快道误命中）")
    print(f"    {'reason':<10} {'gate':<14} {'best_score':<10} {'fastlane':<7} query")
    fastlane_false = 0
    for n in negatives:
        gate = qa_exclude_reason(n["query"])
        rows = _rank(idx, n["query"], n.get("lang", ""), k, 0.0)
        best = max((s for s, _e, _d in rows), default=0.0)
        hit_id, _hs = _match_id(idx, n["query"], n.get("lang", ""))
        fastlane_false += int(hit_id is not None)
        print(f"    {n['reason']:<10} {gate or '-':<14} {_fmt_score(best):<10} "
              f"{('HIT ' + hit_id) if hit_id else 'ok':<7} {n['query']}")
    print(f"    -> 0.90 快道负样本误命中 {fastlane_false}/{len(negatives)}")

    # ---- [4] floor sweep ----
    floors = [float(x) for x in str(args.floor_sweep).split(",") if x.strip()]
    print(f"\n[4] floor sweep（rank floor 档位 → Laya floor / 每词条阈值标定原料）")
    print(f"    {'floor':<7} {'recall@k':<12} {'homophone':<12} {'adjacent@>floor':<16} {'neg@>floor':<12}")
    homophones = [p for p in positives if p["kind"] == "homophone"]
    for floor in floors:
        hits = sum(
            1 for p in positives
            if any(eid == p["expect"] for _s, eid, _d in _rank(idx, p["query"], p.get("lang", ""), k, floor))
        )
        h_hits = sum(
            1 for p in homophones
            if any(eid == p["expect"] for _s, eid, _d in _rank(idx, p["query"], p.get("lang", ""), k, floor))
        )
        adj_over = sum(
            1 for n in negatives if n["reason"] == "adjacent"
            and any(s >= floor for s, _e, _d in _rank(idx, n["query"], n.get("lang", ""), k, floor))
        )
        neg_over = sum(
            1 for n in negatives if n["reason"] in ("offscript", "short_ack")
            and any(s >= floor for s, _e, _d in _rank(idx, n["query"], n.get("lang", ""), k, floor))
        )
        adj_total = max(1, sum(1 for n in negatives if n["reason"] == "adjacent"))
        print(f"    {floor:<7} {hits}/{len(positives)} ({hits / max(1, len(positives)):.0%})   "
              f"{h_hits}/{len(homophones)} ({h_hits / max(1, len(homophones)):.0%})   "
              f"{adj_over}/{adj_total} ({adj_over / adj_total:.0%})          "
              f"{neg_over}/{max(1, len(negatives) - adj_total)}")

    print("\n判读:recall@k 与 homophone 达标是召回车道验收线;floor 档选点看 recall 增益对")
    print("adjacent 入侵率的斜率——floor 压在「recall 拐点」且 adjacent 入侵 <~20% 为候选档,")
    print("最终由 Laya 精判把入侵者拦回。fast-lane(0.90) 误命中必须恒为 0。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
