#!/usr/bin/env python3
"""LLM 前缀缓存命中率报告（W0 验收工具）。

解析 agent worker 日志里的 `LLM_TTFT_MS ... cached=N/M prompt=M` 行，输出：
- 总请求数 / cached=0（全量重 prefill）次数与占比；
- cached/M 比值分布（>=90% 视为「基本全命中」，30%-90% 为「锚点命中」，<30% 视同失效）；
- 每次会话（以 `flow loaded`/`turn_handling` 之后的连续窗口近似）的首轮是否命中。

用法：python scripts/ops/llm_cache_report.py /tmp/bok-agent-w0.log [/tmp/other.log ...]
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


import re
import sys

LINE = re.compile(r"LLM_TTFT_MS\s+(\d+)\s+\(official\)\s+cached=(\d+)/(\d+)")


def main(paths: list[str]) -> int:
    total = cold = full = anchor = dead = 0
    ttfts: list[int] = []
    for path in paths:
        for line in open(path, errors="ignore"):
            m = LINE.search(line)
            if not m:
                continue
            ttft, cached, prompt = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if prompt <= 0:
                continue
            total += 1
            ttfts.append(ttft)
            ratio = cached / prompt
            if cached == 0:
                cold += 1
                dead += 1
            elif ratio >= 0.9:
                full += 1
            elif ratio >= 0.3:
                anchor += 1
            else:
                dead += 1
    ttfts.sort()

    def pct(n: int) -> str:
        return f"{n}/{total} ({(n / total * 100):.0f}%)" if total else "0"

    def med(v: list[int]) -> str:
        return f"{v[len(v) // 2]}ms" if v else "-"

    print(f"requests={total}")
    print(f"  full-hit (cached>=90% prompt): {pct(full)}")
    print(f"  anchor-only (30%-90%):        {pct(anchor)}")
    print(f"  cold/miss  (cached<30%):      {pct(dead)}  <- 越少越好;>0 即有前缀断裂/冷启")
    print(f"  TTFT median={med(ttfts)} p90={med(ttfts[int(len(ttfts) * 0.9):]) or '-'}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    raise SystemExit(main(sys.argv[1:]))
