#!/usr/bin/env python3
"""MiniMax 拟声标记 + emotion 探针(2026-09-16):A 线「更像真人」两问实弹验收。

四组,全部 speech-2.8-hd / 国内端点 / 生产 bidi 路径(MINIMAX_WS_POOL=0 防串档):
  A. 拟声标记:(breath)(coughs)(sighs)(laughs) 写进文本 → MiniMax 转声效还是照念?
     判据 = 时长差 + ASR 回读(sidecar :8787)。回读文本含标记词(或其中文拟读)
     =照念(危险,要剥);不含=转声效(可放心用)。
  B. emotion 参数:MINIMAX_EMOTION 枚举直透 {unset,happy,sad,angry} →
     服务端接受性 + 时长档 + 听感档(落盘人耳验收)。
  C. bidi task_continue 中途换 emotion(决策关键):一条连接 seg1(中性)→seg2,
     对照组 seg2 不带 voice_setting;实验组 seg2 带 voice_setting(emotion=sad)。
     判据 = 服务端报错与否 + seg2 时长/音高比(sad 通常更慢更低;≈1.00 → 被无视)。
     结论决定 emotion 换挡能否免重连(~0.8s)——不能则只配 say 步边界用。
  D. {happy}{/happy} 内联伪语法(ElevenLabs v3 式):预期照念=证伪,LLM 不得吐。

用法:
  SSL_CERT_FILE=$(.venv312/bin/python -c 'import certifi;print(certifi.where())') \\
    .venv312/bin/python scripts/probe_minimax_emotion_tags.py [--rt-only]
API key 取设置 DB tts.api_key(同 tts-pregen);音频落盘 /tmp/minimax_probe/。
ASR 回读只连本机回环 sidecar(127.0.0.1,端口 PROBE_ASR_PORT,默认 8787)。
"""
from __future__ import annotations

import argparse
import asyncio
import audioop
import contextlib
import http.client
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "agent"))

from agent_runtime.providers.livekit_plugins import LanguageState, MiniMaxTTS  # noqa: E402

VOICE = os.environ.get("PROBE_VOICE", "Chinese_wenrounvxing")
MODEL = "speech-2.8-hd"
OUTDIR = os.environ.get("PROBE_OUTDIR", "/tmp/minimax_probe")
# ASR sidecar 固定本机回环:主机硬编码,端口 int 校验(probe 专用,无动态 URL)。
_ASR_HOST = "127.0.0.1"
_ASR_PORT = int(os.environ.get("PROBE_ASR_PORT", "8787"))
_WS_CN = "wss://api.minimax.cn/ws/v1/t2a_v2_bidi"
_BASE_ENV = {"MINIMAX_MODEL": MODEL, "MINIMAX_WS_MODE": "bidi", "MINIMAX_WS_POOL": "0"}

_BASE = "好的，我们已经收到您的信息了。"
_MARKERS = ("breath", "coughs", "sighs", "laughs")
_MARKER_WORDS = {
    "breath": ("breath", "呼吸", "喘"),
    "coughs": ("cough", "咳嗽", "咳"),
    "sighs": ("sigh", "叹", "唉"),
    "laughs": ("laugh", "笑"),
}


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


def _asr_request(method: str, path: str, body: bytes | None = None, timeout: float = 10.0) -> dict:
    conn = http.client.HTTPConnection(_ASR_HOST, _ASR_PORT, timeout=timeout)
    try:
        conn.request(method, path, body=body)
        resp = conn.getresponse()
        return json.loads(resp.read())
    finally:
        conn.close()


def _save_wav(name: str, pcm: bytes) -> str:
    path = os.path.join(OUTDIR, name)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)
    return path


@contextlib.contextmanager
def _env(overrides: dict[str, str]):
    old = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


