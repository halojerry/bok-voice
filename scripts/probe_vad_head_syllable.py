#!/usr/bin/env python3
"""VAD START 前导帧并入会话缓冲 → 号码句头段多解一音（复现探针）。

用途（2026-09-15 T7 C4/C6 定责的决定性复现探针，实证 6/6 复现；热路径修复
的验收锚）：agent 插件把 VAD START 的前导帧（prefix_padding 0.5s +
min_speech 确认窗 0.15s）并入会话 `_pending` 作第一个 chunk，该块尾端切在
首音节中间，模型把半音节补成一个独立音节；增量 finish 沿用 partial 拼接，
把该头段原样带进 FINAL——裸号码句「六四三二零一一一」多解出「六。」头段
（digits 664320111，与外呼 E2E 曾捕获的 9 位号逐位一致）；带「喂」的句子
重复出来的是语气词（非数字），无恙。完整证据链见 scripts/e2e_campaign.py
头部「定责证据」注释。

机制锚（行号为约数、代码会漂，认代码不认行）：
  · apps/agent/agent_runtime/providers/livekit_plugins.py——START_OF_SPEECH
    把 event.frames（prefix padding+确认窗）合帧并进 `_pending`（约 :4580-4597）。
  · 同文件 `_maybe_partial`——≥300ms 节流 + 0.6s 长度门把 `_pending` POST 成
    chunk，partial/增量 finish 沿用 partial 拼接（约 :4812-4814）。

喂法逐帧复刻 `_Qwen3ASRLiveStream`：确认窗（0.5s+0.15s）攒够才 start 会话并
把前导并入 pending；此后每个 32ms INFERENCE_DONE 窗追加，post 门=距上次
≥300ms 且 pending ≥0.6s；停嘴后 hold 800ms 再把剩余 pending 作 /api/finish
的 body 一次性交上去。

判定：每轮 FINAL 经 `agent_runtime.flow._digit_normalize` 归一取位串，与脚本
号码 64320111 逐位比（原一次性版用 `ch.isdigit()` 直提，对中文数字恒空）。
6 轮（喂+号码 ×3 + 裸号码 ×3）全对 = CLEAN（热路径修复后应为 CLEAN 6/6，
退出码 0）；任一轮多出位串 = 缺陷复现（退出码非 0）。

用法：栈在跑（TTS :8788 / ASR :8787）时
  .venv312/bin/python scripts/probe_vad_head_syllable.py
URL 可 TTS_URL / QWEN3_ASR_BASE_URL 覆盖；音色可 BOK_MOCK_CUSTOMER_VOICE
覆盖（默认 Vivian=mock 客户同款）。探针不依赖 agent 栈本身（flow.
_digit_normalize 为纯函数、延迟 import），只需两个 sidecar 在跑。

同批另三支一次性探针（TTS 渲染无锅 p15_probe_wei / sidecar 流式无锅
p15_probe_stream / 前导形态 p15_probe_head）不提升进仓——结论已写在
scripts/e2e_campaign.py 头部定责注释与本机档案
.superpowers/sdd/2026-09-13-sip-edge-p15/task-7-report.md。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
TTS_URL = os.environ.get("TTS_URL", "http://127.0.0.1:8788")
ASR_URL = os.environ.get("QWEN3_ASR_BASE_URL", "http://127.0.0.1:8787")
from urlguard_gate import gate  # SSRF 守卫（2026-09-23，Mimosa）：云端测试设 BOK_PROBE_EXTRA_HOSTS

gate(TTS_URL, ASR_URL)
VOICE = os.environ.get("BOK_MOCK_CUSTOMER_VOICE", "Vivian")
SR = 16000
WIN_MS = 32  # VAD 推理窗
WA_NUMBER = "64320111"  # 与 scripts/e2e_campaign.py 同一号码
RUNS = 3  # 每形态轮数（T7 实证 6/6 复现的口径）


def _digitize(text: str) -> str:
    """中文数字归一成 ASCII 位串（六四三二…→6432…）。

    复用 flow._digit_normalize（纯函数、延迟 import——探针模块本身可被
    pytest/裸 python3 静态加载，不依赖 agent 栈依赖）。"""
    for rel in ("apps/agent", "packages/core"):
        path = str(ROOT / rel)
        if path not in sys.path:
            sys.path.insert(0, path)
    from agent_runtime.flow import _digit_normalize

    return "".join(ch for ch in _digit_normalize(str(text)) if ch.isdigit())


def synth(text: str, lang: str = "zh") -> bytes:
    # 客户话音单点开关（BOK_PROBE_STIMULUS，默认 local=逐字节不变；cloud 走 mm_pcm）。
    # 音色(VOICE)/采样率(SR)沿用脚本级常量；timeout=120 与原实现一致。
    from probe_stimulus import stimulus_pcm

    return stimulus_pcm(
        text, lang, voice=VOICE, tts_url=TTS_URL, sample_rate=SR, timeout=120.0
    )


def sil(sec: float) -> bytes:
    return b"\x00\x00" * int(SR * sec)


def run_once(client: httpx.Client, speech: bytes, *, label: str) -> tuple[str, str, list[str]]:
    """单轮复刻：返回 (final_text, digits, partials)。"""
    pad, speech_sil, tail_sil = sil(0.5), sil(0.45), sil(0.8)
    stream = pad + speech + speech_sil + tail_sil
    win_bytes = int(SR * WIN_MS / 1000) * 2
    sid = ""
    pending = bytearray()
    partials: list[str] = []
    last_post = 0.0
    conf = bytearray()

    for i in range(0, len(stream), win_bytes):
        w = stream[i:i + win_bytes]
        conf.extend(w)
        if not sid:
            # START：确认窗（0.5s padding+0.15s 确认）攒够→开会话并把前导并入 pending
            if len(conf) < int(SR * (0.5 + 0.15)) * 2:
                continue
            sid = client.post(f"{ASR_URL}/api/start",
                              params={"language": "Chinese",
                                      "context": "WhatsApp, 号码"}).json()["session_id"]
            pending.extend(bytes(conf))
            conf.clear()
        else:
            pending.extend(w)
        now = time.monotonic()
        if now - last_post >= 0.3 and len(pending) >= int(SR * 0.6) * 2:
            last_post = now
            pcm = bytes(pending)
            pending.clear()
            d = client.post(f"{ASR_URL}/api/chunk", params={"session_id": sid},
                            content=pcm).json()
            partials.append(str(d.get("text") or ""))

    # hold 800ms → flush：剩余 pending 作 finish body
    time.sleep(0.8)
    out = client.post(f"{ASR_URL}/api/finish", params={"session_id": sid},
                      content=bytes(pending)).json()
    text = str(out.get("text") or "")
    return text, _digitize(text), partials


def main() -> int:
    num = synth("六四三二零一一一")
    wei = synth("喂六四三二零一一一")
    results: list[tuple[str, bool]] = []
    with httpx.Client(timeout=180) as client:
        for label, speech in (("喂+号码", wei), ("裸号码", num)):
            for run in range(RUNS):
                text, digits, partials = run_once(client, speech,
                                                  label=f"{label} #{run+1}")
                ok = digits == WA_NUMBER
                results.append((f"{label} #{run+1}", ok))
                print(f"[{'PASS' if ok else 'FAIL'}] {label} #{run+1} "
                      f"final={text!r} digits={digits!r}"
                      f"{'' if ok else f'（期望 {WA_NUMBER}——头段多解，缺陷复现）'}\n"
                      f"    partials={partials}")
    bad = sum(0 if ok else 1 for _, ok in results)
    total = len(results)
    if bad:
        print(f"VAD_HEAD_SYLLABLE defect reproduced {bad}/{total}"
              f"（期望 CLEAN {total}/{total}）")
        return 1
    print(f"VAD_HEAD_SYLLABLE CLEAN {total}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
