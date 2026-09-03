"""Sidecar smoke test for Qwen3-ASR + Qwen3-TTS (A-line local verification).

Requires both sidecars running:
  ASR: http://127.0.0.1:8787  (QWEN3_ASR_MODEL -> local 0.6b path)
  TTS: http://127.0.0.1:8788  (QWEN3_TTS_PRESET_MODEL/CLONE_MODEL -> local paths)

Verifies:
  1) ASR transcribes zh / yue / en test audio with expected language tags.
  2) TTS preset synthesis works for zh / en, and yue maps to a supported language.
  3) Voice cloning registers a Cantonese reference and synthesizes with it;
     the synthesized audio is then fed back through ASR to check the output
     language tag.
"""

from __future__ import annotations

import json
import struct
import sys
import wave
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ASR = "http://127.0.0.1:8787"
TTS = "http://127.0.0.1:8788"
AUDIO_DIR = ROOT / "tests" / "fixtures" / "audio"
OUT_DIR = ROOT / "data" / "smoke-out"


def assert_step(label: str, cond: bool, data=None) -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {label}", data or "")
    if not cond:
        raise SystemExit(f"FAILED: {label}")


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        assert w.getsampwidth() == 2, f"{path.name} not 16-bit"
        return w.readframes(w.getnframes())


def asr_transcribe(client: httpx.Client, pcm: bytes) -> tuple[str, str]:
    s = client.post(f"{ASR}/api/start").json()["session_id"]
    for i in range(0, len(pcm), 3200):
        client.post(
            f"{ASR}/api/chunk",
            params={"session_id": s},
            content=pcm[i : i + 3200],
            headers={"Content-Type": "application/octet-stream"},
        )
    r = client.post(f"{ASR}/api/finish", params={"session_id": s}).json()
    return str(r.get("text") or ""), str(r.get("language") or "")


def pcm_to_wav(pcm: bytes, path: Path, sample_rate: int = 24000) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)


def main() -> None:
    with httpx.Client(timeout=180) as client:
        # 0) health
        asr_health = client.get(f"{ASR}/health").json()
        tts_health = client.get(f"{TTS}/health").json()
        assert_step("ASR health model_ready", asr_health.get("model_ready") is True, asr_health)
        assert_step("TTS health model_ready", tts_health.get("model_ready") is True, tts_health)

        # 1) ASR zh / yue / en
        expected_lang = {"zh.wav": "Chinese", "yue.wav": "Cantonese", "en.wav": "English"}
        asr_results = {}
        for name, want in expected_lang.items():
            path = AUDIO_DIR / name
            assert path.exists(), f"missing {path}"
            text, lang = asr_transcribe(client, read_wav(path))
            asr_results[name] = (text, lang)
            assert_step(f"ASR {name} -> {want}", bool(text) and want.lower() in lang.lower(), (text, lang))

        # 2) TTS preset speakers
        speakers = client.get(f"{TTS}/v1/speakers").json()
        assert_step("TTS /v1/speakers non-empty", isinstance(speakers, list) and len(speakers) > 0, speakers)

        preset_texts = [
            ("zh", "你好，欢迎致电博克，请问有什么可以帮您？", "Vivian"),
            ("en", "Hello, welcome to Bok. How can I help you today?", "Vivian"),
            ("cantonese", "你好，歡迎致電博克，請問有咩可以幫到你？", "Vivian"),
        ]
        tts_results = {}
        for lang, text, speaker in preset_texts:
            r = client.post(
                f"{TTS}/v1/audio/speech",
                json={"input": text, "language": lang, "voice": speaker, "sample_rate": 24000},
            )
            r.raise_for_status()
            wav_path = OUT_DIR / f"preset-{lang}.wav"
            pcm_to_wav(r.content, wav_path)
            tts_results[lang] = wav_path
            assert_step(f"TTS preset {lang} bytes>0", len(r.content) > 8000, len(r.content))

        # 2b) feedback: preset Mandarin speaker must come back as Chinese.
        zh_text, zh_tag = asr_transcribe(client, read_wav(tts_results["zh"]))
        assert_step(
            "preset-zh ASR tag Chinese",
            bool(zh_text) and "chinese" in zh_tag.lower(),
            (zh_text, zh_tag),
        )

        # 3) voice clone registration (Cantonese reference) + synthesis
        ref = AUDIO_DIR / "yue.wav"
        ref_text = asr_results["yue.wav"][0]
        assert_step("yue ref transcript available", bool(ref_text), ref_text)
        files = {"file": ("yue.wav", ref.read_bytes(), "audio/wav")}
        data = {"voice_id": "yue-clone-1", "ref_text": ref_text, "language": "cantonese"}
        reg = client.post(f"{TTS}/v1/voices/register", files=files, data=data)
        reg.raise_for_status()
        reg_json = reg.json()
        assert_step("voice clone registered", reg_json.get("voice_id") == "yue-clone-1", reg_json)

        clone_texts = [
            ("cantonese", "你好，歡迎致電博克，我哋支持粵語實時通話。"),
            ("zh", "你好，欢迎致电博克，我们支持粤语实时通话。"),
            ("en", "Hello, this is Bok. We support realtime Cantonese calls."),
        ]
        clone_results = {}
        for lang, text in clone_texts:
            r = client.post(
                f"{TTS}/v1/audio/speech",
                json={"input": text, "language": lang, "voice": "yue-clone-1", "sample_rate": 24000},
            )
            r.raise_for_status()
            wav_path = OUT_DIR / f"clone-{lang}.wav"
            pcm_to_wav(r.content, wav_path)
            clone_results[lang] = wav_path
            assert_step(f"TTS clone {lang} bytes>0", len(r.content) > 8000, len(r.content))

        # 4) feedback: transcribe the synthesized clone audio (language tag check).
        #    yue must come back Cantonese (ICL-driven accent); en must stay English.
        #    zh is intentionally skipped: a Cantonese-cloned voice reading Mandarin
        #    text is expected to keep the Cantonese accent and ASR tags it Cantonese.
        clone_lang_expect = {"cantonese": "Cantonese", "en": "English"}
        for lang, wav_path in clone_results.items():
            if lang not in clone_lang_expect:
                print(f"    clone-{lang} -> (language-tag assert skipped by design)")
                continue
            text, lang_tag = asr_transcribe(client, read_wav(wav_path))
            print(f"    clone-{lang} -> ASR({lang_tag}): {text[:60]}")
            assert_step(
                f"clone-{lang} ASR tag {clone_lang_expect[lang]}",
                bool(text) and clone_lang_expect[lang].lower() in lang_tag.lower(),
                (text, lang_tag),
            )

        voices = client.get(f"{TTS}/v1/voices").json()
        assert_step("voice list contains clone", any(v.get("voice_id") == "yue-clone-1" for v in voices), voices)

    print("\nSIDECAR_SMOKE_PASSED")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as exc:
        print("HTTP error:", exc.response.status_code, exc.response.text[:500])
        sys.exit(1)
