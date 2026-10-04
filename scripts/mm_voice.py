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
import urllib.parse
import urllib.request
from pathlib import Path

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _safe_urlopen(req, *, timeout: float, context=None):
    """出站闸门（tools/bok.py 同形状）：urlopen 前就地校验 Request.full_url
    ——仅 http/https、host 非空、无 userinfo；不过闸=PermissionError。
    本脚本目标=MiniMax 云端 TTS 端点（字面量 host 白名单）。"""
    parts = urllib.parse.urlsplit(req.full_url)
    host = (parts.hostname or "").lower()
    if not (
        parts.scheme in ("http", "https")
        and (host in _LOOPBACK_HOSTS or bool(host))
        and not parts.username
        and not parts.password
    ):
        raise PermissionError(f"出站 URL 未过护栏（拒发）: {req.full_url}")
    return urllib.request.urlopen(req, timeout=timeout, context=context)

_VOICES = {
    "cantonese": ("Cantonese_crisp_news_anchor_vv2", "Chinese,Yue"),
    "zh": ("Chinese_wenrounvxing", "Chinese"),
    "en": ("socialmedia_female_2_v1", "English"),
}

# 固定云端端点(字面量):协议 https + host 白名单单点,拒绝任何动态/内网/
# 环回目标(SSRF 边界)。
_T2A_URL = "https://api.minimax.cn/v1/t2a_v2"
_T2A_ORIGIN = "https://api.minimax.cn/"


def mm_pcm(text: str, lang: str) -> bytes:
    """MiniMax 云合成 16k PCM;**磁盘缓存**(2026-10-01):探针语料逐轮同句,
    每次现调云=一通多花 30-60s 网络(BOK_PROBE_STIMULUS=cloud 实测)——以
    (lang,voice,text) 哈希落 app-data/probe-stimulus-cache/,只有首跑付合成
    钱。命中静默回放;写盘失败降级直调。BOK_PROBE_STIMULUS_CACHE=0 关。"""
    import hashlib
    import os

    voice = _VOICES.get(lang, _VOICES["zh"])[0]
    cache_dir = Path.home() / "Library/Application Support/BokVoice/probe-stimulus-cache"
    cache_path = cache_dir / f"{hashlib.sha256(f'{lang}|{voice}|{text}'.encode()).hexdigest()[:24]}.pcm"
    use_cache = os.environ.get("BOK_PROBE_STIMULUS_CACHE", "1") == "1"
    if use_cache:
        try:
            if cache_path.is_file():
                return cache_path.read_bytes()
        except OSError:
            pass
    pcm = b""
    for _attempt in range(3):  # 云端抖动重试(2 次退避),30s×3 才认输
        try:
            pcm = _mm_pcm_fetch(text, lang)
            break
        except (TimeoutError, OSError, RuntimeError):
            if _attempt == 2:
                pcm = b""
                break
            import time as _t

            _t.sleep(2 * (_attempt + 1))
    if not pcm:
        # 云端抖动(30s 读超时×3 实弹):有陈旧缓存宁可复用陈旧,也别让整跑崩
        if use_cache:
            try:
                if cache_path.is_file():
                    print(f"[mm_pcm] cloud timeout — reuse stale cache {cache_path.name}", flush=True)
                    return cache_path.read_bytes()
            except OSError:
                pass
        raise RuntimeError("mm_pcm: cloud fetch failed and no stale cache")
    if use_cache:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(pcm)
        except OSError:
            pass
    return pcm


def _mm_pcm_fetch(text: str, lang: str) -> bytes:
    import certifi

    if not _T2A_URL.startswith(_T2A_ORIGIN):
        raise RuntimeError("t2a endpoint hardening: unexpected origin")

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
        _T2A_URL,
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
    r = json.loads(_safe_urlopen(req, timeout=30, context=ctx).read())
    if not r.get("data", {}).get("audio"):
        raise RuntimeError(f"minimax t2a_v2 empty: {r.get('base_resp')}")
    return bytes.fromhex(r["data"]["audio"])
