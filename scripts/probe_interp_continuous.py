"""B 线「边说边译」连续语流探针（2026-09-17 长度触发子句提交验收）。

与 probe_interpret_latency 的区别：那边逐句带 0.7s 停顿（标点/停顿档就有解），
这边三句**无逗号**短句 butt-join（0.12s < VAD min_silence 0.45s）一次性连续
推流——连续语流无标点无停顿，旧路径结构性只能等 EOS 提交；长度档
（QWEN3_ASR_CLAUSE_LEN_COMMIT=1，B 线默认开）说话中就按跨窗稳定前缀切句。

主判据（两个都要）：
  1. 译文首声早于源话音讲完：first_onset < src_end（边说边译的硬定义）；
  2. 原文轮 ≥2 条：说话中确实切了多段（唔係一句整段兜底）。
A/B 基线：QWEN3_ASR_CLAUSE_LEN_COMMIT=0 重跑应 FAIL（=旧行为），改管线后
两档都要跑。ASR 若对连续音频自行打逗号，标点档也会 mid-speech 提交——探针
照实反映（说明该语料上长度档非必需），句子选取已尽量无自然逗号位。

用法：
  .venv312/bin/python scripts/probe_interp_continuous.py
env:
  BOK_PROBE_LANG_PAIR  默认 "zh,en"
  BOK_PROBE_NOISE_DB   混入高斯噪声的信噪比 dB(如 12)——退化语音语料:ASR
                       不再可靠打标点,标点档哑火,长度档( partial-len )登场。
                       长度档的设计场景正是呢种语音;健康语音上标点档总抢先。
前置：python tools/bok.py serve（含 interp-fwd 与 MT :1236）。
"""
from __future__ import annotations

import asyncio
import math
import os
import random
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "apps" / "agent"))

import e2e_interpret as e2e  # noqa: E402
import httpx  # noqa: E402

import probe_interpret_latency as base  # noqa: E402  (TimedSide + 语音起点检测)

# auth-on 栈(2026-09-15 标准姿势)要求 CP 请求带机器通道 token——E2E 建单/取
# token/收线/读 turns 全是机器语义,Bearer BOK_CP_TOKEN 直通(与 agent worker 同源)。
# 未设 env(老 auth-off 栈)零变化。CP 之外(asr sidecar)不带。
_CP_HEADERS: dict[str, str] = {}
if os.environ.get("BOK_CP_TOKEN", "").strip():
    _CP_HEADERS["Authorization"] = f"Bearer {os.environ['BOK_CP_TOKEN'].strip()}"

TTS_URL = "http://127.0.0.1:8788"

# 两个一气长短语（无内部自然停顿位）：Qwen3-ASR 会按韵律自行打逗号（2026-09-17
# 基线实测「我想请你们帮我查一下，」10 字就交），短短语语料标点档总是抢先——
# 长短语逼 ASR 的标点来得晚（≥13 字才有第一个逗号位），长度档 12 字稳定即切，
# A/B 才量得出首声差。无数字——数字走 join-hold 另有护栏。
SENTENCES = [
    "我想请你们帮我查一下我上周在你们平台下的那笔订单",
    "为什么订单状态一直显示已经发货但是物流信息是空的呢",
]
JOIN_GAP_S = 0.12  # < VAD min_silence 0.45 → 对 VAD 而言係一口气


def tts_pcm(text: str, lang: str) -> bytes:
    with httpx.Client(timeout=60) as client:
        r = client.post(
            f"{TTS_URL}/v1/audio/speech",
            json={"input": text, "language": lang, "voice": "Vivian", "sample_rate": 16000},
        )
        r.raise_for_status()
        return r.content


def strip_silence(pcm: bytes, pad_s: float = 0.06) -> bytes:
    """剥首尾静音留小垫（防句首气声被切），句间只留 JOIN_GAP_S。"""
    step = 320
    start, end = 0, len(pcm)
    for i in range(0, len(pcm) - step + 1, step):
        if e2e.frame_rms(pcm[i : i + step]) >= 100:
            start = max(0, i - int(16000 * pad_s) * 2)
            break
    for i in range(len(pcm) - step, -1, -step):
        if e2e.frame_rms(pcm[i : i + step]) >= 100:
            end = min(len(pcm), i + step + int(16000 * pad_s) * 2)
            break
    return pcm[start:end]


