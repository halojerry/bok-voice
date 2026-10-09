"""interp_lite 语气词标记：LLM 生成 + 瘦 guard（2026-10-09 docs-first 定案）。

官方文档基线：MiniMax t2a_v2_bidi `task_continue.text` 语气词标签清单（本地副本
``/Users/halo/Documents/bok/minimax-api_副本.md``，2026-10-09 核对）——**仅
speech-2.8-hd / speech-2.8-turbo 支持**，共 19 枚：

    (laughs) (chuckle) (coughs) (clear-throat) (groans) (breath) (pant)
    (inhale) (exhale) (gasps) (sniffs) (sighs) (snorts) (burps)
    (lip-smacking) (humming) (hissing) (emm) (sneezes)

薄线姿势（与旧线 160 行确定性转换层 `_apply_voice_tags` 的分野，见计划档 §5）：
MT=DeepSeek 指令遵循强，**生成侧**进系统提示词（枚举 19 枚+「每句至多一枚/只标
真实发声」）；本模块只做**确定性 guard**——TagGate 在 say 流上把括号 token 跨
delta 缓冲后校验：白名单内→归一成 ASCII 规范形（全角/大写变体收编），白名单外
→原样透传（可能是内容括号 (USA)/(广东话)，绝不能误删）。归一判据单源=
``voice_style.norm_voice_tag``（A/B 语气词汇不双轨，2026-10-08 复用立法）。

字幕/落库剥除不复刻：``interpret._caption_text`` / ``interpret._strip_voice_tags``
单源直接 import（剥除面=白名单 ∪ 官方全集，比本模块 19 枚更宽——历史标记全收）。
"""

from __future__ import annotations

from ..voice_style import norm_voice_tag

# 官方 19 枚（task_continue.text 文档字面；测试钉死与 A 线白名单的子集关系）。
OFFICIAL_VOICE_TAGS: frozenset[str] = frozenset(
    {
        "laughs", "chuckle", "coughs", "clear-throat", "groans", "breath", "pant",
        "inhale", "exhale", "gasps", "sniffs", "sighs", "snorts", "burps",
        "lip-smacking", "humming", "hissing", "emm", "sneezes",
    }
)

# 括号 token 形状（镜像旧线 _TAG_PAREN_RE：ASCII+全角，内容不含嵌套括号，≤24 字）。
_TAG_OPEN = "（("
_TAG_CLOSE = "）)"
_TAG_INNER_MAX = 24


class TagGate:
    """say 流上的语气标记 guard（跨 delta 缓冲；单消费者 FIFO 下使用，无锁）。

    feed() 返回"此刻可安全下发的文本"；开口括号后进入挂起态，直到闭括号（校验
    归一/透传）或 inner 超长（当普通文本放行，绝不因半个括号挂死整条流）。
    flush() 在流结束时清挂起残段（饥饿收尾，绝不丢字）。
    观测计数供计划档 §5 漂移率评估：canonicalized=变体收编次数；
    hang_released=悬挂放行次数。
    """

    def __init__(self) -> None:
        self._buf = ""
        self._holding = False
        self.canonicalized = 0
        self.hang_released = 0

    @staticmethod
    def _find(s: str, chars: str) -> int:
        for i, ch in enumerate(s):
            if ch in chars:
                return i
        return -1

    def feed(self, text: str) -> str:
        self._buf += text
        out: list[str] = []
        while True:
            if not self._holding:
                idx = self._find(self._buf, _TAG_OPEN)
                if idx < 0:
                    out.append(self._buf)
                    self._buf = ""
                    break
                out.append(self._buf[:idx])
                self._buf = self._buf[idx + 1:]
                self._holding = True
                continue
            close = self._find(self._buf, _TAG_CLOSE)
            if close >= 0:
                inner = self._buf[:close]
                self._buf = self._buf[close + 1:]
                self._holding = False
                out.append(self._canonical(inner))
                continue
            if len(self._buf) > _TAG_INNER_MAX:
                # 未闭合且 inner 超长：不可能是合法标记 → 含开口括号当普通文本放行。
                self._holding = False
                self.hang_released += 1
                out.append("(" + self._buf)
                self._buf = ""
                continue
            break  # 合法窗口内等待闭括号，留住下一 feed 再判。
        return "".join(out)

    def flush(self) -> str:
        """流结束：挂起残段按普通文本放行（饥饿收尾，绝不丢字）。"""
        if not self._buf:
            return ""
        released = self._buf
        if self._holding:
            self.hang_released += 1
            released = "(" + released
        self._buf = ""
        self._holding = False
        return released

    def _canonical(self, inner: str) -> str:
        norm = norm_voice_tag(inner)
        if norm in OFFICIAL_VOICE_TAGS:
            self.canonicalized += 1
            return f"({norm})"
        # 白名单外=可能的内容括号，逐字原样（保留原括号形）。
        return f"({inner})" if inner else "()"


def speech_text(text: str, tags_on: bool) -> str:
    """整句出声口径（非流式路径/测试用）：门开=已含标记原样（生成侧在 prompt）；
    门关=剥标记（非 2.8 档/总闸关时假人念稿防线）。剥除单源=旧线同名函数。"""
    if tags_on:
        return text
    from ..interpret import _speech_text as _old_speech_text

    return _old_speech_text(text, False)
