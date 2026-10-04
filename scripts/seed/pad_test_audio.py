"""Pad test WAVs with leading/trailing silence for the Silero VAD.

Chrome's --use-file-for-fake-audio-capture loops the file as the mic source.
Without silence at both ends, Silero VAD can treat the loop as one continuous
speech segment (max_buffered_speech fills up, END never fires, ASR never runs).
"""

from __future__ import annotations
# --- scripts import bootstrap (G1) ---
# sys.path 引导(G1 迁移解耦,见 docs/superpowers/plans/2026-10-04-repo-governance-plan.md §3.1):
# 同层时是 no-op;文件挪进任何桶后裸 import 兄弟模块继续解析。
import sys as _sys, pathlib as _pathlib
_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))


import os
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIO_DIR = ROOT / "tests" / "fixtures" / "audio"
PAD_SECONDS = float(os.environ.get("PAD_SECONDS", "0.6"))
TRAIL_SILENCE_SECONDS = float(os.environ.get("TRAIL_SILENCE_SECONDS", "45"))
FILES = ["zh.wav", "cantonese.wav", "en.wav"]


def main() -> None:
    for name in FILES:
        path = AUDIO_DIR / name
        with wave.open(str(path), "rb") as w:
            params = w.getparams()
            frames = w.readframes(w.getnframes())
        pad = int(params.framerate * PAD_SECONDS)
        trail = int(params.framerate * TRAIL_SILENCE_SECONDS)
        silence = b"\x00\x00" * pad  # 16-bit mono
        tail = b"\x00\x00" * trail
        with wave.open(str(path), "wb") as w:
            w.setparams(params)
            w.writeframes(silence + frames + tail)
        print(f"[pad] {name}: +{PAD_SECONDS}s lead / +{TRAIL_SILENCE_SECONDS}s trail -> {params.nframes + pad + trail} frames")


if __name__ == "__main__":
    main()
