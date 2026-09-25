"""A 线说话自然度（voice style）管线 —— MiniMax 2.8 语气标记 + 停顿标记。

阶段 1·P1 人感包（2026-09-25）：LLM 在 prompt 引导下少量输出官方语气标记
（Ethan 拍板六标记：clear-throat/inhale/breath/coughs/emm/exhale）与停顿
标记 `<#0.3#>`；合成层（speech-2.8-hd/turbo）把它们渲染成真声/真停顿。

四条铁律（单测钉死）：
1. **白名单**：只认六标记（大小写/全角括号归一到小写 ASCII）；未知括号词
   只在「句首或句读之后」剥掉（4B 自创 `(微笑)` 的主发位；句中括号可能是
   正经内容如「粤语（广东话）」，不碰）。
2. **停顿钳制**：`<#x#>` 只收 0.05-0.80s（官方允许到 99.99 但通话文本里
   长停顿=死寂）；坏格式剥掉；行首/行尾/相邻重复的停顿去掉。
3. **双门控**：`BOK_A_LINE_VOICE_TAGS`（默认 1，进 _FORWARD_ENV）× 实际
   合成模型含 "2.8"（构造时从 TTS provider 解析，防 persona 覆写 2.6 时
   标记被当文本念出来）。门关 = transform 退化为全剥（2.6 回退档防线）。
4. **单向流**：标记只活在合成层——turns 落库 / 记忆 / 重复锚 / 出站 LLM
   请求四处全部 strip_voice_style（LLM 不见自己上轮的标记=防复制引力）；
   tts 缓存键天然含标记文本（键=text，无需 pregen 改动——罐头/开场白等
   脚本线不带标记）。
"""

from __future__ import annotations

import os
import re
import unicodedata
from typing import AsyncIterable

# 官方六标记（Ethan 2026-09-24 拍板；仅 speech-2.8-hd / 2.8-turbo 支持）
VOICE_TAG_WHITELIST: frozenset[str] = frozenset(
    {"clear-throat", "inhale", "breath", "coughs", "emm", "exhale"}
)

# 括号 token：ASCII + 全角括号，内容不含嵌套括号
_PAREN_RE = re.compile(r"[（(]([^（）()]{1,24})[）)]")
# 停顿 token：任意形态先收（<# #>/<#x#>/<#x.y#>/<#abc#>），数值合法性在
# replacier 里验——单遍化（先 clamp 再删坏格会误删刚归一的合法停顿）。
_PAUSE_ANY_RE = re.compile(r"<#\s*([^#<>]{0,24}?)\s*#>")
_PAUSE_NUM_RE = re.compile(r"^[0-9]{1,3}(?:\.[0-9]{1,3})?$")
# 任意形态停顿 token（剥除侧/边缘清理用）
_PAUSE_ALL_RE = re.compile(r"<#[^#<>]{0,24}#>")
_PAUSE_MIN_S = 0.05
_PAUSE_MAX_S = 0.80
# 句读边界（句首未知括号词的剥除许可位）——无 ^ 锚：search() 下 ^ 对任意
# 窗口恒中（窗口自身就是被搜串的串首），句中括号会全被误剥（2026-09-25
# test_sanitize_unknown_leading_tag_stripped_mid_sentence_kept 实证）。
_SENT_BOUNDARY_RE = re.compile(r"[。！？!?.；;\n]")


def _norm_tag(inner: str) -> str:
    # NFKC:全角字母/数字归一（４Ｂ 偶发全角 (ｅｍｍ) 也收进白名单）
    return (
        unicodedata.normalize("NFKC", inner).strip().lower().replace("－", "-").replace(" ", "").replace("\u3000", "")
    )


def env_gate_on() -> bool:
    """env 总闸（默认开；prod 经 _FORWARD_ENV 下发）。"""
    return os.environ.get("BOK_A_LINE_VOICE_TAGS", "1") == "1"


def a_line_tags_supported(model: str) -> bool:
    """合成档门：仅 2.8 系把标记渲染成声音（镜像 B 线 _voice_tags_supported）。"""
    return "2.8" in (model or "")


def voice_style_enabled_for_tts(tts_provider) -> bool:
    """构造时解析实际合成模型（_model_override 优先于 env——persona 覆写 2.6
    时门自动关，标记不会再被当文本念出）。读不到模型属性=按 env 默认档判。"""
    model = ""
    fn = getattr(tts_provider, "_model", None)
    if callable(fn):
        try:
            model = str(fn() or "")
        except Exception:  # noqa: BLE001 - 探测失败唔阻装配
            model = ""
    if not model:
        model = getattr(tts_provider, "model", "") or ""
    if not model:
        model = os.environ.get("MINIMAX_MODEL", "speech-2.8-hd")
    return env_gate_on() and a_line_tags_supported(str(model))


