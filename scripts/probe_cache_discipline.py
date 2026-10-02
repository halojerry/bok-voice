#!/usr/bin/env python3
"""缓存纪律探针（2026-09-21 讨论稿 §46 配套，只打本机诊断端点）：

证明「越聊越慢」的来源是**每轮新增尾部的 prefill 增长**而非历史缓存 miss，
并演示恒定尾部与截断的行为。三个演示（同一端点同一模型、逐轮流式测 TTFT）：

  A. 增长尾部：记忆行逐轮加（模拟生产 ContextState 尾部单调涨到 1200 字）→ TTFT 逐轮变差；
  B. 恒定尾部：尾部钉两行（滚动压缩摘要形态）→ TTFT 持平；
  C. 截断尖峰：历史砍到只剩最后 2 轮（模拟 LLM_HISTORY_TURNS 截回）→ 该轮全量重 prefill。

安全约束（Mimosa SSRF 加固）：本探针是本地诊断工具，端点**只允许** 127.0.0.1
环回 + http/https；拒绝其他一切主机。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / ".." / "packages" / "core"))
sys.path.insert(0, str(_ROOT / ".." / "apps" / "agent"))

from measure_prompt import build_ctx  # noqa: E402

# 本地诊断端点白名单：只允许环回 127.0.0.1（探针用途本身就是打本机 sidecar）。
_ALLOWED_HOSTS = ("127.0.0.1", "localhost")
BASE = os.environ.get("MLX_URL", "http://127.0.0.1:1241")
MODEL = os.environ.get("MLX_MODEL", "")
MEM_LINE = ("user: 客户話快遞延誤兩日，情緒激動，要求賠償同投訴，已解釋平台賠償三檔方案"
            "並承諾跟進倉庫核實。")
_STOP = ["<|im_end|>", "<|im_start|>", "<|endoftext|>"]


def _guard(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"scheme not allowed: {parts.scheme!r}")
    if (parts.hostname or "") not in _ALLOWED_HOSTS:
        raise ValueError(f"host not allowed (local-diag only): {parts.hostname!r}")
    return url


def _ttft_ms(messages: list[dict], max_tokens: int = 48) -> tuple[float, str]:
    req = Request(
        _guard(f"{BASE.rstrip('/')}/chat/completions"),
        data=json.dumps({
            "model": MODEL, "messages": messages, "max_tokens": max_tokens,
            "temperature": 0.35, "stream": True, "stop": _STOP,
        }).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.monotonic()
    first = -1.0
    text: list[str] = []
    with urlopen(req, timeout=120) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: ") or line[6:] == "[DONE]":
                continue
            try:
                delta = json.loads(line[6:])["choices"][0].get("delta", {})
            except Exception:  # noqa: BLE001 - SSE 心跳行
                continue
            c = delta.get("content")
            if c:
                if first < 0:
                    first = (time.monotonic() - t0) * 1000
                text.append(c)
    return first, "".join(text)


def _convo(prefix: str, turns: list[tuple[str, str, str]]) -> list[dict]:
    msgs = [{"role": "system", "content": prefix}]
    for user, tail, asst in turns:
        msgs.append({"role": "user", "content": f"{user}\n\n{tail}"})
        msgs.append({"role": "assistant", "content": asst})
    return msgs


def main() -> int:
    global MODEL
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=8)
    args = ap.parse_args()
    if not MODEL:
        with urlopen(_guard(f"{BASE.rstrip('/')}/models"), timeout=10) as r:
            ids = [m["id"] for m in json.load(r)["data"]]
        MODEL = next((i for i in ids if not i.startswith("/")), ids[0])
    print(f"endpoint={BASE} model={MODEL}")

    prefix = build_ctx(2).render_instruction_prefix()
    users = ["你好", "我個包裹點解仲未到？", "延遲咗兩日？搞錯啊，我要投訴！", "咁有咩賠償？",
             "我個單號係三七七八九零。", "好，咁幾時覆我？", "WhatsApp點加啊？", "冇其他事啦，唔該。"]
    replies = ["你好，請問係咪陳先生本人？我哋係順丰快遞。", "係嘅，你個包裹上禮拜三已經到咗香港倉。",
               "唔好意思，件貨延遲咗兩日，我幫你跟進緊。", "賠償方面有標準方案，我幫你查返你件貨嘅情況。",
               "你嘅單號尾數係七八九零，冇錯嘛？", "我已經幫你登記咗跟進，四十八小時內覆你。",
               "你可以加我哋WhatsApp，我哋會即時更新進度。", "唔使客氣，仲有咩可以幫到你？"]

    def tail_for(i: int, mode: str) -> str:
        ctx = build_ctx(2)
        if mode == "grow":
            for _ in range(i):  # 記憶逐輪加一行（生產單調漲形態，上限 1200 字）
                ctx.add_summary("user" if _ % 2 == 0 else "assistant", MEM_LINE)
        elif mode == "constant":
            ctx.add_summary("user", MEM_LINE)  # 釘兩行（滾動壓縮摘要形態）
            ctx.add_summary("assistant", MEM_LINE)
        # slim(P1.2 後形態,§48 P1.1)：記憶壓縮到位=零記憶行，尾部=基礎塊恆定
        return ctx.render_context_tail()

    for mode in ("slim", "grow", "constant"):
        label = {
            "slim": "零記憶行=基礎塊恆定（P1.2 後形態）",
            "grow": "逐輪加一行記憶",
            "constant": "釘兩行(壓縮摘要形態)",
        }[mode]
        print(f"\n=== 模式 {mode}（每輪新增尾部：{label}）===")
        history: list[tuple[str, str, str]] = []
        for i in range(args.turns):
            msgs = _convo(prefix, history)
            tail = tail_for(i, mode)
            msgs.append({"role": "user", "content": f"{users[i]}\n\n{tail}"})
            ms, text = _ttft_ms(msgs)
            print(f"  輪{i + 1}: 尾部{len(tail):>4}字  首字 {ms:7.0f}ms  答:{text[:16]!r}")
            history.append((users[i], tail, replies[i]))

    if len(history) >= 8:
        print("\n=== 截斷尖峰（歷史砍到剩最後 2 輪 = 前綴全變，全量重 prefill）===")
        msgs = _convo(prefix, history[-2:])
        tail = tail_for(7, "grow")
        msgs.append({"role": "user", "content": f"{users[-1]}\n\n{tail}"})
        ms, _ = _ttft_ms(msgs)
        print(f"  截斷輪: 首字 {ms:7.0f}ms")
    print("\n判讀：grow 逐輪變差、constant 持平、截斷輪暴漲 ⇒ 「越聊越慢」=尾部增長+截斷，非歷史緩存 miss。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