def mix_noise(pcm: bytes, snr_db: float) -> bytes:
    """混入高斯噪声(按整段信号 RMS 定标)。退化语音语料:标点档哑火,长度档登场。

    固定种子=可复现测试语料,非安全用途(测试音频噪声,不涉任何密钥/令牌)。"""
    n = len(pcm) // 2
    sig = struct.unpack(f"<{n}h", pcm)
    rms = math.sqrt(sum(s * s for s in sig) / max(1, n)) or 1.0
    noise_rms = rms / (10 ** (snr_db / 20.0))
    rng = random.Random(20260917)
    out = bytearray()
    for s in sig:
        v = int(s + rng.gauss(0.0, noise_rms))
        out += struct.pack("<h", max(-32768, min(32767, v)))
    return bytes(out)


async def main() -> int:
    src_lang, tgt_lang = os.environ.get("BOK_PROBE_LANG_PAIR", "zh,en").split(",")
    noise_db = float(os.environ["BOK_PROBE_NOISE_DB"]) if os.environ.get("BOK_PROBE_NOISE_DB") else None
    wav_path = os.environ.get("BOK_PROBE_WAV")

    if wav_path:
        # 真人录音语料（tests/fixtures/audio/*.wav，135s 真人声）：真实韵律/停顿/
        # 噪底——合成 TTS 骗得过韵律打标点的统计，真人声骗不过。
        wav_s = float(os.environ.get("BOK_PROBE_WAV_SECONDS", "30"))
        combined = e2e.read_wav_pcm(Path(wav_path))[: int(16000 * 2 * wav_s)]
        audio_s = len(combined) / 16000 / 2
        print(f"real-wav audio: {audio_s:.1f}s from {wav_path}", flush=True)
    else:
        parts = [strip_silence(tts_pcm(s, src_lang)) for s in SENTENCES]
        gap = b"\x00\x00" * int(16000 * JOIN_GAP_S)
        combined = gap.join(parts)
        if noise_db is not None:
            combined = mix_noise(combined, noise_db)
        audio_s = len(combined) / 16000 / 2
        print(
            f"continuous audio: {audio_s:.1f}s ({len(parts)} sentences butt-joined @{JOIN_GAP_S}s"
            f"{f', noise {noise_db:g}dB' if noise_db is not None else ''})",
            flush=True,
        )

    created = e2e.httpx.post(
        f"{e2e.CONTROL_PLANE_URL}/api/calls",
        json={"account_id": "acc-001", "kind": "interpret", "mode": "live", "direction": "interpret",
              "language": src_lang, "target_lang": tgt_lang, "object_id": "", "glossary": ""},
        headers=_CP_HEADERS,
        timeout=15,
    ).json()
    call_id = created["id"]
    me = base.TimedSide(call_id, f"me-{call_id}")
    other = base.TimedSide(call_id, f"other-{call_id}")
    await me.connect()
    await other.connect()
    await asyncio.sleep(6)  # 等 RoomAgentDispatch 拉起解释器

    onset: float | None = None
    try:
        t0 = time.monotonic()
        await me.push(combined)  # 一口气连续推流(0.8× 实时)
        src_end = t0 + audio_s / 0.8  # 实时说话人口径

        # 等译文音频收敛:captured 4s 无增长或总窗 60s
        deadline = time.monotonic() + 60
        last_len, last_change = len(other.captured), time.monotonic()
        while time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            if len(other.captured) != last_len:
                last_len, last_change = len(other.captured), time.monotonic()
            elif time.monotonic() - last_change > 4:
                break
        onset = base.speech_onset_after(other.timelog, other.captured, t0)
    finally:
        await me.close()
        await other.close()
        try:
            e2e.httpx.post(f"{e2e.CONTROL_PLANE_URL}/api/calls/{call_id}/hangup", headers=_CP_HEADERS, timeout=10)
        except Exception:
            pass

    src_turns = 0
    try:
        with httpx.Client(timeout=10) as client:
            turns = client.get(f"{e2e.CONTROL_PLANE_URL}/api/calls/{call_id}/turns", headers=_CP_HEADERS).json()
        src_turns = sum(
            1
            for t in turns
            if str(t.get("transcript", "")).startswith("原文") and t.get("speaker") == "me"
        )
    except Exception:
        pass

    ok = False
    head_start_ms = None
    if onset is not None:
        head_start_ms = (src_end - onset) * 1000  # 正=译文抢在讲完之前
        ok = onset < src_end and src_turns >= 2
    lag_after_eos_ms = None if onset is None else round((onset - src_end) * 1000)
    print(
        f"INTERPRET_CONTINUOUS_PROBE audio_s={audio_s:.1f} src_turns={src_turns} "
        f"onset={'-' if onset is None else 'yes'} "
        f"head_start_ms={'-' if head_start_ms is None else round(head_start_ms)} "
        f"lag_after_eos_ms={lag_after_eos_ms} "
        f"{'PASS' if ok else 'FAIL'}",
        flush=True,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
