#!/usr/bin/env python3
"""spec decode 隔离 A/B 探针（2026-09-27，llm_draft 实弹验收）——纯客户端，零进程管理。

两个互斥模式：

1. 打印命令行（服务由调用方自己起）：
    .venv312/bin/python scripts/probes/probe_llm_draft_ab.py argv baseline
    .venv312/bin/python scripts/probes/probe_llm_draft_ab.py argv draft
   输出与 serve 逐字节一致的 mlx_lm server 完整命令行（空格分隔、可直贴 bash；
   draft 档=BOK_LLM_DRAFT=1 语义：--draft-model + cache 6→5GB）。

2. 测量（假设 :1239 已有服务在跑，探针只发 HTTP）：
    .venv312/bin/python scripts/probes/probe_llm_draft_ab.py bench > /tmp/draft_base.json

**前置：栈必须 down**——第二份 4B 实例与在跑栈同驻会把 48G 打穿 swap，bench 全废。
测量面（本机共享 GPU 基线摆动 4-5x，结论以隔离 bench+机制为主）：
- decode tps：暖缓存 3 prompt × 2 rep × 128 token 流式，首 token 到末 token 吞吐
  （spec decode 只加速这段）；
- TTFT 暖档：同 prompt 二发，time-to-first-token；
- greedy 一致性：temperature=0 固定 3 prompt——spec decode 数学无损（拒绝采样），
  输出应与 baseline 逐字节一致（mlx-lm#846 类丢 token 的对立面证据；不一致=否决翻闸）。
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


import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # G1c 入桶后 repo 根=parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import bok  # noqa: E402

PORT = "1239"
BASE = f"http://127.0.0.1:{PORT}"

_SYSTEM = (
    "你是一名粤语客服助理，负责处理快递遗失赔偿事宜。守则：语气真诚，先道歉后处理；"
    "赔偿金额必须核实订单后才能告知；不主动提及公司内部流程；客户情绪激动时先安抚。"
    "当前流程：第一步身份确认，第二步来电通知，第三步平台确认，第四步赔偿说明，"
    "第五步办理方式，第六步收尾。请用简洁口语回复，每次不超过两句。"
)
_USERS = [
    "你哋係邊間公司嚟噶？我點知你唔係呃人嘅？",
    "咁我要點樣先可以攞到個賠償啊？要等幾耐？",
    "我想問下如果我張單係拼多多買嘅，咁賠償係咪唔同㗎？",
]


def print_argv(mode: str) -> int:
    import shlex

    if mode == "draft":
        import os

        os.environ["BOK_LLM_DRAFT"] = "1"
    # current 与 serve 同源（cmd_serve :1574 同款构造）
    current = dict(bok.MODELS["mac"] if bok.is_mac() else bok.MODELS["windows"])
    current["llm"] = bok.resolve_llm_repo(current)
    llm_py = bok.sidecar_python("llm-mlx")
    llm_model = bok.model_path({**current, "llm": current["llm"]}, "llm")
    draft_flags = bok._llm_draft_flags(current) if mode == "draft" else []
    argv = bok._mac_llm_server_argv(
        llm_py, llm_model, PORT, current, log_level="WARNING", draft_flags=draft_flags
    )
    print(" ".join(shlex.quote(str(a)) for a in argv))
    return 0


def bench() -> int:
    import httpx

    try:
        if httpx.get(f"{BASE}/health", timeout=3.0).status_code != 200:
            print(f"[ab] :1239 无服务", file=sys.stderr)
            return 2
    except Exception:
        print(f"[ab] :1239 无服务（栈 down 了吗？服务起了吗？）", file=sys.stderr)
        return 2

    client = httpx.Client(base_url=BASE, timeout=120.0)

    def msgs(u: str) -> list[dict]:
        return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": u}]

    # 暖缓存：每 prompt 先发一次
    for u in _USERS:
        client.post(
            "/v1/chat/completions",
            json={"messages": msgs(u), "max_tokens": 1, "temperature": 0.0},
        )
    time.sleep(0.5)

    ttft_ms: list[float] = []
    tps: list[float] = []
    for u in _USERS:
        for _rep in range(2):
            t0 = time.monotonic()
            first = None
            last = t0
            n = 0
            with client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "messages": msgs(u),
                    "max_tokens": 128,
                    "temperature": 0.0,
                    "stream": True,
                },
            ) as r:
                for line in r.iter_lines():
                    if not line.startswith("data: ") or line.strip() == "data: [DONE]":
                        continue
                    try:
                        delta = json.loads(line[6:])["choices"][0]["delta"]
                    except Exception:
                        continue
                    if delta.get("content"):
                        now = time.monotonic()
                        if first is None:
                            first = now
                        last = now
                        n += 1
            if first is None or n < 16:
                continue
            ttft_ms.append((first - t0) * 1000)
            tps.append((n - 1) / max(last - first, 1e-6))

    greedy: list[str] = []
    for u in _USERS:
        r = client.post(
            "/v1/chat/completions",
            json={"messages": msgs(u), "max_tokens": 96, "temperature": 0.0},
        )
        try:
            greedy.append(r.json()["choices"][0]["message"]["content"] or "")
        except Exception:
            greedy.append("")

    def _p50(xs: list[float]) -> float:
        return sorted(xs)[len(xs) // 2] if xs else 0.0

    print(
        json.dumps(
            {
                "ttft_warm_ms_p50": round(_p50(ttft_ms)),
                "ttft_warm_ms_all": [round(x) for x in ttft_ms],
                "tps_p50": round(_p50(tps), 1),
                "tps_all": [round(x, 1) for x in tps],
                "greedy_outputs": greedy,
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("argv", "bench"):
        print("usage: probe_llm_draft_ab.py argv baseline|draft | bench", file=sys.stderr)
        return 2
    if sys.argv[1] == "bench":
        return bench()
    mode = sys.argv[2] if len(sys.argv) > 2 else "baseline"
    if mode not in ("baseline", "draft"):
        print("mode 必须是 baseline|draft", file=sys.stderr)
        return 2
    return print_argv(mode)


if __name__ == "__main__":
    raise SystemExit(main())
