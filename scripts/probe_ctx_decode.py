"""上下文长度→decode 吞吐成本微基准(2026-09-24):prompt 1.7k vs 3.2k 的 decode tps。

背景(同日 W8 追加②):真实通话 prompt 随轮次 1.7k→3.2k 涨(逐轮尾部冻结累积,
LLM_HISTORY_TURNS=8 摊销截断在 16 条消息才触发,7 轮通话永不触发),in-call tps
43→14。本探针把「上下文长度」从共享机噪声里隔离出来:同一端点、两次通过(第二
遍=KV 缓存命中纯 decode)、1.7k/3.2k 交错采样对消机器漂移。判据=tps(3.2k)/
tps(1.7k):≤0.75 → 上下文瘦身刀值得做;≥0.9 → 晚轮变慢主因不是上下文,勿动刀。

用法:.venv312/bin/python scripts/probe_ctx_decode.py
     (PROBE_LLM_4B_URL / PROBE_LLM_4B_MODEL 可覆盖;模型 id 钉进程 argv)
"""

from __future__ import annotations

import os
import statistics
import time

import httpx

BASE = os.environ.get("PROBE_LLM_4B_URL", "http://127.0.0.1:1235/v1")
MODEL = os.environ.get(
    "PROBE_LLM_4B_MODEL", "avan-ag/Qwen3.5-4B-Uncensored-MLX-4bit"
)
DECODE_TOKENS = 48
REPEAT = 3  # 交错组数

# 填充料:话术域文本,重复 N 次逼近目标 prompt_tokens(1.7k/3.2k 两档)。
_UNIT = (
    "客服专员致电商户:您好,关于您店铺上月的三笔快递理赔,我们核对过物流轨迹,"
    "其中两单显示派送异常已超四十八小时,按平台规则可以登记赔付;需要跟您确认"
    "收款方式和开户行信息,登记后三个工作日内到账。"
)


def _messages(target_prompt_tokens: int) -> list[dict]:
    # 粗校准:中文 ≈1 token/字(4bit Qwen 分词),过/欠都在报告里见 usage,不追求精确。
    approx_chars = target_prompt_tokens
    reps = max(1, approx_chars // len(_UNIT) + 1)
    filler = _UNIT * reps
    return [
        {"role": "system", "content": filler},
        {"role": "user", "content": "请用大约六十个字介绍快递理赔的一般流程。"},
    ]


async def _decode_ms(client, msgs) -> tuple[int, int]:
    """一发补全请求 → (completion_tokens, prompt_tokens)。"""
    r = await client.post(
        f"{BASE}/chat/completions",
        json={
            "model": MODEL,
            "messages": msgs,
            "max_tokens": DECODE_TOKENS,
            "temperature": 0,
        },
        timeout=300,
    )
    r.raise_for_status()
    u = r.json()["usage"]
    return u.get("completion_tokens", 0), u.get("prompt_tokens", 0)


async def main() -> int:
    async with httpx.AsyncClient() as client:
        shapes = {"1.7k": _messages(1700), "3.2k": _messages(3200)}
        for name, msgs in shapes.items():
            await _decode_ms(client, msgs)  # 冷通过:建 KV 缓存
        results: dict[str, list[float]] = {k: [] for k in shapes}
        ptokens: dict[str, int] = {}
        for _ in range(REPEAT):
            for name, msgs in shapes.items():
                t0 = time.monotonic()
                gen, pt = await _decode_ms(client, msgs)
                dt = time.monotonic() - t0
                results[name].append(gen / dt)
                ptokens[name] = pt
        for name in shapes:
            vals = results[name]
            print(
                f"[ctx] prompt≈{name} (实测 {ptokens[name]} tok) "
                f"decode tps n={len(vals)} vals={[f'{v:.1f}' for v in vals]} "
                f"p50={statistics.median(vals):.1f}"
            )
        r = statistics.median(results["3.2k"]) / statistics.median(results["1.7k"])
        print(f"\n[ctx] tps 比 = 3.2k/1.7k = {r:.2f}  (≤0.75 → 瘦身刀值得做;≥0.90 → 勿动刀)")
        return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
