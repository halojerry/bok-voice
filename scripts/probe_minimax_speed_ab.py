#!/usr/bin/env python3
"""MiniMax 语速 A/B 探针(2026-09-12):classic HTTP vs bidi WS 是否真吃 speed。

用户实测「开场白/罐头/垫音(1.2 物化)与后续 TTS 回复(bidi 直播)语速唔一样」——
怀疑 t2a_v2_bidi 服务端唔理会 task_start voice_setting.speed。本探针同一文本/
音色/模型,跑 classic×{1.0,1.2} + bidi×{1.0,1.2} 四格,数音频帧算时长:
  - classic 1.2 ≈ classic 1.0 / 1.2 → classic 吃 speed(已知)。
  - bidi 1.2 ≈ classic 1.2 → bidi 都吃,speed 差异另有因(音色/听感)。
  - bidi 1.2 ≈ classic 1.0 → bidi 唔吃 speed,实锤根因。

用法: .venv312/bin/python scripts/probe_minimax_speed_ab.py [--text 自定义]
API key 取设置 DB tts.api_key(同 tts-pregen),SSL_CERT_FILE 已由环境带 certifi。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "agent"))

from agent_runtime.providers.livekit_plugins import LanguageState, MiniMaxTTS  # noqa: E402

TEXT_DEFAULT = (
    "首先我们会先核实您这件货品的订单金额，如果金额不足一百元，"
    "我们会根据香港速递条例帮您申请三百到六百元的赔偿。"
)
VOICE = os.environ.get("SPEED_AB_VOICE", "Chinese_wenrounvxing")


def _api_key_from_settings() -> str:
    db = os.path.expanduser("~/Library/Application Support/BokVoice/bok_voice.db")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
    try:
        row = con.execute("SELECT tts_json FROM global_settings LIMIT 1").fetchone()
    finally:
        con.close()
    if not row:
        return ""
    try:
        cfg = json.loads(row[0])
        return str(cfg.get("api_key") or "")
    except Exception:
        return ""


async def measure(mode: str, speed: float, text: str, key: str) -> float:
    """跑一次合成,返回音频时长(s);失败返回 -1。"""
    os.environ["MINIMAX_WS_MODE"] = mode
    os.environ["MINIMAX_SPEED"] = str(speed)
    tts = MiniMaxTTS(
        voice=VOICE,
        language_state=LanguageState(lang="zh"),
        sample_rate=24000,
        api_key=key,
    )
    samples = 0
    t0 = time.monotonic()
    async with tts.synthesize(text) as stream:
        async for ev in stream:
            frame = getattr(ev, "frame", None)
            if frame is not None:
                samples += frame.samples_per_channel * frame.num_channels
    dur = samples / 24000.0
    wall = time.monotonic() - t0
    print(
        f"[{mode:>7} speed={speed}] audio={dur:6.2f}s wall={wall:5.2f}s "
        f"chars={len(text)} chars_per_s={dur and len(text) / dur:.2f}"
    )
    try:
        await tts.aclose()
    except Exception:
        pass
    return dur


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default=TEXT_DEFAULT)
    args = ap.parse_args()

    key = os.environ.get("MINIMAX_API_KEY", "") or _api_key_from_settings()
    if not key:
        print("缺 MiniMax API key(设置 DB tts.api_key)", file=sys.stderr)
        return 2

    results: dict[tuple[str, float], float] = {}
    for mode in ("classic", "bidi"):
        for speed in (1.0, 1.2):
            dur = await measure(mode, speed, args.text, key)
            results[(mode, speed)] = dur
            await asyncio.sleep(1.0)

    c10, c12 = results[("classic", 1.0)], results[("classic", 1.2)]
    b10, b12 = results[("bidi", 1.0)], results[("bidi", 1.2)]
    print("\n=== 结论 ===")
    if min(c10, c12, b10, b12) < 0:
        print("有格子失败,看上面错误,勿下结论")
        return 1
    print(f"classic 加速比 1.0/1.2 = {c10 / c12:.2f}(应≈1.20)")
    print(f"bidi   加速比 1.0/1.2 = {b10 / b12:.2f}")
    if abs(b10 / b12 - 1.0) < 0.05:
        print("→ bidi 两个 speed 档时长一样:bidi 不吃 speed(实锤, replies 恒 1.0)")
    elif b12 <= b10 * 0.93:
        print("→ bidi 吃 speed(1.2 明显变短):速度差异另有根因(音色/听感)")
    else:
        print("→ bidi speed 效果模糊,人工听感复核")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
