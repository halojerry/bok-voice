"""快语速吃字/回声守卫探针（2026-09-12 Task 2 验收）。

三个修复的实机验收（栈在跑、无其它通话时——GPU 竞态规矩）：
  P1 首字存活:START pre-roll 喂会话——「好的，淘宝，京东」1.4× 速推流,
     用户轮转写应含首字「好」(修复前 1.4× 速首 1-3 字结构性缺失)。
  P2 纯热词答案存活:停嘴层剥尾保头——对象 courier=拼多多(词表含之),
     客户答「拼多多」不再被词表回声守卫整条丢弃。
  P3 平台词不被误伤:同句「淘宝/京东」(淘宝不在词表,守卫零触碰)。

用法:.venv312/bin/python scripts/probe_fast_speech.py
结果落 scripts/.probe_fast_speech.json;退出码非 0=有 case 未过。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_brand_words as pb  # noqa: E402  复用 make_call/tts_pcm/turns_of/wait_settled

CONTROL_PLANE_URL = pb.CONTROL_PLANE_URL


def _ola_speed_pcm(pcm: bytes, factor: float) -> bytes:
    """保音高时长压缩核心（numpy OLA/SOLA）。factor>1=压短（更快）。

    详见 `speedup_pcm`。窗口 60ms、合成步 hop_out=W//4、分析步 hop_in=hop_out*factor；
    每帧在原读位附近 ±hop_out//2 内用归一化互相关找相位对齐偏移（SOLA），
    再 Hann 窗叠加重建并按窗能量归一（保 COLA 增益）。
    """
    import numpy as np

    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    n = int(x.size)
    if n == 0 or factor <= 0:
        return b""
    w = 960               # 60ms @16k
    hop_out = w // 4      # 240：合成步（输出每帧前进）
    hop_in = max(1, int(round(hop_out * factor)))  # 分析步（输入每帧前进）
    if hop_in == hop_out:
        return bytes(pcm)
    n_frames = int(np.ceil(max(0, n - w) / hop_in)) + 1 if n > w else 1
    out_len = (n_frames - 1) * hop_out + w
    corr_len = min(hop_out, w)  # 新帧头与已写输出重叠的相关窗
    search = hop_out // 2       # 对齐搜索半径

    win = np.hanning(w)
    out = np.zeros(out_len, dtype=np.float64)
    wsum = np.zeros(out_len, dtype=np.float64)

    for k in range(n_frames):
        nominal = k * hop_in
        out_start = k * hop_out
        if k == 0 or nominal + corr_len > n:
            shift = 0
        else:
            shift = _best_ola_shift(x, nominal, out, out_start, corr_len, search)
        a = nominal + shift
        if a < 0:
            a = 0
        seg = x[a:a + w]
        if seg.size < w:  # 尾帧补齐零
            seg = np.concatenate([seg, np.zeros(w - seg.size, dtype=np.float64)])
        end = min(out_start + w, out_len)
        take = end - out_start
        out[out_start:end] += seg[:take] * win[:take]
        wsum[out_start:end] += win[:take]

    wsum = np.maximum(wsum, 1e-3)
    out = out / wsum
    out = np.clip(np.rint(out), -32768, 32767).astype(np.int16)
    return out.tobytes()


def _best_ola_shift(
    x, nominal: int, out, out_start: int, corr_len: int, search: int
) -> int:
    """在 ±search 内找使 x[nominal+s] 与已写输出最对齐的 s（归一化互相关）。"""
    import numpy as np

    seg_out = out[out_start:out_start + corr_len]
    if seg_out.size < corr_len:
        seg_out = np.concatenate(
            [seg_out, np.zeros(corr_len - seg_out.size, dtype=np.float64)]
        )
    ob = seg_out - seg_out.mean()
    onorm = float(np.sqrt(np.dot(ob, ob))) + 1e-9
    best_s, best_v = 0, -2.0
    n = int(x.size)
    for s in range(-search, search + 1):
        a = nominal + s
        if a < 0 or a + corr_len > n:
            continue
        seg = x[a:a + corr_len]
        ab = seg - seg.mean()
        anorm = float(np.sqrt(np.dot(ab, ab))) + 1e-9
        v = float(np.dot(ab, ob)) / (anorm * onorm)
        if v > best_v:
            best_v, best_s = v, s
    return best_s


def speedup_pcm(pcm: bytes, factor: float) -> bytes:
    """保音高时长压缩（时间尺度拉伸/压缩，音调不变）。

    旧实现用 ``audioop.ratecv`` 降到低采样率再按 16k 读——那会把音高整体上移
    （花栗鼠音），是**失真人造物**而非真人快语速，反而抬高了 ASR 错误率读数。
    现实现=常速率 16k s16le mono 上的 OLA/SOLA 时域叠加：窗口 ~60ms（960 样本），
    合成步 ``hop_out = window//4``，分析步 ``hop_in = round(hop_out * factor)``
    （factor>1 → 输入推进更快 → 时长压缩）；每帧在 ±hop_out//2 内用归一化互相关
    找与已写输出的最佳相位对齐偏移后 Hann 窗交叠相加并按窗能量归一。factor≈1
    逐字节原样返回。
    """
    return _ola_speed_pcm(pcm, factor)


async def run_case(case: dict) -> dict:
    ts = int(time.time() * 1000) % 100000
    obj = httpx.post(
        f"{CONTROL_PLANE_URL}/api/objects?account_id=acc-001",
        json={"display_name": f"探针-快语速-{ts}", "role_template": "buyer",
              "language": case.get("lang", "zh"), "background": "probe",
              "courier": case.get("courier", "")},
        timeout=10, headers=pb.CP_HEADERS,
    ).json()
    persona = httpx.post(
        f"{CONTROL_PLANE_URL}/api/personas",
        json={"name": f"探针客服快语速{ts}", "language": case.get("lang", "zh"), "tone": "礼貌专业"},
        timeout=10, headers=pb.CP_HEADERS,
    ).json()
    call = httpx.post(
        f"{CONTROL_PLANE_URL}/api/calls",
        json={"account_id": "acc-001", "object_id": obj["id"], "persona_id": persona["id"],
              "mode": "live", "direction": "webrtc", "language": case.get("lang", "zh")},
        timeout=10, headers=pb.CP_HEADERS,
    ).json()
    call_id = call["id"]
    data = httpx.post(f"{CONTROL_PLANE_URL}/api/token",
                      json={"account_id": "acc-001", "call_id": call_id}, timeout=10,
                      headers=pb.CP_HEADERS).json()
    from livekit import rtc

    room = rtc.Room()
    await room.connect(data["serverUrl"], data["participantToken"])
    audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
    src = rtc.LocalAudioTrack.create_audio_track("probe-src", audio_source)
    await room.local_participant.publish_track(
        src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    agent_audio = bytearray()
    started = asyncio.Event()

    def on_track(track, *_a):
        async def _read():
            started.set()
            stream = rtc.AudioStream(track)
            async for ev in stream:
                agent_audio.extend(bytes(ev.frame.data))

        asyncio.get_running_loop().create_task(_read())

    room.on("track_subscribed", on_track)
    await asyncio.wait_for(started.wait(), timeout=20)
    await asyncio.sleep(2.5)  # 开场白起播

    pcm = pb.tts_pcm(case["text"], lang=case.get("tts_lang", case.get("lang", "zh")))
    if case.get("speed", 1.0) != 1.0:
        pcm = speedup_pcm(pcm, case["speed"])
    await pb.push_pcm(audio_source, pcm)
    # 尾静音必须推:VAD END_OF_SPEECH 依赖后续静音帧(不推=永不停嘴=无 FINAL)
    await pb.push_pcm(audio_source, pb.silence_pcm(1.5))
    mark = len(agent_audio)
    await pb.wait_settled(agent_audio, mark, quiet_s=4.0, timeout_s=40)
    turns = pb.turns_of(call_id)
    user_texts = [t.get("transcript", "") for t in turns if t.get("role") == "user"]
    joined = " ".join(user_texts)
    hits = {k: (k in joined) for k in case["expect"]}
    ok = all(hits.values())
    await room.disconnect()
    return {"name": case["name"], "ok": ok, "hits": hits, "user_texts": user_texts,
            "speed": case.get("speed", 1.0), "text": case["text"]}


CASES = [
    # P1+P3:1.4× 快语速整答——首字「好」存活 + 平台词「淘宝」在场(不在词表,零误伤)
    {"name": "fast-head-survival", "text": "好的，淘宝，京东。", "lang": "zh",
     "speed": 1.4, "courier": "顺丰物流", "expect": ["好", "淘宝"]},
    # P2:纯热词答案(courier=拼多多→词表含拼多多)不再被整条丢
    {"name": "pdd-answer-survival", "text": "拼多多。", "lang": "zh",
     "speed": 1.0, "courier": "拼多多", "expect": ["拼多多"]},
]


async def _main() -> int:
    results, failed = [], 0
    for case in CASES:
        r = await run_case(case)
        failed += 0 if r["ok"] else 1
        print(f"[{'PASS' if r['ok'] else 'FAIL'}] {r['name']}: hits={r['hits']} "
              f"user_texts={r['user_texts']!r}")
        results.append(r)
        await asyncio.sleep(2.0)
    Path(__file__).parent.joinpath(".probe_fast_speech.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2))
    print("ALL PASS" if not failed else f"{failed} FAILED")
    return 1 if failed else 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
