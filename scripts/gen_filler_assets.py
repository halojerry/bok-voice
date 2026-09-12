#!/usr/bin/env python3
"""垫话音频资产生成器(2026-09-10 拍板,spec 讨论见会话)。

设计契约:
- 垫话=随源码分发的固定资产:固定音色+固定参数预生成,与运行时人设音色解耦;
- 万能话术:只保留注意力应承(好的/收到/明白/嗯)与等待邀请(稍等/我看下),
  零动作动词——随机触发语境永不穿帮;
- zh/粤 speed=1.2 pitch=0 vol=1.0;en speed=1.0(用户指定);
- <#x#> 停顿标记直传(MiniMax 官方语法,x=秒,0.01-99.99,须夹在可发音文本间);
- 两档时长窗:短句 tier(lines,10 条/语言)目标 1.0-1.5s,窗 [0.9,1.6] WARN,
  窗外 FAIL;长句 tier(long_lines,3 条/语言,文件名 -11..13)覆盖窗目标
  1.7-2.3s,窗 [1.6,2.4] WARN,窗外 FAIL;
- 输出 wav(24k mono 16bit)+manifest.json 到 apps/agent/agent_runtime/assets/fillers/,
  运行时(fillers.py)只播文件,绝不云合成。

用法:
    .venv312/bin/python scripts/gen_filler_assets.py [--dry-run] [--force]
key 读取顺序: settings DB(tts.api_key) → MINIMAX_API_KEY env。端点同生产 worker:
MINIMAX_BASE_URL > MINIMAX_REGION(cn→api.minimax.cn / intl→api.minimax.chat)。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "apps" / "agent"))

OUT_DIR = REPO / "apps" / "agent" / "agent_runtime" / "assets" / "fillers"
SAMPLE_RATE = 24000
MODEL = "speech-2.8-hd"

# 万能话术清单(2026-09-10 用户逐条过审;第一轮实测修订:停顿标记只放句中
# ——尾随标记违反官方「须夹在两段可发音文本之间」被静默忽略;短句以双 token
# 或句中停顿拉到窗内,不靠尾随停顿)。改话术=改这里重新生成,一个 PR。
# en 社媒女声逗号处拖长腔明显(Mm-hm, sure.=2.57s 实测),en 全部无逗号短句。
FILLERS: dict[str, dict] = {
    "zh": {
        # 2026-09-12 普通话默认音色切用户克隆 moss_audio_*(agent._MINIMAX_DEFAULT_VOICES
        # 同步拍板)——垫话资产层与运行时默认同源,换默认=换资产重生成。
        "voice": "moss_audio_aaa1346a-7ce7-11f0-8e61-2e6e3c7ee85d",
        "speed": 1.2,
        "pitch": 0,
        # 2026-09-12 用户定档重排:旧池「好的您稍等」式万能腔机械——按集运理赔
        # 话术语域重写(订单/物流/核实/这单),口语语气词自然衔接;1.5-2.0s 口径。
        "lines": [
            "哎好的，您稍等，我马上查。",
            "您别急，我看一下您这单的情况。",
            "好的您说，我这就去核实。",
            "嗯稍等，我查下物流。",
            "收到收到，我马上给您查这单。",
            "您慢慢说，我在听着呢。",
            "好嘞，稍等，我帮您核实。",
            "别着急，我正在看您这单。",
            "嗯<#0.2#>您稍等，我看一下。",
            "好的稍等，马上给您查清楚。",
        ],
        "win": ((1.35, 2.15), (1.25, 2.3)),  # 短句 tier zh 专属窗(目标 1.5-2.0s)
        "long_lines": [
            # 首轮实测修订:zh 双逗号/冗字拖腔超窗(「您别急」轮 3.49s),删中段
            # 压回窗内;万能话术不变(应承+等待邀请,查一下=等待邀请动词面)。
            "好的，您稍等，我马上帮您看一下。",
            "收到收到，我这就帮您查一下。",
            "麻烦您稍等一下，我马上看一下。",
        ],
    },
    "cantonese": {
        "voice": "Cantonese_crisp_news_anchor_vv2",
        "speed": 1.2,
        "pitch": 0,
        # 2026-09-12 用户定档重排:旧池 0.9-1.5s 偏短,整批换 1.5-2.0s 口径
        # (13-16 字/条,应承+等待邀请万能话术不变);窗见 win 字段。
        "lines": [
            "唔使急，你慢慢講，我幫你睇緊。",
            "好嘅，你稍等一陣，我即刻幫你核實。",
            "收到，你等我幾秒，查緊喇。",
            "明白，你繼續講，我聽緊。",
            "好，你講先，我而家幫你睇返。",
            "冇問題，你稍等多一陣，我跟進緊。",
            "唔使擔心，我幫你睇緊先。",
            "好嘅，你等陣，我幫你查下先。",
            "收到，你慢慢講，我聽緊。",
            "嗯<#0.3#>你稍等，我就幫你睇。",
        ],
        "win": ((1.35, 2.15), (1.25, 2.3)),  # 短句 tier 粤语专属窗(目标 1.5-2.0s)
        "long_lines": [
            "好嘅，你稍等陣，我而家就幫你睇下。",
            "收到，唔好急，等我幫你睇下先。",
            "明白，麻煩你稍等多一陣，我即刻睇。",
        ],
    },
    "en": {
        "voice": "socialmedia_female_2_v1",
        "speed": 1.0,
        "pitch": 0,
        # 2026-09-12 同步重排:按话术语域(order/shipment/verify)+零停顿连读
        # (en 音色逗号拖腔实证)——无逗号短句,目标 1.2-1.8s。
        "lines": [
            "Sure let me check your order right away.",
            "Okay give me a second to look into it.",
            "Hold on I'm checking your shipment now.",
            "One moment please I'll verify your order.",
            "Let me pull up your order details now.",
            "Just a moment while I check on this.",
            "Checking your order now.",
            "Give me a second to verify it.",
            "Checking the shipping info now.",
            "Sure I'll look into that for you right away.",
        ],
        "win": ((1.1, 2.0), (1.0, 2.2)),  # 短句 tier en 专属窗(目标 1.2-1.8s)
        # en 社媒女声停顿拖腔实证(两轮):逗号(Mm-hm, sure.=2.57s)与 <#0.3#>
        # 标记都令相邻词拖长 +1.8s 以上——en 长句一律零停顿连读,靠 9 词左右
        # (~0.2s/词)落窗;只保留等待邀请/应承语义,无中段句号。
        "long_lines": [
            "Just a moment and I will check that.",
            "One moment and I will check on it.",
            "One moment please and I will check that.",
        ],
    },
}

WIN_WARN = (0.9, 1.6)
WIN_FAIL = (0.8, 1.8)
LONG_WIN_WARN = (1.6, 2.4)  # 长句 tier:覆盖窗目标 1.7-2.3s
LONG_WIN_FAIL = (1.5, 2.6)


def load_api_key() -> str:
    raw = os.environ.get("MINIMAX_API_KEY", "").strip()
    if raw:
        return raw
    db = Path.home() / "Library" / "Application Support" / "BokVoice" / "bok_voice.db"
    if db.exists():
        row = sqlite3.connect(str(db)).execute(
            "SELECT tts_json FROM global_settings WHERE id='global'"
        ).fetchone()
        if row:
            key = str((json.loads(row[0]) or {}).get("api_key") or "").strip()
            if key:
                return key
    print("FAIL: no MiniMax key (settings DB / MINIMAX_API_KEY)", file=sys.stderr)
    sys.exit(2)


def endpoint() -> str:
    base = os.environ.get("MINIMAX_BASE_URL", "").strip().rstrip("/")
    if base:
        return base
    region = os.environ.get("MINIMAX_REGION", "cn").strip().lower()
    return "https://api.minimax.cn" if region == "cn" else "https://api.minimax.chat"


def synth_pcm(key: str, base: str, text: str, voice: str, speed: float, pitch: int) -> bytes:
    """t2a_v2 HTTP → 24k mono 16bit PCM。停顿标记 <#x#> 随 text 直传(句中)。

    RPM 限频(1002)退避重试 ×3。"""
    body = {
        "model": MODEL,
        "text": text,
        "stream": False,
        "voice_setting": {"voice_id": voice, "speed": float(speed), "vol": 1.0, "pitch": int(pitch)},
        "audio_setting": {"sample_rate": SAMPLE_RATE, "format": "pcm", "channel": 1},
    }
    import certifi, ssl, time

    ctx = ssl.create_default_context(cafile=certifi.where())
    last = ""
    for attempt in range(3):
        req = urllib.request.Request(
            f"{base}/v1/t2a_v2",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            data = json.loads(resp.read().decode())
        code = int(data.get("base_resp", {}).get("status_code", -1))
        if code == 0:
            audio = (data.get("data") or {}).get("audio")
            if not audio:
                raise RuntimeError(f"minimax empty audio: {str(data)[:200]}")
            return bytes.fromhex(audio)
        last = str(data.get("base_resp"))
        if code == 1002:  # rate limit(RPM)——退避后重试
            time.sleep(10 * (attempt + 1))
            continue
        break
    raise RuntimeError(f"minimax base_resp={last}")


def trim_silence(pcm: bytes) -> bytes:
    """首尾静音修剪(阈值 200,保留 20ms 余量)+15ms fade 防咔哒。"""
    import array

    samples = array.array("h")
    samples.frombytes(pcm)
    n = len(samples)
    thr = 200
    lead = 0
    while lead < n and abs(samples[lead]) < thr:
        lead += 1
    trail = n
    while trail > lead and abs(samples[trail - 1]) < thr:
        trail -= 1
    keep = 320  # 20ms @24k 余量
    lead = max(0, lead - keep)
    trail = min(n, trail + keep)
    out = samples[lead:trail]
    fade = 360  # 15ms
    for i in range(min(fade, len(out))):
        out[i] = int(out[i] * i / fade)
        out[-1 - i] = int(out[-1 - i] * i / fade)
    return out.tobytes()


def write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="超窗也写资产(manifest 标 warn)")
    args = ap.parse_args()

    key = load_api_key()
    base = endpoint()
    print(f"endpoint={base} model={MODEL} out={OUT_DIR}")

    manifest: dict[str, list[dict]] = {}
    failures: list[str] = []
    if not args.dry_run:
        OUT_DIR.mkdir(parents=True, exist_ok=True)

    for lang, cfg in FILLERS.items():
        entries: list[dict] = []
        tiers = [
            (cfg["lines"], 1, WIN_WARN, WIN_FAIL),
            (cfg.get("long_lines", []), 2, LONG_WIN_WARN, LONG_WIN_FAIL),
        ]
        for lines, tier, warn_win, fail_win in tiers:
            # 语言专属窗覆盖(cantonese 短句 tier 1.5-2.0s 定档,2026-09-12)
            if tier == 1 and "win" in cfg:
                warn_win, fail_win = cfg["win"]
            for i, text in enumerate(lines, 1):
                name = f"{lang}-{i:02d}.wav" if tier == 1 else f"{lang}-{10 + i}.wav"
                if args.dry_run:
                    print(f"[plan] {name}: {text!r} voice={cfg['voice']} speed={cfg['speed']}")
                    continue
                target = OUT_DIR / name
                if target.exists() and not args.force:
                    with wave.open(str(target), "rb") as w:
                        dur = w.getnframes() / w.getframerate()
                    print(f"SKIP {name}: {dur:.2f}s (已存在)")
                    entries.append({"text": text, "file": name, "dur_s": round(dur, 2)})
                    continue
                pcm = synth_pcm(key, base, text, cfg["voice"], cfg["speed"], cfg["pitch"])
                pcm = trim_silence(pcm)
                dur = len(pcm) / 2 / SAMPLE_RATE
                ok = fail_win[0] <= dur <= fail_win[1]
                warn = not (warn_win[0] <= dur <= warn_win[1])
                mark = "OK " if not warn else ("WARN" if ok else "FAIL")
                print(f"{mark} {name}: {dur:.2f}s {text!r}")
                if not ok and not args.force:
                    failures.append(f"{name} {dur:.2f}s 超窗 {text!r}")
                    continue
                write_wav(target, pcm)
                entries.append({"text": text, "file": name, "dur_s": round(dur, 2)})
        manifest[lang] = entries
        import time as _t

        _t.sleep(2)  # 语言批次间限频缓冲

    if args.dry_run:
        return 0
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"manifest.json written ({sum(len(v) for v in manifest.values())} entries)")
    if failures:
        print("FAIL(超窗未写入,改话术后重跑):", *failures, sep="\n  ", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
