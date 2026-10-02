"""DeepSeek 官方「思考开关」实测（2026-09-21）。

官方文档事实（api-docs.deepseek.com/api/create-chat-completion）——本探针的判据来源：

  - `thinking` 是 `/chat/completions` 的**请求体字段**，`{"type": "enabled"|"disabled"}`，
    **默认 enabled**；用 OpenAI SDK 时**必须放进 `extra_body`**（SDK 本身不认识该字段）。
  - `reasoning_effort` 同样控开关：`none`=关，`low`/`high`/`max`=开，默认 `high`。
  - 模型名 `deepseek-v4-flash` / `deepseek-v4-pro`；旧 `deepseek-chat` / `deepseek-reasoner`
    **2026-07-24 停用**，过渡期分别指向 v4-flash 的非思考 / 思考模式。
  - 磁盘前缀缓存自动生效（**从第 0 个 token 起严格前缀**才算命中），
    命中 $0.014/M vs 未命中 $0.14/M；`usage.prompt_cache_hit_tokens` 可读。

本探针量三件事：
  ① 关思考后首 content 字延迟 + content 是否非空（此前踩过「默认思考开→content 为空
     且 finish=length」的坑，通话侧会静默哑火）；
  ② 默认（思考开）的 reasoning_tokens 占比，确认它确实是那笔浪费；
  ③ 同一长前缀二次请求的 prompt_cache_hit_tokens，对齐我们「静态前缀 1692 tok + 尾部
     追加」的请求形状。

key 只从环境变量 `DEEPSEEK_API_KEY` 读，绝不落盘。用法：
    DEEPSEEK_API_KEY=... runtime/python/bin/python scripts/probe_deepseek_thinking.py [模型名...]
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from openai import AsyncOpenAI

BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

# 候选模型：旧别名 + 新名，实测看哪些还在线。
CANDIDATES = ["deepseek-v4-flash", "deepseek-flash", "deepseek-v4-pro", "deepseek-chat"]

ASK = "用一句话回答：你们的实时语音客服支持粤语吗？"

# 复刻真实通话请求形状：一段长静态前缀（≈真实 1692 tok 量级）+ 一句问话。
_PREFIX_UNIT = (
    "你是拨出电话客服助理，通话语言全程粤语，中途不切换；"
    "客户如果质疑身份，先简短安抚再重申官方可回拨渠道，每通只详答一次；"
    "赔偿金额档位只在客户主动询问赔偿时说明，其余轮次一律不报数字；"
)
_LONG_PREFIX = (_PREFIX_UNIT * 40).strip()


def _client() -> AsyncOpenAI:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise SystemExit("DEEPSEEK_API_KEY missing（只走 env，不落盘）")
    return AsyncOpenAI(api_key=key, base_url=BASE_URL, timeout=90.0, max_retries=0)


async def list_models(c: AsyncOpenAI) -> list[str]:
    try:
        r = await c.models.list()
        return [m.id for m in r.data]
    except Exception as exc:  # noqa: BLE001 - 列表失败不影响后续点名实测
        print(f"models: 列表失败 {exc!r}")
        return []


async def timed(
    c: AsyncOpenAI,
    model: str,
    thinking: str | None,
    *,
    messages: list[dict] | None = None,
    label: str = "",
) -> dict | None:
    """一次流式请求，量首 content 字延迟 / 总延迟 / usage。thinking=None 表示不发该字段。"""
    body: dict = {}
    if thinking:
        body["thinking"] = {"type": thinking}
    msgs = messages or [{"role": "user", "content": ASK}]
    t0 = time.monotonic()
    first: float | None = None
    parts: list[str] = []
    usage = None
    try:
        stream = await c.chat.completions.create(
            model=model,
            messages=msgs,
            max_tokens=300,
            temperature=0.35,
            stream=True,
            stream_options={"include_usage": True},
            extra_body=body,
        )
        async for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if chunk.choices:
                delta = chunk.choices[0].delta
                text = getattr(delta, "content", None)
                if text:
                    if first is None:
                        first = (time.monotonic() - t0) * 1000
                    parts.append(text)
    except Exception as exc:  # noqa: BLE001 - 实测探针，失败要看见
        print(f"  ✗ {label or model} thinking={thinking or '默认'} 请求失败: {exc!r}")
        return None
    total = (time.monotonic() - t0) * 1000
    content = "".join(parts).strip()
    reasoning = 0
    if usage is not None:
        detail = getattr(usage, "completion_tokens_details", None)
        reasoning = int(getattr(detail, "reasoning_tokens", 0) or 0) if detail else 0
    tag = label or f"{model} thinking={thinking or '默认'}"
    print(
        f"  {'✓' if content else '✗空'} {tag:<38} "
        f"首字={first if first is None else round(first)}ms 总={round(total)}ms "
        f"len={len(content)} reasoning_tok={reasoning} "
        f"prompt_tok={getattr(usage, 'prompt_tokens', '?') if usage else '?'} "
        f"finish={(usage and 'ok') or '?'}"
    )
    if content:
        print(f"      正文: {content[:70]}")
    return {"content": content, "first_ms": first, "total_ms": total, "reasoning_tok": reasoning}


async def cache_probe(c: AsyncOpenAI, model: str, thinking: str | None) -> None:
    """同一长前缀连打两次：第二发应命中官方磁盘前缀缓存。"""
    print(f"\n[前缀缓存] {model} thinking={thinking or '默认'} 前缀 {len(_LONG_PREFIX)} 字")
    msgs = [
        {"role": "system", "content": _LONG_PREFIX},
        {"role": "user", "content": ASK},
    ]
    for i in (1, 2):
        body: dict = {}
        if thinking:
            body["thinking"] = {"type": thinking}
        try:
            r = await c.chat.completions.create(
                model=model,
                messages=msgs,
                max_tokens=80,
                temperature=0.35,
                extra_body=body,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  第{i}发 ✗ {exc!r}")
            return
        u = r.usage
        hit = int(getattr(u, "prompt_cache_hit_tokens", 0) or 0)
        miss = int(getattr(u, "prompt_cache_miss_tokens", 0) or 0)
        total = int(getattr(u, "prompt_tokens", 0) or 0)
        ratio = (hit / total * 100) if total else 0.0
        print(
            f"  第{i}发 prompt={total} hit={hit} miss={miss} "
            f"命中率={ratio:.1f}% content_len={len((r.choices[0].message.content or '').strip())}"
        )


async def main() -> int:
    c = _client()
    online = await list_models(c)
    print(f"models: {online}")

    wanted = sys.argv[1:] or [m for m in CANDIDATES if not online or m in online]
    if not wanted:
        print("没有可用模型，退出")
        return 1

    print("\n[思考开关] 每模型打三态：关 / 默认 / 显式开")
    ok_model: str | None = None
    for m in wanted:
        print(f"\n— {m}")
        for thinking in ("disabled", None, "enabled"):
            res = await timed(c, m, thinking)
            if res and res["content"] and thinking == "disabled":
                ok_model = ok_model or m

    if ok_model:
        await cache_probe(c, ok_model, "disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
