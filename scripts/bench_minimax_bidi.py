#!/usr/bin/env python3
"""MiniMax bidi 直连台架(2026-09-29,Ethan「查官方文档,是不是我们配置有问题」)。

官方文档定案(t2a_v2_bidi):
  - 句末标点(。!?…?.)→立即合成零加延;逗号级→"攒够才切";无标点→兜底窗干等
  - task_continue 任意粒度(逐字也行),服务端自己攒句
我们生产管线:首 6 字早发,**后续按句界对齐**(复读守卫+句界缓冲)——
本台架把 first_continue_to_audio_ms 拆成「我们扣字的账」vs「服务端合成的账」,
并同场对照 speech-2.8-hd vs speech-2.8-turbo(官方低延迟档)。

场景(每场新连接+task_start,rep ×3):
  A_full     一次发整句(句号结尾)      → 服务端纯合成地板(合成+RTT)
  B_prod     6 字即发,700ms 后发余句   → 我们当前生产形状
  D_charbychar 逐字 28ms 间隔流式       → 「照 LLM 原速立刻转发」=修复形状
  C_comma_hold 6 字后 1.5s 不再发       → 逗号攒句阈值/无标点兜底窗探针

用法: .venv312/bin/python scripts/bench_minimax_bidi.py [--voice Cantonese_GentleLady]
凭据: 从 bok_voice.db global_settings.tts_json 读 api_key(零字面量)。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import time

import websockets

DB = "/Users/halo/Library/Application Support/BokVoice/bok_voice.db"
WS_URL = "wss://api.minimax.cn/ws/v1/t2a_v2_bidi"
SENT = "好嘅，我即刻幫你查下你個包裹嘅賠償進度。"
HEAD6 = SENT[:6]  # 「好嘅，我即刻」(含一枚逗号,同生产首切)
REST = SENT[6:]


def _api_key() -> str:
    con = sqlite3.connect(DB)
    raw = con.execute("select tts_json from global_settings").fetchone()[0]
    con.close()
    return str(json.loads(raw).get("api_key") or "")


async def _first_audio_at(ws, deadline: float) -> float | None:
    """读到首个含 audio 的消息,返回时刻(perf_counter);超时返回 None。"""
    while time.perf_counter() < deadline:
        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.perf_counter()))
        msg = json.loads(raw)
        data = msg.get("data") or {}
        if data.get("audio"):
            return time.perf_counter()
    return None


async def _run_once(key: str, model: str, voice: str, scenario: str) -> float:
    async with websockets.connect(
        WS_URL,
        additional_headers={"Authorization": f"Bearer {key}"},
        open_timeout=10,
        max_size=20_000_000,
    ) as ws:
        await ws.send(json.dumps({
            "event": "task_start",
            "model": model,
            "voice_setting": {"voice_id": voice, "speed": 1.0, "vol": 1.0, "pitch": 0},
            "audio_setting": {"sample_rate": 24000, "format": "pcm", "channel": 1},
            "language_boost": "Chinese,Yue",
        }))
        # 等 task_started
        t0 = None
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if msg.get("event") == "task_started":
                break
        t0 = time.perf_counter()
        if scenario == "A_full":
            await ws.send(json.dumps({"event": "task_continue", "text": SENT}))
        elif scenario == "B_prod":
            await ws.send(json.dumps({"event": "task_continue", "text": HEAD6}))
            await asyncio.sleep(0.7)
            await ws.send(json.dumps({"event": "task_continue", "text": REST}))
        elif scenario == "D_charbychar":
            for ch in SENT:
                await ws.send(json.dumps({"event": "task_continue", "text": ch}))
                await asyncio.sleep(0.028)
        elif scenario == "C_comma_hold":
            await ws.send(json.dumps({"event": "task_continue", "text": HEAD6}))
            # 不再发,观察兜底窗
        elif scenario == "E_early_flush":
            # 早发 6 字 + 立刻 task_flush(官方:flush=已缓冲文本立即合成,会话不关)
            await ws.send(json.dumps({"event": "task_continue", "text": HEAD6}))
            await ws.send(json.dumps({"event": "task_flush"}))
        elif scenario == "F_flush_then_continue":
            # E 完整性:flush 出首声后,余句照常 continue,验会话活着+第二段音频
            await ws.send(json.dumps({"event": "task_continue", "text": HEAD6}))
            await ws.send(json.dumps({"event": "task_flush"}))
            t1 = await _first_audio_at(ws, deadline=t0 + 6.0)
            rest_t0 = time.perf_counter()
            await ws.send(json.dumps({"event": "task_continue", "text": REST}))
            await ws.send(json.dumps({"event": "task_flush"}))
            t2 = await _first_audio_at(ws, deadline=time.perf_counter() + 6.0)
            return (t1 - t0) * 1000 if t1 else -1, (t2 - rest_t0) * 1000 if t2 else -1
        elif scenario.startswith("G_"):
            # N×flush 扫描（2026-10-03 批次0.7）:G_<n>_<f|x>——首 n 字即发,
            # f=紧随 task_flush(生产头段催产)/x=只早发不催产(第八波原形)。
            # 返回首帧 ms(自首 continue 起算);余句照发保会话完整(不计时)。
            _, _n_s, _f_s = scenario.split("_")
            _head = SENT[: int(_n_s)]
            _rest = SENT[int(_n_s):]
            await ws.send(json.dumps({"event": "task_continue", "text": _head}))
            if _f_s == "f":
                await ws.send(json.dumps({"event": "task_flush"}))
            t1 = await _first_audio_at(ws, deadline=t0 + 6.0)
            if _rest:
                await ws.send(json.dumps({"event": "task_continue", "text": _rest}))
                if _f_s == "f":
                    await ws.send(json.dumps({"event": "task_flush"}))
            return (t1 - t0) * 1000 if t1 else -1.0
        # 收到首个含 audio 的消息即停(首帧)
        deadline = t0 + 6.0
        while time.perf_counter() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.perf_counter()))
            msg = json.loads(raw)
            data = msg.get("data") or {}
            if data.get("audio"):
                return (time.perf_counter() - t0) * 1000
        return -1.0  # 6s 无音频


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", default="Cantonese_GentleLady")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--sweep", action="store_true",
                    help="N×flush 扫描(批次0.7):N∈{1,2,4,6} × flush∈{on,off}")
    args = ap.parse_args()
    key = _api_key()
    if not key:
        print("no api_key in settings DB")
        return 1
    if args.sweep:
        print(f"{'model':18s} {'cell':14s} reps_ms")
        for model in ("speech-2.8-hd",):
            for n in (1, 2, 4, 6):
                for f in ("f", "x"):
                    sc = f"G_{n}_{f}"
                    vals = []
                    for _ in range(args.reps):
                        try:
                            vals.append(await _run_once(key, model, args.voice, sc))
                        except Exception as e:  # noqa: BLE001
                            vals.append(-99)
                            print(f"  exc {model}/{sc}: {e}")
                        await asyncio.sleep(0.4)
                    print(f"{model:18s} N={n} flush={'on' if f == 'f' else 'off':3s} "
                          + " ".join(f"{v:7.0f}" for v in vals))
                    await asyncio.sleep(0.6)
        return 0
    print(f"{'model':18s} {'scenario':14s} reps_ms")
    for model in ("speech-2.8-hd",):
        for sc in ("F_flush_then_continue",):
            vals = []
            for _ in range(args.reps):
                try:
                    vals.append(await _run_once(key, model, args.voice, sc))
                except Exception as e:  # noqa: BLE001
                    vals.append(-99)
                    print(f"  exc {model}/{sc}: {e}")
                await asyncio.sleep(0.3)
            if sc == "F_flush_then_continue":
                cells = " ".join(f"{a}/{b}" for a, b in vals)  # 首帧ms/余句帧ms
                print(f"{model:18s} {sc:14s} {cells}")
            else:
                print(f"{model:18s} {sc:14s} " + " ".join(f"{v:7.0f}" for v in vals))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
