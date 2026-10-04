#!/usr/bin/env python3
"""MiniMax bidi 首 chunk 提前切 —— 耳朵 A/B 样本生成器（2026-09-28）。

对 3 条代表性粤语文本各合成两次：
  * ``BOK_TTS_FIRST_CHUNK_CHARS=0`` → 旧行为（首块等句界 / 整段透传）= *_sentence.wav
  * ``BOK_TTS_FIRST_CHUNK_CHARS=10`` → 首块句内 ≥10 字提前切 = *_early.wav

落盘 16k 单声道 PCM → WAV，并逐条打印首音频毫秒与总时长，供人耳对比。

用法（无参数即可）：
    .venv312/bin/python scripts/ab_tts_first_chunk.py
    .venv312/bin/python scripts/ab_tts_first_chunk.py --out /tmp/ab

API key / 音色来自 CP settings（``GET /api/settings?internal=true`` 的
``tts.api_key`` + 人设 ``reference_audio``），与运行时 / scripts/pregen_tts.py 同源；
也可用 ``MINIMAX_API_KEY`` / ``MINIMAX_VOICE`` 覆盖。
"""

from __future__ import annotations
# --- scripts import bootstrap (G1) ---
# sys.path 引导(G1 迁移解耦,见 docs/superpowers/plans/2026-10-04-repo-governance-plan.md §3.1):
# 同层时是 no-op;文件挪进任何桶后裸 import 兄弟模块继续解析。
import sys as _sys, pathlib as _pathlib
_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))


import argparse
import asyncio
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import wave
from pathlib import Path

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _safe_urlopen(req, *, timeout: float):
    """出站闸门（tools/bok.py 同形状）：urlopen 前就地校验 Request.full_url
    ——仅 http/https、host 非空、无 userinfo；不过闸=PermissionError。
    本探针目标=本地 CP / MiniMax 诊断端点。"""
    parts = urllib.parse.urlsplit(req.full_url)
    host = (parts.hostname or "").lower()
    if not (
        parts.scheme in ("http", "https")
        and (host in _LOOPBACK_HOSTS or bool(host))
        and not parts.username
        and not parts.password
    ):
        raise PermissionError(f"出站 URL 未过护栏（拒发）: {req.full_url}")
    return urllib.request.urlopen(req, timeout=timeout)


ROOT = Path(__file__).resolve().parents[1]
for _p in ("apps/agent", "packages/core"):
    _path = str(ROOT / _p)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    LanguageState,
    MiniMaxTTS,
)

SAMPLE_RATE = 16000  # 16k PCM（耳朵 A/B 足够；运行时默认 24k）
LANG = "cantonese"

# (tag, 文本)。文本 1=短首句；2=中等；3=长首句（≥25 字含单号数字 run）。
TEXTS: list[tuple[str, str]] = [
    ("short", "好的。我即刻幫你核實。"),
    ("mid", "好，我哋即刻提交資料，專員會加你收截圖。"),
    (
        "long",
        "你的單號係六四三一一三三，我哋已經收到你嘅資料，"
        "專員會喺今日內加你WhatsApp，麻煩你留意一下，多謝你耐心等候。",
    ),
]


def _cp_get(base: str, path: str, token: str) -> object:
    url = f"{base.rstrip('/')}{path}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with _safe_urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _voice_from_personas(personas: list) -> str:
    """人设 reference_audio（{lang: voice_id} JSON）取粤语音色，缺省回落 zh。"""
    for p in personas or []:
        ref = (p or {}).get("reference_audio")
        if not ref:
            continue
        try:
            m = json.loads(ref) if isinstance(ref, str) else ref
        except Exception:  # noqa: BLE001 - 坏 JSON 跳过
            continue
        if isinstance(m, dict):
            v = str(m.get(LANG) or m.get("zh") or "")
            if v:
                return v
    return ""


def _resolve_voice(settings: dict, personas: list) -> str:
    env_v = os.environ.get("MINIMAX_VOICE", "")
    if env_v:
        return env_v
    tts_cfg = settings.get("tts") or {}
    raw = tts_cfg.get("voice")
    if isinstance(raw, dict):
        v = str(raw.get(LANG) or raw.get("zh") or "")
        if v:
            return v
    if isinstance(raw, str) and raw:
        return raw
    return _voice_from_personas(personas)


def _fetch_settings_and_personas(base: str, token: str) -> tuple[dict, list]:
    settings = _cp_get(base, "/api/settings?internal=true", token)
    if not isinstance(settings, dict):
        settings = {}
    try:
        personas = _cp_get(base, "/api/personas?account_id=", token) or []
    except Exception:  # noqa: BLE001 - 人设不可读不阻合成（音色走 env/设置）
        personas = []
    return settings, list(personas)


