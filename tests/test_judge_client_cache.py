"""编排审计第二波 FIX 3:judge LLM 客户端生命周期（复用 + 可中止身份）。

病灶（审计 J1）:`_llm_judge` 每次调用都新建 `AsyncOpenAI(...)` 且无人 close
——每发判定泄漏一个 httpx 连接池;且请求不带请求身份，judge 流在 W-ABORT
协议（services/llm-mlx/bok_mlx_server.py:X-Bok-Req-Id 头 + POST /v1/abort）
下**不可中止**——判定超时后服务端生成线程（mlx 单生成线程零取消路径）继续
解码占槽，后续判定排队（:1235 代理透传 x-bok-req-id，见 queue_proxy.py:272，
代理路径端到端可中止）。

修:①模块级 `_JUDGE_CLIENT_CACHE` 按 (base_url, api_key, timeout) 复用客户端
（进程生命周期持有，不中途 close）;②每次调用 mint `_rid`，仅本机档带
`X-Bok-Req-Id` 头;③超时/失败后尽力 `POST /v1/abort`（urllib + to_thread,
全吞）——成功/云端档零打扰。
"""

from __future__ import annotations

import asyncio
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "apps" / "agent"))

import agent_runtime.agent as agent_mod  # noqa: E402

_SRC = (_REPO / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")

_LOCAL = "http://127.0.0.1:1235/v1"
_CLOUD = "https://api.deepseek.com/v1"
_HEX32 = re.compile(r"^[0-9a-f]{32}$")


class _FakeAsyncOpenAI:
    """最小 AsyncOpenAI 替身（镜像 tests/test_intent_judge_runtime.py 形状）。"""

    instances: list["_FakeAsyncOpenAI"] = []
    error: Exception | None = None
    reply: str = "OK"

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.calls: list[dict] = []
        type(self).instances.append(self)

    @property
    def chat(self):
        return SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if type(self).error is not None:
            raise type(self).error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=type(self).reply))])


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    _FakeAsyncOpenAI.instances = []
    _FakeAsyncOpenAI.error = None
    _FakeAsyncOpenAI.reply = "OK"
    agent_mod._JUDGE_CLIENT_CACHE.clear()
    monkeypatch.setattr("openai.AsyncOpenAI", _FakeAsyncOpenAI)
    yield
    agent_mod._JUDGE_CLIENT_CACHE.clear()


def _judge(base_url: str = _LOCAL, **kwargs):
    return asyncio.run(
        agent_mod._llm_judge(base_url, "m", [{"role": "user", "content": "x"}], **kwargs)
    )


# ------------------------------------------------------------------ 客户端复用


def test_client_reused_for_same_lane():
    """同 (base_url, api_key, timeout) 两发 → 同一个客户端实例（池不再泄漏）。"""
    assert _judge() == "OK"
    assert _judge() == "OK"
    assert len(_FakeAsyncOpenAI.instances) == 1
    assert _FakeAsyncOpenAI.instances[0].init_kwargs["base_url"] == _LOCAL
    assert _FakeAsyncOpenAI.instances[0].init_kwargs["max_retries"] == 1


def test_client_distinct_per_base_url():
    _judge(_LOCAL)
    _judge("http://127.0.0.1:1237/v1")
    assert len(_FakeAsyncOpenAI.instances) == 2


def test_client_distinct_per_timeout_budget():
    """timeout 进缓存键:5s（flow judge）与 20s（意图判据）预算不得互相污染。"""
    _judge(_LOCAL)
    _judge(_LOCAL, timeout=20.0)
    assert len(_FakeAsyncOpenAI.instances) == 2


def test_cached_client_not_reused_across_client_class_swap(monkeypatch):
    """替身/热换类安全:缓存的实例类 ≠ 当前解析类 → 重建（测试面防跨档串味）。"""
    _judge(_LOCAL)
    assert len(_FakeAsyncOpenAI.instances) == 1

    class _Other:
        made = 0

        def __init__(self, **kwargs):
            type(self).made += 1
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        async def _create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))])

    monkeypatch.setattr("openai.AsyncOpenAI", _Other)
    assert _judge(_LOCAL) == "OK"
    assert _Other.made == 1, "换类后必须新建，不得把旧类实例交给新调用方"


# ------------------------------------------------------------------ 请求身份


def test_local_req_id_header_injected():
    _judge(_LOCAL)
    headers = _FakeAsyncOpenAI.instances[0].calls[0].get("extra_headers")
    assert headers is not None
    assert _HEX32.match(headers["X-Bok-Req-Id"]), headers


def test_cloud_has_no_req_id_header():
    """云端档零打扰（abort 协议只服务本机 mlx wrapper）。"""
    _judge(_CLOUD)
    assert "extra_headers" not in _FakeAsyncOpenAI.instances[0].calls[0]


def test_req_id_fresh_per_call():
    _judge(_LOCAL)
    _judge(_LOCAL)
    calls = _FakeAsyncOpenAI.instances[0].calls
    assert calls[0]["extra_headers"]["X-Bok-Req-Id"] != calls[1]["extra_headers"]["X-Bok-Req-Id"]


# ------------------------------------------------------------------ 失败即中止


