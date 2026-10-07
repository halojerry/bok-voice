"""A 线说话自然度（voice style）管线 —— MiniMax 2.8 语气标记 + 停顿标记。

阶段 1·P1 人感包（2026-09-25）：LLM 在 prompt 引导下少量输出官方语气标记
（初始六件 clear-throat/inhale/breath/coughs/emm/exhale；2026-10-06 W3 扩
sighs/chuckle/laughs 三件，Ethan 裁定每轮上限 2→3+标点语气）与停顿
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
   tts 缓存键天然含标记文本（键=text，无需 pregen 改动）。
5. **罐头线标记（2026-10-06 罐头线语气标记票）**：步骤文案（say 步/开场白
   ref）允许作者直接携带白名单标记——`script_line_speech_text` 是脚本直念线
   的规范形单源（pregen `_say_step_lines`/`_opening_line` 与运行时
   `FlowController.step_say_text` 同调），缓存键两侧按同一规范形对齐；无括
   号/停顿 token 的文案走快路径逐字节原样返回（marker-free 零漂移铁律）。
   非 2.8 合成档的兜底剥除由 MiniMaxTTS._prep_outbound 实例门承担（既有），
   env 总闸 BOK_A_LINE_VOICE_TAGS=0 时规范形同样全剥（kill-switch 跨 LLM/
   脚本两线语义一致）。
"""

from __future__ import annotations

import os
import re
import unicodedata
from typing import AsyncIterable

