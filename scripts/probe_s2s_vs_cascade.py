#!/usr/bin/env python3
"""S2S 流控引擎 vs 级联引擎「同稿双跑」对照探针（2026-10-04，feat/s2s-spike）。

同一份四步粤语话术、同一份 corpus 语料（reports/asr-whisper-bench/corpus-v2），
两条引擎各跑一遍，输出可对照的四个口径：

  ① 四步步序完整性（级联=CP turns 的 template_step/provider/gen；S2S=worker 轮账本）
  ② WA 捕号 64321109 捕获 + 逐位复述确认
  ③ 每轮墙钟首声（用户停嘴→agent 首帧音频，两侧同一量尺：agent 主音频轨
     16k 重采样后 20ms 帧 RMS≥200 累计 ≥0.12s 判首声；垫话音轨单列不计入 headline）
  ④ 轮总数（用户轮 4 轮）与剧本一致

—— 级联侧 ————————————————————————————————————————————————
建对象（demo- 前缀=测试族）→ 绑模板（话术「快遞通知四步-S2S對照-粵」，幂等 upsert）
→ 建 live 通话 → /api/token 进房 → 推 4 段 corpus → 轮间等 agent 说完 → 拉
/api/calls/{id}/turns 取证。级联栈必须已在跑（tools/bok.py serve，勿动）。

—— S2S 侧 ————————————————————————————————————————————————
roomConfig 直派 agent_name=bok-s2s-flow（s2s_flow_worker.py），推同一份语料，
读 worker stdout 账本（S2S_FLOW 行）。前置：serve :8795 + worker :8086 已起。

用法：
  .venv312/bin/python scripts/probe_s2s_vs_cascade.py \
      [--engine both|cascade|s2s] [--runs 1] [--template-id 08ade4374c40] \
      [--out /tmp/s2s_vs_cascade_results.json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import struct
import time
import wave
from pathlib import Path

import httpx

CP = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")
ACCOUNT = os.environ.get("BOK_ACCOUNT", "acc-001")
LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
LK_KEY = os.environ.get("LIVEKIT_API_KEY", "devkey")
LK_SECRET = os.environ.get("LIVEKIT_API_SECRET", "devsecret")

REPO = Path(__file__).resolve().parents[1]
CORPUS = REPO / "reports" / "asr-whisper-bench" / "corpus-v2"
S2S_WORKER_LOG = Path("/tmp/s2s_flow_worker.log")

TEMPLATE_NAME = "快遞通知四步-S2S對照-粵"
PERSONA_VOICE = "Cantonese_crisp_news_anchor_vv2"  # 与 S2S serve --minimax_tts_voice 同声
STEPS = [
    {"goal": "確認身份與來意",
     "ref": "您好，請問係陳大文先生嗎？我係快捷快遞嘅客服，你有個包裹今日到咗我哋倉。"},
    {"goal": "通知貨件詳情", "say": True,
     "ref": "你件嘢係京東買嘅，而家喺我哋中轉倉，聽日可以派到。"},
    {"goal": "登記WhatsApp號碼", "say": True,
     "ref": "麻煩您留個WhatsApp號碼，方便我哋發收貨確認俾您。\n"
            # 两处语法钉（首跑实证）：①分支行行头只认简体「如果客户」（flow.py
            # _BRANCH_LINE_RE 字面量，繁体「如果客戶」整行被丢弃）；②条件体要含
            # OBJECTION 家族词「投诉」（_BRANCH_FAMILY 简体）才吃到家族匹配——
            # 豆包 ASR 输出简体，纯繁体条件+bigram 会被简繁差异打空。
            "如果客户話件爛咗或者想投诉→【跳第4步】唔好意思，我哋會跟進，聽日派件你可以先檢查。"},
    {"goal": "收尾告別", "say": True,
     "ref": "多謝您嘅配合，我哋聽日準時送到，再見。"},
]
# 剧本：步1答 / 步2答 / 步3喂号（含 WhatsApp 号） / 步4答（与 S2S 侧同稿）
SCRIPT = ["canto_12.wav", "brand_canto_02.wav", "digit_canto_04.wav", "canto_11.wav"]
SCRIPT_TEXT = {
    "canto_12.wav": "你哋幾時可以先送到我度",
    "brand_canto_02.wav": "我件貨係京東買的",
    "digit_canto_04.wav": "我WhatsApp號碼係六四三二一一零九",
    "canto_11.wav": "我件貨爛咗想投訴",
}
EXPECTED_NUMBER = "64321109"

RMS_THRESHOLD = 200.0     # 与 e2e_real_customer / s2s probe 同口径
ONSET_ACCUM_S = 0.12      # 首声判据：累计有声 ≥0.12s
ANSWER_SILENCE_S = 2.0    # 尾静默 ≥2s 判本轮说完
MUTE_SPEECH_S = 0.30
DEFAULT_TIMEOUT = 60.0

_CN_DIGITS = {
    "零": "0", "〇": "0", "洞": "0", "一": "1", "幺": "1", "二": "2", "两": "2",
    "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
}
_SEP = set(" \u3000-–—~～·")
_FW = {chr(0xFF10 + i): str(i) for i in range(10)}


def digit_runs(text: str, min_len: int = 3) -> list[str]:
    runs: list[str] = []
    cur: list[str] = []

    def flush():
        if len(cur) >= min_len:
            runs.append("".join(cur))
        cur.clear()

    for ch in text or "":
        if "0" <= ch <= "9":
            cur.append(ch)
        elif ch in _FW:
            cur.append(_FW[ch])
        elif ch in _CN_DIGITS:
            cur.append(_CN_DIGITS[ch])
        elif ch in _SEP:
            continue
        else:
            flush()
    flush()
    return runs


def reads_back(text: str, number: str) -> bool:
    if number in (text or ""):
        return True
    return any(number in run for run in digit_runs(text, min_len=4))


def percentile(values: list[float], p: float) -> float | None:
    xs = sorted(values)
    if not xs:
        return None
    idx = max(0, min(len(xs) - 1, math.ceil(p / 100 * len(xs)) - 1))
    return xs[idx]


# —— 音频量尺（两侧共用）———————————————————————————————————————
def frame_rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n <= 0:
        return 0.0
    frames = struct.unpack(f"<{n}h", pcm)
    return math.sqrt(sum(x * x for x in frames) / n)


def speech_stats(pcm: bytes, processed: int) -> tuple[float, float, int]:
    """增量统计 (speech_secs, silent_secs, new_processed)，20ms 帧 RMS 口径。"""
    step = 320
    speech = silent = 0.0
    while processed + step <= len(pcm):
        if frame_rms(pcm[processed:processed + step]) >= RMS_THRESHOLD:
            speech += 0.02
            silent = 0.0
        else:
            silent += 0.02
        processed += step
    return speech, silent, processed


class AgentTap:
    """agent 音频攢存：主轨（roomio_audio，非 background_audio）与垫轨分开。"""

    def __init__(self) -> None:
        self.main = bytearray()
        self.bg = bytearray()
        self.tasks: list[asyncio.Task] = []
        self.main_seen = asyncio.Event()

    def attach(self, track) -> None:
        import livekit.rtc as rtc

        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        is_bg = getattr(track, "name", "") == "background_audio"
        buf = self.bg if is_bg else self.main

        async def _read() -> None:
            try:
                stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
                async for ev in stream:
                    frame = getattr(ev, "frame", ev)
                    buf.extend(bytes(frame.data))
            except Exception:  # noqa: BLE001
                pass

        if not is_bg:
            self.main_seen.set()
        self.tasks.append(asyncio.get_running_loop().create_task(_read()))

    def close(self) -> None:
        for t in self.tasks:
            t.cancel()


async def wait_first_audio(buf: bytes, mark: int, timeout_s: float) -> float | None:
    """mark 之后的累计首声（≥0.12s），返回等待秒数；超时 None。"""
    t0 = time.perf_counter()
    probe = mark
    speech = 0.0
    deadline = t0 + timeout_s
    while time.perf_counter() < deadline:
        s, _sil, probe = speech_stats(bytes(buf), probe)
        speech += s
        if speech >= ONSET_ACCUM_S:
            return time.perf_counter() - t0
        await asyncio.sleep(0.05)
    return None


async def wait_turn_end(buf: bytes, mark: int, timeout_s: float) -> tuple[float, float]:
    """等到本轮说完（有声≥0.3s 后尾静默≥2s）。返回 (speech_s, silent_s)。"""
    probe = mark
    speech = 0.0
    silent = 0.0
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        s, sil, probe = speech_stats(bytes(buf), probe)
        speech += s
        if s > 0:
            silent = 0.0
        else:
            silent += sil
        if speech >= MUTE_SPEECH_S and silent >= ANSWER_SILENCE_S:
            break
        await asyncio.sleep(0.05)
    return speech, silent


async def feed_wav(source, wav: Path, *, frame_ms: int = 10) -> bytes:
    """真时推 16k 单声道 wav（10ms 帧，1.0× 实时）。返回 PCM 便于时长核对。"""
    import livekit.rtc as rtc

    with wave.open(str(wav), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1, wav
        pcm = w.readframes(w.getnframes())
    step = int(16000 * frame_ms / 1000) * 2
    for i in range(0, len(pcm), step):
        chunk = pcm[i:i + step]
        if len(chunk) < step:
            chunk = chunk + b"\x00" * (step - len(chunk))
        await source.capture_frame(
            rtc.AudioFrame(data=chunk, sample_rate=16000, num_channels=1,
                           samples_per_channel=step // 2)
        )
        await asyncio.sleep(frame_ms / 1000)
    return pcm


# —— 级联侧 —————————————————————————————————————————————————
def _cp_headers() -> dict[str, str]:
    tok = os.environ.get("BOK_CP_TOKEN", "").strip()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def ensure_template(steps: list[dict] | None = None) -> str:
    steps = steps or STEPS
    payload_steps = json.dumps(steps, ensure_ascii=False)
    hot = "WhatsApp,快捷快遞,京東,中轉倉,包裹,陳大文"
    items = httpx.get(f"{CP}/api/templates", params={"account_id": ACCOUNT},
                      timeout=10, headers=_cp_headers()).json()
    items = items.get("items", items) if isinstance(items, dict) else items
    for t in items:
        if t.get("name") == TEMPLATE_NAME:
            tid = str(t.get("id"))
            httpx.put(f"{CP}/api/templates/{tid}",
                      json={"steps_json": payload_steps, "hotwords": hot,
                            "language": "cantonese"},
                      timeout=10, headers=_cp_headers()).raise_for_status()
            return tid
    r = httpx.post(f"{CP}/api/templates", json={
        "account_id": ACCOUNT, "name": TEMPLATE_NAME, "language": "cantonese",
        "hotwords": hot, "steps_json": payload_steps,
    }, timeout=10, headers=_cp_headers())
    r.raise_for_status()
    return str(r.json()["id"])


def steps_variant(variant: str) -> list[dict]:
    """cascade-steps=say（默认，正稿直念罐头腿）/ llm（剥 say=1，同稿走 LLM 应答腿）。"""
    if variant == "llm":
        return [{k: v for k, v in s.items() if k != "say"} for s in STEPS]
    return STEPS


class CascadeCall:
    def __init__(self, template_id: str) -> None:
        self.template_id = template_id
        self.ts = int(time.time() * 1000) % 100000
        self.object_name = f"demo-陳大文-{self.ts}"
        self.call_id = ""

    def _post(self, path: str, payload: dict) -> dict:
        r = httpx.post(f"{CP}{path}", json=payload, timeout=15, headers=_cp_headers())
        r.raise_for_status()
        return r.json()

    def create(self) -> None:
        obj = self._post(f"/api/objects?account_id={ACCOUNT}", {
            "display_name": self.object_name,
            "role_template": "buyer",
            "language": "cantonese",
            "background": "s2s-vs-cascade probe",
            "contact_channel": "WhatsApp",
            "template_id": self.template_id,
        })
        persona = self._post(f"/api/personas?account_id={ACCOUNT}", {
            "name": f"S2S對照客服-{self.ts}",
            "language": "cantonese",
            "tone": "礼貌专业",
            "reference_audio": PERSONA_VOICE,
        })
        call = self._post("/api/calls", {
            "account_id": ACCOUNT,
            "object_id": obj["id"],
            "persona_id": persona["id"],
            "mode": "live",
            "direction": "webrtc",
            "language": "cantonese",
        })
        self.call_id = str(call["id"])

    async def token(self) -> tuple[str, str]:
        d = self._post("/api/token", {"account_id": ACCOUNT, "call_id": self.call_id})
        return str(d["serverUrl"]), str(d["participantToken"])

    def finish(self) -> None:
        for path in (f"/api/calls/{self.call_id}/hangup", f"/api/calls/{self.call_id}/settle"):
            try:
                httpx.post(f"{CP}{path}", timeout=30, headers=_cp_headers())
            except Exception:  # noqa: BLE001
                pass

    async def turns(self, settle_s: float = 15.0) -> list[dict]:
        last: list[dict] = []
        stable = 0
        deadline = time.perf_counter() + settle_s
        while time.perf_counter() < deadline:
            try:
                rows = httpx.get(f"{CP}/api/calls/{self.call_id}/turns",
                                 timeout=10, headers=_cp_headers()).json()
            except Exception:  # noqa: BLE001
                rows = []
            if rows and len(rows) == len(last):
                stable += 1
                if stable >= 2:
                    return rows
            else:
                stable = 0
            last = rows
            await asyncio.sleep(1.5)
        return last


async def run_cascade(run_no: int, template_id: str, *, timeout: float,
                      track_wait: float = 75.0) -> dict:
    import livekit.rtc as rtc

    call = CascadeCall(template_id)
    call.create()
    print(f"[cascade#{run_no}] call={call.call_id} object={call.object_name}", flush=True)

    room = rtc.Room()
    tap = AgentTap()
    room.on("track_subscribed", lambda track, _p, _pt: tap.attach(track))
    server_url, token = await call.token()
    await room.connect(server_url, token)
    source = rtc.AudioSource(16000, 1)
    track = rtc.LocalAudioTrack.create_audio_track("customer-src", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )

    opening: dict = {}
    rounds: list[dict] = []
    try:
        # 共享机宿主机 load 抖到 worker 阈值线时 dispatch 会晚到（派发看门狗
        # 6/11/16s 重派），给足窗口；超时=本跑作废由 main 重试。
        await asyncio.wait_for(tap.main_seen.wait(), timeout=track_wait)
        t_join = time.perf_counter()
        # 开场白：等喇叭出声+说完
        onset = await wait_first_audio(tap.main, 0, 45)
        speech, silent = await wait_turn_end(tap.main, 0, 45)
        opening = {
            "first_main_ms": round(onset * 1000) if onset is not None else None,
            "speech_s": round(speech, 2),
            "joined_to_onset_ms": round((time.perf_counter() - t_join) * 1000),
        }
        print(f"[cascade#{run_no}] opening first={opening['first_main_ms']}ms "
              f"speech={opening['speech_s']}s", flush=True)

        for i, name in enumerate(SCRIPT):
            wav = CORPUS / name
            mark = len(tap.main)
            bg_mark = len(tap.bg)
            pcm = await feed_wav(source, wav)
            t_done = time.perf_counter()
            onset = await wait_first_audio(tap.main, mark, timeout)
            bg_onset = await wait_first_audio(tap.bg, bg_mark, 0.05)
            speech, silent = await wait_turn_end(tap.main, mark, timeout)
            row = {
                "i": i + 1,
                "file": name,
                "text": SCRIPT_TEXT.get(name, ""),
                "user_dur_s": round(len(pcm) / 32000.0, 2),
                "first_main_ms": round(onset * 1000) if onset is not None else None,
                "first_bg_ms": (round(bg_onset * 1000) if bg_onset is not None else None),
                "speech_s": round(speech, 2),
                "answered": speech >= MUTE_SPEECH_S,
            }
            rounds.append(row)
            print(f"[cascade#{run_no}] 轮{i+1} {name} first={row['first_main_ms']}ms "
                  f"speech={row['speech_s']}s bg={row['first_bg_ms']}", flush=True)
            await asyncio.sleep(0.8)
    finally:
        tap.close()
        try:
            await room.disconnect()
        except Exception:  # noqa: BLE001
            pass
        call.finish()

    turns = await call.turns()
    assistant_rows = [
        {"role": t.get("role"), "transcript": str(t.get("transcript") or ""),
         "provider": str(t.get("provider") or ""), "gen": str(t.get("gen") or ""),
         "template_step": t.get("template_step"), "perceived_ms": t.get("perceived_ms"),
         "latency_ms": t.get("latency_ms")}
        for t in turns
    ]
    return {
        "engine": "cascade", "run": run_no, "call_id": call.call_id,
        "object_name": call.object_name, "template_id": template_id,
        "opening": opening, "rounds": rounds, "turns": assistant_rows,
    }


# —— S2S 侧 —————————————————————————————————————————————————
class LedgerReader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = path.stat().st_size if path.exists() else 0
        self.rows: list[dict] = []
        self.full: list[dict] = []
        self.events: list[dict] = []

    def poll(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(self.offset)
            data = fh.read()
            self.offset = fh.tell()
        for line in data.splitlines():
            line = line.strip()
            if line.startswith("S2S_FLOW_FULL "):
                try:
                    self.full.append(json.loads(line[len("S2S_FLOW_FULL "):]))
                except json.JSONDecodeError:
                    pass
            elif line.startswith("S2S_FLOW_EVT "):
                try:
                    self.events.append(json.loads(line[len("S2S_FLOW_EVT "):]))
                except json.JSONDecodeError:
                    pass
            elif line.startswith("S2S_FLOW "):
                try:
                    self.rows.append(json.loads(line[len("S2S_FLOW "):]))
                except json.JSONDecodeError:
                    pass

    def full_text(self, idx: int) -> str:
        if 0 <= idx < len(self.full):
            return str(self.full[idx].get("asst_text") or "")
        if 0 <= idx < len(self.rows):
            return str(self.rows[idx].get("asst_text") or "")
        return ""


async def run_s2s(run_no: int, *, timeout: float, object_name: str,
                  track_wait: float = 75.0) -> dict:
    import livekit.rtc as rtc
    from livekit.api import AccessToken, VideoGrants
    from livekit.protocol.agent_dispatch import RoomAgentDispatch
    from livekit.protocol.room import RoomConfiguration

    ledger = LedgerReader(S2S_WORKER_LOG)
    room_name = "call-s2sflow-" + os.urandom(3).hex()
    room = rtc.Room()
    tap = AgentTap()
    room.on("track_subscribed", lambda track, _p, _pt: tap.attach(track))

    tok = AccessToken(LK_KEY, LK_SECRET)
    tok.identity = "flow-probe"
    tok.name = "flow-probe"
    tok.with_grants(VideoGrants(room=room_name, room_join=True,
                                can_publish=True, can_subscribe=True))
    tok.with_room_config(RoomConfiguration(agents=[RoomAgentDispatch(
        agent_name="bok-s2s-flow",
        metadata=json.dumps({"call_id": room_name, "object_id": "s2s-vc-probe",
                             "object_name": object_name, "account_id": ACCOUNT}),
    )]))
    await room.connect(LIVEKIT_URL, tok.to_jwt())
    source = rtc.AudioSource(16000, 1)
    track = rtc.LocalAudioTrack.create_audio_track("customer-src", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    print(f"[s2s#{run_no}] room={room_name}", flush=True)

    opening: dict = {}
    rounds: list[dict] = []
    try:
        await asyncio.wait_for(tap.main_seen.wait(), timeout=track_wait)
        await asyncio.sleep(1.0)
        onset = await wait_first_audio(tap.main, 0, 45)
        speech, _silent = await wait_turn_end(tap.main, 0, 45)
        opening = {"first_main_ms": round(onset * 1000) if onset is not None else None,
                   "speech_s": round(speech, 2)}
        ledger.poll()
        print(f"[s2s#{run_no}] opening first={opening['first_main_ms']}ms "
              f"rows={len(ledger.rows)}", flush=True)

        for i, name in enumerate(SCRIPT):
            wav = CORPUS / name
            mark = len(tap.main)
            pcm = await feed_wav(source, wav)
            t_done = time.perf_counter()
            onset = await wait_first_audio(tap.main, mark, timeout)
            speech, _silent = await wait_turn_end(tap.main, mark, timeout)
            ledger.poll()
            row = {
                "i": i + 1, "file": name, "text": SCRIPT_TEXT.get(name, ""),
                "user_dur_s": round(len(pcm) / 32000.0, 2),
                "first_main_ms": round(onset * 1000) if onset is not None else None,
                "speech_s": round(speech, 2),
                "answered": speech >= MUTE_SPEECH_S,
                "ledger_rows": len(ledger.rows),
            }
            rounds.append(row)
            print(f"[s2s#{run_no}] 轮{i+1} {name} first={row['first_main_ms']}ms "
                  f"speech={row['speech_s']}s rows={len(ledger.rows)}", flush=True)
            await asyncio.sleep(0.8)
        # 收尾：等账本齐（5 行=开场+4 轮）或稳定
        deadline = time.perf_counter() + timeout
        last = -1
        stable = 0
        while time.perf_counter() < deadline:
            ledger.poll()
            if len(ledger.rows) >= 5:
                break
            if len(ledger.rows) == last:
                stable += 1
                if stable >= 20:
                    break
            else:
                stable = 0
                last = len(ledger.rows)
            await asyncio.sleep(0.05)
    finally:
        tap.close()
        try:
            await room.disconnect()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.5)
        ledger.poll()

    rows = list(ledger.rows)
    full = list(ledger.full)
    for i, r in enumerate(rows):
        if i < len(full):
            r["asst_text_full"] = full[i].get("asst_text")
    return {
        "engine": "s2s", "run": run_no, "room": room_name,
        "opening": opening, "rounds": rounds,
        "ledger": rows,
        "events": [e for e in ledger.events if e.get("kind") in
                   ("wa_captured", "step_advance", "readback_missing", "opening_triggered")],
    }


# —— 判定 ————————————————————————————————————————————————————
def verdict_cascade(res: dict) -> dict:
    turns = res.get("turns") or []
    asst = [t for t in turns if t.get("role") == "assistant" and t.get("transcript")]
    steps = [int(t.get("template_step") or 0) for t in asst]
    user_rows = [t for t in turns if t.get("role") == "user"]
    # WA 捕获：任何一轮 assistant 文本复述 EXPECTED_NUMBER
    readback = [t["transcript"] for t in asst if reads_back(t["transcript"], EXPECTED_NUMBER)]
    return {
        "assistant_rows": len(asst),
        "user_rows": len(user_rows),
        "step_seq": steps,
        "step_order_ok": bool(steps) and steps[0] == 1 and all(
            b >= a for a, b in zip(steps, steps[1:])) and 4 in steps,
        "wa_captured": bool(readback),
        "readback_text": readback[0] if readback else "",
        "lane_seq": [f"{t.get('provider') or '-'}/{t.get('gen') or '-'}" for t in asst],
    }


def verdict_s2s(res: dict) -> dict:
    rows = res.get("ledger") or []
    steps = [int(r.get("step") or 0) for r in rows]
    cap = [r for r in rows if int(r.get("step") or 0) == 3 and r.get("wa_captured")]
    captured = any(str(r.get("wa_captured")) == EXPECTED_NUMBER for r in cap)
    readback_idx = [
        i for i, r in enumerate(rows)
        if int(r.get("step") or 0) == 3 and str(r.get("wa_captured")) == EXPECTED_NUMBER
        and reads_back(str(r.get("asst_text_full") or r.get("asst_text") or ""), EXPECTED_NUMBER)
    ]
    fabricated = [
        (i, run) for i, r in enumerate(rows)
        for run in digit_runs(str(r.get("asst_text_full") or r.get("asst_text") or ""), 3)
        if run not in EXPECTED_NUMBER
    ]
    return {
        "assistant_rows": len(rows),
        "step_seq": steps,
        "step_order_ok": bool(steps) and steps[0] == 1 and all(
            b >= a for a, b in zip(steps, steps[1:])) and 4 in steps,
        "wa_captured": captured,
        "readback_ok": bool(readback_idx),
        "fabricated_numbers": fabricated,
    }


def summarize(engine: str, res: dict) -> dict:
    firsts = [r["first_main_ms"] for r in res["rounds"] if r.get("first_main_ms") is not None]
    return {
        "engine": engine,
        "run": res.get("run"),
        "first_main_ms": {
            "values": firsts,
            "p50": percentile(firsts, 50),
            "max": max(firsts) if firsts else None,
        },
        "answered": sum(1 for r in res["rounds"] if r.get("answered")),
        "rounds": len(res["rounds"]),
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["both", "cascade", "s2s"], default="both")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--template-id", default="")
    ap.add_argument("--cascade-steps", choices=["say", "llm"], default="say",
                    help="say=正稿罐头直念腿（默认，生产话术步姿态）；llm=剥 say 走 LLM 应答腿")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--out", default="/tmp/s2s_vs_cascade_results.json")
    args = ap.parse_args()

    template_id = ensure_template(STEPS)  # 恒先把 canon（say 版）落定
    variant = steps_variant(args.cascade_steps)
    if variant is not STEPS:
        ensure_template(variant)
    print(f"[probe] template={template_id} name={TEMPLATE_NAME} "
          f"cascade_steps={args.cascade_steps} "
          f"(--template-id={args.template_id or '-'} 仅留档)", flush=True)

    results: list[dict] = []

    async def _with_retry(fn, label: str, attempts: int = 3):
        last_exc: BaseException | None = None
        for k in range(1, attempts + 1):
            try:
                return await fn()
            except (asyncio.TimeoutError, TimeoutError) as exc:
                last_exc = exc
                print(f"[probe] {label} 第 {k} 跑 75s 未见 agent（宿主 load 抖动），重试…",
                      flush=True)
                await asyncio.sleep(3)
        raise last_exc  # type: ignore[misc]

    for i in range(1, args.runs + 1):
        if args.engine in ("both", "cascade"):
            res = await _with_retry(
                lambda: run_cascade(i, template_id, timeout=args.timeout), f"cascade#{i}")
            res["verdict"] = verdict_cascade(res)
            res["summary"] = summarize("cascade", res)
            results.append(res)
        if args.engine in ("both", "s2s"):
            res = await _with_retry(
                lambda: run_s2s(i, timeout=args.timeout,
                                object_name=f"demo-陳大文-s2sflow-{i}"), f"s2s#{i}")
            res["verdict"] = verdict_s2s(res)
            res["summary"] = summarize("s2s", res)
            results.append(res)

    out = {"ts": int(time.time()), "template_id": template_id,
           "cascade_steps": args.cascade_steps, "runs": results}
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n================ 汇总 ================", flush=True)
    for res in results:
        v = res["verdict"]
        print(f"\n[{res['engine']}#{res['run']}] " +
              (f"call={res.get('call_id')} " if res.get("call_id") else f"room={res.get('room')} "))
        print(f"  首声 ms: {[r['first_main_ms'] for r in res['rounds']]} "
              f"(p50={res['summary']['first_main_ms']['p50']})")
        print(f"  轮数: {res['summary']['rounds']} 有答: {res['summary']['answered']}")
        print(f"  step_seq: {v['step_seq']} order_ok={v['step_order_ok']} "
              f"wa_captured={v['wa_captured']}")
        if res["engine"] == "cascade":
            print(f"  lanes: {v['lane_seq']}")
            if v.get("readback_text"):
                print(f"  readback: {v['readback_text'][:60]!r}")
        else:
            print(f"  readback_ok={v.get('readback_ok')} "
                  f"fabricated={v.get('fabricated_numbers')}")
    print(f"\n[probe] JSON → {args.out}", flush=True)
    if variant is not STEPS:
        # llm 补臂跑完把模板还原成 canon（say 版）——DB 终态恒=正稿直念姿态
        ensure_template(STEPS)
        print("[probe] template 已还原 canon（say 版）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
