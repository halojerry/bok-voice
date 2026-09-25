"""LLM 同卡争用微基准(2026-09-24):4B prefill 单飞 vs 9B 并行时的往返膨胀。

背景:judge(9B,:1237)与回复生成(4B,:1235)同卡。agent.py `_wait_link_idle`
docstring 引用过 2026-09-22 的一次性实测(prefill 853→2228ms),但探针从未落盘——
本文件把它固化:同口径随时复测,为「judge 闲时让路闸」与 W10-12(CUDA 迁移)提供
决策数字。

口径:max_tokens=1 的补全请求=纯 prefill+调度往返(零生成),5 发取中位;
「contended」腿=同刻在 9B 上发一个 max_tokens=256 的长生成请求,让 4B 的
5 发 prefill 落在 9B 解码窗口内。零真栈依赖(直打两个本地 OpenAI 端点)。

用法:.venv312/bin/python scripts/probe_llm_contention.py
     (端点可 env 覆盖:PROBE_LLM_4B_URL / PROBE_LLM_9B_URL / PROBE_LLM_9B_MODEL)
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time

BASE_4B = os.environ.get("PROBE_LLM_4B_URL", "http://127.0.0.1:1235/v1")
BASE_9B = os.environ.get("PROBE_LLM_9B_URL", "http://127.0.0.1:1237/v1")
# 模型 id 钉进程实载(W5 教训:mlx_lm /v1/models 是 HF 缓存扫描,列盘上全部目录
# 而非已加载模型——拿 models[0] 会把 4B 端点标成 9B id;真值=进程 argv)。
MODEL_4B = os.environ.get(
    "PROBE_LLM_4B_MODEL", "avan-ag/Qwen3.5-4B-Uncensored-MLX-4bit"
)
MODEL_9B = os.environ.get(
    "PROBE_LLM_9B_MODEL", "huihui-ai/Huihui-Qwen3.5-9B-abliterated-MLX-4bit"
)
N = 5
# 与 A 线回复形状同量级的 prompt(~300 token):量的是 prefill,不是 1-token 玩具。
PROMPT_4B = "请用一句话总结以下情况:" + "客户来电查询快递理赔进度，" * 40


async def _prefill_ms(client, base: str, model: str) -> float:
    t0 = time.monotonic()
    r = await client.post(
        f"{base}/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": PROMPT_4B}],
            "max_tokens": 1,
            "temperature": 0,
        },
        timeout=60,
    )
    r.raise_for_status()
    return (time.monotonic() - t0) * 1000


async def main() -> int:
    import httpx

    async with httpx.AsyncClient() as client:
        m4, m9 = MODEL_4B, MODEL_9B
        print(f"[contend] 4B={BASE_4B} model={m4!r}")
        print(f"[contend] 9B={BASE_9B} model={m9!r}")

        alone = [await _prefill_ms(client, BASE_4B, m4) for _ in range(N)]
        await asyncio.sleep(1.0)

        async def _nine_busy() -> None:
            try:
                await client.post(
                    f"{BASE_9B}/chat/completions",
                    json={
                        "model": m9,
                        "messages": [{"role": "user", "content": "请详细说明快递理赔的完整流程与注意事项。"}],
                        "max_tokens": 256,
                        "temperature": 0,
                    },
                    timeout=120,
                )
            except Exception as exc:  # noqa: BLE001 - 9B 腿失败=报告并退出非零
                raise RuntimeError(f"9B busy 请求失败: {exc!r}") from exc

        async def _probes_under_load(out: list[float]) -> None:
            await asyncio.sleep(0.3)  # 等 9B prefill 起跑再开始采样
            for _ in range(N):
                out.append(await _prefill_ms(client, BASE_4B, m4))

        contended: list[float] = []
        await asyncio.wait_for(
            asyncio.gather(_nine_busy(), _probes_under_load(contended)), timeout=180
        )

        a50 = statistics.median(alone)
        c50 = statistics.median(contended)
        print(f"\n[contend] 4B prefill alone     n={N} vals={[f'{v:.0f}' for v in alone]} p50={a50:.0f}ms")
        print(f"[contend] 4B prefill contended n={len(contended)} vals={[f'{v:.0f}' for v in contended]} p50={c50:.0f}ms")
        print(f"[contend] 膨胀 = {c50 - a50:+.0f}ms ({(c50 / a50 - 1) * 100:+.0f}%)")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
