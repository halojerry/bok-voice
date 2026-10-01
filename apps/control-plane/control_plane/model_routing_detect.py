"""本地 LLM 端点自动发现（2026-09-27，Ethan 拍板：本地车道免手打端口/模型名）。

模型路由（`global_settings.model_routing_json`）配本地车道时要手敲 `base_url`
（`http://127.0.0.1:1235` 等）与手抄 `/models` 里的模型 id——本条给设置面一个
「检测本地端点」按钮的后端：并行探已知候选端点的 OpenAI 兼容 `/v1/models`，
把可达端点与模型清单回给出仓面（web 一键填入车道草稿，**检测绝不落库**）。

纯数据面，零业务状态；`probe_endpoint` 任何网络/协议失败都折成数据（`ok=False`），
**绝不 raise**；出仓字典**永不含 api_key 材料**（候选只由 base_url 组成）。

依赖注入友好：模块级 `import httpx`（测试 monkeypatch 本模块的 `httpx` 名字即可
换成 `httpx.MockTransport` 工厂，不碰全局 httpx——`TestClient` 也吃全局 httpx）。
"""
from __future__ import annotations

import asyncio
from typing import Any, Mapping

import httpx

__all__ = ["DEFAULT_LOCAL_BASE_URLS", "candidate_base_urls", "probe_endpoint", "detect_local"]

# 本地侧车/服务缺省端点（与 bok.py / CP env 链约定一致）：
#   1235 = MLX 主 LLM（a_reply/judge/settle/mining 缺省）、
#   1236 = MT 专用、1237 = settle/judge 9B 专线、
#   18100/18101 = CUDA 部署 sglang-a / sglang-judge（docs/CUDA-DEPLOY.md）。
# `_api_base` 会把无版本段的值补成 `/v1`（base_url 消费点 `f"{base}/chat/completions"`）。
DEFAULT_LOCAL_BASE_URLS: tuple[str, ...] = (
    "http://127.0.0.1:1235",
    "http://127.0.0.1:1236",
    "http://127.0.0.1:1237",
    "http://127.0.0.1:18100",
    "http://127.0.0.1:18101",
)


def _api_base(raw: Any) -> str:
    """规范化端点基址：strip + 去尾斜杠 + 缺版本段补 `/v1`。

    路由表里存的 base_url 恒含 `/v1`（`model_routes._MLX_DEFAULT` 同款），而候选
    缺省值写的是裸 `host:port`——统一成「可直接填进车道、`f"{base}/chat/completions"`
    即通」的形状，回给出仓面的 base_url 因此对 UI 直接可用。空/非法输入回空串。
    """
    base = str(raw or "").strip()
    if not base:
        return ""
    base = base.rstrip("/")
    if not base:
        return ""
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def candidate_base_urls(routing_raw: str | Mapping[str, Any] | None, env: Mapping[str, Any]) -> list[str]:
    """候选端点，去重且保序：(a) env MLX_LLM_BASE_URL、 (b) 本地缺省端口、
    (c) 当前路由表 lanes+presets 里出现过的每个 base_url（活性复查）。

    去重按规范化后的 API 基址（`:1235` 与 `:1235/v1` 视为同一端点），命中即跳过，
    保证同一端点只探一次、且顺序稳定（env → 缺省 → 路由表）。
    """
    ordered: list[str] = []

    def add(raw: Any) -> None:
        base = _api_base(raw)
        if base and base not in ordered:
            ordered.append(base)

    add(env.get("MLX_LLM_BASE_URL"))
    for default in DEFAULT_LOCAL_BASE_URLS:
        add(default)
    for cfg in _routing_base_urls(routing_raw):
        add(cfg)
    return ordered


def _routing_base_urls(routing_raw: str | Mapping[str, Any] | None) -> list[str]:
    """从路由原始串里抽所有非空 base_url（lanes 在前、presets 在后，保序）。

    宽容解析：坏 JSON/坏结构回空清单，绝不 raise（与运行时 `parse_routing` 同姿态）。
    """
    if isinstance(routing_raw, Mapping):
        data: Any = dict(routing_raw)
    else:
        text = str(routing_raw or "").strip()
        if not text:
            return []
        try:
            import json

            data = json.loads(text)
        except (ValueError, TypeError):
            return []
    if not isinstance(data, Mapping):
        return []

    out: list[str] = []

    def scan(container: Any) -> None:
        if not isinstance(container, Mapping):
            return
        for cfg in container.values():
            if isinstance(cfg, Mapping):
                url = str(cfg.get("base_url") or "").strip()
                if url:
                    out.append(url)

    scan(data.get("lanes"))
    presets = data.get("presets")
    if isinstance(presets, Mapping):
        for preset in presets.values():
            scan(preset)
    return out


async def probe_endpoint(base_url: str, timeout: float = 1.5) -> dict:
    """探单个端点：GET `{base}/v1/models`（OpenAI 兼容，本地侧车无鉴权）。

    返回 `{"base_url", "ok", "models", "error"}`；**绝不 raise**。
    - 2xx：ok=True，models=sorted(id 字符串)；
    - 401：端点活但需鉴权 → ok=True，models=[]，error="auth"（证明活性）；
    - 其它非 2xx / 网络 / 协议错误：ok=False，models=[]，error=人话串。
    """
    base = _api_base(base_url)
    result: dict = {"base_url": base, "ok": False, "models": [], "error": ""}
    if not base:
        result["error"] = "empty base_url"
        return result
    url = f"{base}/models"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
        status = int(resp.status_code)
        if status == 401:
            # 鉴权失败=端点确实在场（活体证明）；不猜模型清单。
            result["ok"] = True
            result["error"] = "auth"
            return result
        if status >= 400:
            result["error"] = f"HTTP {status}"
            return result
        payload = resp.json()
        data = payload.get("data") if isinstance(payload, Mapping) else None
        ids = [
            str(item.get("id") or "")
            for item in (data if isinstance(data, list) else [])
            if isinstance(item, Mapping)
        ]
        result["models"] = sorted(i for i in ids if i)
        result["ok"] = True
        return result
    except Exception as exc:  # noqa: BLE001 - 探活失败是数据不是异常，绝不外抛
        result["error"] = repr(exc)
        return result


async def detect_local(
    routing_raw: str | Mapping[str, Any] | None, env: Mapping[str, Any]
) -> dict:
    """并行探测全部候选端点（`asyncio.gather`，非串行）。

    可达端点排在前面（组内保候选顺序），总墙钟受单个 `timeout` 约束（并发而非累加）。
    出仓：`{"endpoints": [ {base_url, ok, models, error}, ... ]}`——永不含 api_key。
    """
    candidates = candidate_base_urls(routing_raw, env)
    probes = await asyncio.gather(*(probe_endpoint(c) for c in candidates))
    reachable = [p for p in probes if p["ok"]]
    unreachable = [p for p in probes if not p["ok"]]
    return {"endpoints": reachable + unreachable}
