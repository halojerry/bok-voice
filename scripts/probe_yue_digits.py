#!/usr/bin/env python3
"""TTS 粤语数字读音探针：合成并(可选)ASR 回读,定位 0-9 / 尾号读法错误。

背景:客户报「尾号7890」,粤语客服要读出「七八九零」,但个别粤语 TTS 音色把
孤立汉字(尤其「九」gau2)读错/读成普通话。本脚本对指定音色合成多组数字文本,
存到 /tmp/probe_yue_digits/<voice>/ 下;若本地 Qwen3-ASR(:8787)在线,自动转写
比对比对,输出「哪把声、哪个字」啱/错表。

用法(库已安装: .venv312/bin/python):
  .venv312/bin/python scripts/probe_yue_digits.py --voice Cantonese_Male_news_anchor_vv2
  .venv312/bin/python scripts/probe_yue_digits.py --voice Cantonese_Male_news_anchor_vv2 \
      Cantonese_crisp_news_anchor_vv2 Cantonese_GentleLady --no-asr

未传 --voice:自动读设置页 DB 实际生效音色(speaker/speaker_cantonese)+ 固定 2 个后备。
API key 取 MINIMAX_API_KEY env,缺则读 settings DB tts.api_key(与 agent 同源)。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# 允许 script 直接跑(imports agent_runtime,而 repo 根 apps/agent 要先入 path)
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO / "apps" / "agent"))

DIGIT_CHARS = "零一二三四五六七八九"

# 合成素材:裸逐字、带标点分隔、真实话术
CASES = [
    ("digits_run", "零一二三四五六七八九"),
    ("digits_sep", "零、一、二、三、四、五、六、七、八、九"),
    ("tail7890_run", "尾號七八九零"),
    ("tail7890_sep", "尾號七、八、九、零"),
    ("tail1234", "尾號一二三四"),
    ("confirm", "收到，尾號係七八九零，啱唔啱？"),
]

_CN_HTTP = "https://api.minimax.cn/v1/t2a_v2"
_INTL_HTTP = "https://api.minimax.chat/v1/t2a_v2"
_CN_WS = "wss://api.minimax.cn/ws/v1/t2a_v2"
_INTL_WS = "wss://api.minimax.chat/ws/v1/t2a_v2"
_BACKUP_YUE = ["Cantonese_crisp_news_anchor_vv2", "Cantonese_GentleLady"]


def _settings_path() -> Path:
    home = Path.home()
    for cand in (home / "Library/Application Support/BokVoice/bok_voice.db",):
        if cand.exists():
            return cand
    return home / "Library/Application Support/BokVoice/bok_voice.db"


def _db_voice_and_key(db: Path) -> tuple[str, str]:
    """读实际生效的 tts speaker/speaker_cantonese + api_key(与 agent 同源)。"""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute("SELECT tts_json FROM global_settings WHERE id='global'").fetchone()
        con.close()
    except Exception:
        return "", ""
    if not row or not row[0]:
        return "", ""
    try:
        cfg = json.loads(row[0])
    except Exception:
        return "", ""
    speaker = str(cfg.get("speaker") or "").strip()
    # 新键 speaker_cantonese 优先；旧键 speaker_yue 作只读别名(DB 迁移后只剩新键)。
    speaker_ca = str(cfg.get("speaker_cantonese") or cfg.get("speaker_yue") or "").strip()
    return (speaker or speaker_ca or ""), str(cfg.get("api_key") or "").strip()


def _minimax_region_http() -> str:
    base = os.environ.get("MINIMAX_BASE_URL", "").strip()
    if base:
        return base.rstrip("/")
    region = os.environ.get("MINIMAX_REGION", "cn").strip().lower()
    return _INTL_HTTP if region in {"intl", "global", "chat"} else _CN_HTTP


def _endpoint_ws() -> str:
    base = os.environ.get("MINIMAX_WS_URL", "").strip()
    if base:
        return base
    region = os.environ.get("MINIMAX_REGION", "cn").strip().lower()
    return _INTL_WS if region in {"intl", "global", "chat"} else _CN_WS


async def _synth_http(client, key: str, voice: str, text: str, out_dir: Path) -> bool:
    """HTTP 整段合成(与 agent 回退路径同构),存 wav/pcm。"""
    url = _minimax_region_http()
    payload = {
        "model": os.environ.get("MINIMAX_MODEL", "speech-2.8-hd"),
        "text": text,
        "stream": False,
        "voice_setting": {
            "voice_id": voice,
            "speed": float(os.environ.get("MINIMAX_SPEED", "1")),
            "vol": float(os.environ.get("MINIMAX_VOL", "1")),
            "pitch": int(os.environ.get("MINIMAX_PITCH", "0")),
            "emotion": os.environ.get("MINIMAX_EMOTION", "calm"),
        },
        "audio_setting": {"sample_rate": 24000, "format": "pcm", "channel": 1},
    }
    try:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        data = resp.json()
    except Exception as exc:
        print(f"  [http] 请求失败 {exc!r}")
        return False
    if resp.status_code != 200 or data.get("base_resp", {}).get("status_code", 0) != 0:
        print(f"  [http] {resp.status_code} {str(data)[:200]}")
        return False
    audio_hex = data.get("data", {}).get("audio") or ""
    if not audio_hex:
        print("  [http] 无 audio 字段")
        return False
    pcm = bytes.fromhex(audio_hex)
    (out_dir / "raw.pcm").write_bytes(pcm)
    _pcm_to_wav(out_dir / "raw.pcm", out_dir / "out.wav", 24000)
    return True


def _pcm_to_wav(pcm: Path, wav: Path, rate: int) -> None:
    """PCM S16LE → WAV(header),供手动播放。"""
    data = pcm.read_bytes()
    hdr = bytearray()
    hdr += b"RIFF" + (36 + len(data)).to_bytes(4, "little") + b"WAVE"
    hdr += b"fmt " + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
    hdr += (1).to_bytes(2, "little") + rate.to_bytes(4, "little")
    hdr += (rate * 2).to_bytes(4, "little") + (2).to_bytes(2, "little")
    hdr += (16).to_bytes(2, "little")
    hdr += b"data" + len(data).to_bytes(4, "little")
    wav.write_bytes(bytes(hdr) + data)


async def _asr_file(client, wav_path: Path) -> str:
    """Qwen3-ASR sidecar(:8787, chunked API)转写;不在线/失败返回 ''。

    contract: POST /api/start → {session_id};POST /api/chunk?session_id= 发裸 PCM
    bytes;POST /api/finish?session_id= → {text, language}(mlx 后端 finish 时整段转写)。
    素材是 24000Hz s16le,sidecar 会自己 _resample 到模型采样率。
    """
    try:
        st = await client.post("http://127.0.0.1:8787/api/start", timeout=10)
        sid = str(st.json().get("session_id") or "")
        if not sid:
            return ""
        pcm = wav_path.with_suffix(".pcm").read_bytes()
        # chunk 响应文本常为空(mlx 后端等 finish 先出);finish 先系最终转写。
        await client.post(
            f"http://127.0.0.1:8787/api/chunk?session_id={sid}",
            content=pcm,
            headers={"Content-Type": "application/octet-stream"},
            timeout=30,
        )
        fin = await client.post(f"http://127.0.0.1:8787/api/finish?session_id={sid}", timeout=60)
        if fin.status_code == 200:
            data = fin.json()
            return str(data.get("text") or "").strip()
    except Exception:
        pass
    return ""


async def _probe_voice(key: str, voice: str, do_asr: bool) -> None:
    print(f"\n=== 音色: {voice} ===")
    root = Path("/tmp/probe_yue_digits") / voice.replace("/", "_")
    root.mkdir(parents=True, exist_ok=True)
    async with __import__("httpx").AsyncClient() as client:
        for name, text in CASES:
            d = root / name
            d.mkdir(parents=True, exist_ok=True)
            ok = await _synth_http(client, key, voice, text, d)
            if not ok:
                print(f"  ✗ {name:18s} 合成失败(跳过)")
                continue
            # 留点间隔,唔好一口气打爆 MiniMax RPM(HTTP 整段并发限制较紧)
            await asyncio.sleep(1.0)
            asr = ""
            if do_asr:
                asr = await _asr_file(client, d / "out.wav")
            mark = "?"
            if not do_asr or not asr:
                mark = "~"  # 未 ASR 或 ASR 空:请人耳听 /tmp wav
            else:
                want = text.replace(" ", "").replace("，", "")
                got = asr.replace(" ", "").replace("，", "")
                # 接受子串命中;真实粤语字在 ASR 转写会有繁简/同音浮动,额外容许每字对得上
                mark = "✓" if want in got or all(ch not in "零一二三四五六七八九" or ch in got for ch in want) else "✗"
            print(f"  {mark} {name:18s} ASR: {asr or '(ASR 不可用/空,听 /tmp wav)'}")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--voice", nargs="+", default=[], help="MiniMax 音色 ID(可多个);缺省读 DB + 后备")
    ap.add_argument("--no-asr", action="store_true", help="跳过 ASR 回读(只合成到 /tmp)")
    args = ap.parse_args()

    db = _settings_path()
    db_voice, db_key = _db_voice_and_key(db)
    key = os.environ.get("MINIMAX_API_KEY", "") or db_key
    if not key:
        print("找不到 MiniMax API key: 设 MINIMAX_API_KEY,或设置页 tts.api_key")
        sys.exit(2)

    voices = [v for v in args.voice] if args.voice else []
    if not voices and db_voice:
        voices = [db_voice]
    if not voices:
        print("找不到生效音色(speaker/speaker_cantonese 都空),请 --voice 指定")
        sys.exit(2)
    # 去重、保留顺序。显式 --voice 只合成指定音色(唔自动加后备,避免撞 MiniMax RPM);
    # 自动取 DB 音色时先试 DB 音色,再补固定 2 个后备粤语 anchor。
    seen: set[str] = set()
    ordered = [v for v in voices if not (v in seen or seen.add(v))]
    if not args.voice:
        for v in _BACKUP_YUE:
            if v not in seen:
                ordered.append(v)
                seen.add(v)

    if db_voice:
        print(f"DB 当前生效 tts 音色: {db_voice}")
    else:
        print("DB 未配 speaker/speaker_cantonese,用 --voice")
    t0 = time.time()
    for v in ordered:
        await _probe_voice(key, v, not args.no_asr)
    print(f"\n耗时 {time.time() - t0:.0f}s;产物在 /tmp/probe_yue_digits/<voice>/")


if __name__ == "__main__":
    asyncio.run(main())
