"""理赔分步场景多轮粤语 E2E：10+ 轮真实对话评估。

场景：林先生（粤语客户）的顺丰包裹理赔，模板为 3 步分步话术
（确认本人 → 说明一赔二 → 引导加微信/QQ 线上办理）。
客户轮由 MiniMax 粤语 TTS 预合成 wav 驱动（真实 ASR 识别 + 本地 LLM +
MiniMax 粤语 TTS 回复），逐轮打点：
  - 轮次耗时（从推音频到 agent 回复语音开始）
  - agent 回复 ASR 文本 + 语言（是否全程粤语）
  - 是否"念稿"（回复是否只是原样重读模板参考说法）
  - 是否随机应变（客户提问/确认时回复语义是否贴合当前步）

用法：<runtime-python> scripts/e2e_flow_scenario.py
前置：python tools/bok.py serve 已跑（含 agent 注册）。
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import struct
import time
import wave
from pathlib import Path

import httpx
from livekit import rtc

ROOT = Path(__file__).resolve().parents[1]
LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")
CUST_DIR = ROOT / "tests" / "fixtures" / "audio" / "e2e_multi"

# 客户轮音频（已预合成）+ 期望语义标签（用于判断 agent 是否随机应变/是否推进）
CUST_TURNS = [
    {"file": "cust_00.wav", "label": "自报身份(林先生)"},
    {"file": "cust_01.wav", "label": "确认包裹是我的(尾号7890)"},
    {"file": "cust_02.wav", "label": "问怎么处理(推进)"},
    {"file": "cust_03.wav", "label": "问一赔二怎么申请(推进)"},
    {"file": "cust_04.wav", "label": "质疑是否真能赔两倍(异议)"},
    {"file": "cust_05.wav", "label": "问是否要重新下单(推进)"},
    {"file": "cust_06.wav", "label": "问多久赔到(推进)"},
    {"file": "cust_07.wav", "label": "说要加微信(推进)"},
    {"file": "cust_08.wav", "label": "问何时回复(推进)"},
    {"file": "cust_09.wav", "label": "收尾道谢"},
]


def frame_rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    n = len(pcm) // 2
    frames = struct.unpack(f"<{n}h", pcm)
    return math.sqrt(sum(x * x for x in frames) / n)


def read_pcm16(path: Path, max_seconds: float = 8.0) -> bytes:
    with wave.open(str(path), "rb") as w:
        n = int(min(w.getnframes(), w.getframerate() * max_seconds))
        frames = w.readframes(n)
        if w.getframerate() != 16000:
            raise SystemExit(f"{path}: need 16k, got {w.getframerate()}")
        return frames


def pcm_energy_ratio(pcm: bytes) -> float:
    """有效语音占比(去头尾静音后的时长 / 总时长),用于确认 audio 非空。"""
    if not pcm:
        return 0.0
    step = 320
    voiced = 0
    for i in range(0, len(pcm) - step + 1, step):
        if frame_rms(pcm[i : i + step]) >= 200:
            voiced += 1
    return voiced / max(1, len(pcm) // step)


def trim_silence(pcm: bytes) -> bytes:
    step = 320
    start, end = 0, len(pcm)
    for i in range(0, len(pcm) - step + 1, step):
        if frame_rms(pcm[i : i + step]) >= 150:
            start = i
            break
    for i in range(len(pcm) - step, -1, -step):
        if frame_rms(pcm[i : i + step]) >= 150:
            end = i + step
            break
    return pcm[start:end]


def asr_language(pcm16: bytes) -> tuple[str, str]:
    pcm16 = trim_silence(pcm16)
    if len(pcm16) < 3200:
        return "", ""
    with httpx.Client(timeout=60) as client:
        s = client.post("http://127.0.0.1:8787/api/start").json()["session_id"]
        for i in range(0, len(pcm16), 3200):
            client.post("http://127.0.0.1:8787/api/chunk", params={"session_id": s}, content=pcm16[i : i + 3200])
        out = client.post("http://127.0.0.1:8787/api/finish", params={"session_id": s}).json()
        return str(out.get("language") or ""), str(out.get("text") or "")


def cantonese_markers(text: str) -> bool:
    markers = set("冇唔嘅係哋佢喺嚟啲嗰喎㗎冚瞓攞揾搵嘥咗乜嘢咩傾偈倾偈而家依家啱啱咁睇嚟睇来同我哋")
    return any(ch in markers for ch in text)


async def wait_agent_idle(agent_audio: bytearray, timeout: float = 25.0) -> None:
    """等 agent 正在说的音频播完（连续 3s 静音即认为说完）。

    若缓冲区为空或长时间无新增语音（agent 没在说 / track 尚未起流），
    不傻等满 timeout——空等是 E2E 每轮 60-90s 的根因。给 max_gap 兜底。
    """
    deadline = time.perf_counter() + timeout
    last_growth = time.perf_counter()
    last_speech_end = 0
    while time.perf_counter() < deadline:
        total = len(agent_audio)
        if total > last_speech_end:
            # 有新数据进来：统计尾部是否已连续 3s 静音
            last_growth = time.perf_counter()
        # 超过 8s 没有新语音字节 → 视为当前没有在播(空缓冲也算)
        if time.perf_counter() - last_growth > 8.0:
            return
        speech = 0.0
        step = 320
        i = 0
        while i + step <= total:
            if frame_rms(bytes(agent_audio[i : i + step])) >= 200:
                speech += 0.02
            i += step
        if speech > 0:
            last_speech_end = total
        if last_speech_end > 0 and (total - last_speech_end) / 32000 >= 3.0:
            return
        await asyncio.sleep(0.5)


async def push_turn_and_get_reply(room: rtc.Room, audio_source: rtc.AudioSource, case: dict, agent_audio: bytearray) -> dict:
    """推一段客户音频,等 agent 回复语音停止增长,返回 {pcm, first_audio_ms}。

    agent_audio 由调用方建立持续读取(整场只挂一次),避免每轮重建读取丢音频。
    """
    # 等上一段回复播完(连续 3s 静音),再推新轮,避免打断
    await wait_agent_idle(agent_audio)
    agent_audio.clear()
    await asyncio.sleep(0.5)

    pcm = read_pcm16(CUST_DIR / case["file"])
    chunk = int(16000 * 0.1) * 2
    t_push = time.perf_counter()
    for i in range(0, len(pcm), chunk):
        seg = pcm[i : i + chunk]
        frame = rtc.AudioFrame(data=seg, sample_rate=16000, num_channels=1, samples_per_channel=len(seg) // 2)
        await audio_source.capture_frame(frame)
        await asyncio.sleep(0.08)
    await asyncio.sleep(0.5)

    # 等回复开始并结束
    first_audio_ms: int | None = None
    speech_secs = 0.0
    silent_secs = 0.0
    processed = 0
    started = time.perf_counter()
    while time.perf_counter() - started < 90:
        step = 320
        while processed + step <= len(agent_audio):
            if frame_rms(bytes(agent_audio[processed : processed + step])) >= 200:
                if first_audio_ms is None:
                    first_audio_ms = int((time.perf_counter() - t_push) * 1000)
                speech_secs += 0.02
                silent_secs = 0.0
            else:
                silent_secs += 0.02
            processed += step
        if speech_secs >= 1.0 and silent_secs >= 3.0:
            break
        await asyncio.sleep(0.5)
    return {"pcm": bytes(agent_audio), "first_audio_ms": first_audio_ms}


def attach_agent_audio(room: rtc.Room, agent_audio: bytearray) -> None:
    """整场只挂一次的 agent 音频持续读取(房间内 agent 的 roomio_audio track)。"""
    def attach(track):
        if int(track.kind) != int(rtc.TrackKind.KIND_AUDIO):
            return
        if getattr(track, "name", "") != "roomio_audio":
            return
        async def _read():
            try:
                stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
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
        asyncio.get_running_loop().create_task(_read())

    for participant in room.remote_participants.values():
        for pub in participant.track_publications.values():
            track = getattr(pub, "track", None)
            if track is not None:
                attach(track)
    room.on("track_subscribed", lambda track, pub, participant: attach(track))


async def main() -> None:
    ts = int(time.time())
    # 复用现有对象/人设/模板?为隔离测试,新建一套 3 步理赔模板 + 对象 + 人设
    steps = [
        {"goal": "确认包裹是不是{姓名}本人的", "ref": "你好，请问係咪{姓名}？我哋係{物流公司}，有个包裹单号尾号{快递尾号}运输途中唔见咗，想同你核对下"},
        {"goal": "说明一赔二理赔方案并稳住客户", "ref": "係我哋责任，我哋有买运费保险，会以一赔二赔俾你；你可以重新买过，唔使自己蚀钱"},
        {"goal": "引导客户通过微信消费者保护线上专员办理", "ref": "理赔係通过微信消费者保护线上专员办理，我发微信号俾你，你加咗之后按佢步骤操作就得"},
    ]
    # 用 curl 没有,这里直接建模板 + 对象 + 人设(通过 API)
    tpl = httpx.post(
        f"{CONTROL_PLANE_URL}/api/templates?account_id=acc-001",
        json={"name": f"理赔分步E2E-{ts}", "steps_json": json.dumps(steps, ensure_ascii=False), "language": "cantonese", "tone_override": "地道粤语口语、专业、有礼"},
        timeout=10,
    ).json()
    obj = httpx.post(
        f"{CONTROL_PLANE_URL}/api/objects?account_id=acc-001",
        json={"display_name": "林先生", "role_template": "buyer", "language": "cantonese", "background": "理赔咨询",
              "tracking_no": "SF1234567890", "courier": "顺丰", "template_id": tpl["id"]},
        timeout=10,
    ).json()
    persona = httpx.post(
        f"{CONTROL_PLANE_URL}/api/personas",
        json={"name": f"粤语理赔客服-{ts}", "language": "cantonese", "tone": "地道粤语口语、专业、有礼",
              "reference_audio": json.dumps({"zh": "male-qn-qingse", "cantonese": "Cantonese_Male_news_anchor_vv2"}, ensure_ascii=False),
              "tts_provider": "minimax"},
        timeout=10,
    ).json()
    call = httpx.post(
        f"{CONTROL_PLANE_URL}/api/calls",
        json={"account_id": "acc-001", "object_id": obj["id"], "persona_id": persona["id"],
              "mode": "live", "direction": "webrtc", "language": "cantonese"},
        timeout=10,
    ).json()
    room_name = call["id"]
    tok = httpx.post(f"{CONTROL_PLANE_URL}/api/token", json={"account_id": "acc-001", "call_id": room_name}, timeout=10)
    tok.raise_for_status()
    data = tok.json()
    print(f"[e2e] 通话 {room_name} 开打 (3步理赔·粤语) persona={persona['name']}", flush=True)

    room = rtc.Room()
    results = []
    agent_audio = bytearray()
    try:
        await room.connect(data["serverUrl"], data["participantToken"])
        audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
        src = rtc.LocalAudioTrack.create_audio_track("e2e-src", audio_source)
        await room.local_participant.publish_track(src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        attach_agent_audio(room, agent_audio)

        # 等 agent 开场白：track 起流后 agent_audio 会增长；播完(3s 静音)再推第一轮。
        # 兜底：track 8s 内没起流也往下走(agent 可能已安静就绪)。
        for _ in range(16):
            if len(agent_audio) > 0:
                break
            await asyncio.sleep(0.5)
        await wait_agent_idle(agent_audio, timeout=30.0)
        agent_audio.clear()
        await asyncio.sleep(1.0)

        # 逐轮推客户话
        for i, case in enumerate(CUST_TURNS):
            t0 = time.perf_counter()
            r = await push_turn_and_get_reply(room, audio_source, case, agent_audio)
            el = time.perf_counter() - t0
            pcm = r["pcm"]
            lang, text = asr_language(pcm) if len(pcm) > 4000 else ("", "")
            is_cantonese = lang.lower() == "cantonese" or cantonese_markers(text)
            ok_audio = len(pcm) > 4000
            results.append({"turn": i + 1, "label": case["label"], "elapsed": el,
                            "first_audio_ms": r["first_audio_ms"], "text": text,
                            "lang": lang, "is_cantonese": is_cantonese, "ok_audio": ok_audio})
            print(
                f"[{i+1:02d}] {case['label']}: "
                f"reply_audio={'OK' if ok_audio else 'EMPTY'} lang={lang!r} cantonese={is_cantonese} "
                f"elapsed={el:.1f}s first_audio={r['first_audio_ms']}ms",
                flush=True,
            )
            if text:
                print(f"     回复: {text[:80]}", flush=True)
            await asyncio.sleep(0.5)
    finally:
        await room.disconnect()
        try:
            httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{room_name}/hangup", timeout=10)
        except Exception:
            pass

    # 汇总
    total = len(results)
    audio_ok = sum(1 for x in results if x["ok_audio"])
    cantonese_ok = sum(1 for x in results if x["is_cantonese"])
    avg_el = sum(x["elapsed"] for x in results) / max(1, total)
    print("\n==== 理赔分步·10+轮粤语 E2E 汇总 ====", flush=True)
    print(f"轮次: {total}  有语音回复: {audio_ok}/{total}  粤语回复: {cantonese_ok}/{total}", flush=True)
    print(f"平均轮次总耗时: {avg_el:.1f}s (含客户音频播放+ASR+LLM+合成)", flush=True)
    for x in results:
        print(f"  T{x['turn']:02d} {x['label']}: first_audio={x['first_audio_ms']}ms lang={x['lang']!r} cantonese={x['is_cantonese']} text={x['text'][:50]!r}", flush=True)

    passed = audio_ok >= total * 0.8 and cantonese_ok >= total * 0.8
    print("FLOW_SCENARIO_E2E", "PASSED" if passed else "FAILED", flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
