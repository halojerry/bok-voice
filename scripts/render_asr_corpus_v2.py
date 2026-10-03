#!/usr/bin/env python3
"""ASR 评测语料 v2：粤语条目重渲（好音频版，2026-10-03）。

背景：原 asr-whisper-bench 语料的粤语条目是 2026-09-24 用本地 TTS 普通话预设声
（Vivian）渲染的——report.md 自记「本语料=本地 TTS 渲染的粤语短句…可懂度差」。
所有引擎在该音频上分数全被污染（whisper 0.849 / MiniMax 0.621 / 本地 0.438 /
豆包 0.748），呈现「粤语都烂」假象；换真粤语声（macOS 内建 Sinji，zh_HK）重渲后
三家全部近满分（豆包 0.074 / MiniMax 0.000 / 本地 0.035，n=6 实弹）。

本脚本：读原 corpus/manifest.json → 粤语条目用 ``say -v Sinji`` 重渲 16k mono
wav、非粤语条目原样复制 → 写 corpus-v2（同 id、同参考文本，仅音频换源；
manifest 增 "audio" 溯源字段）。仅 macOS（say / afconvert）。

用法：
  .venv312/bin/python scripts/render_asr_corpus_v2.py \
      [--out reports/asr-whisper-bench/corpus-v2]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "reports" / "asr-whisper-bench" / "corpus"
OUT_DEFAULT = ROOT / "reports" / "asr-whisper-bench" / "corpus-v2"
VOICE = "Sinji"  # macOS 内建粤语声（zh_HK）


def _render_canto(text: str, out_wav: Path) -> None:
    aiff = out_wav.with_suffix(".aiff")
    subprocess.run(["say", "-v", VOICE, "-o", str(aiff), text], check=True)
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
         str(aiff), str(out_wav)],
        check=True,
    )
    aiff.unlink(missing_ok=True)


def _dur_s(wav_path: Path) -> float:
    with wave.open(str(wav_path), "rb") as w:
        return round(w.getnframes() / w.getframerate(), 2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    items = json.loads((SRC / "manifest.json").read_text(encoding="utf-8"))
    new_items = []
    for it in items:
        dst = out / it["file"]
        if it["lang"] == "cantonese":
            _render_canto(it["text"], dst)
            it = {**it, "dur_s": _dur_s(dst), "audio": f"rendered:{VOICE}"}
        else:
            shutil.copy2(SRC / it["file"], dst)
            it = {**it, "audio": "original"}
        new_items.append(it)
        print(f"[{it['id']}] {it['lang']:9} {it['dur_s']:4.1f}s {it['audio']}", flush=True)
    (out / "manifest.json").write_text(
        json.dumps(new_items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] {len(new_items)} items → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
