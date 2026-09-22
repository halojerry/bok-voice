"""DeepSeek「思考开关」契约与三面接线（2026-09-21）。

背景（用户口径）：沉淀/纪要是**非实时**后台重活，思考开着更准；**对话**LLM 关思考
换首字延迟。官方文档事实与实测见 ``bok_voice_core.deepseek_llm`` 模块 docstring。

三个面各钉一组：
1. 纯函数判端点与档位（含伪域名 ``api.deepseek.com.attacker.tld`` 必须拒绝）；
2. 对话侧 ``DeepSeekLLM`` 把档位放进 ``extra_body``（官方：OpenAI SDK 必须走这里）；
3. 纪要侧 ``Summarizer`` 显式开思考**并**抬 token 预算（思考与正文共用预算，
   512 不够会整段烧在 reasoning 上、content 出空串 → 静默退 ``_fallback``）。
"""

from __future__ import annotations

import httpx
import pytest

from bok_voice_core.deepseek_llm import (
    is_deepseek_endpoint,
    thinking_extra_body,
)

DS = "https://api.deepseek.com/v1"


# ---------------------------------------------------------------- 1. 纯函数


def test_deepseek_endpoint_defaults_to_thinking_disabled():
    """官方 `thinking` 默认 enabled，而我们的 max_tokens 很小 → 缺省必须是关。"""
    assert thinking_extra_body(DS) == {"thinking": {"type": "disabled"}}
    assert thinking_extra_body(DS, "") == {"thinking": {"type": "disabled"}}
    assert thinking_extra_body(DS, "  ") == {"thinking": {"type": "disabled"}}


def test_deepseek_endpoint_explicit_enabled_wins():
    """沉淀/纪要侧显式开思考。"""
    assert thinking_extra_body(DS, "enabled") == {"thinking": {"type": "enabled"}}
    assert thinking_extra_body(DS, "ENABLED") == {"thinking": {"type": "enabled"}}


def test_unknown_mode_falls_back_to_disabled():
    """非法档位不许静默变 enabled（官方默认就是 enabled，写错档位=又想烧预算）。"""
    for junk in ("banana", "off", "0", "true"):
        assert thinking_extra_body(DS, junk) == {"thinking": {"type": "disabled"}}


def test_local_endpoints_get_no_thinking_field():
    """本地 MLX（:1235/:1236/:1237）不认识该字段，多发一个未知键是纯风险。"""
    for url in (
        "http://127.0.0.1:1235/v1",
        "http://localhost:1237/v1",
        "http://192.168.1.9:1236/v1",
        "",
        "not-a-url",
    ):
        assert thinking_extra_body(url) == {}
        assert thinking_extra_body(url, "enabled") == {}
        assert is_deepseek_endpoint(url) is False


def test_lookalike_host_is_rejected():
    """裸 endswith 会把伪域名当官方——必须用「等于或点号后缀」判定。"""
    assert is_deepseek_endpoint("https://api.deepseek.com.attacker.tld/v1") is False
    assert thinking_extra_body("https://api.deepseek.com.attacker.tld/v1") == {}
    assert is_deepseek_endpoint("https://notapi.deepseek.com.evil.io/v1") is False


def test_real_subdomain_and_case_and_space_are_accepted():
    for url in (
        "https://foo.api.deepseek.com/v1",
        "https://API.DeepSeek.COM/v1",
        "  https://api.deepseek.com/v1  ",
    ):
        assert is_deepseek_endpoint(url) is True, url
        assert thinking_extra_body(url) == {"thinking": {"type": "disabled"}}


# ---------------------------------------------------------------- 2. 对话侧


def _conversation_llm(base_url: str):
    from agent_runtime.providers.livekit_plugins import DeepSeekLLM

    return DeepSeekLLM(api_key="k", model="deepseek-flash", base_url=base_url)


