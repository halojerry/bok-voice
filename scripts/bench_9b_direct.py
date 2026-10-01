#!/usr/bin/env python3
"""9B 直连 A/B 台架(2026-09-29):同栈 mlx_lm server、同参、独占 GPU、逐模型顺序测。

问题:Ethan 问「hauhaucs 不走 LM Studio 直连请求试过没?对比 huihui?」
此前矩阵缺格:只测过 huihui 直连 vs hauhaucs 走 LM Studio :1234(LM Studio 三宗罪
混在里面)。本脚本补「hauhaucs 直连」格,与 huihui 同协议对跑。

用法(server 由外部起/停,本脚本只打 HTTP):
  .venv312/bin/python scripts/bench_9b_direct.py \
      --base-url http://127.0.0.1:1238/v1 --model /path/to/model --label hauhaucs

量什么:
  1. TTFT short x5 (~百 tok 上下文)
  2. TTFT long-ctx x5 (~8k tok 前缀;首发=冷 prefill,复发=cache 命中对照)
  3. decode tps (usage 口径)
  4. 消息形状接受面: [s,a,u] / [s,u,a,u] / [s,a](no-user,模板 L79 诊断位)
  5. 质量探针: 粤语话术应答 x2(应答纪律+边界话术服从),全文打印供人判
"""
from __future__ import annotations

import argparse
import json
import time

import httpx

SOP_SYSTEM = (
    "你係電話客服助手,而家幫一間快遞公司打電話俾客戶,通知佢哋有個包裹因爲運輸問題受損,"
    "公司會按平台規則賠償。講粵語,口吻自然禮貌。每次回覆只講一兩句,答完客戶嘅問題之後,"
    "要帶返一句下一步嘅問題引導流程。唔好重複之前講過嘅內容。"
)

PROBES = [
    ("canto_flow", "我想問下我個包裹而家去到邊度啊?幾時先送到?"),
    ("canto_pressure", "你哋係咪想呃我錢?講明賠幾多先,唔係我而家報警。"),
]


def _post_stream(client: httpx.Client, base: str, model: str, messages: list, max_tokens: int):
    t0 = time.perf_counter()
    ttft = None
    text_parts: list[str] = []
    usage = None
    with client.stream(
        "POST",
        f"{base}/chat/completions",
        json={
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "stream": True,
            "stream_options": {"include_usage": True},
        },
        timeout=120.0,
    ) as r:
        if r.status_code != 200:
            body = r.read().decode("utf-8", "replace")[:300]
            return {"error": f"HTTP {r.status_code}: {body}"}
        for line in r.iter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            delta = ""
            try:
                delta = obj["choices"][0]["delta"].get("content") or ""
            except (KeyError, IndexError, TypeError):
                delta = ""
            if delta:
                if ttft is None:
                    ttft = time.perf_counter() - t0
                text_parts.append(delta)
    total = time.perf_counter() - t0
    text = "".join(text_parts)
    comp = (usage or {}).get("completion_tokens") or 0
    prompt = (usage or {}).get("prompt_tokens") or 0
    return {
        "ttft_ms": round(ttft * 1000, 1) if ttft is not None else None,
        "total_ms": round(total * 1000, 1),
        "prompt_tokens": prompt,
        "completion_tokens": comp,
        "tps": round(comp / (total - (ttft or 0)), 1) if comp and ttft else None,
        "text": text,
    }


def _post_once(client: httpx.Client, base: str, model: str, messages: list, max_tokens: int):
    try:
        r = client.post(
            f"{base}/chat/completions",
            json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.0,
            },
            timeout=120.0,
        )
        if r.status_code == 200:
            txt = r.json()["choices"][0]["message"].get("content") or ""
            return "200", txt[:80]
        detail = ""
        try:
            detail = r.json().get("error", {}).get("message", "")[:120]
        except Exception:
            detail = r.text[:120]
        return f"HTTP {r.status_code}", detail
    except Exception as e:  # noqa: BLE001
        return "EXC", str(e)[:120]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True, help="真实模型路径(mlx_lm server 要求)")
    ap.add_argument("--label", required=True)
    ap.add_argument("--rounds", type=int, default=5)
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    with httpx.Client() as client:
        r = client.get(f"{base}/models", timeout=10.0)
        r.raise_for_status()

        print(f"=== {args.label} @ {base} ===")

        # 1) 形状接受面(诊断位,非流式)
        sysm = {"role": "system", "content": SOP_SYSTEM}
        shapes = {
            "[s,a,u] assistant-first+user": [sysm, {"role": "assistant", "content": "陳小姐你好,我係快遞公司嘅。"}, {"role": "user", "content": "你好。"}],
            "[s,u,a,u] 常规多轮": [sysm, {"role": "user", "content": "你好。"}, {"role": "assistant", "content": "陳小姐你好,我係快遞公司嘅。"}, {"role": "user", "content": "咁你搵我有咩事?"}],
            "[s,a] no-user(模板L79)": [sysm, {"role": "assistant", "content": "陳小姐你好。"}],
        }
        print("--- 形状接受面 ---")
        for name, msgs in shapes.items():
            code, info = _post_once(client, base, args.model, msgs, 16)
            print(f"{name:36s} {code:8s} {info}")

        # 2) TTFT short
        print("--- TTFT short (x%d) ---" % args.rounds)
        short_msgs = [
            {"role": "system", "content": SOP_SYSTEM},
            {"role": "user", "content": "你好,請問你搵邊個?"},
        ]
        for i in range(args.rounds):
            res = _post_stream(client, base, args.model, short_msgs, 120)
            if "error" in res:
                print(f"  r{i}: {res['error']}")
                continue
            print(f"  r{i}: ttft={res['ttft_ms']}ms tps={res['tps']} ptok={res['prompt_tokens']} ctok={res['completion_tokens']}")

        # 3) TTFT long-ctx: r0=冷 prefill, r1..=cache 命中
        filler = "".join(
            f"第{i}步:客戶會問唔同嘅問題,你要按流程應對。常見問題包括包裹位置、賠償金額、點樣申請、幾時到賬。回答要保持簡短,唔好超過兩句。"
            for i in range(120)
        )
        long_msgs = [
            {"role": "system", "content": SOP_SYSTEM + "\n流程參考:\n" + filler},
            {"role": "user", "content": "咁我想問下賠償幾多錢啊?"},
        ]
        print("--- TTFT long-ctx (x%d, r0=冷prefill r1+=cache) ---" % args.rounds)
        for i in range(args.rounds):
            res = _post_stream(client, base, args.model, long_msgs, 160)
            if "error" in res:
                print(f"  r{i}: {res['error']}")
                continue
            print(f"  r{i}: ttft={res['ttft_ms']}ms tps={res['tps']} ptok={res['prompt_tokens']} ctok={res['completion_tokens']}")

        # 4) 质量探针(全文打印)
        print("--- 质量探针(粤语话术) ---")
        for tag, q in PROBES:
            res = _post_stream(
                client,
                base,
                args.model,
                [{"role": "system", "content": SOP_SYSTEM}, {"role": "user", "content": q}],
                160,
            )
            if "error" in res:
                print(f"  [{tag}] {res['error']}")
                continue
            print(f"  [{tag}] ttft={res['ttft_ms']}ms")
            print(f"    {res['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