async def synth_to_wav(name: str, text: str, key: str, extra_env: dict[str, str] | None = None) -> float:
    """生产 MiniMaxTTS(bidi)合成一段文本,落盘 wav,返回音频时长(s)。"""
    env = dict(_BASE_ENV)
    env.update(extra_env or {})
    with _env(env):
        tts = MiniMaxTTS(
            voice=VOICE,
            language_state=LanguageState(lang="zh"),
            sample_rate=24000,
            api_key=key,
        )
        pcm = bytearray()
        t0 = time.monotonic()
        async with tts.synthesize(text) as stream:
            async for ev in stream:
                frame = getattr(ev, "frame", None)
                if frame is not None:
                    pcm.extend(bytes(frame.data))
        wall = time.monotonic() - t0
        try:
            await tts.aclose()
        except Exception:
            pass
    dur = len(pcm) / 2 / 24000.0
    path = _save_wav(f"{name}.wav", bytes(pcm))
    print(
        f"[{name:>18}] audio={dur:6.2f}s wall={wall:5.2f}s chars={len(text)} "
        f"chars_per_s={len(text) / dur if dur else 0:5.2f} -> {path}",
        flush=True,
    )
    return dur


def read_wav_pcm(name: str) -> bytes:
    with wave.open(os.path.join(OUTDIR, name), "rb") as w:
        return w.readframes(w.getnframes())


def asr_roundtrip(pcm_24k: bytes) -> str:
    """24k PCM → 16k 重采样 → sidecar 回读,返回转写文本(失败空串)。"""
    try:
        pcm_16k, _ = audioop.ratecv(pcm_24k, 2, 1, 24000, 16000, None)
        sid = _asr_request("POST", "/api/start?language=Chinese")["session_id"]
        return str(
            _asr_request(
                "POST",
                "/api/finish?" + urllib.parse.urlencode({"session_id": sid}),
                body=pcm_16k,
                timeout=60.0,
            ).get("text")
            or ""
        )
    except Exception as exc:
        print(f"  [asr_roundtrip fail] {exc!r}", flush=True)
        return ""


def judge_marker_roundtrips() -> None:
    print("\n  -- ASR 回读判词(含标记词=照念;不含=声效) --", flush=True)
    for name in ("base",) + _MARKERS:
        wav = "A0_base" if name == "base" else f"A_{name}"
        text = asr_roundtrip(read_wav_pcm(wav + ".wav"))
        flag = ""
        if name != "base":
            hit = any(wd in text.lower() for wd in _MARKER_WORDS[name])
            flag = "  <- 照念(危险)" if hit else "  <- 声效(可用)"
        print(f"  {name:>8}: {text!r}{flag}", flush=True)


def pitch_proxy(name: str) -> float:
    """浊帧基频中位数(Hz,自相关粗估):sad 预期显著低于 neutral。失败 0。"""
    try:
        import numpy as np
    except ImportError:
        return 0.0
    pcm = np.frombuffer(read_wav_pcm(name + ".wav"), dtype=np.int16).astype(np.float32)
    sr = 24000
    frame, hop = sr // 2, sr // 10  # 500ms 窗 100ms 步
    f0s = []
    for i in range(0, len(pcm) - frame, hop):
        seg = pcm[i : i + frame]
        if np.sqrt(np.mean(seg**2)) < 120:  # 静音/气声帧跳过
            continue
        seg = seg - seg.mean()
        ac = np.correlate(seg, seg, "full")[frame - 1 :]
        lo, hi = sr // 400, sr // 60  # 60-400Hz 基频搜索带
        if hi >= len(ac) or ac[:lo].max() <= 0:
            continue
        lag = lo + int(np.argmax(ac[lo:hi]))
        if ac[lag] < 0.35 * ac[0]:
            continue
        f0s.append(sr / lag)
    return float(np.median(f0s)) if f0s else 0.0