def strip_voice_style(text: str) -> str:
    """剥全部标记与停顿（turns/记忆/锚/出站请求侧用）。

    只剥白名单标记 + `<#…#>` token——句中未知括号是可能的内容，不碰
    （合成侧 sanitize 才管 4B 自创标签）。字符串首尾的剥除不引入边缘空格
    （中间保留一个空格防词粘连;首尾 replace 成空串）。"""
    if not text:
        return text

    def _r(m: re.Match) -> str:
        return "" if m.start() == 0 or m.end() == len(text) else " "

    out = _PAREN_RE.sub(
        lambda m: _r(m) if _norm_tag(m.group(1)) in VOICE_TAG_WHITELIST else m.group(0), text
    )
    out = _PAUSE_ALL_RE.sub(_r, out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip() if not out.strip() else out


def sanitize_speech_text(text: str) -> str:
    """合成侧 sanitize（门开档）：白名单归一 + 句首未知括号剥除 + 停顿钳制。"""
    if not text:
        return text

    def _paren(m: re.Match) -> str:
        inner = _norm_tag(m.group(1))
        if inner in VOICE_TAG_WHITELIST:
            return f"({inner})"
        # 未知括号词：只在句首/句读之后剥（自创标签主发位）；句中当内容保留
        prefix = text[: m.start()]
        if not prefix.strip() or _SENT_BOUNDARY_RE.search(prefix[-2:] if prefix else ""):
            return "" if m.start() == 0 else " "
        return m.group(0)

    out = _PAREN_RE.sub(_paren, text)

    def _pause(m: re.Match) -> str:
        raw = (m.group(1) or "").strip()
        _edge = "" if m.start() == 0 else " "
        if not _PAUSE_NUM_RE.fullmatch(raw):
            return _edge  # 坏格式（<#abc#>/<# #>/超长）剥除
        val = float(raw)
        if val <= 0:
            return _edge
        val = min(max(val, _PAUSE_MIN_S), _PAUSE_MAX_S)
        s = f"{val:.2f}".rstrip("0").rstrip(".")
        return f"<#{s}#>"

    out = _PAUSE_ANY_RE.sub(_pause, out)
    # 行首/行尾停顿去掉（必须夹在可念文本之间）；相邻停顿合一
    out = re.sub(r"^(?:\s*<#[^#<>]{0,24}#>\s*)+", "", out)
    out = re.sub(r"(?:\s*<#[^#<>]{0,24}#>\s*)+$", "", out)
    out = re.sub(r"(<#[^#<>]{0,24}#>)\s*(<#[^#<>]{0,24}#>)", r"\1", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out


# ---------------------------------------------------------------------------
# TTS 流式 transform（AgentSession.tts_text_transforms 契约：
# AsyncIterable[str] -> AsyncIterable[str]；text->text 的普通函数会把流对象
# 当文本、整条 TTS 静音——2026-09-07 EMOTION_TAG_PILOT 实证，勿回退。）
# ---------------------------------------------------------------------------

_TOKEN_START_RE = re.compile(r"[（(]|<#")
_HOLD_MAX_CHARS = 40  # 残缺 token 上限：超过当垃圾照发（防 runaway 卡流）


def _split_safe(buf: str, enabled: bool) -> tuple[str, str]:
    """把 buffer 切成「可安全发出」+「可能是残缺 token 的尾巴」。

    标记 token 可能被流切块劈开——尾巴里留最后一段未闭合的 `(` 或 `<#`，
    其余立即 sanitize 发出（sanitize 只在 token 内部动手，切点不跨 token
    即安全）。"""
    hold = 0
    for m in _TOKEN_START_RE.finditer(buf):
        tail = buf[m.start():]
        if tail.startswith("<#"):
            closed = "#>" in tail[2:]
        else:
            closed = ("）" in tail[1:]) or (")" in tail[1:])
        if not closed:
            hold = len(tail)
    if hold > _HOLD_MAX_CHARS:
        hold = 0
    if hold:
        emit, carry = buf[:-hold], buf[-hold:]
    else:
        emit, carry = buf, ""
    fn = sanitize_speech_text if enabled else strip_voice_style
    return fn(emit), carry


def make_tts_voice_style_transform(enabled: bool):
    """工厂：门开=sanitize（保留白名单）；门关=全剥（2.6 回退档防线）。"""
    async def _transform(chunks: AsyncIterable[str]):
        carry = ""
        async for chunk in chunks:
            buf = carry + str(chunk)
            carry = ""
            out, carry = _split_safe(buf, enabled)
            if out:
                yield out
        if carry:
            fn = sanitize_speech_text if enabled else strip_voice_style
            tail = fn(carry)
            if tail:
                yield tail
    return _transform


# ---------------------------------------------------------------------------
# prompt「说话自然度」块（标准书面中文——无条件进前缀的共享文本一律书面语，
# test_zh_prompt_purity_no_cantonese_marks 钉死；整场字节静态=KV 安全）。
# 渲染位：ContextState.render_instruction_prefix() 尾部（voice_style_on 置位时）。
# ---------------------------------------------------------------------------

NATURALNESS_BLOCK = """【说话自然度】
想让语气更像真人，可以遵守下面几条：
- 要查询或查找信息时，可以在句读之后加 (emm)，例如：「您稍等，(emm)我帮您查一下」。
- 停顿标记 <#0.3#> 可以插在两个短句中间，例如：「您先别急<#0.3#>我马上帮您看」。
- (breath) 表示换一口气，可以用在要展开较长解释之前。
- 其余声音标记（如 (clear-throat)、(inhale)、(exhale)、(coughs)）不要主动使用。
- 开场和应承可以换着说法，不要每轮同一句开头。
- 说错了就直接重新说一遍正确的，不用道歉也不用解释。
纪律：回复的第一个字之前不要放任何标记或停顿；每轮回复最多用 1 个标记，整通电话最多用 3 次；拿不准就不用；标记只是给语音系统的，客户听到的是自然的声音。"""
