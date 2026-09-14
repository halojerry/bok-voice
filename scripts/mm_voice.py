"""MiniMax 云合成 16k PCM 探针话音线(2026-09-13)。

本地 Qwen3-TTS 合成音的粤语短词 ASR 可懂度差(「拼多多」→「二。二。」实测),
E2E/验收探针需要与生产同源的清晰话音:t2a_v2 非流式直出 16k PCM,
粤=Cantonese_crisp_news_anchor_vv2(A 线生产兜底音色)+Chinese,Yue。
回读实证:「拼多多,淘寶,定京東」→「拼多多。淘宝,定正东」(2/3 全对,
质变)。key 从设置 DB tts_json 读(不落明文);CA 按仓库铁律走 certifi。
"""
from __future__ import annotations

import json
import sqlite3
import ssl
import urllib.request
from pathlib import Path

_VOICES = {
    "cantonese": ("Cantonese_crisp_news_anchor_vv2", "Chinese,Yue"),
    "zh": ("Chinese_wenrounvxing", "Chinese"),
    "en": ("socialmedia_female_2_v1", "English"),
}


def mm_pcm(text: str, lang: str) -> bytes:
    import certifi

    db = sqlite3.connect(
        str(Path.home() / "Library/Application Support/BokVoice/bok_voice.db")
    )
    key = json.loads(
        db.execute(
            "SELECT tts_json FROM global_settings ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()[0]
    )["api_key"]
    voice, boost = _VOICES.get(lang, _VOICES["zh"])
    ctx = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(
        "https://api.minimax.cn/v1/t2a_v2",
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        data=json.dumps(
            {
                "model": "speech-2.8-hd",
                "text": text,
                "stream": False,
                "voice_setting": {"voice_id": voice, "speed": 1.0},
                "audio_setting": {"format": "pcm", "sample_rate": 16000},
                "language_boost": boost,
            }
        ).encode(),
    )
    r = json.loads(urllib.request.urlopen(req, timeout=30, context=ctx).read())
    if not r.get("data", {}).get("audio"):
        raise RuntimeError(f"minimax t2a_v2 empty: {r.get('base_resp')}")
    return bytes.fromhex(r["data"]["audio"])
