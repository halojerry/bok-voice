"""bf16 vs 8bit 数字回归检查（2026-09-17 ASR 模型档位 A/B 辅助）。

铁律背景：ASR 模型 4bit 曾实证数字路径同音字滑失（九→狗/號→后），当年因此
钉死 8bit。bf16 候选必须先过数字关再谈碎裂句收益。
复用 probe_hotword_ab 的合成/解码/归一化件；两后端各跑 ROUNDS 轮取好成绩，
判据=转写中数字串（含中文数字词）与原句逐位一致。
用法：<python> scripts/probe_asr_digits_ab.py --asr-urls http://127.0.0.1:8787,http://127.0.0.1:8789
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))

import probe_hotword_ab as hab  # noqa: E402

DIGIT_SENTENCES = [
    "我個單號係八六五三二七四零",
    "幫我查下單號三七七八九零",
    "我的电话号码是一三八二三五六七八九",
]

_CN_DIGITS = "零一二三四五六七八九"


def digit_runs(s: str) -> list[str]:
    """抽出数字串：阿拉伯数字连串 + 中文数字词连串（归一化后）。"""
    runs: list[str] = []
    buf = ""
    for ch in s:
        if ch.isdigit():
            buf += ch
        elif ch in _CN_DIGITS:
            buf += str(_CN_DIGITS.index(ch))
        else:
            if len(buf) >= 3:
                runs.append(buf)
            buf = ""
    if len(buf) >= 3:
        runs.append(buf)
    return runs


def main() -> int:
    ap = argparse.ArgumentParser(description="ASR 数字回归 A/B")
    ap.add_argument("--asr-urls", default="http://127.0.0.1:8787,http://127.0.0.1:8789")
    ap.add_argument("--rounds", type=int, default=2)
    args = ap.parse_args()
    urls = [u.strip().rstrip("/") for u in args.asr_urls.split(",") if u.strip()]

    print(f"后端={urls} 轮数={args.rounds}")
    audios = []
    for s in DIGIT_SENTENCES:
        audios.append(hab.silence_pcm(0.2) + hab.tts_pcm(s) + hab.silence_pcm(0.2))
        time.sleep(0.2)

    all_pass = True
    for url in urls:
        print(f"\n=== {url} ===")
        for i, sent in enumerate(DIGIT_SENTENCES):
            want = digit_runs(hab._norm_text(sent))
            best: tuple[int, list[str], str, float] | None = None
            for rnd in range(1, args.rounds + 1):
                text, e2e = hab.asr_decode(audios[i], hab.CONTEXTS["extended"])
                got = digit_runs(hab._norm_text(text))
                ok = got == want
                if best is None or (ok and not best[0]) or (ok == best[0] and e2e < best[3]):
                    best = (ok, got, text, e2e)
                time.sleep(hab.SENT_GAP_S)
            assert best is not None
            mark = "✓" if best[0] else "✗"
            all_pass = all_pass and best[0]
            print(f"  {mark} 「{sent}」")
            print(f"      期望={want}")
            print(f"      实得={best[1]}  {best[3] * 1000:.0f}ms  「{best[2][:40]}」")
    print(f"\nDIGITS_AB {'PASS' if all_pass else 'FAIL'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
