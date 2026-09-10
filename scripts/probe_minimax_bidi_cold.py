#!/usr/bin/env python3
"""bidi 冷启动归因探针(2026-09-10)。

回答:实测 3.17s TTS 首包离群的构成——
A) 握手段(connect+connected_success+task_start/task_started RTT)
B) 服务端处女连接首次合成预热(同连接第1 vs 第2次合成对比)
C) 空闲后/2201 断连后重建连接的合成首包回升
用法: MINIMAX_API_KEY=... .venv312/bin/python scripts/probe_minimax_bidi_cold.py
"""
from __future__ import annotations
import asyncio, json, os, sys, time

import websockets  # 仓库运行时已有

MODEL = os.environ.get("MINIMAX_MODEL", "speech-2.8-hd")
VOICE = os.environ.get("MINIMAX_BIDI_PROBE_VOICE", "Cantonese_crisp_news_anchor_vv2")
ENDPOINT = os.environ.get(
    "MINIMAX_WS_URL",
    f"wss://api.{'minimax.io' if os.environ.get('MINIMAX_REGION','cn')=='intl' else 'minimax.cn'}"
    "/ws/v1/t2a_v2_bidi",
)
TEXT = os.environ.get("MINIMAX_BIDI_PROBE_TEXT", "好，我而家就幫你睇下。")

async def connect_start(key: str):
    t0 = time.monotonic()
    ws = await websockets.connect(
        ENDPOINT, additional_headers={"Authorization": f"Bearer {key}"},
        open_timeout=10, max_size=20_000_000, ping_interval=None,
    )
    t_connect = time.monotonic()
    try:
        await asyncio.wait_for(ws.recv(), timeout=10)  # connected_success
    except Exception:
        pass
    # 字段与 livekit_plugins._task_start_payload 同源;audio_setting pcm/24k/mono
    await ws.send(json.dumps({
        "event": "task_start", "model": MODEL,
        "voice_setting": {"voice_id": VOICE, "speed": 1.2, "vol": 1.0, "pitch": 0},
        "audio_setting": {"sample_rate": 24000, "bitrate": 128000, "format": "pcm", "channel": 1},
        "stream_options": {"exclude_aggregated_audio": True},
    }))
    resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
    assert resp.get("event") == "task_started", resp
    return ws, (time.monotonic() - t0) * 1000, (time.monotonic() - t_connect) * 1000

async def synth_once(ws, tag: str) -> float:
    """一次合成:发文本+flush,量首音频与全部收完耗时(ms)。"""
    t0 = time.monotonic(); first = None
    # text 顶层字段,同 livekit_plugins._send_text({event, text});data 嵌套服务端
    # 静默丢弃(task_flushed 空手而回),以仓内实现为准。
    await ws.send(json.dumps({"event": "task_continue", "text": TEXT}))
    await ws.send(json.dumps({"event": "task_flush"}))
    t_text = time.monotonic()
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=30)
        msg = json.loads(raw)
        ev = msg.get("event"); data = msg.get("data") or {}
        if (data.get("audio") or "") and first is None:
            first = (time.monotonic() - t_text) * 1000
        if ev == "task_flushed":
            break
    print(f"[{tag}] text→first_audio={first and round(first)}ms total={(time.monotonic()-t0)*1000:.0f}ms")
    return first or -1

async def main() -> int:
    key = os.environ.get("MINIMAX_API_KEY", "")
    if not key:
        print("缺 MINIMAX_API_KEY(设置 DB tts.api_key 的值)", file=sys.stderr); return 2
    ws, total_ms, post_ms = await connect_start(key)
    print(f"[connect] total={total_ms:.0f}ms (ws握手后段={post_ms:.0f}ms)")
    await synth_once(ws, "处女连接·第1次合成")
    await synth_once(ws, "同连接·第2次合成")
    for idle_s in (30, 60, 110):
        t0 = time.monotonic()
        while time.monotonic() - t0 < idle_s:  # 60s 自管 ping,同生产
            await asyncio.sleep(min(60, idle_s)); await asyncio.wait_for(ws.ping(), timeout=10)
        await synth_once(ws, f"ping 保活空闲{idle_s}s 后")
    # 不 ping 挂 130s:等 2201(官方 ~120s 空闲断连)
    print("[2201 观察] 停 ping 130s …", flush=True)
    t0 = time.monotonic(); closed = None
    while time.monotonic() - t0 < 140:
        try:
            await asyncio.wait_for(ws.recv(), timeout=10)
        except asyncio.TimeoutError:
            continue  # 10s 无服务端推送=连接仍活着,继续观察 2201
        except Exception as exc:
            closed = f"{time.monotonic()-t0:.0f}s {exc!r}"
            break
    print(f"[2201 观察] 结果: {closed or '未断(140s)'}")
    ws2, t2, _ = await connect_start(key)
    print(f"[2201 重建] connect={t2:.0f}ms")
    await synth_once(ws2, "重建后·第1次合成")
    await ws2.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
