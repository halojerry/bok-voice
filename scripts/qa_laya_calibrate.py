#!/usr/bin/env python3
"""Laya QA 车道 floor 标定（golden 集上画 coverage-accuracy，2026-09-25）。

用法：
    .venv312/bin/python scripts/qa_laya_calibrate.py [--ts 0.70,0.75,0.80,0.85,0.90,0.95]
    [--lang cantonese] [--limit 0]

依赖真栈：:8791（laya sidecar）+ :8789（embed）。对 golden 集每条正样本走
**生产同款**召回（词面 rank ∪ 语义 rank 并池 → decide_qa_match 原始面），
负样本（adjacent/offscript）同流程量误播；扫阈值 t 出表：

    coverage   = p≥t 且 choice==expect 的正样本占比（真实能播的比例）
    accuracy   = p≥t 且 choice≠NONE 的轮里选对的比例（播错罐头的风险面）
    neg_false  = 负样本 p≥t 且 choice≠NONE 的条数（误播绝对数,验收要求 0）
    none_rej   = 负样本高置信 NONE 占比（拒绝质量）

生态纪律（docs/LAYA-EVAL.md + 官方 README）：floor 不能抄社区数字,必须在自家
数据上按「该 coverage 下的实测精度」选点——本脚本就是那个「自家数据」。
判读：neg_false=0 的档里取 accuracy≥0.97 的最大 coverage;t 不够用时先调
召回池不是调门（门是最后一道,不是第一道）。

输出纯 stdout（不写文件）。退出码：0 正常 / 2 依赖不可达 / 3 golden 集缺失。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))
sys.path.insert(0, str(ROOT / "packages" / "core"))

GOLDEN_PATH = ROOT / "tests" / "qa_golden_set.json"


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ts", default="0.70,0.75,0.80,0.85,0.90,0.95")
    parser.add_argument("--lang", default="", help="只标定指定语言（空=全部）")
    parser.add_argument("--limit", type=int, default=0, help="截前 N 条（0=全量）")
    args = parser.parse_args()

    if not GOLDEN_PATH.exists():
        print(f"golden 集缺失: {GOLDEN_PATH}", file=sys.stderr)
        return 3
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    entries = golden["entries"]
    positives = [p for p in golden["positives"] if not args.lang or p.get("lang") == args.lang]
    negatives = [
        n
        for n in golden["negatives"]
        if n["reason"] in ("adjacent", "offscript")
        and (not args.lang or n.get("lang") in ("", args.lang))
    ]
    if args.limit:
        positives = positives[: args.limit]
        negatives = negatives[: args.limit]

    from agent_runtime.intent_semantic import EmbedClient
    from agent_runtime.laya_judge import decide_qa_match
    from agent_runtime.qa_gate import QaIndex, QaSemanticIndex

    # 依赖探活：embed（语义召回腿）+ laya（判定）。
    embed_client = EmbedClient()
    if await embed_client.embed(["探活"]) is None:
        print("[calibrate] :8789 embed 不可达——语义召回腿无法标定", file=sys.stderr)
        return 2
    probe = await decide_qa_match(["探活"], [dict(entries[0])], enabled=True)
    if probe.get("verdict") in ("off", "unavailable"):
        print(f"[calibrate] :8791 laya 不可达（probe={probe}）", file=sys.stderr)
        return 2

    sem_index = await QaSemanticIndex.build(embed_client, entries)
    if sem_index is None:
        print("[calibrate] 语义索引构建失败", file=sys.stderr)
        return 2
    lex_index = QaIndex(entries)

    async def pool_for(query: str, lang: str) -> list[dict]:
        """生产同款并池：词面 rank(k=8,floor=env 默认) ∪ 语义 rank(k=3)。"""
        rows: dict[str, float] = {}
        try:
            for score, eid in lex_index.rank(query, k=8, floor=0.40, lang=lang or None):
                rows[str(eid)] = float(score)
        except Exception:  # noqa: BLE001 - 与生产 fail-open 同姿势
            pass
        try:
            for score, eid in await sem_index.rank(query, k=3, lang=lang or ""):
                eid = str(eid)
                if float(score) > rows.get(eid, -1.0):
                    rows[eid] = float(score)
        except Exception:  # noqa: BLE001
            pass
        ranked = sorted(rows.items(), key=lambda t: t[1], reverse=True)[:8]
        return [c for c in (lex_index.by_id(eid) for eid, _s in ranked) if c is not None]

    async def run_one(item: dict) -> dict:
        pool = await pool_for(item["query"], item.get("lang", ""))
        if not pool:
            return {"pool": 0, "verdict": "no_pool", "choice": "", "p": 0.0, "conf": 0.0, "ms": 0.0}
        t0 = time.monotonic()
        dec = await decide_qa_match(
            [f"客户原话：{item['query']}", "当前步骤：解答客户疑问"], pool, enabled=True
        )
        return {
            "pool": len(pool),
            "verdict": dec.get("verdict", ""),
            "choice": str(dec.get("choice") or ""),
            "p": float(dec.get("p") or 0.0),
            "conf": float(dec.get("conf") or 0.0),
            "ms": (time.monotonic() - t0) * 1000,
        }

    print(f"[calibrate] positives={len(positives)} negatives={len(negatives)} entries={len(entries)}")

    pos_rows = []
    for i, p in enumerate(positives):
        r = await run_one(p)
        r["kind"] = p["kind"]
        r["expect"] = p["expect"]
        r["query"] = p["query"]
        r["correct"] = r["choice"] == p["expect"]
        pos_rows.append(r)
        if (i + 1) % 20 == 0:
            print(f"  positives {i + 1}/{len(positives)} …", flush=True)

    neg_rows = []
    for n in negatives:
        r = await run_one(n)
        r["reason"] = n["reason"]
        r["query"] = n["query"]
        neg_rows.append(r)

    called = [r for r in pos_rows + neg_rows if r["verdict"] not in ("no_pool",)]
    ms = [r["ms"] for r in called]
    print(
        f"\n[latency] decide 调用 {len(called)} 次:"
        f" p50={statistics.median(ms):.0f}ms max={max(ms):.0f}ms"
        if ms
        else "\n[latency] 零调用（召回池全空？）"
    )

    verdict_dist: dict[str, int] = {}
    for r in pos_rows:
        verdict_dist[r["verdict"]] = verdict_dist.get(r["verdict"], 0) + 1
    print(f"[verdict 分布·正样本] {verdict_dist}")

    ts = [float(x) for x in args.ts.split(",") if x.strip()]
    print(
        f"\n{'t':>5} {'coverage':>9} {'accuracy':>9} {'neg_false':>9} {'none_rej':>8}  判读"
    )
    best: tuple[float, float] | None = None
    for t in sorted(ts):
        played = [r for r in pos_rows if r["verdict"] == "hit" and r["p"] >= t]
        correct = [r for r in played if r["correct"]]
        coverage = len(played) / len(pos_rows) if pos_rows else 0.0
        accuracy = len(correct) / len(played) if played else 0.0
        neg_play = [r for r in neg_rows if r["verdict"] == "hit" and r["p"] >= t]
        neg_none = [
            r for r in neg_rows if r["verdict"] == "none" and r["p"] >= t
        ]
        none_rej = len(neg_none) / len(neg_rows) if neg_rows else 0.0
        note = ""
        if not neg_play and accuracy >= 0.97 and played:
            note = "<-- 候选档"
            if best is None or coverage > best[1]:
                best = (t, coverage)
        print(
            f"{t:>5.2f} {coverage:>8.0%} {accuracy:>9.0%} {len(neg_play):>9} "
            f"{none_rej:>8.0%}  {note}"
        )

    wrong = [r for r in pos_rows if r["verdict"] == "hit" and not r["correct"]]
    if wrong:
        print(f"\n[播错风险面] hit 但选错的 {len(wrong)} 条（看 t 档是否已拦）:")
        for r in wrong[:10]:
            print(f"  p={r['p']:.2f} conf={r['conf']:.2f} 选={r['choice']} 期望={r['expect']} {r['query']!r}")
    no_pool = [r for r in pos_rows if r["verdict"] == "no_pool"]
    if no_pool:
        print(f"\n[召回缺口] 并池为空 {len(no_pool)} 条（首选治召回不是治门）:")
        for r in no_pool[:6]:
            print(f"  kind={r['kind']} {r['query']!r}")

    if best:
        print(f"\n[建议] BOK_LAYA_QA_P={best[0]:.2f}（neg_false=0 且 accuracy≥0.97 的最大 coverage={best[1]:.0%}）")
    else:
        print("\n[建议] 无达标档——先治召回池/词条，不是调门")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