def test_conversation_llm_disables_thinking_by_default(monkeypatch):
    """通话侧 max_tokens 只有 160：思考开着=整段烧在 reasoning 上、正文空串（静默哑火）。"""
    monkeypatch.delenv("DEEPSEEK_THINKING", raising=False)
    body = _conversation_llm(DS)._opts.extra_body
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 160


def test_conversation_llm_env_can_enable_thinking(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_THINKING", "enabled")
    assert _conversation_llm(DS)._opts.extra_body["thinking"] == {"type": "enabled"}


def test_conversation_llm_local_endpoint_has_no_thinking_key(monkeypatch):
    """同一类指向本地端点（显式配置）时不得多出 thinking 键。"""
    monkeypatch.delenv("DEEPSEEK_THINKING", raising=False)
    body = _conversation_llm("http://127.0.0.1:1235/v1")._opts.extra_body
    assert "thinking" not in body


def test_default_model_is_not_the_deprecated_alias():
    """`deepseek-chat` 官方 2026-07-24 停用——缺省名不得再指向它。"""
    assert _conversation_llm(DS)._opts.model == "deepseek-flash"


# ---------------------------------------------------------------- 3. 纪要侧


def _turn(role: str, text: str):
    class _T:
        def __init__(self, role: str, transcript: str):
            self.role = role
            self.transcript = transcript

    return _T(role, text)


_TURNS = [_turn("user", "你好，我想了解一下你们的产品。"), _turn("assistant", "好的。")]


def _capture(monkeypatch) -> dict:
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": '{"summary":"s","new_topics":[],"insight":null}'}}]}

    def fake_post(url, json=None, timeout=None, headers=None):
        captured["url"] = url
        captured["payload"] = json
        captured["timeout"] = timeout
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    return captured


def test_settle_enables_thinking_and_raises_budget(monkeypatch):
    """纪要=非实时：思考显式开，且预算要容得下「思考+JSON 正文」。"""
    monkeypatch.delenv("BOK_SETTLE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("BOK_SETTLE_LLM_MODEL", raising=False)
    captured = _capture(monkeypatch)

    from control_plane.summarize import Summarizer

    settings = {"llm": {"provider": "deepseek", "base_url": DS, "model": "deepseek-v4-pro"}}
    result = Summarizer().build(_TURNS, {"object_id": "o", "account_id": "a"}, settings)

    assert result["summary"] == "s"
    assert captured["payload"]["thinking"] == {"type": "enabled"}
    assert captured["payload"]["max_tokens"] == 2048


def test_settle_local_endpoint_payload_unchanged(monkeypatch):
    """本地端点逐字节同旧：无 thinking 键、max_tokens 仍是 512。"""
    monkeypatch.delenv("BOK_SETTLE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("BOK_SETTLE_LLM_MODEL", raising=False)
    captured = _capture(monkeypatch)

    from control_plane.summarize import Summarizer

    settings = {"llm": {"provider": "local_openai", "base_url": "http://127.0.0.1:1237/v1", "model": "/m"}}
    Summarizer().build(_TURNS, {"object_id": "o", "account_id": "a"}, settings)

    assert "thinking" not in captured["payload"]
    assert captured["payload"]["max_tokens"] == 512


def test_settle_thinking_raises_timeout_too(monkeypatch):
    """光抬预算不抬超时=结构上跑不通（实测 v4-pro 思考档 5/5 ReadTimeout）。"""
    monkeypatch.delenv("BOK_SETTLE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("BOK_SETTLE_LLM_MODEL", raising=False)
    monkeypatch.delenv("BOK_SETTLE_THINKING_TIMEOUT_S", raising=False)
    captured = _capture(monkeypatch)

    from control_plane.summarize import Summarizer

    settings = {"llm": {"provider": "deepseek", "base_url": DS, "model": "deepseek-v4-pro"}}
    Summarizer(timeout=15.0).build(_TURNS, {"object_id": "o", "account_id": "a"}, settings)

    assert captured["timeout"] >= 90.0
    assert captured["payload"]["thinking"] == {"type": "enabled"}


def test_settle_sends_bearer_when_key_present(monkeypatch):
    """云端 settle 的凭据口：先前不带 Authorization，云端点一律 401（换云走不通）。"""
    monkeypatch.delenv("BOK_SETTLE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("BOK_SETTLE_LLM_MODEL", raising=False)
    monkeypatch.delenv("BOK_SETTLE_LLM_API_KEY", raising=False)
    captured = _capture(monkeypatch)

    from control_plane.summarize import Summarizer

    settings = {"llm": {"provider": "deepseek", "base_url": DS, "model": "m", "api_key": "sk-x"}}
    Summarizer().build(_TURNS, {"object_id": "o", "account_id": "a"}, settings)

    assert captured["headers"] == {"Authorization": "Bearer sk-x"}


def test_settle_mlx_placeholder_is_not_a_credential(monkeypatch):
    """设置页本地卡存的是哨兵 `"mlx"`（不是凭据）——不许变成 Authorization 头。"""
    monkeypatch.delenv("BOK_SETTLE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("BOK_SETTLE_LLM_MODEL", raising=False)
    monkeypatch.delenv("BOK_SETTLE_LLM_API_KEY", raising=False)
    captured = _capture(monkeypatch)

    from control_plane.summarize import Summarizer

    settings = {"llm": {"provider": "local_openai", "base_url": "http://127.0.0.1:1237/v1", "model": "/m", "api_key": "mlx"}}
    Summarizer().build(_TURNS, {"object_id": "o", "account_id": "a"}, settings)

    assert captured["headers"] is None


def test_settle_broken_json_falls_back_with_a_visible_log(monkeypatch, capsys):
    """模型吐坏 JSON 时退指标摘要，但**必须留痕**——静默是这类丢数据藏最久的原因。"""
    monkeypatch.delenv("BOK_SETTLE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("BOK_SETTLE_LLM_MODEL", raising=False)

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            # 本机 9B 实测的真实坏法：结构齐全但缺逗号
            return {"choices": [{"message": {"content": '{"summary":"s"\n"new_topics":[]}'}}]}

    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None, headers=None: _Resp())

    from control_plane.summarize import Summarizer

    settings = {"llm": {"provider": "local_openai", "base_url": "http://127.0.0.1:1237/v1", "model": "/m"}}
    result = Summarizer().build(_TURNS, {"object_id": "o", "account_id": "a"}, settings)

    assert result["new_topics"] == []  # 走了 fallback
    out = capsys.readouterr().out
    assert "解析失败" in out
    assert "new_topics/insight 全丢" in out


# ---------------------------------------------------------------- 4. 判据侧


def test_judge_passes_thinking_body_for_deepseek(monkeypatch):
    """判据换云：max_tokens 只有 8-32，必须带「缺省关思考」否则静默全 miss。"""
    import asyncio

    from agent_runtime.agent import _llm_judge

    captured: dict = {}

    class _Choice:
        message = type("M", (), {"content": "advance"})()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _Resp()

    class _Client:
        chat = type("C", (), {"completions": _Completions()})()

        def __init__(self, **kwargs):
            captured["client"] = kwargs

    monkeypatch.delenv("FLOW_JUDGE_LLM_THINKING", raising=False)
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", _Client)

    out = asyncio.run(_llm_judge(DS, "deepseek-flash", [{"role": "user", "content": "hi"}]))
    assert out == "advance"
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}


def test_judge_local_endpoint_sends_empty_extra_body(monkeypatch):
    import asyncio

    from agent_runtime.agent import _llm_judge

    captured: dict = {}

    class _Choice:
        message = type("M", (), {"content": "advance"})()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _Resp()

    class _Client:
        chat = type("C", (), {"completions": _Completions()})()

        def __init__(self, **kwargs):
            pass

    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", _Client)

    asyncio.run(_llm_judge("http://127.0.0.1:1237/v1", "local", [{"role": "user", "content": "hi"}]))
    assert captured["extra_body"] == {}
