"""沉淀/纪要质量的**三方对照**（2026-09-21）：本机 9B vs DeepSeek flash vs v4-pro。

用户问题：「知识库沉淀、通话纪要的质量呢？」——纪要（`Summarizer`）产出三件：
summary / new_topics / insight，其中 new_topics 会**沉淀成知识**、insight 进洞察卡，
所以它的质量直接决定知识库的成色。

本探针取一通真实通话的完整转写，用三个后端各跑一次 `Summarizer._via_llm`，并排打印，
供人读裁决。客观项只打「耗时 / topics 条数 / 是否走了 fallback」，质量留给人读。

**口径注意**：本机 9B 这次**思考是关不掉的**（本地 MLX 没有 thinking 概念，它就是
个 9B）；云端两腿按用户口径**显式开思考**（`thinking_extra_body(base, "enabled")`
走的就是生产同一份契约）。所以这不是「同档比」，是「本地 9B vs 云端思考档」。

数据面：转写由 sqlite3 CLI 导出成 JSON（本脚本零 SQL）。
用法：
    DEEPSEEK_API_KEY=... python scripts/probe_settle_parity.py [--turns /tmp/call_turns.json]

导出：
  sqlite3 -readonly -json "$DB" "select role, transcript from turns
    where call_id='<CID>' and coalesce(line,'a')='a' order by rowid;" > /tmp/call_turns.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "packages" / "core"))
sys.path.insert(0, str(_ROOT / "apps" / "control-plane"))

from control_plane.summarize import _SYSTEM, Summarizer  # noqa: E402

JUDGE_9B = "/Users/halo/.lmstudio/models/huihui-ai/Huihui-Qwen3.5-9B-abliterated-mlx-4bit"
DS = "https://api.deepseek.com/v1"


class _Turn:
    def __init__(self, role: str, transcript: str) -> None:
        self.role = role
        self.transcript = transcript


def _legs() -> list[tuple[str, str, str]]:
    out = [("本机9B（无思考档）", "http://127.0.0.1:1237/v1", JUDGE_9B)]
    if os.environ.get("DEEPSEEK_API_KEY"):
        out += [("DS-flash（思考开）", DS, "deepseek-flash"), ("DS-v4pro（思考开）", DS, "deepseek-v4-pro")]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", nargs="+", default=["/tmp/call_turns.json"])
    ap.add_argument("--kind", default="outbound")
    args = ap.parse_args()

    summ = Summarizer()
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    call = {"object_id": "obj-real", "account_id": "acc-001", "kind": args.kind}
    # 每条腿的体检账：非空（真出稿）vs 空（走了确定性 fallback=这条腿白跑）
    tally: dict[str, list[int]] = {name: [0, 0] for name, *_ in _legs()}

    for path in args.turns:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
        turns = [_Turn(r.get("role", "?"), r.get("transcript", "") or "") for r in rows]
        transcript = summ._render_transcript(turns)
        print(f"\n=== {Path(path).name}  轮数={len(turns)}  转写={len(transcript)} 字 ===")
        for name, base, model in _legs():
            t0 = time.monotonic()
            try:
                out = summ._via_llm(base, model, transcript, call, _SYSTEM, key)
            except Exception as exc:  # noqa: BLE001 - 对照探针，失败要看见
                print(f"  {name}: ✗ {exc!r}")
                tally[name][0] += 0
                tally[name][1] += 1
                continue
            ms = (time.monotonic() - t0) * 1000
            topics = out.get("new_topics") or []
            ok = bool(out.get("summary"))
            tally[name][0 if ok else 1] += 1
            print(f"  {name} ({ms:.0f}ms) 非空={ok} topics={len(topics)} insight={bool(out.get('insight'))}")
            print(f"    summary: {out.get('summary', '')[:170]}")
            for t in topics[:2]:
                print(f"    · {json.dumps(t, ensure_ascii=False)[:150]}")

    print("\n汇总（出稿 / 空稿  =  合格率）")
    for name, (ok, empty) in tally.items():
        total = ok + empty
        print(f"  {name:<20} {ok}/{total}  合格率={ok / total * 100:.0f}%" if total else f"  {name}: 无样本")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