async def _synth_stream(provider: MiniMaxTTS, text: str) -> tuple[bytes, float, float]:
    """真流式合成（bidi stream），返回 (pcm, 首音频 ms, 总时长 ms)。"""
    buf = bytearray()
    t0 = time.monotonic()
    first_ms = -1.0
    async with provider.stream() as stream:
        stream.push_text(text)
        stream.end_input()
        async for ev in stream:
            frame = getattr(ev, "frame", None)
            if frame is None:
                continue
            data = frame.data
            if first_ms < 0:
                first_ms = (time.monotonic() - t0) * 1000
            buf.extend(data.tobytes() if isinstance(data, memoryview) else bytes(data))
    total_ms = (time.monotonic() - t0) * 1000
    return bytes(buf), first_ms, total_ms


def _write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)


def _build_provider(api_key: str, voice: str) -> MiniMaxTTS:
    return MiniMaxTTS(
        voice={LANG: voice, "zh": voice},
        language_state=LanguageState(lang=LANG),
        sample_rate=SAMPLE_RATE,
        api_key=api_key,
    )


async def main_async() -> int:
    ap = argparse.ArgumentParser(description="MiniMax bidi 首 chunk 提前切 A/B 样本")
    ap.add_argument(
        "--out",
        default=os.environ.get(
            "BOK_TTS_AB_OUT", str(Path.home() / "Desktop" / "tts_first_chunk_ab")
        ),
        help="输出目录（缺省 $BOK_TTS_AB_OUT 或 ~/Desktop/tts_first_chunk_ab）",
    )
    ap.add_argument("--cp", default=os.environ.get("BOK_CP_URL", "http://127.0.0.1:8000"))
    ap.add_argument(
        "--chars",
        default=None,
        help="自定义臂集（逗号分隔，如 0,6,8,10）；缺省=三臂 0/6/10",
    )
    args = ap.parse_args()

    token = os.environ.get("BOK_CP_TOKEN", "")
    try:
        settings, personas = _fetch_settings_and_personas(args.cp, token)
    except Exception as exc:  # noqa: BLE001 - CP 不可达仍可用 env key/voice
        print(f"WARN: CP settings 拉取失败（{exc!r}）——回退 env MINIMAX_API_KEY/MINIMAX_VOICE", flush=True)
        settings, personas = {}, []

    tts_cfg = settings.get("tts") or {}
    api_key = os.environ.get("MINIMAX_API_KEY") or str(tts_cfg.get("api_key") or "")
    voice = _resolve_voice(settings, personas)
    if not api_key:
        print(
            "MINIMAX api_key 缺失（CP settings tts.api_key / 环境 MINIMAX_API_KEY）——无法合成",
            flush=True,
        )
        return 1
    if not voice:
        print(
            "音色缺失（人设 reference_audio / 设置 tts.voice / 环境 MINIMAX_VOICE）——无法合成",
            flush=True,
        )
        return 1

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    # 生产档=bidi（服务端攒句）;必须走 bidi 这条持久连接才吃到首 chunk 提前切。
    os.environ.setdefault("MINIMAX_WS_MODE", "bidi")
    # 粤语通话运行时靠 language_boost 锁语种，A/B 补齐同款环境。
    os.environ.setdefault("MINIMAX_LANGUAGE_BOOST", "Chinese,Yue")
    # 三臂（2026-09-30 Phase 5a）：0=句界旧行为 / 6=快刀候选 / 10=现行默认——
    # Ethan 耳测定 N 后 _tts_first_chunk_chars 默认翻档。--chars 可自定义臂集。
    print(f"OUT {out_dir}  voice={voice}  rate={SAMPLE_RATE}", flush=True)

    arms: list[tuple[str, str]] = [
        ("0", "sentence"),
        ("6", "early6"),
        ("10", "early10"),
    ]
    _chars_arg = getattr(args, "chars", None)
    if _chars_arg:
        arms = [(c.strip(), f"n{c.strip()}") for c in _chars_arg.split(",") if c.strip()]

    for idx, (tag, text) in enumerate(TEXTS):
        print(f"\n=== [{idx}] {tag}: {text}", flush=True)
        for mode, suffix in arms:
            os.environ["BOK_TTS_FIRST_CHUNK_CHARS"] = mode
            provider = _build_provider(api_key, voice)
            try:
                pcm, first_ms, total_ms = await _synth_stream(provider, text)
            finally:
                # 关掉 bidi 持久连接，banner 干净。
                try:
                    await provider.aclose()
                except Exception:  # noqa: BLE001
                    pass
            path = out_dir / f"{idx}_{tag}_{suffix}.wav"
            _write_wav(path, pcm)
            dur_ms = len(pcm) / 2 / SAMPLE_RATE * 1000
            print(
                f"  {suffix:8s} BOK_TTS_FIRST_CHUNK_CHARS={mode:>2s}  "
                f"first_audio_ms={first_ms:.0f}  total_ms={total_ms:.0f}  "
                f"audio_ms={dur_ms:.0f}  bytes={len(pcm)}  -> {path.name}",
                flush=True,
            )
    print("\nDONE — 逐 tag 对比 *_sentence / *_early6 / *_early10 首声与断点，耳测定 N。", flush=True)
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
