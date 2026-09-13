#!/usr/bin/env python3
"""QA 快路词库命中率探针:真实转写挖出的问法 vs 现库,报 would-hit 率与 top 未命中。

数据源:
  - 现库条目  GET /api/qa-entries(enabled=1)
  - 真实问法  GET /api/reports/qa-pairs?min_calls=1&limit=200(服务端 mine_qa_pairs)

命中判定与 agent.py 命中块同源:QaIndex.match 实返回 (entry|None, score)
(keyword-only 签名,阈值在 match 内部把守),entry is not None 即 would-hit;
None 一律计 miss。未命中清单按通话数降序 = Task 10 补词条工单。

用法:
    BOK_CP_URL=http://127.0.0.1:8000 .venv312/bin/python scripts/probe_qa_hit.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

CP = os.environ.get("BOK_CP_URL", "http://127.0.0.1:8000")


def _get(path: str):
    req = urllib.request.Request(CP + path)
    tok = os.environ.get("BOK_CP_TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def summarize(rows: list[dict], pairs: list[dict]) -> dict:
    from agent_runtime.qa_gate import QaIndex  # 延迟 import(需 PYTHONPATH 指向 apps/agent)

    idx = QaIndex(rows)
    hit = 0
    misses = []
    for p in pairs:
        lang = p.get("lang") or "zh"
        step = p.get("step_index")
        # qa_gate.QaIndex.match 实签名 keyword-only、返回 (entry|None, score):
        # 命中即 entry 非 None(与 agent.py:2299 命中块同款判定,阈值 match 内部已把守)。
        entry, _score = idx.match(p["question"], lang=lang, step_index=step)
        if entry is not None:
            hit += 1
        else:
            misses.append({"question": p["question"], "calls": p.get("calls", 0), "lang": lang})
    misses.sort(key=lambda e: -e["calls"])
    total = len(pairs)
    return {"pairs_total": total, "would_hit": hit,
            "would_hit_rate": (hit / total) if total else 0.0,
            "top_misses": misses[:20]}


def main() -> int:
    data = _get("/api/qa-entries")
    rows = data.get("items", data) if isinstance(data, dict) else data
    rows = [r for r in rows if r.get("enabled", 1)]
    rep = _get("/api/reports/qa-pairs?min_calls=1&limit=200")
    pairs = rep.get("pairs", rep) if isinstance(rep, dict) else rep
    out = summarize(list(rows), list(pairs))
    print(f"库={len(rows)} 条 | 真实问法={out['pairs_total']} | would-hit {out['would_hit']}"
          f" ({out['would_hit_rate']:.0%})")
    for m in out["top_misses"]:
        print(f"  MISS[{m['calls']}通] ({m['lang']}) {m['question']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
