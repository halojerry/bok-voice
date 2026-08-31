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

# 官方 mood → Qwen3-TTS CustomVoice instruct（语音语气）。未命中回落空串。
MOOD_INSTRUCT: dict[str, str] = {
    "excited": "语气兴奋、热情，语速稍快",
    "happy": "语气欢快、亲切，带笑意",
    "playful": "语气俏皮、轻松，略带调侃",
    "curious": "语气好奇、关注，略带疑问感",
    "surprised": "语气惊讶、上扬",
    "hopeful": "语气乐观、温暖，充满希望",
    "empathetic": "语气体贴、柔和，饱含理解与安抚",
    "sad": "语气低沉、克制，带着歉意",
    "angry": "语气坚定、郑重，先安抚再表达立场",
    "anxious": "语气温和、稳定，放慢节奏安抚",
    "calm": "语气平静、自然，字正腔圆",
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


@dataclass
class EmotionState:
    """共享的当前情绪状态：LLM 侧写入，TTS 侧读取合成动态语气。"""

    mood: str = "calm"

    def instruct_for_mood(self) -> str:
        return MOOD_INSTRUCT.get(self.mood, "")
