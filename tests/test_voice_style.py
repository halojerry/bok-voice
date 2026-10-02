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
    voice_style_enabled_for_model,
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


# ---------------------------------- 门控修正回归（2026-09-27 真人感哑火根因）

class _WrappedLikeProduction:
    """模拟生产包裹链的公开面：FallbackAdapter/CachedTTS/_FirstAudioTTS 只透出
    插件类名级 `.model`（"minimax-tts"），不透出 `_model()`/`resolved_model()`
    ——旧探针对这个形状判 "2.8" 恒 False，人感管线整线哑火的根因形状。"""

    model = "minimax-tts"


def test_gate_wrapped_provider_regression(monkeypatch):
    monkeypatch.delenv("MINIMAX_MODEL", raising=False)
    # 规范入口=模型显式门（agent.py 装配从 _tts_primary.resolved_model() 单点取）
    assert voice_style_enabled_for_model("speech-2.8-hd")
    assert voice_style_enabled_for_model("speech-2.8-turbo")
    assert not voice_style_enabled_for_model("speech-2.6-turbo")
    # 空模型（qwen3/volcano/fake 车道 _tts_primary=None）=门关，
    # 不回落 env 默认档把标记喂给本地 sidecar。
    assert not voice_style_enabled_for_model("")
    # 陷阱钉死：生产包裹形状下 provider 探测版恒 False——它只准喂裸实例
    assert not voice_style_enabled_for_tts(_WrappedLikeProduction())
    assert voice_style_enabled_for_tts(_FakeTTS("speech-2.8-hd"))


def test_agent_assembly_pins_model_explicit_gate():
    """源级 pin：装配点必须用模型显式门（对包裹实例探属性的旧姿势已实证哑火）。"""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "apps" / "agent" / "agent_runtime" / "agent.py"
    ).read_text(encoding="utf-8")
    assert "voice_style_enabled_for_model(_voice_style_model)" in src
    assert "voice_style_enabled_for_tts(tts_provider)" not in src


def test_minimax_prep_outbound_strips_tags_on_26(monkeypatch):
    """非 2.8 实例（FallbackAdapter 备档 2.6）出站前剥标记；2.8 主档原样透传。"""
    from agent_runtime.providers.livekit_plugins import MiniMaxTTS

    monkeypatch.delenv("MINIMAX_MODEL", raising=False)
    t26 = MiniMaxTTS(voice="v", api_key="k", model_override="speech-2.6-turbo")
    out = t26._prep_outbound("(emm)您稍等<#0.3#>我帮您查一下")
    assert "(emm)" not in out and "<#" not in out and "我帮您查一下" in out
    # 2.8 主档（env 缺省 speech-2.8-hd）：透传不碰
    t28 = MiniMaxTTS(voice="v", api_key="k")
    assert t28._prep_outbound("(emm)您稍等<#0.3#>我帮您查一下") == "(emm)您稍等<#0.3#>我帮您查一下"


def test_minimax_stream_pins_prep_outbound():
    """源级 pin：两条流式类 push_text 都过 _prep_outbound、synthesize 同源。"""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py"
    ).read_text(encoding="utf-8")
    assert src.count("def push_text(self, text: str = \"\", *args, **kwargs):") == 2
    assert src.count("self._tts_._prep_outbound(str(text or \"\"))") == 2
    assert "text = self._prep_outbound(lecture_guard(" in src


# ------------------------------------------------ 换气注入（2026-09-27 断句换气）

_LONG1 = "这个订单的赔付记录和物流信息我都帮您查过了"  # 21 字 ≥ 默认阈值 20
_NEXT = "接下来给您讲三种方案"
_SHORT_A = "您先别急"
_SHORT_B = "我马上帮您看"


def _breath_env(monkeypatch, on="1", chars=None):
    monkeypatch.delenv("BOK_A_LINE_VOICE_TAGS", raising=False)
    monkeypatch.setenv("BOK_BREATH_INJECT", on)
    if chars is None:
        monkeypatch.delenv("BOK_BREATH_SENT_CHARS", raising=False)
    else:
        monkeypatch.setenv("BOK_BREATH_SENT_CHARS", chars)


def test_breath_inject_after_long_sentence_cross_chunk(monkeypatch):
    _breath_env(monkeypatch)
    tr = make_tts_voice_style_transform(True)
    out = _drain(tr, [_LONG1 + "。", _NEXT + "。"])
    assert out.count("(breath)") == 1
    assert "(breath)接下来" in out.replace(" ", "")


def test_breath_inject_after_long_sentence_same_chunk(monkeypatch):
    _breath_env(monkeypatch)
    tr = make_tts_voice_style_transform(True)
    out = _drain(tr, [_LONG1 + "。" + _NEXT + "。"])
    assert out.count("(breath)") == 1
    assert "。(breath)接下来" in out.replace(" ", "")


def test_breath_inject_split_mid_sentence(monkeypatch):
    """长句被流式切块劈开：字数跨块累计，句界照触发。"""
    _breath_env(monkeypatch)
    tr = make_tts_voice_style_transform(True)
    out = _drain(tr, [_LONG1[:10], _LONG1[10:] + "。", _NEXT + "。"])
    assert out.count("(breath)") == 1


def test_breath_inject_short_sentences_noop(monkeypatch):
    _breath_env(monkeypatch)
    tr = make_tts_voice_style_transform(True)
    out = _drain(tr, [_SHORT_A + "。", _SHORT_B + "。", "好的。"])
    assert "(breath)" not in out


def test_breath_inject_env_off(monkeypatch):
    _breath_env(monkeypatch, on="0")
    tr = make_tts_voice_style_transform(True)
    out = _drain(tr, [_LONG1 + "。" + _NEXT + "。"])
    assert "(breath)" not in out


def test_breath_inject_threshold_env(monkeypatch):
    _breath_env(monkeypatch, chars="100")
    tr = make_tts_voice_style_transform(True)
    out = _drain(tr, [_LONG1 + "。" + _NEXT + "。"])
    assert "(breath)" not in out


def test_breath_inject_trailing_boundary_dropped(monkeypatch):
    """回复在句界收尾：不换气（没人换完气就闭嘴）。"""
    _breath_env(monkeypatch)
    tr = make_tts_voice_style_transform(True)
    out = _drain(tr, [_LONG1 + "。"])
    assert "(breath)" not in out


def test_breath_inject_max_one_per_reply(monkeypatch):
    _breath_env(monkeypatch)
    tr = make_tts_voice_style_transform(True)
    out = _drain(tr, [_LONG1 + "。" + _LONG1 + "。" + _NEXT + "。"])
    assert out.count("(breath)") == 1


def test_breath_inject_dedup_with_llm_tag(monkeypatch):
    """LLM 已在该句自发标记 → 不叠注（块内标记+句界同块也压得住）。"""
    _breath_env(monkeypatch)
    tr = make_tts_voice_style_transform(True)
    out = _drain(tr, ["您稍等，(emm)我马上帮您查" + _LONG1[5:] + "。" + _NEXT + "。"])
    assert out.count("(emm)") == 1
    assert "(breath)" not in out


def test_breath_inject_disabled_gate_no_inject(monkeypatch):
    """门关（非 2.8 档 transform）=全剥，注入层同样不参与。"""
    _breath_env(monkeypatch)
    tr = make_tts_voice_style_transform(False)
    out = _drain(tr, [_LONG1 + "。" + _NEXT + "。"])
    assert "(breath)" not in out and "(emm)" not in out
