#!/usr/bin/env python3
"""DeepSeek 云端探活（A线优化总计划 · 批次0.5）。

用法：
    DEEPSEEK_API_KEY=... .venv/bin/python scripts/probe_deepseek_cloud.py

凭据只从环境变量读，不落任何文件；报告写 reports/deepseek-cloud/<ts>.json。

步骤（对应计划 0.5）：
  1) GET /models —— 确认模型清单（deepseek-flash 是否在线）
  2) 流式 TTFT：小 prompt ×3（地板）+ 大 prompt ~2.5k tok 连发两遍
     （第一遍 cache miss=最坏、第二遍应命中缓存）+ 思考关闭走 body.thinking
  3) max_tokens=160 正文非空校验（防「思考烧预算出空串」的 2026-09-21 坑）
  4) prefix 续写 warm 实验（beta）：对照组 vs 预热组的 prompt_cache_hit_tokens
     —— 预热请求 [system, assistant(greeting) prefix=True]（max_tokens=1），
     真实请求 [system, assistant(greeting), user] 读命中读数；两组用不同
     salt 前缀隔离缓存域，避免互污。
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
BASE = (os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").strip() or "").rstrip("/")
BETA = (os.environ.get("DEEPSEEK_BETA_BASE_URL", "https://api.deepseek.com/beta").strip() or "").rstrip("/")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash").strip() or "deepseek-flash"
TIMEOUT = float(os.environ.get("DEEPSEEK_PROBE_TIMEOUT", "90") or 90)

REPORT_DIR = Path(__file__).resolve().parents[1] / "reports" / "deepseek-cloud"


def _headers() -> dict:
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def _thinking_disabled() -> dict:
    """缺省关思考（仓库契约 bok_voice_core.deepseek_llm：预算小，思考烧空正文）。"""
    return {"thinking": {"type": "disabled"}}


def list_models(client: httpx.Client) -> dict:
    r = client.get(f"{BASE}/models", headers=_headers())
    r.raise_for_status()
    data = r.json()
    return {"status": r.status_code, "ids": [m.get("id") for m in data.get("data", [])]}


def stream_once(client: httpx.Client, messages: list[dict], *, max_tokens: int, tag: str) -> dict:
    """一发流式请求：测 TTFT（首个正文 delta）与总时长，读最终 chunk 的 usage。"""
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": 0.3,
        **_thinking_disabled(),
    }
    out = {"tag": tag, "ttft_ms": None, "total_ms": None, "content_chars": 0,
           "reasoning_chars": 0, "usage": None, "finish_reason": None, "error": None}
    t0 = time.perf_counter()
    try:
        with client.stream("POST", f"{BASE}/chat/completions", headers=_headers(), json=payload) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                body = line[len("data:"):].strip()
                if body == "[DONE]":
                    break
                try:
                    chunk = json.loads(body)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content") or ""
                    reasoning = delta.get("reasoning_content") or ""
                    if content and out["ttft_ms"] is None:
                        out["ttft_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                    out["content_chars"] += len(content)
                    out["reasoning_chars"] += len(reasoning)
                    if choices[0].get("finish_reason"):
                        out["finish_reason"] = choices[0]["finish_reason"]
                if chunk.get("usage"):
                    out["usage"] = chunk["usage"]
    except Exception as exc:  # noqa: BLE001 - 探针失败是数据
        out["error"] = repr(exc)
    out["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


def nonempty_check(client: httpx.Client) -> dict:
    """max_tokens=160 非流式：确认正文非空（思考关的验收线）。"""
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "用一句话回答：1+1 等于几？"}],
        "max_tokens": 160,
        "temperature": 0.3,
        **_thinking_disabled(),
    }
    r = client.post(f"{BASE}/chat/completions", headers=_headers(), json=payload)
    r.raise_for_status()
    data = r.json()
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    return {
        "content": (msg.get("content") or "")[:80],
        "content_len": len(msg.get("content") or ""),
        "reasoning_len": len(msg.get("reasoning_content") or ""),
        "usage": data.get("usage"),
    }


def _big_block(salt: str, approx_tokens: int = 2500) -> str:
    """近似 ~2.5k token 的中文块（≈1.7 字符/token），salt 置头保证缓存未命中。"""
    unit = (
        "我们是集运中转仓客服团队，负责处理跨境包裹的丢失、延误与理赔核对工作，"
        "每一通电话都需要先核对客户身份与订单信息，再按服务协议约定的口径说明赔付方案，"
        "全程遵守合规话术，不承诺超出协议范围的赔偿，也不引导客户离开官方渠道沟通。"
    )
    target_chars = int(approx_tokens * 1.7)
    body = [f"[probe-salt:{salt}]"]
    while sum(len(x) for x in body) < target_chars:
        body.append(unit)
    return "".join(body)


def prefix_warm_experiment(client: httpx.Client) -> dict:
    """对照组 vs 预热组：读两组真实请求的 prompt_cache_hit_tokens。"""
    salt = uuid.uuid4().hex[:12]
    system = f"[probe-{salt}] " + _big_block(salt, approx_tokens=900) + "\n以上是背景资料。"
    greeting_a = f"[probe-{salt}-A] 你好，我是集运客服，想和您核对一件包裹的情况，耽误一分钟可以吗？"
    greeting_b = f"[probe-{salt}-B] 您好呀，这里是客服中心，有一个包裹的异常需要跟您确认一下，方便吗？"
    user_msg = "你好，请问有什么事？"
    out: dict = {"salt": salt}

    # 对照组：未预热，直接真实形状请求（B 前缀）
    ctrl = client.post(
        f"{BASE}/chat/completions", headers=_headers(),
        json={"model": MODEL, "messages": [
            {"role": "system", "content": system},
            {"role": "assistant", "content": greeting_b},
            {"role": "user", "content": user_msg},
        ], "max_tokens": 16, "temperature": 0.3, **_thinking_disabled()},
    )
    ctrl.raise_for_status()
    ctrl_usage = ctrl.json().get("usage") or {}
    out["control_hit_tokens"] = ctrl_usage.get("prompt_cache_hit_tokens")
    out["control_miss_tokens"] = ctrl_usage.get("prompt_cache_miss_tokens")
    out["control_prompt_tokens"] = ctrl_usage.get("prompt_tokens")

    # 预热组：先发 prefix 续写（beta，assistant prefix=True，max_tokens=1）
    warm = client.post(
        f"{BETA}/chat/completions", headers=_headers(),
        json={"model": MODEL, "messages": [
            {"role": "system", "content": system},
            {"role": "assistant", "content": greeting_a, "prefix": True},
        ], "max_tokens": 1, "temperature": 0.3, **_thinking_disabled()},
    )
    warm.raise_for_status()
    warm_usage = warm.json().get("usage") or {}
    out["warm_prompt_tokens"] = warm_usage.get("prompt_tokens")
    time.sleep(1.5)  # 给缓存落地留一拍
    real = client.post(
        f"{BASE}/chat/completions", headers=_headers(),
        json={"model": MODEL, "messages": [
            {"role": "system", "content": system},
            {"role": "assistant", "content": greeting_a},
            {"role": "user", "content": user_msg},
        ], "max_tokens": 16, "temperature": 0.3, **_thinking_disabled()},
    )
    real.raise_for_status()
    real_usage = real.json().get("usage") or {}
    out["warm_hit_tokens"] = real_usage.get("prompt_cache_hit_tokens")
    out["warm_miss_tokens"] = real_usage.get("prompt_cache_miss_tokens")
    out["warm_prompt_tokens_total"] = real_usage.get("prompt_tokens")
    return out


def main() -> int:
    if not API_KEY:
        print("ERROR: DEEPSEEK_API_KEY 未设置（凭据只从环境变量读）", file=sys.stderr)
        return 2
    result: dict = {"ts": int(time.time()), "model": MODEL, "base": BASE, "beta": BETA}
    with httpx.Client(timeout=TIMEOUT) as client:
        try:
            result["models"] = list_models(client)
            print(f"[models] {result['models']}", flush=True)
        except Exception as exc:  # noqa: BLE001
            result["models"] = {"error": repr(exc)}
            print(f"[models] ERROR {exc!r}", flush=True)
            return 1

        small = [{"role": "user", "content": "你好，请用一句话介绍你自己。"}]
        result["small_runs"] = []
        for i in range(3):
            run = stream_once(client, small, max_tokens=64, tag=f"small-{i + 1}")
            result["small_runs"].append(run)
            print(f"[small-{i + 1}] ttft={run['ttft_ms']}ms total={run['total_ms']}ms "
                  f"chars={run['content_chars']} reasoning={run['reasoning_chars']} err={run['error']}", flush=True)

        big_salt = uuid.uuid4().hex[:12]
        big = [{"role": "system", "content": _big_block(big_salt)},
               {"role": "user", "content": "请用一句话概括上面的工作范围。"}]
        result["big_runs"] = []
        for i in range(2):
            run = stream_once(client, big, max_tokens=64, tag=f"big-{i + 1}-{'miss' if i == 0 else 'hit?'}")
            result["big_runs"].append(run)
            hit = (run.get("usage") or {}).get("prompt_cache_hit_tokens")
            miss = (run.get("usage") or {}).get("prompt_cache_miss_tokens")
            print(f"[big-{i + 1}] ttft={run['ttft_ms']}ms total={run['total_ms']}ms "
                  f"hit={hit} miss={miss} err={run['error']}", flush=True)

        try:
            ne = nonempty_check(client)
            result["nonempty"] = ne
            print(f"[nonempty] len={ne['content_len']} reasoning_len={ne['reasoning_len']} "
                  f"content={ne['content']!r}", flush=True)
        except Exception as exc:  # noqa: BLE001
            result["nonempty"] = {"error": repr(exc)}
            print(f"[nonempty] ERROR {exc!r}", flush=True)

        try:
            pw = prefix_warm_experiment(client)
            result["prefix_warm"] = pw
            print(f"[prefix-warm] control: hit={pw.get('control_hit_tokens')} miss={pw.get('control_miss_tokens')} "
                  f"prompt={pw.get('control_prompt_tokens')} | warm: hit={pw.get('warm_hit_tokens')} "
                  f"miss={pw.get('warm_miss_tokens')} prompt={pw.get('warm_prompt_tokens_total')} "
                  f"(warm_req_prompt={pw.get('warm_prompt_tokens')})", flush=True)
        except Exception as exc:  # noqa: BLE001
            result["prefix_warm"] = {"error": repr(exc)}
            print(f"[prefix-warm] ERROR {exc!r}", flush=True)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{result['ts']}-deepseek-cloud.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[report] {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