# 官方标记白名单（2026-09-24 拍板六件 + 2026-10-06 W3 扩三件 sighs/chuckle/laughs
# ——B 线 interpret.py _VOICE_TAG_RE 已核实官方支持；A/B 台架物化 reports/voice-tags-ab/
# 待人耳终裁。仅 speech-2.8-hd / 2.8-turbo 支持）
VOICE_TAG_WHITELIST: frozenset[str] = frozenset(
    {"clear-throat", "inhale", "breath", "coughs", "emm", "exhale", "sighs", "chuckle", "laughs"}
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


def norm_voice_tag(inner: str) -> str:
    """括号词归一（NFKC/小写/连字符/去空白）——A/B 两线语气词汇判据共用单源。

    2026-10-08 B 线复用立法：interpret 的标记归一/剥除吃同一张判据
    （VOICE_TAG_WHITELIST + 本函数），A/B 语气词汇不双轨。"""
    # NFKC:全角字母/数字归一（４Ｂ 偶发全角 (ｅｍｍ) 也收进白名单）
    return (
        unicodedata.normalize("NFKC", inner).strip().lower().replace("－", "-").replace(" ", "").replace("\u3000", "")
    )


# 既有内部名（本模块调用点零改动）
_norm_tag = norm_voice_tag


def env_gate_on() -> bool:
    """env 总闸（默认开；prod 经 _FORWARD_ENV 下发）。"""
    return os.environ.get("BOK_A_LINE_VOICE_TAGS", "1") == "1"


def a_line_tags_supported(model: str) -> bool:
    """合成档门：仅 2.8 系把标记渲染成声音（镜像 B 线 _voice_tags_supported）。"""
    return "2.8" in (model or "")


# ---------------------------------------------------------------------------
# 换气注入（2026-09-27，断句换气=真人感最大单点）：transform 流式统计句长，
# 长句句界后自动补 (breath)。env 两键均进 _FORWARD_ENV（bok.py 白名单表）。
# ---------------------------------------------------------------------------
_BREATH_TAG = "(breath)"
_SENT_END_RE = re.compile(r"[。！？!?]")


def breath_inject_enabled() -> bool:
    """换气注入总闸（默认开；关=只留 LLM 自发标记）。"""
    return os.environ.get("BOK_BREATH_INJECT", "1") == "1"


def _breath_min_sent_chars() -> int:
    """触发阈值：句正文 ≥N 字才在句界换气（默认 12——2026-10-06 W3 实测定档：
    【回复长度】铁律（两句≤40 字）下 llm 轮非末句最长 16 字，旧缺省 20 字地板
    恒够不着=换气注入概率≈0；12 吃住绝大多数非末句。probe_voice_style_gate.py
    --live 可复测产标记率）。"""
    try:
        return max(0, int(os.environ.get("BOK_BREATH_SENT_CHARS", "12")))
    except Exception:  # noqa: BLE001 - 坏值回默认
        return 12


def _has_whitelist_tag(text: str) -> bool:
    """文本中是否含白名单标记（_split_safe 的 hold 机制保证完整 token 落同一
    emit，逐 emit 检查即跨句精确）。"""
    for m in _PAREN_RE.finditer(text or ""):
        if _norm_tag(m.group(1)) in VOICE_TAG_WHITELIST:
            return True
    return False


def voice_style_enabled_for_model(model: str) -> bool:
    """门（模型显式版）：装配点已知**实际合成模型**时用这把——这是规范入口。

    勿对包裹后的 provider 探属性：生产装配链 MiniMax→FallbackAdapter→CachedTTS
    →_FirstAudioTTS 的公开 `.model` 是插件名（"minimax-tts"），判 "2.8" 恒 False
    → prompt 块永不注入 + 标记全剥，整条人感管线哑火（2026-09-27 实证根因；
    test_voice_style_gate_wrapped_provider_regression 钉住）。"""
    return env_gate_on() and a_line_tags_supported(str(model or ""))


def voice_style_enabled_for_tts(tts_provider) -> bool:
    """门（provider 探测版）：只在拿到裸 provider 时用；装配点应改用
    voice_style_enabled_for_model（模型字符串从 _tts_primary.resolved_model()
    单点取，见 agent.py 装配注释）。

    _model_override 优先于 env——persona 覆写 2.6 时门自动关，标记不会再被
    当文本念出。读不到模型属性=按 env 默认档判。"""
    model = ""
    fn = getattr(tts_provider, "_model", None)
    if callable(fn):
        try:
            model = str(fn() or "")
        except Exception:  # noqa: BLE001 - 探测失败唔阻装配
            model = ""
    if not model:
        rf = getattr(tts_provider, "resolved_model", None)
        if callable(rf):
            try:
                model = str(rf() or "")
            except Exception:  # noqa: BLE001
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
# 罐头线（脚本直念线）规范形（2026-10-06 罐头线语气标记票）：步骤文案允许作者
# 直接携带白名单语气/停顿标记，pregen 物化时烧进缓存音频，运行时同键命中——
# 前提是两侧对同一份 ref 首行算出**逐字节相同**的规范形。单源=本函数：
# pregen `_say_step_lines`/`_opening_line` 与运行时 `FlowController.step_say_text`
# 同调（缓存键 text 维度天然对齐）。快路径=无 `(`/`（`/`<#` 的文案原样返回
# （sanitize 的空白收敛对英文双空格等文案也不发生）——marker-free 文案零漂移。
# env 总闸关（BOK_A_LINE_VOICE_TAGS=0）→ 规范形全剥：kill-switch 对 LLM 线
# （transform 全剥档）与罐头线语义一致。模型维度的兜底（persona 覆写 2.6）
# 不在这里判——MiniMaxTTS._prep_outbound 实例门在合成出站前剥（既有单测钉住），
# 本函数保持模型无关（pregen/运行时两侧同一环境同一结果，键不会因模型档分叉）。
# ---------------------------------------------------------------------------


def _has_marker_token(text: str) -> bool:
    """文本是否含任何标记形状 token（括号词或停顿）——快路径判据。"""
    return "(" in text or "（" in text or "<#" in text


def script_line_speech_text(text: str) -> str:
    """脚本直念线文本规范形：作者标记 → 合成安全形态（纯函数，env 只读总闸）。

    - 白名单标记归一成小写 ASCII（`（Chuckle）`→`(chuckle)`，MiniMax 只认
      ASCII 括号形态，全角原样会被当文本念出来并烧进缓存）；
    - 句首/句读后未知括号词剥除（人工文案里的舞台指示「（停顿两秒）」主发位）；
    - 停顿钳制 0.05-0.80s、坏格式剥除、行首尾/相邻停顿清理；
    - env 总闸关 → 再叠 strip（标记从规范形中消失，pregen/运行时同步剥，
      缓存键仍两侧一致）。
    无标记 token 的文案逐字节原样返回。"""
    if not text or not _has_marker_token(text):
        return text
    canonical = sanitize_speech_text(text)
    if not env_gate_on():
        canonical = strip_voice_style(canonical)
    return canonical


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
    """工厂：门开=sanitize（保留白名单）；门关=全剥（2.6 回退档防线）。

    门开时叠加**换气注入**（2026-09-27，Ethan 拍板「断句换气最能体现真人感」）：
    刚说完的句子够长（≥`BOK_BREATH_SENT_CHARS`，默认 20 字≈4s 语流）→ 在句界
    后注入一枚 (breath)。约束：每条回复至多 1 枚（_transform 每次合成请求新建
    =天然按回复重置）、该句已带白名单标记不重复注、回复尾界不注（没人换完气
    就收线）。LLM 自发标记照旧（prompt 引导），这层是 4B 不听话时的保底。
    `BOK_BREATH_INJECT=0` 关。"""
    inject_on = enabled and breath_inject_enabled()
    min_chars = _breath_min_sent_chars()

    async def _transform(chunks: AsyncIterable[str]):
        carry = ""
        sent_chars = 0  # 当前句正文累计（跨块维护；空白不计）
        sent_had_tag = False  # 当前句是否已带白名单标记（防双重换气，块粒度）
        breath_used = False  # 每条回复至多一枚（_transform 每次合成请求新建）
        pending = False  # 块尾句界待注：下一块有正文才落地（无下一块=收尾不注）
        async for chunk in chunks:
            buf = carry + str(chunk)
            carry = ""
            if inject_on and pending and buf.lstrip():
                i = len(buf) - len(buf.lstrip())
                buf = buf[:i] + _BREATH_TAG + buf[i:]
                pending = False
                breath_used = True
            if inject_on and not breath_used:
                # 本块含白名单标记 → 保守抑制（LLM 已自发换气/标记，不叠注）；
                # 先于扫描判（同块「标记+句界」也压得住）。
                if _has_whitelist_tag(buf):
                    sent_had_tag = True
                # raw 层扫描（先于 split/sanitize）：句界原位插入，sanitize 对
                # 白名单标记原位保留；块尾句界走 pending 延后（跨块/收尾两态）。
                parts: list[str] = []
                n = len(buf)
                for idx, ch in enumerate(buf):
                    parts.append(ch)
                    if _SENT_END_RE.match(ch):
                        if not breath_used and not sent_had_tag and sent_chars >= min_chars:
                            if idx < n - 1:
                                parts.append(_BREATH_TAG)
                                breath_used = True
                            else:
                                pending = True
                        sent_chars = 0
                        sent_had_tag = False
                    elif not ch.isspace():
                        sent_chars += 1
                buf = "".join(parts)
            out, carry = _split_safe(buf, enabled)
            if out:
                yield out
        if carry:
            fn = sanitize_speech_text if enabled else strip_voice_style
            tail = fn(carry)
            if tail:
                yield tail
        # 块尾 pending 未落地=回复在句界收尾，不换气
    return _transform


# ---------------------------------------------------------------------------
# prompt「说话自然度」块（标准书面中文——无条件进前缀的共享文本一律书面语，
# test_zh_prompt_purity_no_cantonese_marks 钉死；整场字节静态=KV 安全）。
# 渲染位：ContextState.render_instruction_prefix() 尾部（voice_style_on 置位时）。
# ---------------------------------------------------------------------------

NATURALNESS_BLOCK = """【说话自然度】
结合对话语境（客户的情绪、正在谈的事）让语气更像真人，遵守下面几条：
- 每轮回复通常在第一句讲完之后放 1 个声音标记，让语句有呼吸感；拿不准就不放。
- 按语境选标记：安抚、致歉、共情用 (sighs) 或 (breath)；查询、思考用 (emm)；客户轻松满意用 (chuckle)；客户讲到有趣的事可以跟着 (laughs)。
- 例：「查到了，您的订单已经到香港仓。(breath)接下来给您讲怎么安排派送。」；「您稍等，(emm)我帮您查一下」；「您先别急<#0.3#>我马上帮您看」。
- 自然的标点也是语气：迟疑用 ……，强调用 ！，反问用 ？，按语境放心用。
- 纪律：回复的第一个字之前不要放任何标记或停顿；每轮回复最多 3 个标记；整通电话最多 6 次；短回应（客户只说了一两个词或数字时）不放标记；其余标记（(clear-throat)、(exhale)、(coughs)、(inhale)）不要主动使用；标记只是给语音系统的，客户听到的是自然的声音。
- 开场和应承可以换着说法，不要每轮同一句开头。说错了就直接重新说一遍正确的，不用道歉也不用解释。"""
