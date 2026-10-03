"""Bok patch: MiniMax bidi TTS 参数（官方协议凭据与音色；字段带 minimax_tts_ 前缀）。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MiniMaxTTSHandlerArguments:
    minimax_tts_api_key: str = field(
        default="",
        metadata={"help": "MiniMax API Key（Bearer）。留空回退 env MINIMAX_TTS_API_KEY。"},
    )
    minimax_tts_voice: str = field(
        default="Cantonese_crisp_news_anchor_vv2",
        metadata={"help": "音色 ID（粤语系见 apps/web/lib/minimax-voices.ts 目录）。"},
    )
    minimax_tts_model: str = field(
        default="speech-2.8-hd",
        metadata={"help": "模型（speech-2.8-hd；标记系统生效档）。"},
    )
    minimax_tts_region: str = field(
        default="cn",
        metadata={"help": "cn=api.minimax.cn / intl=api.minimax.chat。"},
    )
    minimax_tts_endpoint: str = field(
        default="",
        metadata={"help": "显式 bidi WS 端点（覆盖 region；仅公网 wss/https）。"},
    )
    minimax_tts_sample_rate: int = field(
        default=16000,
        metadata={"help": "出声采样率（8000/16000/24000，管线内部转 16k）。"},
    )
    minimax_tts_speed: float = field(default=1.0, metadata={"help": "语速 0.5-2。"})
    minimax_tts_vol: float = field(default=1.0, metadata={"help": "音量 0-10。"})
    minimax_tts_pitch: int = field(default=0, metadata={"help": "音调 -12 到 12。"})
