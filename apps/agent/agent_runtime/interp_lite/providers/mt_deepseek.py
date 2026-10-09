"""DeepSeek MT 客户端（interp_lite；docs-first 直连，无插件包装）。

官方文档基线（本地副本 ``/Users/halo/Documents/bok/deepseek-api_副本.md``，2026-10-09 核对）：
- 端点：``POST {base}/chat/completions``（缺省 base ``https://api.deepseek.com``），Bearer 鉴权。
- 模型：``deepseek-flash`` / ``deepseek-v4-pro``。
- **思考模式默认 enabled 且 effort 默认 high**——MT 小 max_tokens 会被思维链烧空出空串，
  故请求体显式 ``"thinking": {"type": "disabled"}``（文档定案：该字段是请求体成员，
  官方 SDK 才需要 extra_body；本客户端裸 JSON 直发）。
- 流式：``stream=true`` SSE，``data: [DONE]`` 结尾；``stream_options.include_usage=true``
  使末块携带 ``usage``——``prompt_cache_hit_tokens`` 官方回读（静态系统前缀命中量，
  延迟账本直接记账）。思维链关→ ``temperature`` 生效（``top_p`` 非思考模式恒 1.0 忽略）。
- ``finish_reason`` 含 ``aborted``/``content_filter``（观测口径）。

运营加固（非官方要求）：端点 scheme/host 校验（拒环回/私有/保留地址）；持久
AsyncClient 复用连接（逐句翻译每句都付 TLS 握手 = 白付 1-2 个 RTT）。

单消费者契约：B 线每方向一个 FIFO worker，串行调 ``stream()``；``last_metrics``
逐调用覆写，无锁。车道解析经 ``bok_voice_core.model_routes.resolve_route("mt", …)``
单源（openai 档→本客户端；local 档=本地姿势，lite 拒装）。
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

DEEPSEEK_BASE_DEFAULT = "https://api.deepseek.com"
DEEPSEEK_MODEL_DEFAULT = "deepseek-flash"


class MTHTTPError(RuntimeError):
    """非 200 响应（status/body 摘要随异常外抛，供致命错误分类器消费）。"""

    def __init__(self, status: int, body: str):
        self.status = int(status)
        self.body = str(body)[:300]
        super().__init__(f"deepseek mt http {self.status}: {self.body}")


class MTPostureError(RuntimeError):
    """mt 车道非云端档（openai）——lite 是 cloud-only 姿势，装配期拒装。"""


def endpoint_ok(url: str) -> bool:
    """https 公网端点护栏（运营加固）：拒环回/私有/保留/链路本地与非 https。"""
    try:
        parts = urlsplit(url)
    except Exception:  # noqa: BLE001
        return False
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower().strip(".")
    if not host or host == "localhost" or host.endswith((".local", ".internal", ".lan", ".localhost")):
        return False
    try:
        return bool(ipaddress.ip_address(host).is_global)
    except ValueError:
        return True  # 域名：放行（解析在连接层）


def build_messages(instructions: str, pairs: list[tuple[str, str]], text: str) -> list[dict]:
    """单句翻译消息序列（语义镜像旧线 _build_mt_context：system + 滚动「源→译」对 + 当前句）。

    滚动对进消息流（DeepSeek 无工具轮次时 reasoning_content 无关；纯 content 多轮）。
    """
    msgs: list[dict] = [{"role": "system", "content": instructions}]
    for src_t, tgt_t in pairs:
        msgs.append({"role": "user", "content": src_t})
        msgs.append({"role": "assistant", "content": tgt_t})
    msgs.append({"role": "user", "content": text})
    return msgs


class DeepSeekMT:
    """逐句流式翻译客户端。用法（单消费者 FIFO）：

        async for delta in mt.stream(msgs): …
        mt.last_metrics  # {"prompt_cache_hit_tokens": int, "total_tokens": int, "cached_pct": float}
    """

    def __init__(
        self,
        *,
        base_url: str = DEEPSEEK_BASE_DEFAULT,
        model: str = DEEPSEEK_MODEL_DEFAULT,
        api_key: str = "",
        max_tokens: int = 512,
        temperature: float | None = None,
        read_timeout_s: float = 12.0,
        client=None,  # 测试缝：注入 MockTransport 的 AsyncClient（生产缺省自建）
    ):
        self._base = (base_url or DEEPSEEK_BASE_DEFAULT).rstrip("/")
        self._model = model or DEEPSEEK_MODEL_DEFAULT
        self._key = api_key
        self._max_tokens = int(max_tokens)
        # 非思考模式 temperature 生效；None=读 LLM_TEMPERATURE（既有键，显式覆盖口），
        # 缺省 0.3=翻译贴原文的低采样档（旧线 0.7 是 Hy-MT2 推荐，DeepSeek 无此包袱）。
        if temperature is None:
            try:
                temperature = float(os.environ.get("LLM_TEMPERATURE", "") or 0.3)
            except ValueError:
                temperature = 0.3
        self._temperature = float(temperature)
        self._read_timeout_s = float(read_timeout_s)
        self._client = client
        self.last_metrics: dict = {}

    def _ensure_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=self._read_timeout_s, write=5.0, pool=5.0),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):  # 收尾尽力而为
                await self._client.aclose()
            self._client = None

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """SSE 流式翻译：yield 内容增量；流尽后 ``last_metrics`` 就绪。

        非 200 → MTHTTPError（外层致命分类器判 402/401 快败）。
        """
        url = f"{self._base}/chat/completions"
        if not endpoint_ok(self._base):
            raise MTPostureError(f"mt endpoint rejected by guard: {self._base!r}")
        body = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            # 官方默认 enabled+high；小 max_tokens 下思维链烧空预算=静默空串。
            "thinking": {"type": "disabled"},
        }
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        client = self._ensure_client()
        usage: dict = {}
        try:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    raise MTHTTPError(resp.status_code, text)
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except ValueError:
                        continue
                    u = chunk.get("usage")
                    if isinstance(u, dict):
                        usage = u
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        yield delta
        finally:
            hit = int(usage.get("prompt_cache_hit_tokens") or 0)
            total = int(usage.get("prompt_tokens") or 0)
            self.last_metrics = {
                "prompt_cache_hit_tokens": hit,
                "prompt_tokens": total,
                "cached_pct": round(hit / total, 3) if total else 0.0,
            }


def from_routing(routing_raw: str = "") -> DeepSeekMT:
    """经 model_routes mt 车道构造（单源解析；非 openai 档 → MTPostureError）。"""
    from bok_voice_core.model_routes import PROVIDER_OPENAI, resolve_route

    route = resolve_route("mt", os.environ, routing_raw)
    if route.provider != PROVIDER_OPENAI:
        raise MTPostureError(f"mt lane provider={route.provider!r} — interp-lite is cloud-only")
    return DeepSeekMT(
        base_url=route.base_url or DEEPSEEK_BASE_DEFAULT,
        model=route.model or DEEPSEEK_MODEL_DEFAULT,
        api_key=route.api_key or "",
    )
