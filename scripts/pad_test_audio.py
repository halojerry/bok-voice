"""Pad test WAVs with leading/trailing silence for the Silero VAD.

Chrome's --use-file-for-fake-audio-capture loops the file as the mic source.
Without silence at both ends, Silero VAD can treat the loop as one continuous
speech segment (max_buffered_speech fills up, END never fires, ASR never runs).
"""

from __future__ import annotations

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