def test_abort_fired_on_timeout_with_minted_rid(monkeypatch):
    """超时后必须按**当发身份**发 abort（身份=请求头里那一个，不是新 mint）。"""
    _FakeAsyncOpenAI.error = TimeoutError("judge timed out")
    fired: list[tuple[str, str]] = []
    monkeypatch.setattr(agent_mod, "_post_abort", lambda base, rid: fired.append((base, rid)))

    async def main():
        out = await agent_mod._llm_judge(_LOCAL, "m", [{"role": "user", "content": "x"}])
        await asyncio.sleep(0.05)  # 让 to_thread 的 abort 任务跑完
        return out

    assert asyncio.run(main()) == ""
    assert len(fired) == 1
    base, rid = fired[0]
    assert base == _LOCAL
    assert rid == _FakeAsyncOpenAI.instances[0].calls[0]["extra_headers"]["X-Bok-Req-Id"]


def test_abort_not_fired_for_cloud(monkeypatch):
    _FakeAsyncOpenAI.error = TimeoutError("judge timed out")
    fired: list[tuple[str, str]] = []
    monkeypatch.setattr(agent_mod, "_post_abort", lambda base, rid: fired.append((base, rid)))

    async def main():
        assert await agent_mod._llm_judge(_CLOUD, "m", [{"role": "user", "content": "x"}]) == ""
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert fired == []


def test_abort_not_fired_on_success(monkeypatch):
    fired: list[tuple[str, str]] = []
    monkeypatch.setattr(agent_mod, "_post_abort", lambda base, rid: fired.append((base, rid)))
    assert _judge(_LOCAL) == "OK"
    assert fired == []


# ------------------------------------------------------------------ abort 载荷形状


class _CapturedRequest(Exception):
    pass


def test_post_abort_posts_server_contract(monkeypatch):
    """载荷形状=services/llm-mlx/bok_mlx_server.py 契约:{root}/v1/abort + {"request_id"}。"""
    seen: dict = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.method
        seen["data"] = req.data
        seen["timeout"] = timeout
        seen["ctype"] = req.get_header("Content-type")
        raise _CapturedRequest("stop here")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    agent_mod._post_abort(_LOCAL, "abc123")  # 绝不 raise（_CapturedRequest 被吞）
    assert seen["url"] == "http://127.0.0.1:1235/v1/abort"
    assert seen["method"] == "POST"
    assert seen["data"] == b'{"request_id": "abc123"}'
    assert seen["ctype"] == "application/json"
    assert seen["timeout"] == 0.5


def test_mlx_abort_endpoint_derivation():
    assert agent_mod._mlx_abort_endpoint(_LOCAL) == "http://127.0.0.1:1235/v1/abort"
    assert agent_mod._mlx_abort_endpoint("http://127.0.0.1:1235") == "http://127.0.0.1:1235/v1/abort"
    assert agent_mod._mlx_abort_endpoint(_LOCAL + "/") == "http://127.0.0.1:1235/v1/abort"
    assert agent_mod._mlx_abort_endpoint("") == ""


def test_post_abort_never_raises(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    agent_mod._post_abort(_LOCAL, "rid")  # 尽力语义:失败全吞


def test_post_abort_noop_without_identity(monkeypatch):
    called = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: called.append(a))
    agent_mod._post_abort("", "rid")
    agent_mod._post_abort(_LOCAL, "")
    assert called == []


# ------------------------------------------------------------------ 源级 pin


def _judge_body() -> str:
    """`_llm_judge` 函数体切片（到下一个模块级定义）——长 docstring 下窗口法不可靠。"""
    i = _SRC.index("async def _llm_judge(")
    j = _SRC.index("\ndef ", i)
    return _SRC[i:j]


def _judge_helpers_block() -> str:
    """judge 客户端/abort 助手块（模块级,`_JUDGE_CLIENT_CACHE` 注释起）。"""
    i = _SRC.index("_JUDGE_CLIENT_CACHE: dict")
    j = _SRC.index("async def _llm_judge(")
    return _SRC[i:j]


def test_source_pin_client_cache_module_level():
    assert "_JUDGE_CLIENT_CACHE: dict" in _SRC
    block = _judge_helpers_block()
    assert "_JUDGE_CLIENT_CACHE.get(key)" in block
    assert "_JUDGE_CLIENT_CACHE[key] = client" in block
    assert "type(client) is not cls" in block, "换类重建守卫（测试替身/热换类安全）"


def test_source_pin_header_gated_on_local():
    body = _judge_body()
    assert "X-Bok-Req-Id" in body
    assert "_local = _is_local_base_url(base_url)" in body
    gate = body.index("if _local:")
    hdr = body.index("extra_headers")
    assert gate < hdr, "身份头必须在本机档门内"


def test_source_pin_abort_in_failure_path():
    body = _judge_body()
    assert "asyncio.to_thread(_post_abort, base_url, _rid)" in body
    assert "FIRE_FORGET_EXEMPT" in body, "尽力中止的裸 create_task 必须带豁免标记（lint 诚实）"
    assert "uuid.uuid4().hex" in body


def test_source_pin_proxy_transparency_note():
    """注释必须写明代理路径可中止（queue_proxy 透传 x-bok-req-id），防后人误删。"""
    i = _SRC.index("judge LLM 客户端生命周期")
    j = _SRC.index("async def _llm_judge(")
    assert "queue_proxy" in _SRC[i:j]
