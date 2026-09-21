"""E5 增补单测：B 线 MT 出口确定性语言校验器 + 单次强化重试。

三块：
  A. 纯函数判据 ``bok_voice_core.mt_lang_check``（真实形态样本/混合脚本/空/数字/
     普粤盲区）；
  B. 接线 ``interpret._mt_once``——错语言触发**至多一次**强化重试、二次失败按
     现状出稿、kill-switch 回退、重试超时不拖垮、通用回退 LLM 无重试口；
  C. 结构锚：校验器只坐在 ``_mt_once`` 这一个出口点 + 强化 prompt 字面。

不依赖真实服务；假 LLM 提供 chat/chat_retry 两条流。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

from bok_voice_core import mt_lang_check as mlc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

import agent_runtime.interpret as interpret  # noqa: E402


# ---------------------------------------------------------------------------
# A. 纯函数判据
# ---------------------------------------------------------------------------

# 真实形态样本（每语言 2 条：日常长短句 + 短应答）。
ZH_SAMPLES = ["麻烦你帮我查一下这个单号的物流信息。", "好的，我马上处理。"]
CANTONESE_SAMPLES = ["唔該幫我查下個單號嘅物流。", "好嘅，我即刻幫你搞掂。"]


def test_zh_samples_pass_for_zh_target():
    for s in ZH_SAMPLES:
        assert mlc.looks_like_language(s, "zh") is True
        assert mlc.language_match_score(s, "zh") > 0.8


def test_cantonese_samples_pass_for_cantonese_target():
    for s in CANTONESE_SAMPLES:
        assert mlc.looks_like_language(s, "cantonese") is True
        assert mlc.language_match_score(s, "cantonese") > 0.8


def test_english_samples_pass_for_en_target():
    for s in ["Could you check the tracking number for me?", "Sure, one moment please."]:
        assert mlc.looks_like_language(s, "en") is True
        assert mlc.language_match_score(s, "en") > 0.9


def test_script_family_mismatch_is_caught():
    """靶心：en 目标却出整句汉字 / CJK 目标却出整句英文 → 判不通过。"""
    # en 目标收到源文回声（汉字）
    assert mlc.looks_like_language("麻烦你帮我查一下这个单号。", "en") is False
    # zh 目标收到一行英文
    assert mlc.looks_like_language("Could you check the tracking number?", "zh") is False
    # cantonese 目标收到一行英文
    assert mlc.looks_like_language("Could you check the tracking number?", "cantonese") is False
    assert mlc.language_match_score("Could you check it?", "zh") < 0.2


def test_mixed_script_reality():
    """混合脚本（中英夹杂是常态）：CJK 目标容忍夹英文，en 目标容忍夹少量汉字。"""
    # CJK 目标 + 少量英文词 → 通过
    assert mlc.looks_like_language("您的 order 已經 shipped，預計明天到。", "cantonese") is True
    assert mlc.looks_like_language("好的，OK，马上处理。", "zh") is True
    # en 目标 + 一个汉字人名 → 通过（拉丁仍占主导）
    assert mlc.looks_like_language("Okay, I will contact Zhang Wei about it.", "en") is True
    # 但 CJK 主导则 en 目标不通过
    assert mlc.looks_like_language("好的好的，谢谢", "en") is False
    # 港式粤语重度中英夹杂（英文占多数）仍应通过 CJK 目标（口径宽容，防假重试）
    assert mlc.looks_like_language("OK, 我 send 俾你。", "cantonese") is True


def test_empty_whitespace_and_digits_only_pass_as_insufficient():
    """空/纯空白/纯数字/纯标点：证据不足 → 放行（fail-open，绝不触发重试）。"""
    for s in ["", "   ", "\n\t ", "12345", "852-1234-5678", "。。。！！", "🚚🚚"]:
        assert mlc.looks_like_language(s, "zh") is True
        assert mlc.looks_like_language(s, "en") is True
        assert mlc.looks_like_language(s, "cantonese") is True
        assert mlc.language_match_score(s, "en") == 1.0


def test_short_text_below_natural_floor_is_not_judged():
    """自然字符低于地板（<4）→ 不判（「OK」这类短应答不因语言差异触发重试）。"""
    assert mlc.script_counts("OK") == (0, 2)
    assert mlc.looks_like_language("OK", "zh") is True   # 2 < 4 → 证据不足
    assert mlc.looks_like_language("好的", "en") is True  # 2 < 4 → 证据不足


def test_cantonese_vs_zh_limitation_is_documented_not_guessed():
    """**已知盲区**：普粤同属 CJK 族，判据**不**区分二者（故意放行，防假重试）。

    普通话句子当 cantonese 输出、粤语句子当 zh 输出，都判通过——这是模块
    docstring 明写的「能与不能」边界，不是 bug。``has_cantonese_markers`` 只作
    诊断信号，不参与门控。
    """
    assert mlc.looks_like_language("麻烦你帮我查一下这个单号的物流信息。", "cantonese") is True
    assert mlc.looks_like_language("唔該幫我查下個單號嘅物流。", "zh") is True
    # 诊断信号确实能看出「有粤语特征」——但它不改变门控结论。
    assert mlc.has_cantonese_markers("唔該幫我查下個單號嘅物流。") is True
    assert mlc.has_cantonese_markers("麻烦你帮我查一下这个单号的物流信息。") is False


def test_target_lang_aliases_and_unknown():
    """别名归一；未知语言不判（放行）——本模块不认识的语言不背锅。"""
    assert mlc.normalize_target_lang("English") == "en"
    assert mlc.normalize_target_lang("粤语") == "cantonese"
    assert mlc.normalize_target_lang("Mandarin") == "zh"
    assert mlc.normalize_target_lang("klingon") == ""
    assert mlc.looks_like_language("I will handle it now", "klingon") is True
    assert mlc.language_match_score("whatever", "") == 1.0


def test_script_ratio_helpers():
    cjk, latin = mlc.script_counts("好的 OK")
    assert (cjk, latin) == (2, 2)
    cr, lr = mlc.script_ratio("好的 OK")
    assert abs(cr - 0.5) < 1e-9 and abs(lr - 0.5) < 1e-9
    assert mlc.script_ratio("12345") == (0.0, 0.0)


# ---------------------------------------------------------------------------
# B. 接线 interpret._mt_once（单次重试 / 二次失败出稿 / kill-switch / 超时）
# ---------------------------------------------------------------------------


class _Chunk:
    def __init__(self, content):
        self.delta = type("D", (), {"content": content})()


class _FakeStream:
    def __init__(self, text: str):
        self._text = text

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        yield _Chunk(self._text)


class _SlowStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(30)
        raise StopAsyncIteration


class _RetryProvider:
    """chat 依次吐 outputs；chat_retry 依次吐 retry_outputs。记录调用序列。"""

    def __init__(self, outputs, retry_outputs):
        self._out = list(outputs)
        self._retry = list(retry_outputs)
        self.calls: list[str] = []

    def chat(self, *, chat_ctx, conn_options=None, **kw):
        self.calls.append("chat")
        return _FakeStream(self._out.pop(0))

    def chat_retry(self, *, chat_ctx, conn_options=None, **kw):
        self.calls.append("chat_retry")
        return _FakeStream(self._retry.pop(0))


class _NoRetryProvider:
    """通用回退 LLM：只有 chat，没有 chat_retry。"""

    def __init__(self, output):
        self._out = output
        self.calls: list[str] = []

    def chat(self, *, chat_ctx, conn_options=None, **kw):
        self.calls.append("chat")
        return _FakeStream(self._out)


def _ctx():
    return interpret._build_mt_context("", [], "你好")


def test_mt_once_passes_through_when_language_ok(monkeypatch):
    monkeypatch.delenv("BOK_INTERP_MT_LANGGUARD", raising=False)
    p = _RetryProvider(["I will check it right away."], ["SHOULD-NOT-BE-CALLED"])
    out = asyncio.run(interpret._mt_once(p, _ctx(), target_lang="en"))
    assert out == "I will check it right away."
    assert p.calls == ["chat"]  # 正常轮零重试


def test_mt_once_retries_once_and_accepts_retry_output(monkeypatch):
    """首次错语言 → 恰好一次 chat_retry；重试通过 → 采用重试输出。"""
    monkeypatch.delenv("BOK_INTERP_MT_LANGGUARD", raising=False)
    p = _RetryProvider(["好的好的，谢谢"], ["I will handle it right now."])
    out = asyncio.run(interpret._mt_once(p, _ctx(), target_lang="en"))
    assert out == "I will handle it right now."
    assert p.calls == ["chat", "chat_retry"]  # 至多一次，无第三次


def test_mt_once_retry_second_failure_emits_as_is(monkeypatch):
    """重试仍错语言 → 按现状出稿（= 重试输出，**绝不**回退源文），且只重试一次。"""
    monkeypatch.delenv("BOK_INTERP_MT_LANGGUARD", raising=False)
    p = _RetryProvider(["I will handle it"], ["Will handle it soon"])
    out = asyncio.run(interpret._mt_once(p, _ctx(), target_lang="zh"))
    assert out == "Will handle it soon"  # 不是源文「你好」，不是首次输出
    assert p.calls == ["chat", "chat_retry"]  # 单次，无循环


def test_mt_once_kill_switch_disables_check_and_retry(monkeypatch):
    """BOK_INTERP_MT_LANGGUARD=0 → 出口不校验、不重试，错语言照原样出稿。"""
    monkeypatch.setenv("BOK_INTERP_MT_LANGGUARD", "0")
    p = _RetryProvider(["I will handle it"], ["SHOULD-NOT-BE-CALLED"])
    out = asyncio.run(interpret._mt_once(p, _ctx(), target_lang="zh"))
    assert out == "I will handle it"
    assert p.calls == ["chat"]


def test_mt_once_no_target_lang_disables_check(monkeypatch):
    """target_lang 缺省（旧调用面无语言）→ 不判不重试，向后兼容。"""
    monkeypatch.delenv("BOK_INTERP_MT_LANGGUARD", raising=False)
    p = _RetryProvider(["I will handle it"], ["SHOULD-NOT-BE-CALLED"])
    out = asyncio.run(interpret._mt_once(p, _ctx()))
    assert out == "I will handle it"
    assert p.calls == ["chat"]


def test_mt_once_unsupported_provider_emits_as_is(monkeypatch):
    """通用回退 LLM 无 chat_retry → 不重试，照现状出稿（只留观测行）。"""
    monkeypatch.delenv("BOK_INTERP_MT_LANGGUARD", raising=False)
    p = _NoRetryProvider("I will handle it")
    out = asyncio.run(interpret._mt_once(p, _ctx(), target_lang="zh"))
    assert out == "I will handle it"
    assert p.calls == ["chat"]


def test_mt_once_empty_first_output_no_retry(monkeypatch):
    monkeypatch.delenv("BOK_INTERP_MT_LANGGUARD", raising=False)
    p = _RetryProvider([""], ["SHOULD-NOT-BE-CALLED"])
    out = asyncio.run(interpret._mt_once(p, _ctx(), target_lang="en"))
    assert out == ""
    assert p.calls == ["chat"]


def test_mt_once_retry_timeout_emits_first_output(monkeypatch):
    """重试超时 → 回退到首次输出（不抛出、不卡死），有界。"""
    monkeypatch.delenv("BOK_INTERP_MT_LANGGUARD", raising=False)

    class _TimeoutRetryProvider:
        def __init__(self):
            self.calls: list[str] = []

        def chat(self, *, chat_ctx, conn_options=None, **kw):
            self.calls.append("chat")
            return _FakeStream("I will handle it")

        def chat_retry(self, *, chat_ctx, conn_options=None, **kw):
            self.calls.append("chat_retry")
            return _SlowStream()

    p = _TimeoutRetryProvider()
    out = asyncio.run(interpret._mt_once(p, _ctx(), target_lang="zh", timeout_s=0.05))
    assert out == "I will handle it"  # 超时后回退首次输出
    assert p.calls == ["chat", "chat_retry"]


# ---------------------------------------------------------------------------
# C. 结构锚：单一出口点 + 强化 prompt 字面
# ---------------------------------------------------------------------------

_INTERP = Path(interpret.__file__)


def _mt_once_body() -> str:
    src = _INTERP.read_text(encoding="utf-8")
    m = re.search(r"async def _mt_once\(.*?\n(?=\n?(?:async )?def )", src, re.S)
    assert m, "找不到 _mt_once 函数体"
    return m.group(0)


def test_validator_sits_at_single_point_in_pipeline():
    """校验器只在 ``_mt_once`` 这一个出口点被调用（全文件 2 处调用，皆在函数内）。"""
    src = _INTERP.read_text(encoding="utf-8")
    body = _mt_once_body()
    assert body.count("looks_like_language(") == 2, "校验应在 _mt_once 内出现（首判+重试复判）"
    assert src.count("looks_like_language(") == 2, "文件内不得在 _mt_once 之外再调校验器"


def test_single_retry_is_structurally_bounded():
    """重试结构性地只有一次：出口点恰好两次开流（首次 + 一次 retry=True），且无循环。"""
    body = _mt_once_body()
    assert body.count("_mt_open_stream(") == 2  # 首次 + 一次 retry=True
    assert body.count("retry=True") == 1
    assert 'getattr(llm_provider, "chat_retry", None)' in body  # 重试口经能力探测
    assert "while " not in body, "_mt_once 内不得有循环（重试不可能累加）"
    assert body.count("await _mt_collect(") == 2


def test_kill_switch_default_on_and_env_registered():
    """kill-switch 默认开（未设 env 时 enabled=True），键名与 bok 透传面一致。"""
    saved = os.environ.pop("BOK_INTERP_MT_LANGGUARD", None)
    try:
        assert interpret._mt_lang_guard_enabled() is True
    finally:
        if saved is not None:
            os.environ["BOK_INTERP_MT_LANGGUARD"] = saved
    assert "BOK_INTERP_MT_LANGGUARD" in _INTERP.read_text(encoding="utf-8")
    # 注册在 _interp_env 的 B 线透传元组（与 BOK_INTERP_MT_CONTEXT 同机制）。
    bok_src = (Path(__file__).resolve().parents[1] / "tools" / "bok.py").read_text(encoding="utf-8")
    assert '"BOK_INTERP_MT_CONTEXT",' in bok_src and '"BOK_INTERP_MT_LANGGUARD",' in bok_src


def test_plugin_strengthened_prompt_literal():
    """强化 prompt 字面（重试轮专用）：IMPORTANT RETRY 前缀 + 模板句内约束。"""
    from agent_runtime.providers.livekit_plugins import _mt_prompt

    normal = _mt_prompt("你好", "en")
    assert "IMPORTANT RETRY" not in normal
    assert normal == "将以下文本翻译为 `英语`，注意只需要输出翻译后的结果，不要额外解释：\n\n`你好`"

    retry = _mt_prompt("你好", "en", retry=True)
    assert retry.startswith("IMPORTANT RETRY:")
    assert "`英语`" in retry and "不得保留原文" in retry
    assert retry.endswith("`你好`")


def test_plugin_chat_retry_uses_strengthened_prompt():
    """StatelessMTLLM.chat_retry 与 chat 同透传，只换强化 prompt。"""
    from livekit.agents import llm as agents_llm

    from agent_runtime.providers.livekit_plugins import StatelessMTLLM

    class _RecorderLLM(agents_llm.LLM):
        def __init__(self):
            super().__init__()
            self.calls: list[list] = []

        def chat(self, *, chat_ctx, tools=None, conn_options=None, parallel_tool_calls=None,
                 tool_choice=None, extra_kwargs=None):
            self.calls.append([(m.role, m.text_content) for m in chat_ctx.items])
            return None

    async def _run():
        inner = _RecorderLLM()
        ctx = agents_llm.ChatContext()
        ctx.add_message(role="user", content="hello there")
        StatelessMTLLM(inner, "en").chat_retry(chat_ctx=ctx)
        assert len(inner.calls) == 1
        prompt = inner.calls[0][0][1]
        assert prompt.startswith("IMPORTANT RETRY:")
        assert "不得保留原文" in prompt and "hello there" in prompt

    asyncio.run(_run())
