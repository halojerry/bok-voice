#!/usr/bin/env python3
"""judge 跨进程争用台架（批次0.2 余项，2026-10-03）。

问题：judge（:1235 代理→:1239 4B MLX 进程）与 a_reply（:1238 闸→:1237 9B
MLX 进程）是两个独立进程、同一块 GPU——给 judge 加压时，a_reply 的 TTFT
位移多少？这是「en 慢轮是否被判官抢 GPU」的定量裁定台架。

方法：同一条 ≈2k token 的 reply 形请求（X-Bok-Lane: reply）直打 :1238，
两臂各 reps 次：
  A 静默臂：无并发 judge；
  B 争用臂：请求在途期间，后台连发 K 个 judge 形请求（短 prompt、
    max_tokens=8、无 lane 头=bg）到 :1235。
报每臂 TTFT 序列 + p50，以及 B 臂 judge 请求的完成数/耗时。

凭据零需求（本地口）。用法：
    ./pkgruntime-aside/python/bin/python3.12 scripts/bench_judge_contention.py [--reps 3] [--judges 6]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

import httpx

REPLY_URL = "http://127.0.0.1:1238/v1/chat/completions"
JUDGE_URL = "http://127.0.0.1:1235/v1/chat/completions"
MODEL_9B = "huihui-ai/Huihui-Qwen3.5-9B-abliterated-mlx-4bit"


def _reply_prompt() -> list[dict]:
    """≈2k token 的 system + 一句 user（贴生产前缀尺度；重复块保证缓存可命）。"""
    unit = (
        "我们是集运中转仓客服团队，负责处理跨境包裹的丢失、延误与理赔核对工作，"
        "每一通电话都需要先核对客户身份与订单信息，再按服务协议约定的口径说明赔付方案，"
        "全程遵守合规话术，不承诺超出协议范围的赔偿。"
    )
    system = ("[bench-judge-contention] " + unit * 40)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "客户问进度，一句话回复。"},
    ]


async def _one_reply(client: httpx.AsyncClient) -> float:
    """一发 reply 形流式请求，返回 TTFT ms（首个正文 delta）。"""
    payload = {
        "model": MODEL_9B,
        "messages": _reply_prompt(),
        "stream": True,
        "max_tokens": 8,
        "temperature": 0.3,
    }
    t0 = time.perf_counter()
    ttft = -1.0
    async with client.stream("POST", REPLY_URL, json=payload,
                             headers={"X-Bok-Lane": "reply"}, timeout=60) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                chunk = json.loads(body)
            except json.JSONDecodeError:
                continue
            delta = ((chunk.get("choices") or [{}])[0].get("delta") or {})
            if delta.get("content"):
                ttft = (time.perf_counter() - t0) * 1000
                break
    return ttft


async def _one_judge(client: httpx.AsyncClient, idx: int) -> float:
    """一发 judge 形请求（短 prompt、max_tokens=8、bg 车道），返回耗时 ms。"""
    payload = {
        "model": "/Users/halo/.lmstudio/models/avan-ag/Qwen3.5-4B-Uncensored-MLX-4bit",
        "messages": [{"role": "user", "content": f"判定：客户说第{idx}句话，输出一个词。"}],
        "max_tokens": 8,
        "temperature": 0,
    }
    t0 = time.perf_counter()
    try:
        r = await client.post(JUDGE_URL, json=payload, timeout=30)
        r.raise_for_status()
    except Exception:  # noqa: BLE001 - 台架只记失败
        pass
    return (time.perf_counter() - t0) * 1000


def _p50(vals: list[float]) -> float:
    vals = sorted(v for v in vals if v >= 0)
    return vals[len(vals) // 2] if vals else -1.0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--judges", type=int, default=6)
    args = ap.parse_args()

    async with httpx.AsyncClient() as client:
        # 暖机一发（冷 prefill 不进统计）
        warm = await _one_reply(client)
        print(f"[warmup] ttft={warm:.0f}ms", flush=True)

        quiet: list[float] = []
        for _ in range(args.reps):
            quiet.append(await _one_reply(client))
            await asyncio.sleep(0.5)
        print("[A 静默] ttft_ms:", " ".join(f"{v:.0f}" for v in quiet),
              f" p50={_p50(quiet):.0f}", flush=True)

        loaded: list[float] = []
        judge_ms: list[float] = []
        for _ in range(args.reps):
            judges = [asyncio.create_task(_one_judge(client, i)) for i in range(args.judges)]
            await asyncio.sleep(0.05)  # 让 judge 先占上
            loaded.append(await _one_reply(client))
            judge_ms.extend(await asyncio.gather(*judges))
            await asyncio.sleep(0.5)
        print("[B 争用] ttft_ms:", " ".join(f"{v:.0f}" for v in loaded),
              f" p50={_p50(loaded):.0f}", flush=True)
        print(f"[B 争用] judge n={len(judge_ms)} 耗时_ms:",
              " ".join(f"{v:.0f}" for v in judge_ms), flush=True)

        dq, dl = _p50(quiet), _p50(loaded)
        print(f"[结论] reply TTFT p50 位移 = {dl - dq:+.0f}ms ({dq:.0f} -> {dl:.0f})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
