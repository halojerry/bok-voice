from __future__ import annotations

from dataclasses import dataclass, field

# 官方 AgentMood 11 枚举。LLM 输出的 <expr label="..."/> 必须是英文词，
# 前端/框架才会归一化成功（livekit match_mood 用英文关键词表，中文会回落 calm）。
MOODS: tuple[str, ...] = (
    "excited",
    "happy",
    "playful",
    "curious",
    "surprised",
    "hopeful",
    "empathetic",
    "sad",
    "angry",
    "anxious",
    "calm",
)

# 中文关键词 → 官方 mood（供 classify/校验兜底；expression-trainer 词库可后续替换）
SENTIMENT_LEXICON: dict[str, list[str]] = {
    "excited": ["兴奋", "激动", "迫不及待", "太好了", "太棒了", "恭喜"],
    "happy": ["开心", "高兴", "满意", "愉快", "感谢", "谢谢"],
    "playful": ["开玩笑", "俏皮", "逗你", "调皮"],
    "curious": ["好奇", "想知道", "请问", "为什么"],
    "surprised": ["惊讶", "没想到", "居然", "吃惊"],
    "hopeful": ["期待", "希望", "有望", "乐观", "别担心"],
    "empathetic": ["理解", "体谅", "共情", "明白您的感受", "感同身受", "辛苦"],
    "sad": ["失望", "难过", "遗憾", "抱歉"],
    "angry": ["生气", "不满意", "投诉", "愤怒", "火大"],
    "anxious": ["担心", "着急", "焦虑", "别急", "尽快"],
    "calm": ["平静", "好的", "没问题", "请放心", "可以"],
}


@dataclass
class EmotionProcessor:
    lexicon: dict[str, list[str]] = field(default_factory=lambda: SENTIMENT_LEXICON)

    def classify(self, text: str) -> str:
        for emotion, words in self.lexicon.items():
            if any(w in text for w in words):
                return emotion
        return "calm"

    @staticmethod
    def normalize(label: str | None) -> str:
        """把任意 label 规整到官方 AgentMood 11 枚举；未识别回落 calm。"""
        key = (label or "").strip().lower()
        return key if key in MOODS else "calm"
