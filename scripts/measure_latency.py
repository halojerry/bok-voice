"""逐阶段延迟实测：ASR / LLM 回复 / 翻译 / TTS / 总链路估算。

运行：.venv312/bin/python scripts/measure_latency.py
（需 sidecar 8787/8788 MLX 后端 + mlx_lm LLM 服务 1235）
"""

from __future__ import annotations

import os
import time
import wave
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ASR = os.environ.get("QWEN3_ASR_BASE_URL", "http://127.0.0.1:8787")
TTS = os.environ.get("QWEN3_TTS_BASE_URL", "http://127.0.0.1:8788")
LLM = os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1")
MODEL = os.environ.get(
    "MLX_LLM_MODEL",
    "/Users/halo/.lmstudio/models/avan-ag/Qwen3.5-4B-Uncensored-MLX-4bit",
)


def read_wav_pcm(path: Path, max_seconds: float = 3.0) -> bytes:
    with wave.open(str(path), "rb") as w:
        n = int(min(w.getnframes(), w.getframerate() * max_seconds))
        return w.readframes(n)


def ms(t0: float) -> str:
    return f"{(time.perf_counter() - t0) * 1000:.0f}ms"


def measure_asr(client: httpx.Client) -> None:
    # 与 agent 同款「整包上传」契约（P0 提速后）：start(language 提示) → finish(body=PCM)。
    # 分别测 auto 与 cantonese 提示，验证粤语转写走语言提示。
    pcm = read_wav_pcm(ROOT / "tests/fixtures/audio/yue.wav")
    for hint in ("", "cantonese"):
        t0 = time.perf_counter()
        s = client.post(
            f"{ASR}/api/start", params={"language": hint} if hint else None, timeout=30
        ).json()["session_id"]
        r = client.post(
            f"{ASR}/api/finish", params={"session_id": s}, content=pcm,
            headers={"Content-Type": "application/octet-stream"}, timeout=60,
        ).json()
        print(f"ASR 转写 hint={hint or 'auto':<10}: {ms(t0)}   text={r.get('text','')[:36]!r}")


def measure_llm(client: httpx.Client) -> None:
    t0 = time.perf_counter()
    first = None
    parts = []
    with client.stream(
        "POST", f"{LLM}/chat/completions", timeout=120,
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "用一句话回答：你好，请介绍一下你们的产品。"}],
            "stream": True, "max_tokens": 160,
        },
    ) as resp:
        for line in resp.iter_lines():
            if not line.strip() or line.startswith(":") or not line.startswith("data:"):
                continue
            import json

            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            c = (obj.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
            if c:
                if first is None:
                    first = time.perf_counter()
                parts.append(c)
    print(f"LLM 回复               : 首token {ms(first)}  完整 {ms(t0)}  ({len(''.join(parts))} 字)")


def measure_translate(client: httpx.Client) -> None:
    t0 = time.perf_counter()
    r = client.post(
        f"{LLM}/chat/completions", timeout=120,
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": "Translate to English. Output ONLY the translation."},
                {"role": "user", "content": "Source language: Chinese (Simplified)\nTarget language: English\nText: 你好，我想了解一下你们的产品。"},
            ],
            "stream": False, "max_tokens": 160,
        },
    ).json()
    print(f"翻译(一句)             : {ms(t0)}   out={r.get('choices',[{}])[0].get('message',{}).get('content','')[:30]!r}")


def measure_tts(client: httpx.Client, text: str, lang: str, label: str) -> None:
    t0 = time.perf_counter()
    first_byte = None
    total = 0
    with client.stream(
        "POST", f"{TTS}/v1/audio/speech", timeout=180,
        json={"input": text, "language": lang, "sample_rate": 24000, "streaming": True, "chunk_ms": 200},
    ) as r:
        r.raise_for_status()
        for chunk in r.iter_bytes():
            if first_byte is None:
                first_byte = time.perf_counter()
            total += len(chunk)
    dur = total / 2 / 24000
    print(f"TTS 合成({label})       : 完成 {ms(t0)}  首包 {ms(first_byte)}  ({dur:.1f}s 音频, 流式模式)")


def main() -> None:
    with httpx.Client(timeout=180) as client:
        print("=== 单阶段延迟（当前机器，warm 状态）===")
        measure_asr(client)
        measure_llm(client)
        measure_translate(client)
        measure_tts(client, "你好，欢迎致电博克，请问有什么可以帮您？", "zh", "一句 17 字")
        measure_tts(client, "唔該晒你今日嘅時間，我哋會跟進。", "cantonese", "粤语 15 字")
        print("\n说明：VAD 判定说完=0.45s + endpointing min=0.35s（max 1.2s 自适应）；")
        print("A线端到端≈VAD+ASR+LLM首句+TTS首包。注：VAD/endpointing 不能为追低延迟收太紧——")
        print("离线式 ASR 转写需 ~0.5-1.2s，端点判定要等得起它，否则「转写晚于轮次提交」回复被丢。")


if __name__ == "__main__":
    main()