async def test_a_markers(key: str) -> None:
    print("\n=== A. 拟声标记:(breath)(coughs)(sighs)(laughs) 声效 or 照念 ===", flush=True)
    durs: dict[str, float] = {}
    durs["base"] = await synth_to_wav("A0_base", _BASE, key)
    for m in _MARKERS:
        durs[m] = await synth_to_wav(f"A_{m}", f"好的，({m})我们已经收到您的信息了。", key)
    judge_marker_roundtrips()
    print("  -- 时长差(vs base) --", flush=True)
    for m in _MARKERS:
        print(f"  {m:>8}: {durs[m] - durs['base']:+.2f}s", flush=True)


async def test_b_emotion(key: str) -> None:
    print("\n=== B. voice_setting.emotion 直透 {unset,happy,sad,angry} ===", flush=True)
    for emo in ("", "happy", "sad", "angry"):
        await synth_to_wav(f"B_{emo or 'unset'}", _BASE, key, extra_env={"MINIMAX_EMOTION": emo} if emo else {})
    print("  -- 音高中位数 Hz(浊帧) --", flush=True)
    for emo in ("unset", "happy", "sad", "angry"):
        print(f"  {emo:>8}: {pitch_proxy('B_' + emo):6.1f}", flush=True)


async def _bidi_collect_seg(ws, gap_s: float = 3.0, hard_s: float = 25.0) -> tuple[bytes, list[str]]:
    """收一段:音频 hex 攒 PCM,is_final 或静默 gap 结束。返回 (pcm, 非音频事件列表)。"""
    pcm = bytearray()
    events: list[str] = []
    t0 = time.monotonic()
    got_audio = False
    while True:
        left = hard_s - (time.monotonic() - t0)
        if left <= 0:
            events.append("HARD_TIMEOUT")
            break
        wait = min(left, gap_s if got_audio else 15.0)
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=wait)
        except asyncio.TimeoutError:
            if got_audio:
                events.append("GAP_END")
            break
        except Exception as exc:
            events.append(f"CLOSED:{exc!r}")
            break
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        audio = (msg.get("data") or {}).get("audio") or ""
        if audio:
            got_audio = True
            pcm.extend(bytes.fromhex(audio))
        if msg.get("is_final"):
            events.append("is_final")
            break
        if not audio:
            events.append(json.dumps(msg, ensure_ascii=False)[:160])
    return bytes(pcm), events


