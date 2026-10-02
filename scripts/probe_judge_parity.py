"""意图识别（judge）的**三方对照**（2026-09-21）：本机 9B vs DeepSeek flash vs v4-pro。

用户问题：「意图识别呢？」——judge 有两个消费端：①`parse_judge_output` 的
advance/stay/objection 决定要不要翻篇；②`parse_judge_route` 的 route/conf 决定
建单/转人工/降级（conf ≥ 0.7 才动作）。本探针对**真实通话里客户说过的话**跑三腿，
并排打印三件事（verdict / route / conf），供人读裁决。

**为什么只打并排不打「准确率」**：真库里没有 route 的人工标注——route 一旦命中就
被消费掉（建单/播确认语），事后看不出「本该建单却没建」。硬造一个标量准确率是
自欺。可以客观给的只有**一致率**与**分歧清单**，后者留给人读。

数据面：样本由 sqlite3 CLI 导出成 JSON（本脚本零 SQL）。用法：
    python scripts/probe_judge_parity.py [--turns /tmp/real_turns.json] [--n 12]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "packages" / "core"))
sys.path.insert(0, str(_ROOT / "apps" / "agent"))
sys.path.insert(0, str(_ROOT / "scripts"))

from agent_runtime.flow import (  # noqa: E402
    FOLLOWUP_CONF_MIN,
    JUDGE_ROUTE_MAX_TOKENS,
    FlowController,
    build_judge_messages,
    parse_judge_output,
    parse_judge_route,
)

JUDGE_LOCAL = ("http://127.0.0.1:1237/v1", "/Users/halo/.lmstudio/models/huihui-ai/Huihui-Qwen3.5-9B-abliterated-mlx-4bit")
DS = "https://api.deepseek.com/v1"


def _legs() -> list[tuple[str, str, str, str]]:
    """(名字, base_url, model, api_key)。云端腿缺 key 就不上。"""
    out = [("本机9B", JUDGE_LOCAL[0], JUDGE_LOCAL[1], "mlx")]
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        out += [("DS-flash", DS, "deepseek-flash", key), ("DS-v4pro", DS, "deepseek-v4-pro", key)]
    return out


async def _judge(base: str, model: str, key: str, msgs: list[dict]) -> str:
    from agent_runtime.agent import _llm_judge  # noqa: PLC0415

    return await _llm_judge(base, model, msgs, max_tokens=JUDGE_ROUTE_MAX_TOKENS, timeout=25.0, api_key=key)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", default="/tmp/real_turns.json")
    ap.add_argument("--n", type=int, default=12)
    args = ap.parse_args()

    from measure_prompt import OBJ, STEPS  # noqa: PLC0415

    rows = json.loads(Path(args.turns).read_text(encoding="utf-8"))[: args.n]
    legs = _legs()
    agree: dict[str, int] = {}
    print(f"三腿：{[n for n, *_ in legs]}\nconf ≥ {FOLLOWUP_CONF_MIN} 才算够建单线\n")

    for i, row in enumerate(rows, 1):
        step = max(0, min(int(row.get("template_step") or 0), len(STEPS) - 1))
        fc = FlowController.from_template({"steps_json": json.dumps(STEPS, ensure_ascii=False)}, OBJ)
        for _ in range(step):
            fc.advance()
        goal, ref = fc.current_goal_ref()
        msgs = build_judge_messages(
            current_index=fc.current + 1,
            total=len(fc.steps),
            overview_lines=fc.overview_goal_lines(),
            goal=goal,
            ref=ref,
            next_goal=fc.next_goal(),
            user_text=row["user_text"],
            facts=fc.vars_map,
            route_enabled=True,
        )
        print(f"[{i}] 第{step + 1}步 / {row.get('language')}  客户：{row['user_text']}")
        seen: dict[str, tuple[str, str, float]] = {}
        for name, base, model, key in legs:
            raw = await _judge(base, model, key, msgs)
            v, r, c = parse_judge_output(raw), *parse_judge_route(raw)
            seen[name] = (v, r, c)
            flag = "建单线" if c >= FOLLOWUP_CONF_MIN else ""
            print(f"    {name:<10} verdict={v:<9} route={r:<17} conf={c:.2f} {flag}")
        names = list(seen)
        for a in range(len(names)):
            for b in range(a + 1, len(names)):
                k = f"{names[a]} vs {names[b]}"
                same = seen[names[a]][:2] == seen[names[b]][:2]
                agree[k] = agree.get(k, 0) + (1 if same else 0)
        if len(set(t[:2] for t in seen.values())) > 1:
            print("    ↑ 分歧（verdict/route 不全同）")

    print(f"\n一致率（verdict+route 全同 / {len(rows)} 轮）")
    for k, v in sorted(agree.items()):
        print(f"  {k:<22} {v}/{len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
