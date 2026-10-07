# --- scripts import bootstrap (G1) ---
# sys.path 引导(G1 迁移解耦,见 docs/superpowers/plans/2026-10-04-repo-governance-plan.md §3.1):
# 同层时是 no-op;文件挪进任何桶后裸 import 兄弟模块继续解析。
"""风暴到期自清 timer 实弹探针（2026-10-07 死气窗票验收件，PR #198 配套）。

仪器形状（真栈实弹，非单测）：
  连发打断 LLM 回复 ×3-5（20s 窗内）→ `[storm] engage` → **静默 15s** → 观测
  ① `[storm] expiry-resume (quiet timer` 打点（timer 真发火）
  ② 静默期内回收线音频到达（storm-reclaim 车道出声）
  ③ 尾问拿到真回复（状态已清=正常轮路径恢复）
三关全过 = PASS。任一缺 = FAIL 并打出日志窗取证（engage/轮次/expiry 行）。

用法（前置：最新 main 栈在跑，`BOK_LOCAL_TTS=1` 供刺激渲染）：
  .venv312/bin/python scripts/probes/probe_storm_expiry.py
对象名 `probe-storm-*` 命中心跳豁免前缀（nudge 不干扰静默窗观测）。
"""

from __future__ import annotations

import sys as _sys
import pathlib as _pathlib

_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))

import asyncio  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402

import httpx  # noqa: E402
import livekit.rtc as rtc  # noqa: E402

from e2e_barge_in import (  # noqa: E402
    CONTROL_PLANE_URL,
    LANG,
    _CP_HEADERS,
    push_pcm,
    speech_stats,
    tts_pcm,
    wait_speech_then_silence,
)

AGENT_LOG = os.environ.get(
    "BOK_AGENT_LOG",
    os.path.expanduser("~/Library/Application Support/BokVoice/logs/agent.log"),
)

# 打断弹药（短句,1-1.5s 量级;循环使用直到 engage）
BURSTS = [
    "等等，你先听我讲。",
    "唔好意思，我再讲一句。",
    "仲有，我想补充一下。",
    "你先唔好讲，听我讲完。",
    "我仲有嘢想问。",
]
FINAL_Q = "好啦，我想问下你们集运点样收费？"


def _log_size() -> int:
    try:
        return os.path.getsize(AGENT_LOG)
    except OSError:
        return 0


def _log_since(mark: int) -> str:
    try:
        with open(AGENT_LOG, "rb") as f:
            f.seek(mark)
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


async def _wait_speech(agent_audio: bytearray, processed_from: int, need_s: float, timeout_s: float) -> float:
    probe = processed_from
    acc = 0.0
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        s, _, probe = speech_stats(bytes(agent_audio), probe)
        acc += s
        if acc >= need_s:
            return acc
        await asyncio.sleep(0.2)
    return acc


