from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# expression-trainer lexicon stub; later load data/emotion-lexicon.json
SENTIMENT_LEXICON: dict[str, list[str]] = {
    "happy": ["开心", "太好了", "满意"],
    "angry": ["生气", "不满意", "投诉"],
    "sad": ["失望", "难过"],
}


@dataclass
class EmotionProcessor:
    lexicon: dict[str, list[str]] = field(default_factory=lambda: SENTIMENT_LEXICON)

    def classify(self, text: str) -> str:
        for emotion, words in self.lexicon.items():
            if any(w in text for w in words):
                return emotion
        return "neutral"