async def test_c_midstream(key: str) -> None:
    import websockets

    print("\n=== C. bidi task_continue 中途带 voice_setting 换 emotion ===", flush=True)
    seg1 = "先生您好，我这边是速递理赔专员。"
    seg2 = "关于您这一件货品的赔偿金额，我们会按条例帮您核算清楚。"

    async def run(tag: str, seg2_vs: dict | None) -> float:
        t0 = time.monotonic()
        ws = await websockets.connect(
            _WS_CN,
            additional_headers={"Authorization": f"Bearer {key}"},
            open_timeout=10,
            max_size=20_000_000,
        )
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
            print(f"  [{tag}] connect: {json.dumps(hello, ensure_ascii=False)[:120]}", flush=True)
            start = {
                "event": "task_start",
                "model": MODEL,
                "voice_setting": {"voice_id": VOICE, "speed": 1.2, "vol": 1.0, "pitch": 0},
                "audio_setting": {"sample_rate": 24000, "format": "pcm", "channel": 1},
                "stream_options": {"exclude_aggregated_audio": True},
            }
            await ws.send(json.dumps(start))
            resp = json.loads(await asyncio.wait_for(ws.recv(), 15))
            print(f"  [{tag}] task_start -> {json.dumps(resp, ensure_ascii=False)[:160]}", flush=True)
            if resp.get("event") != "task_started":
                print(f"  [{tag}] START_FAIL, abort", flush=True)
                return -1.0
            await ws.send(json.dumps({"event": "task_continue", "text": seg1}))
            pcm1, ev1 = await _bidi_collect_seg(ws)
            print(f"  [{tag}] seg1 dur={len(pcm1) / 2 / 24000:.2f}s events={ev1[:4]}", flush=True)
            msg: dict = {"event": "task_continue", "text": seg2}
            if seg2_vs:
                msg["voice_setting"] = seg2_vs
            await ws.send(json.dumps(msg))
            pcm2, ev2 = await _bidi_collect_seg(ws)
            dur2 = len(pcm2) / 2 / 24000
            print(
                f"  [{tag}] seg2 dur={dur2:.2f}s events={ev2[:6]} wall={time.monotonic() - t0:.1f}s",
                flush=True,
            )
            _save_wav(f"C_{tag}_seg1.wav", pcm1)
            _save_wav(f"C_{tag}_seg2.wav", pcm2)
            return dur2
        finally:
            with contextlib.suppress(Exception):
                await ws.close()

    sad_vs = {"voice_id": VOICE, "speed": 1.2, "vol": 1.0, "pitch": 0, "emotion": "sad"}
    d_ctrl = await run("ctrl", None)
    d_sad = await run("sad", sad_vs)
    if d_ctrl > 0 and d_sad > 0:
        ratio = d_sad / d_ctrl if d_ctrl else 0.0
        f_ctrl = pitch_proxy("C_ctrl_seg2")
        f_sad = pitch_proxy("C_sad_seg2")
        fratio = f_sad / f_ctrl if f_ctrl else 0.0
        print(
            f"  -- seg2 对比: 时长比={ratio:.3f} 音高比={fratio:.3f} "
            f"(ctrl {f_ctrl:.0f}Hz vs sad {f_sad:.0f}Hz) --",
            flush=True,
        )
        honored = ratio < 0.92 and fratio < 0.9  # sad 双指标都应显著低
        print(
            "  -- 判定: "
            + (
                "voice_setting 中途生效 → 步级换挡免重连可期"
                if honored
                else "被无视(双指标均≈1.00) → 换 emotion 只能重连(~0.8s),只配 say 步边界"
            ),
            flush=True,
        )


async def test_d_pseudo_tags(key: str) -> None:
    print("\n=== D. {happy}{/happy} 内联伪语法(预期照念=证伪) ===", flush=True)
    await synth_to_wav("D_pseudo", "好的{happy}我们已经收到您的信息了{/happy}。", key)
    text = asr_roundtrip(read_wav_pcm("D_pseudo.wav"))
    hit = any(wd in text.lower() for wd in ("happy", "花括号", "大括号"))
    print(
        f"  回读: {text!r}  "
        f"{'<- 照念(证伪:MiniMax 无内联情绪语法)' if hit else '<- 未念出(仍无证据支持该语法,勿用)'}",
        flush=True,
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip", choices=["a", "b", "c", "d"], action="append", default=[])
    ap.add_argument("--rt-only", action="store_true", help="不合成,只对已有 wav 重跑回读判词")
    args = ap.parse_args()
    os.makedirs(OUTDIR, exist_ok=True)
    if args.rt_only:
        judge_marker_roundtrips()
        text = asr_roundtrip(read_wav_pcm("D_pseudo.wav"))
        hit = any(wd in text.lower() for wd in ("happy", "花括号", "大括号"))
        print(f"  D 回读: {text!r}  {'<- 照念(证伪)' if hit else '<- 未念出'}", flush=True)
        return
    key = os.environ.get("MINIMAX_API_KEY", "") or _api_key_from_settings()
    if not key:
        print("缺 MiniMax API key(设置 DB tts.api_key)", file=sys.stderr)
        sys.exit(1)
    skip = set(args.skip)
    if "a" not in skip:
        await test_a_markers(key)
    if "b" not in skip:
        await test_b_emotion(key)
    if "c" not in skip:
        await test_c_midstream(key)
    if "d" not in skip:
        await test_d_pseudo_tags(key)
    print(f"\n音频全部落盘 {OUTDIR}/ (人耳验收用)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
