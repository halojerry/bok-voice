"""Wave MT 单测：StatelessMTLLM 模板/无状态回归 + MlxLlmLLM 采样 env 进 extra_body。

不依赖真实服务：包装层用 llm.LLM 假内芯捕获 chat 入参，采样断言只看 _opts.extra_body。
"""

from __future__ import annotations

import asyncio

from livekit.agents import llm as agents_llm

from agent_runtime.providers.livekit_plugins import MlxLlmLLM, StatelessMTLLM


class _RecorderLLM(agents_llm.LLM):
    """捕获 chat 入参的假内芯（不产流，断言只看收到的 chat_ctx）。"""

    def __init__(self):
        super().__init__()
        self.calls: list[dict] = []

    def chat(self, *, chat_ctx, tools=None, conn_options=None, parallel_tool_calls=None,
             tool_choice=None, extra_kwargs=None):
        self.calls.append(
            {
                "items": [(m.role, m.text_content) for m in chat_ctx.items],
                "tools": tools,
                "conn_options": conn_options,
                "extra_kwargs": extra_kwargs,
            }
        )
        return None


def _conversation_ctx() -> agents_llm.ChatContext:
    ctx = agents_llm.ChatContext()
    ctx.add_message(role="system", content="你是同传引擎。")
    for i in range(3):
        ctx.add_message(role="user", content=f"历史问 {i}")
        ctx.add_message(role="assistant", content=f"历史答 {i}")
    ctx.add_message(role="user", content="hello there")
    return ctx


def test_mt_template_exact_cantonese_and_zh():
    """模板精确断言：历史/system 全丢，只剩一条套官方模板的 user 消息。"""
    inner = _RecorderLLM()
    StatelessMTLLM(inner, "cantonese").chat(chat_ctx=_conversation_ctx())
    assert len(inner.calls) == 1
    assert inner.calls[0]["items"] == [
        (
            "user",
            "将以下文本翻译为 `粤语`，注意只需要输出翻译后的结果，不要额外解释：\n\n`hello there`",
        )
    ]

    zh_inner = _RecorderLLM()
    StatelessMTLLM(zh_inner, "zh").chat(chat_ctx=_conversation_ctx())
    assert zh_inner.calls[0]["items"] == [
        (
            "user",
            "将以下文本翻译为 `中文`，注意只需要输出翻译后的结果，不要额外解释：\n\n`hello there`",
        )
    ]


def test_mt_is_stateless_across_histories():
    """无状态：历史天差地别，只要最后一条 user 文本相同，发给内芯的 ctx 逐字节一致。"""
    inner = _RecorderLLM()
    wrapper = StatelessMTLLM(inner, "en")

    ctx_a = agents_llm.ChatContext()
    ctx_a.add_message(role="system", content="场景 A：集运客服，讲了 40 轮粤语。")
    ctx_a.add_message(role="user", content="之前讲咗一大輪關於包裹破損賠償嘅嘢")
    ctx_a.add_message(role="assistant", content="A 的回复历史")
    ctx_a.add_message(role="user", content="hello there")

    ctx_b = agents_llm.ChatContext()
    ctx_b.add_message(role="user", content="hello there")

    wrapper.chat(chat_ctx=ctx_a)
    wrapper.chat(chat_ctx=ctx_b)
    assert inner.calls[0]["items"] == inner.calls[1]["items"]
    assert inner.calls[0]["items"][0][1] == (
        "将以下文本翻译为 `英语`，注意只需要输出翻译后的结果，不要额外解释：\n\n`hello there`"
    )


def test_mt_delegates_and_passes_through_without_user_text():
    """无 user 文本原样透传；conn_options/extra_kwargs 照传内芯（对齐其他包装层）。"""
    from livekit.agents import APIConnectOptions

    inner = _RecorderLLM()
    wrapper = StatelessMTLLM(inner, "cantonese")
    opts = APIConnectOptions(max_retry=1, timeout=5.0)

    empty = agents_llm.ChatContext()
    empty.add_message(role="system", content="冇 user 消息")
    wrapper.chat(chat_ctx=empty, conn_options=opts, extra_kwargs={"foo": "bar"})
    # 原样透传：items 原封不动，conn_options/extra_kwargs 照传。
    assert inner.calls[0]["items"] == [("system", "冇 user 消息")]
    assert inner.calls[0]["conn_options"] is opts
    assert inner.calls[0]["extra_kwargs"] == {"foo": "bar"}

    wrapper.chat(chat_ctx=_conversation_ctx(), conn_options=opts)
    assert inner.calls[1]["conn_options"] is opts


def test_mt_model_provider_declared_from_inner():
    """model/provider 展示内芯真实值（metrics/usage 面板口径）。"""

    class _TaggedProvider(_RecorderLLM):
        provider = "mlx"  # 类属性遮蔽基类只读 property（MlxLlmLLM 同款）

    class _TaggedModel(_TaggedProvider):
        @property
        def model(self):  # noqa: D102 - 基类 property 覆盖
            return "mt/local-model"

    assert StatelessMTLLM(_TaggedProvider(), "en").provider == "mlx"
    assert StatelessMTLLM(_TaggedModel(), "en").model == "mt/local-model"


def test_mt_prewarm_delegates_to_inner():
    """_prewarm_impl 委托内芯（MT 模型同样吃 1-token 暖机收益）。"""
    inner = _RecorderLLM()
    fired = {"n": 0}

    async def _fake_prewarm() -> None:
        fired["n"] += 1

    inner._prewarm_impl = _fake_prewarm  # type: ignore[method-assign]
    asyncio.run(StatelessMTLLM(inner, "en")._prewarm_impl())
    assert fired["n"] == 1


def test_mlx_llm_sampling_envs_into_extra_body(monkeypatch):
    """采样 env 注入 extra_body；未设时 A 线默认路径零变化（stop/max_tokens 原样）。"""
    for key in ("LLM_TOP_P", "LLM_TOP_K", "LLM_REPETITION_PENALTY", "LLM_TEMPERATURE", "LLM_MAX_TOKENS"):
        monkeypatch.delenv(key, raising=False)

    # 默认路径：三个采样 env 都没设 → extra_body 唔带采样键。
    base = MlxLlmLLM(base_url="http://127.0.0.1:1235/v1", model="main/model")
    body = base._opts.extra_body or {}
    assert "top_p" not in body and "top_k" not in body and "repetition_penalty" not in body
    assert body["max_tokens"] == 160
    assert "<|im_end|>" in body["stop"]

    # B 线 MT 档：env 注入 → 同一 extra_body 携带采样参数（top_k 收整数）。
    monkeypatch.setenv("LLM_TOP_P", "0.6")
    monkeypatch.setenv("LLM_TOP_K", "20")
    monkeypatch.setenv("LLM_REPETITION_PENALTY", "1.05")
    monkeypatch.setenv("LLM_MAX_TOKENS", "512")
    mt = MlxLlmLLM(base_url="http://127.0.0.1:1236/v1", model="mt/model")
    body2 = mt._opts.extra_body or {}
    assert body2["top_p"] == 0.6
    assert body2["top_k"] == 20 and isinstance(body2["top_k"], int)
    assert body2["repetition_penalty"] == 1.05
    assert body2["max_tokens"] == 512
    assert "<|im_end|>" in body2["stop"]

    # 非法值当没配，唔炸构造。
    monkeypatch.setenv("LLM_TOP_P", "abc")
    bad = MlxLlmLLM(base_url="http://127.0.0.1:1236/v1", model="mt/model")
    assert "top_p" not in (bad._opts.extra_body or {})
