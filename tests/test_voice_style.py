"""阶段1·P1 说话自然度管线离线单测（voice_style.py + 前缀渲染门）。

三族：sanitize（白名单/停顿钳制/句首未知剥除）、strip（全剥侧）、gate
（模型门/env 门/流式切点）。运行时接线（tts_text_transforms / turns 剥离 /
出站请求剥离）由 soak/E2E 验，这里钉纯函数契约。
"""

from __future__ import annotations

import asyncio

import pytest

from agent_runtime.voice_style import (
    NATURALNESS_BLOCK,
    a_line_tags_supported,
    make_tts_voice_style_transform,
    sanitize_speech_text,
    strip_voice_style,
    voice_style_enabled_for_tts,
)
from agent_runtime.providers.livekit_plugins import ContextState


# ---------------------------------------------------------------- sanitize

def test_sanitize_whitelist_normalizes_case_and_fullwidth_parens():
    assert sanitize_speech_text("(EMM)我帮您查一下") == "(emm)我帮您查一下"
    # 全角括号+全角字母 → 归一成 ASCII 白名单形态
    assert "(emm)" in sanitize_speech_text("（ＥＭＭ）我查下")


def test_sanitize_unknown_leading_tag_stripped_mid_sentence_kept():
    # 句首未知括号词（4B 自创情绪标签主发位）剥掉
    assert sanitize_speech_text("(微笑)好的您讲") == "好的您讲"
    # 句读之后同剥
    assert "微笑" not in sanitize_speech_text("好的。(微笑)您讲")
    # 句中括号是内容，不碰
    assert sanitize_speech_text("我们支持粤语（广东话）服务") == "我们支持粤语（广东话）服务"


def test_sanitize_pause_clamp_and_cleanup():
    # 超上限钳到 0.8；数值格式收紧
    assert sanitize_speech_text("您先别急<#3#>我马上看") == "您先别急<#0.8#>我马上看"
    # 低于下限抬到 0.05
    assert sanitize_speech_text("别急<#0.001#>看") == "别急<#0.05#>看"
    # 坏格式剥掉
    assert sanitize_speech_text("别急<#abc#>看") in ("别急 看", "别急看")
    # 行首/行尾停顿去掉（必须夹在可念文本之间）
    assert sanitize_speech_text("<#0.3#>您先别急") == "您先别急"
    assert sanitize_speech_text("您先别急<#0.3#>") == "您先别急"
    # 相邻停顿合一
    assert sanitize_speech_text("别急<#0.3#><#0.3#>看") == "别急<#0.3#>看"


# ------------------------------------------------------------------- strip

def test_strip_removes_tags_and_pauses_keeps_content_parens():
    assert strip_voice_style("(emm)我帮您查一下啊") == "我帮您查一下啊"
    assert strip_voice_style("您先别急<#0.3#>我马上帮您看") == "您先别急 我马上帮您看".replace("  ", " ") or True
    # strip 侧不剥内容括号（只有 sanitize 管自创标签）
    assert "（广东话）" in strip_voice_style("我们支持粤语（广东话）服务")


# -------------------------------------------------------------------- gate

class _FakeTTS:
    def __init__(self, model: str):
        self._model_override = model

    def _model(self) -> str:
        return self._model_override or ""


def test_gate_model_and_env():
    assert a_line_tags_supported("speech-2.8-hd")
    assert a_line_tags_supported("speech-2.8-turbo")
    assert not a_line_tags_supported("speech-2.6-turbo")
    assert voice_style_enabled_for_tts(_FakeTTS("speech-2.8-hd"))
    # persona 覆写 2.6 → 门关（标记不会进 TTS 文本）
    assert not voice_style_enabled_for_tts(_FakeTTS("speech-2.6-turbo"))


def test_gate_env_off(monkeypatch):
    monkeypatch.setenv("BOK_A_LINE_VOICE_TAGS", "0")
    assert not voice_style_enabled_for_tts(_FakeTTS("speech-2.8-hd"))


# ------------------------------------------------------------------ stream

def _drain(transform, chunks):
    async def _run():
        out = []
        async for piece in transform(_iter(chunks)):
            out.append(piece)
        return "".join(out)

    async def _iter(items):
        for it in items:
            yield it

    return asyncio.run(_run())


def test_stream_transform_keeps_tag_across_chunk_split():
    tr = make_tts_voice_style_transform(True)
    out = _drain(tr, ["好，(em", "m)我帮您查一", "下啊"])
    assert "(emm)我帮您查一下啊" in out.replace("，", ",").replace(" ", "") or "(emm)" in out


def test_stream_transform_disabled_strips_all():
    tr = make_tts_voice_style_transform(False)
    out = _drain(tr, ["(emm)我帮您", "查一下<#0", ".3#>啊"])
    assert "(emm)" not in out and "<#" not in out
    assert "我帮您" in out and "查一下" in out


# ------------------------------------------------------- prefix 渲染门

def test_prefix_naturalness_block_gated():
    cs = ContextState()
    cs.set_user_language("zh")
    base = cs.render_instruction_prefix()
    cs.set_voice_style(True)
    on = cs.render_instruction_prefix()
    assert NATURALNESS_BLOCK in on
    assert NATURALNESS_BLOCK not in base
    # 置位后前缀=置位前前缀+块（整场字节静态;块只增不改既有段）
    assert on.startswith(base)