async def main() -> None:
    obj = httpx.post(
        f"{CONTROL_PLANE_URL}/api/objects?account_id=acc-001",
        headers=_CP_HEADERS,
        json={
            "display_name": f"probe-storm-{int(time.time())}",
            "role_template": "buyer",
            "language": LANG,
            "background": "storm expiry probe",
        },
        timeout=10,
    ).json()
    persona = httpx.post(
        f"{CONTROL_PLANE_URL}/api/personas?account_id=acc-001",
        headers=_CP_HEADERS,
        json={"name": "probe客服", "language": LANG, "tone": "礼貌专业"},
        timeout=10,
    ).json()
    tpls = httpx.get(
        f"{CONTROL_PLANE_URL}/api/templates?account_id=acc-001",
        headers=_CP_HEADERS,
        timeout=10,
    ).json()
    tpls = tpls if isinstance(tpls, list) else tpls.get("items") or []
    tpl = next(
        (t for t in tpls if str(t.get("language")) == LANG
         and "e2e" not in str(t.get("name", "")).lower()
         and "probe" not in str(t.get("name", "")).lower()),
        None,
    )
    if not tpl:
        raise SystemExit("无可用同语言模板——先建模板")
    call = httpx.post(
        f"{CONTROL_PLANE_URL}/api/calls",
        headers=_CP_HEADERS,
        json={
            "account_id": "acc-001",
            "object_id": obj["id"],
            "persona_id": persona["id"],
            "template_id": str(tpl.get("id")),
            "mode": "live",
            "direction": "webrtc",
            "language": LANG,
        },
        timeout=10,
    ).json()
    room_name = call["id"]
    resp = httpx.post(
        f"{CONTROL_PLANE_URL}/api/token",
        headers=_CP_HEADERS,
        json={"account_id": "acc-001", "call_id": room_name},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    room = rtc.Room()
    agent_audio = bytearray()
    read_tasks: list[asyncio.Task] = []

    def attach(track):
        if int(track.kind) != int(rtc.TrackKind.KIND_AUDIO):
            return
        if getattr(track, "name", "") not in ("roomio_audio", "background_audio"):
            return

        async def _read():
            stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
            try:
                async for event in stream:
                    frame = getattr(event, "frame", event)
                    agent_audio.extend(bytes(frame.data))
            finally:
                await stream.aclose()

        read_tasks.append(asyncio.get_running_loop().create_task(_read()))

    def on_track(track, publication, participant):
        attach(track)

    room.on("track_subscribed", on_track)
    mark0 = _log_size()
    try:
        await room.connect(data["serverUrl"], data["participantToken"])
        print(f"[storm-probe] joined {room_name} (lang={LANG})", flush=True)
        audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
        src = rtc.LocalAudioTrack.create_audio_track("storm-probe-src", audio_source)
        await room.local_participant.publish_track(
            src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )

        # 开场白播完
        state = {"processed": 0, "speech": 0.0, "silent": 0.0}
        await wait_speech_then_silence(
            agent_audio, state, need_speech=1.0, need_silence=3.0, timeout=40
        )
        agent_audio.clear()

        # ---- 阶段一：连发打断直到 engage（最多 5 轮，20s 窗内 ≥3 次）----
        engaged = False
        for i, burst in enumerate(BURSTS):
            q = "我想问下你们平台系咪要跑路了？" if i == 0 else "你仲有咩想讲？"
            await push_pcm(audio_source, tts_pcm(q))
            got = await _wait_speech(agent_audio, 0, need_s=0.6, timeout_s=25)
            if got < 0.6:
                print(f"[storm-probe] round{i}: reply_no_speech ({got:.1f}s)", flush=True)
                continue
            await push_pcm(audio_source, tts_pcm(burst))
            await asyncio.sleep(1.2)  # 等 agent 侧 speech_created(interrupted) 落账
            if "[storm] engage" in _log_since(mark0):
                engaged = True
                print(f"[storm-probe] engaged after {i + 1} interrupts", flush=True)
                break
        if not engaged:
            window = "\n".join(
                ln for ln in _log_since(mark0).splitlines() if "[storm]" in ln
            )[-800:]
            print(f"PROBE_STORM_EXPIRY FAIL reason=no_engage\n{window}", flush=True)
            raise SystemExit(1)

        # ---- 阶段二：静默 15s——观测 ①expiry 打点 ②回收线出声 ----
        audio_len_at_silence = len(agent_audio)
        silence_deadline = time.perf_counter() + 15
        expiry_hit = False
        reclaim_audio = 0.0
        probe = audio_len_at_silence
        while time.perf_counter() < silence_deadline:
            if not expiry_hit and "[storm] expiry-resume (quiet timer" in _log_since(mark0):
                expiry_hit = True
            s, _, probe = speech_stats(bytes(agent_audio), probe)
            reclaim_audio += s
            await asyncio.sleep(0.3)

        # ---- 阶段三：尾问真回复 ----
        before_final = len(agent_audio)
        await push_pcm(audio_source, tts_pcm(FINAL_Q))
        final_speech = await _wait_speech(agent_audio, before_final, need_s=1.0, timeout_s=25)

        storm_window = "\n".join(
            ln for ln in _log_since(mark0).splitlines()
            if "[storm]" in ln or "storm-reclaim" in ln
        )[-800:]
        print(f"[storm-probe] expiry_hit={expiry_hit} reclaim_audio={reclaim_audio:.1f}s "
              f"final_speech={final_speech:.1f}s", flush=True)
        ok = expiry_hit and reclaim_audio >= 0.4 and final_speech >= 1.0
        print(
            f"PROBE_STORM_EXPIRY {'PASS' if ok else 'FAIL'} "
            f"expiry={int(expiry_hit)} reclaim_audio={reclaim_audio:.1f}s "
            f"final={final_speech:.1f}s\n{storm_window}",
            flush=True,
        )
        raise SystemExit(0 if ok else 1)
    finally:
        for t in read_tasks:
            t.cancel()
        await room.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
