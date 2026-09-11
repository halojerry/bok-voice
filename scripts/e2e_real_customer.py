"""真实客户多轮对话 E2E：模拟真人客户拿真问题跟 AI 连续聊一整场。

与既有 E2E（单发机械句、逐 case 断言）不同，本脚本跑的是「一场完整对话」：
  - 三个场景 = 三个人设 × 语言 × 6/6/5 轮真问题（文案在下方 SCENARIOS，运营可改）；
  - 每轮：TTS 现场合成客户话音 → 推音频轨 → 等 AI 答完（累计语音后再静默
    ANSWER_SILENCE_S 视为答完，或 ANSWER_TIMEOUT_S 兜底）→ 人味停顿 0.8-1.5s → 下一轮；
  - 整场结束挂断，从 /api/calls/{id}/turns 拉全量对话记录；
  - 产出可读报告：逐轮「客户说 → AI 答前 60 字 → 该轮实测指标」+ agent.log 本通
    窗口的 QA_FASTPATH / BOK_FILLER / WhatsApp 证据行。

句形铁律：每轮 ≤12 字、无逗号、无大停顿（vad-pause 劈轮免疫，见 AGENTS.md）。
对象 display_name 用 E2E- 前缀 = 心跳/收线豁免——哑轮是本脚本要测的信号，
不能让 8s 心跳垫话把死气掩盖（否则哑轮会被 nudge 音频「救活」成假有答）。

退出码：全场 AI 哑轮 ≥2 → FAIL(1)，否则 PASS(0)；QA/垫话命中只是信息位不强断言。

用法：<python> scripts/e2e_real_customer.py [--scenario zh|cantonese|en|all] [--persona-id X]
前置：`python tools/bok.py serve`（CP 8000 / LiveKit 7880 / TTS 8788），真 /api/token。
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import random
import struct
import time
from pathlib import Path

import httpx
from livekit import rtc

CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")
TTS_URL = os.environ.get("TTS_URL", "http://127.0.0.1:8788")
LOG_PATH = Path.home() / "Library" / "Application Support" / "BokVoice" / "logs" / "agent.log"

# 答完判定：出现过语音后，连续静默 ≥2.5s 视为答完；30s 无声=哑轮。
ANSWER_TIMEOUT_S = float(os.environ.get("BOK_CUSTOMER_TIMEOUT_S", "30"))
ANSWER_SILENCE_S = float(os.environ.get("BOK_CUSTOMER_SILENCE_S", "2.5"))
# 哑轮判据：本轮 agent 语音（含垫话 out-of-band）累计不足 0.3s。
MUTE_SPEECH_S = 0.3
FAIL_MUTE_ROUNDS = 2  # 哑 ≥2 轮 = FAIL

# 客户话音统一走本地 TTS sidecar 预设音色（三语同一把嗓子已足够拟真）。
CUSTOMER_VOICE = "Vivian"

# ---------------------------------------------------------------------------
# 场景文案（真实客户口吻，运营可直接改；句形铁律：≤12 字、无逗号、无大停顿）
# persona_voice = 该场 AI 人设音色（MiniMax 目录，见 apps/web/lib/minimax-voices.ts）
# ---------------------------------------------------------------------------
SCENARIOS: dict[str, dict] = {
    "zh": {
        "label": "小普（普通话·查件焦虑）",
        "lang": "zh",
        "persona_voice": "Chinese_crisp_podcaster_nv1",
        "lines": [
            "你好",
            "我的快递三天了还没到",
            "是在淘宝买的",
            "那我的件会丢吗",
            "好我想了解一下你们的产品",  # QA 词条原文——应命中罐头快路
            "行那你查一下",  # 慢生成轮——垫话观察位
        ],
    },
    "cantonese": {
        "label": "小九（粤语·查件+报 WhatsApp）",
        "lang": "cantonese",
        "persona_voice": "Cantonese_GentleLady",
        "lines": [
            "你好",
            "我個件遲咗三日未到",
            "京東買嘅",
            "咁會唔會丄件",
            "你哋WhatsApp號碼幾多",
            "六四三二五四三",  # 报号（7 位 ≥4 位门槛）——看 WA 捕获
        ],
    },
    "en": {
        "label": "Elen（英语·短问询）",
        "lang": "en",
        "persona_voice": "English_GentleTeacher",
        "lines": [
            "hi there",
            "my parcel is late",
            "bought it on Taobao",
            "will my item be lost",
            "how do I contact you",
        ],
    },
}

# agent.log 证据行（本通字节窗口内 grep；行式见 agent.py / fillers.py）
LOG_MARKERS = (
    b"QA_FASTPATH hit=",
    b"QA_FASTPATH_SUMMARY",
    b"BOK_FILLER fired",
    b"BOK_FILLER voice_fallback",
    b"[whatsapp]",
)


def frame_rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    n = len(pcm) // 2
    frames = struct.unpack(f"<{n}h", pcm)
    return math.sqrt(sum(x * x for x in frames) / n)


def speech_stats(pcm: bytes, processed: int) -> tuple[float, float, int]:
    """增量统计 (speech_secs, silent_secs, new_processed)，20ms 帧 RMS 口径。"""
    step = 320
    speech = silent = 0.0
    while processed + step <= len(pcm):
        if frame_rms(pcm[processed : processed + step]) >= 200:
            speech += 0.02
            silent = 0.0
        else:
            silent += 0.02
        processed += step
    return speech, silent, processed


def tts_pcm(text: str, lang: str) -> bytes:
    with httpx.Client(timeout=60) as client:
        r = client.post(
            f"{TTS_URL}/v1/audio/speech",
            json={"input": text, "language": lang, "voice": CUSTOMER_VOICE, "sample_rate": 16000},
        )
        r.raise_for_status()
        return r.content


async def push_pcm(audio_source: rtc.AudioSource, pcm: bytes) -> None:
    chunk = int(16000 * 0.1) * 2
    for i in range(0, len(pcm), chunk):
        seg = pcm[i : i + chunk]
        frame = rtc.AudioFrame(
            data=seg, sample_rate=16000, num_channels=1, samples_per_channel=len(seg) // 2
        )
        await audio_source.capture_frame(frame)
        await asyncio.sleep(0.08)


def log_slice_markers(offset: int) -> list[str]:
    """取 agent.log 自 offset 起的字节窗口，返回命中的证据行（保序去重）。"""
    if not LOG_PATH.exists():
        return []
    try:
        new_bytes = LOG_PATH.read_bytes()[offset:]
    except Exception:
        return []
    lines: list[str] = []
    for raw in new_bytes.splitlines():
        if any(m in raw for m in LOG_MARKERS):
            line = raw.decode("utf-8", errors="replace").strip()
            if line and line not in lines:
                lines.append(line)
    return lines[:40]


def create_call(lang: str, persona_id: str | None) -> tuple[str, str]:
    """建对象+人设+通话，返回 (call_id, persona_voice)。对象 E2E- 前缀=心跳豁免。"""
    ts = int(time.time() * 1000) % 100000
    obj = httpx.post(
        f"{CONTROL_PLANE_URL}/api/objects?account_id=acc-001",
        json={
            "display_name": f"E2E-真实客户{lang}-{ts}",
            "role_template": "buyer",
            "language": lang,
            "background": "real customer e2e",
        },
        timeout=10,
    ).json()
    if persona_id:
        persona = httpx.get(f"{CONTROL_PLANE_URL}/api/personas/{persona_id}", timeout=10).json()
        voice = str(persona.get("reference_audio") or "")
    else:
        persona = httpx.post(
            f"{CONTROL_PLANE_URL}/api/personas?account_id=acc-001",
            json={
                "name": f"E2E真实客服{lang}",
                "language": lang,
                "tone": "礼貌专业",
                "reference_audio": SCENARIOS[lang]["persona_voice"],
            },
            timeout=10,
        ).json()
        voice = str(persona.get("reference_audio") or "")
    call = httpx.post(
        f"{CONTROL_PLANE_URL}/api/calls",
        json={
            "account_id": "acc-001",
            "object_id": obj["id"],
            "persona_id": persona["id"],
            "mode": "live",
            "direction": "webrtc",
            "language": lang,
        },
        timeout=10,
    ).json()
    return str(call["id"]), voice


async def wait_greeting(agent_audio: bytearray) -> bool:
    """等开场白播完（≥1s 语音 + 3s 尾静音）；冷 worker 重试 3 轮。"""
    state = {"processed": 0, "speech": 0.0, "silent": 0.0}
    for _ in range(3):
        if state["speech"] < 1.0:
            state["processed"] = 0
            state["speech"] = 0.0
            state["silent"] = 0.0
        deadline = time.perf_counter() + 45
        while time.perf_counter() < deadline:
            s, sil, state["processed"] = speech_stats(bytes(agent_audio), state["processed"])
            state["speech"] += s
            state["silent"] += sil
            if state["speech"] >= 1.0 and state["silent"] >= 3.0:
                return True
            await asyncio.sleep(0.3)
    return state["speech"] >= 1.0


async def play_and_listen(
    audio_source: rtc.AudioSource, agent_audio: bytearray, pcm: bytes
) -> dict:
    """推一轮客户话音，等 AI 答完。返回该轮实测指标。"""
    mark = len(agent_audio)
    await push_pcm(audio_source, pcm)
    t_done = time.perf_counter()
    probe = mark
    speech = 0.0
    first_audio_ms: float | None = None
    deadline = t_done + ANSWER_TIMEOUT_S
    while time.perf_counter() < deadline:
        s, sil, probe = speech_stats(bytes(agent_audio), probe)
        speech += s
        if s > 0:
            sil = 0.0
        if first_audio_ms is None and speech >= 0.12:
            first_audio_ms = (time.perf_counter() - t_done) * 1000
        if first_audio_ms is not None and sil >= ANSWER_SILENCE_S:
            break
        await asyncio.sleep(0.1)
    return {
        "user_dur_s": len(pcm) / 32000.0,
        "first_audio_ms": first_audio_ms,
        "speech_s": speech,
        "answered": speech >= MUTE_SPEECH_S,
    }


async def fetch_turns(call_id: str, settle_s: float = 12.0) -> list[dict]:
    """挂断后轮询拉全量 turns，直到条数稳定（结算落库有写入窗口）。"""
    last: list[dict] = []
    stable = 0
    deadline = time.perf_counter() + settle_s
    while time.perf_counter() < deadline:
        rows = httpx.get(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/turns", timeout=10).json()
        if rows and len(rows) == len(last):
            stable += 1
            if stable >= 2:
                return rows
        else:
            stable = 0
        last = rows
        await asyncio.sleep(1.5)
    return last


def blocks_from_turns(turns: list[dict]) -> list[dict]:
    """按 user 轮分块：每块 = 客户话 + 其后连续的 AI 行（含开场白独立块）。"""
    blocks: list[dict] = []
    for t in turns:
        role = str(t.get("role") or "")
        if role == "user" or not blocks:
            blocks.append({"user": t if role == "user" else None, "replies": []})
            if role != "user":
                blocks[-1]["replies"].append(t)
        else:
            blocks[-1]["replies"].append(t)
    return blocks


async def run_scenario(key: str, persona_id: str | None) -> dict:
    sc = SCENARIOS[key]
    lang = sc["lang"]
    lines: list[str] = sc["lines"]
    print(f"\n[real-customer] 场景 {key} · {sc['label']} —— 预合成 {len(lines)} 轮客户话音…", flush=True)
    pcms = [tts_pcm(text, lang) for text in lines]

    call_id, voice = create_call(lang, persona_id)
    log_offset = LOG_PATH.stat().st_size if LOG_PATH.exists() else 0
    print(f"[real-customer] call={call_id} persona_voice={voice!r} (log offset {log_offset})", flush=True)

    room = rtc.Room()
    agent_audio = bytearray()
    read_tasks: list[asyncio.Task] = []

    def attach(track) -> None:
        if int(track.kind) != int(rtc.TrackKind.KIND_AUDIO):
            return
        # roomio_audio=回复主音轨；background_audio=垫话 out-of-band（也算出声）
        if getattr(track, "name", "") not in ("roomio_audio", "background_audio"):
            return

        async def _read() -> None:
            stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
            try:
                async for event in stream:
                    frame = getattr(event, "frame", event)
                    agent_audio.extend(bytes(frame.data))
            except Exception:
                pass
            finally:
                try:
                    await stream.aclose()
                except Exception:
                    pass

        read_tasks.append(asyncio.get_running_loop().create_task(_read()))

    def on_track(track, _pub, _participant) -> None:
        attach(track)

    room.on("track_subscribed", on_track)
    for participant in room.remote_participants.values():
        for pub in participant.track_publications.values():
            track = getattr(pub, "track", None)
            if track is not None:
                attach(track)

    measures: list[dict] = []
    setup_ok = False
    try:
        data = httpx.post(
            f"{CONTROL_PLANE_URL}/api/token",
            json={"account_id": "acc-001", "call_id": call_id},
            timeout=10,
        ).json()
        await room.connect(data["serverUrl"], data["participantToken"])
        audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
        src = rtc.LocalAudioTrack.create_audio_track("customer-src", audio_source)
        await room.local_participant.publish_track(
            src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        setup_ok = await wait_greeting(agent_audio)
        if not setup_ok:
            print(f"[real-customer] WARN 开场白 45s×3 未出声，照常推进（预计全场哑轮）", flush=True)
        agent_audio.clear()
        await asyncio.sleep(0.5)

        for i, (text, pcm) in enumerate(zip(lines, pcms), start=1):
            m = await play_and_listen(audio_source, agent_audio, pcm)
            m["text"] = text
            measures.append(m)
            flag = "✓" if m["answered"] else "✗哑"
            first = f"{m['first_audio_ms'] / 1000:.1f}s" if m["first_audio_ms"] is not None else "-"
            print(
                f"    轮{i} 「{text}」 话音{m['user_dur_s']:.1f}s → 首声 {first} · 语音 {m['speech_s']:.1f}s · {flag}",
                flush=True,
            )
            await asyncio.sleep(random.uniform(0.8, 1.5))  # 人味停顿
    except Exception as exc:
        print(f"[real-customer] 场景 {key} 异常中断: {exc!r}", flush=True)
    finally:
        try:
            await room.disconnect()
        except Exception:
            pass
        for t in read_tasks:
            t.cancel()
        try:
            httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/hangup", timeout=10)
            httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/settle", timeout=30)
        except Exception:
            pass

    turns = await fetch_turns(call_id)
    evidence = log_slice_markers(log_offset)
    return {
        "key": key,
        "label": sc["label"],
        "lang": lang,
        "call_id": call_id,
        "voice": voice,
        "setup_ok": setup_ok,
        "measures": measures,
        "turns": turns,
        "evidence": evidence,
    }


def print_report(res: dict) -> None:
    """成品报告：整段对话记录 + 逐轮指标 + 本场证据行。"""
    print("\n" + "═" * 72, flush=True)
    print(f"场景 {res['key']} · {res['label']}   call={res['call_id']}  AI音色={res['voice'] or '(人设默认)'}", flush=True)
    print("═" * 72, flush=True)

    turns = res["turns"]
    if not turns:
        print("（对话记录落库为空——CP 未收到任何轮）", flush=True)
    blocks = blocks_from_turns(turns)
    print(f"对话记录（CP turns 落库，共 {len(turns)} 条）：", flush=True)
    round_no = 0
    for b in blocks:
        if b["user"] is None:
            for r in b["replies"]:
                print(f"  [AI·开场] {(r.get('transcript') or '').strip()}", flush=True)
            continue
        round_no += 1
        print(f"  轮{round_no} 客户: {(b['user'].get('transcript') or '').strip()}", flush=True)
        for r in b["replies"]:
            lat = r.get("latency_ms") or 0
            lat_s = f" · LLM {lat}ms" if lat else ""
            print(f"       AI  : {(r.get('transcript') or '').strip()[:60]}{lat_s}", flush=True)

    print("\n逐轮实测（讲完→AI 首声 / 语音总量）：", flush=True)
    mute = 0
    for i, m in enumerate(res["measures"], start=1):
        if not m["answered"]:
            mute += 1
        first = f"{m['first_audio_ms'] / 1000:.1f}s" if m["first_audio_ms"] is not None else "无"
        flag = "有答" if m["answered"] else "✗ 哑"
        print(
            f"  轮{i} 「{m['text']}」→ 首声 {first} · 语音 {m['speech_s']:.1f}s · {flag}",
            flush=True,
        )

    if res["evidence"]:
        print("\n本场证据行（agent.log 本通窗口）：", flush=True)
        for line in res["evidence"]:
            print(f"  {line}", flush=True)

    total = len(res["measures"])
    print(f"\n小计[{res['key']}]: {total} 轮 · 有答 {total - mute} · 哑 {mute}"
          + ("" if res["setup_ok"] else " · ⚠开场白未检出（setup 疑似失败）"), flush=True)
    res["mute"] = mute


async def main() -> int:
    parser = argparse.ArgumentParser(description="真实客户多轮对话 E2E")
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    parser.add_argument("--persona-id", default=None, help="覆盖人设（对该次运行所有场景生效）")
    args = parser.parse_args()
    keys = list(SCENARIOS) if args.scenario == "all" else [args.scenario]

    results = []
    for key in keys:
        results.append(await run_scenario(key, args.persona_id))
        print_report(results[-1])

    total_rounds = sum(len(r["measures"]) for r in results)
    total_mute = sum(r["mute"] for r in results)
    print("\n" + "═" * 72, flush=True)
    print(
        f"REAL_CUSTOMER_E2E 场景={len(results)} 总轮数={total_rounds} 哑轮={total_mute} → "
        f"{'FAIL' if total_mute >= FAIL_MUTE_ROUNDS else 'PASS'}",
        flush=True,
    )
    return 1 if total_mute >= FAIL_MUTE_ROUNDS else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
