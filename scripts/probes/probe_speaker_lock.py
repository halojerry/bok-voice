# --- scripts import bootstrap (G1) ---
"""W5 声纹锁真实语音阈值实弹探针（2026-10-07 接线波；离线零网络零栈依赖）。

素材=macOS ``say`` 多音色真渲染（Meijia zh_TW / Eddy zh_CN / Daniel en_GB /
Kyoko ja_JP 四把基频×共振峰实质不同的嗓音）× 环境音混合臂（office.wav 底噪
@ SNR 12/6/0dB）。回答一个问题：**0.55/0.40 双阈 + 1.5s 判定窗在真语音上还够不够分**。

四臂：
  A 同人净音频：enroll utt0 → admit utt1..3（同人不同文本）→ 期望全 hit；
  B 同人+底噪：utt1 与 office 环境音按 SNR 12/6/0dB 混 → 期望仍 hit
    （**误杀通话对象=最坏结局**：0 false-drop 是硬判据）；
  C 异人全矩阵：informational——**v1 嵌入已实证不分人**（真嗓音异人
    0.816-0.973 与同人带噪重叠，见模块 docstring 勘误），只报分布不判 PASS；
  D 纯环境音段（无语音）→ 期望全部 < drop_sim（v1 的真实能力面：环境音滤除）。
另跑一段 SegmentSpeakerGate 端到端（enroll→hit(带噪同人)→drop(纯环境音段)）。

PASS 判据：A+B 零 false-drop **且** D 纯环境音全判丢 **且** E 端到端 ok；
任一不满足 exit 1。输出：stdout 表 + ``reports/speaker-lock-probe/result.json``
（reports/ 已 gitignore）。

边界（明账）：``say`` 合成嗓音≠真人录音——谱形分布可能偏乐观/偏悲观各半；
本探针给的是阈值余量证据，不是野外误杀率。实弹观测行=
``SPEAKER_LOCK_DROP/ENROLL/RELOCK``。仅 macOS（say/afconvert）。
"""

from __future__ import annotations
# --- scripts import bootstrap (G1) ---
# sys.path 引导(G1 迁移解耦,见 docs/superpowers/plans/2026-10-04-repo-governance-plan.md §3.1):
# 同层时是 no-op;文件挪进任何桶后裸 import 兄弟模块继续解析。
import sys as _sys, pathlib as _pathlib  # noqa: E401
_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))

import argparse
import json
import os
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.speaker_lock import (  # noqa: E402
    DEFAULT_DROP_SIM,
    SegmentSpeakerGate,
    SpeakerLock,
    cosine,
    embed_pcm,
)

# 探针自带总闸（生产=worker env；离线探针缺省开才能测门本身）。
os.environ.setdefault("BOK_SPEAKER_LOCK", "1")

SR = 16000
REPORTS_DIR = ROOT / "reports" / "speaker-lock-probe"

# 四把嗓音×四句文本（每句 2-4s；文本差异=同人异文本相似度的判定材料）。
VOICE_UTTS = {
    "meijia": (
        "Meijia",
        [
            "你好，我叫美佳，请问有什么可以帮到你的吗？",
            "我想查一下我上个月的那张订单，单号还没有收到。",
            "麻烦你稍等一下，我帮你核对一下资料再回复你。",
            "好的，那我们今天就先这样，祝你生活愉快，再见。",
        ],
    ),
    "eddy": (
        "Eddy (中文（中国大陆）)",
        [
            "您好，这里是客服中心，请问您找哪一位？",
            "我这个事情已经反映了三次了，到现在都没有人处理。",
            "你们这个平台到底靠不靠谱，我怎么听说要跑路了。",
            "行，那我把我的手机号码再报一次给你记录一下。",
        ],
    ),
    "daniel": (
        "Daniel",
        [
            "Hello, this is Daniel from the support team, how can I help?",
            "I have been waiting for my refund for almost two weeks now.",
            "Could you please check the status of my latest order?",
            "Alright, thank you very much for your help today, goodbye.",
        ],
    ),
    "kyoko": (
        "Kyoko",
        [
            "こんにちは、カスタマーサポートの京子でございます。",
            "先月のご注文の件についてお問い合わせですね。",
            "少々お待ちください、確認いたしますので。",
            "それでは本日はご連絡ありがとうございました。",
        ],
    ),
}
SNRS_DB = (12.0, 6.0, 0.0)


def render_pcm(voice: str, text: str, out_dir: Path, tag: str) -> bytes:
    """``say`` 渲染 → afconvert 16k mono s16le PCM（缓存到 reports 目录幂等复用）。"""
    aiff = out_dir / f"{tag}.aiff"
    wav = out_dir / f"{tag}.wav"
    if not wav.exists():
        subprocess.run(
            ["say", "-v", voice, "-o", str(aiff), text],
            check=True, capture_output=True, timeout=30,
        )
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)],
            check=True, capture_output=True, timeout=30,
        )
    with wave.open(str(wav)) as w:
        assert w.getframerate() == SR and w.getnchannels() == 1 and w.getsampwidth() == 2
        return w.readframes(w.getnframes())


def _pcm_to_f(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0


def _f_to_pcm(sig: np.ndarray) -> bytes:
    peak = float(np.abs(sig).max()) if len(sig) else 0.0
    scaled = sig / max(peak, 1e-9) * 0.7 * 32767.0
    return scaled.astype("<i2").tobytes()


def load_ambience(path: Path) -> np.ndarray:
    """环境音 wav（任意采样率 mono）→ 16k float。"""
    with wave.open(str(path)) as w:
        assert w.getnchannels() == 1
        raw = w.getframerate(), np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)
    sr, sig = raw
    if sr != SR:
        n = int(len(sig) * SR / sr)
        sig = np.interp(np.linspace(0.0, len(sig) - 1.0, n), np.arange(len(sig)), sig)
    return sig


def mix_at_snr(voice_pcm: bytes, amb: np.ndarray, snr_db: float, seed: int) -> bytes:
    """语音+环境音按 SNR 混合（RMS 域）：误杀风险臂的「车里/办公室里通话」形状。"""
    rng = np.random.default_rng(seed)
    v = _pcm_to_f(voice_pcm)
    if len(amb) <= len(v):
        amb = np.tile(amb, int(np.ceil(len(v) / len(amb))))
    start = int(rng.integers(0, len(amb) - len(v) + 1))
    a = amb[start : start + len(v)]
    v_rms = float(np.sqrt(np.mean(v**2))) or 1e-9
    a_rms = float(np.sqrt(np.mean(a**2))) or 1e-9
    gain = v_rms / a_rms / (10.0 ** (snr_db / 20.0))
    return _f_to_pcm(v + gain * a)


def sim_of(lock: SpeakerLock, pcm: bytes) -> float:
    return float(cosine(embed_pcm(pcm), lock._centroid))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    _amb_default = ROOT / "apps" / "agent" / "agent_runtime" / "assets" / "ambience" / "office.wav"
    ap.add_argument("--ambience", default=str(_amb_default))
    args = ap.parse_args()
    if sys.platform != "darwin":
        print("probe_speaker_lock: macOS only (say/afconvert)", file=sys.stderr)
        return 2
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[probe] drop_sim={DEFAULT_DROP_SIM} window=1.5s voices={list(VOICE_UTTS)} snrs={SNRS_DB}")
    pcms: dict[str, list[bytes]] = {}
    for key, (voice, texts) in VOICE_UTTS.items():
        pcms[key] = [render_pcm(voice, t, REPORTS_DIR, f"{key}_{i}") for i, t in enumerate(texts)]
        durs = [len(p) / (2 * SR) for p in pcms[key]]
        print(f"[probe] rendered {key:7s} dur_s={[f'{d:.1f}' for d in durs]}")

    amb = load_ambience(Path(args.ambience))
    results: dict = {"arms": {"same_clean": [], "same_noisy": [], "cross": [], "amb_only": []}}

    # ---- A 同人净音频 ----
    print("\n== A. 同人净音频（enroll utt0 → admit utt1..3；期望 hit）==")
    locks: dict[str, SpeakerLock] = {}
    for key, segs in pcms.items():
        lock = SpeakerLock()
        assert lock.enroll(segs[0]), key
        locks[key] = lock
        for i, seg in enumerate(segs[1:], 1):
            ok, reason = lock.admit(seg)
            sim = sim_of(lock, seg)
            results["arms"]["same_clean"].append({"voice": key, "utt": i, "sim": round(sim, 3), "reason": reason})
            print(f"  {key:7s} utt{i}  sim={sim:.3f}  {reason}")
    false_drop = sum(1 for r in results["arms"]["same_clean"] if r["reason"] == "drop")

    # ---- B 同人+底噪 ----
    print("\n== B. 同人+office 底噪 @SNR（期望仍 hit；false-drop=FAIL）==")
    for key, segs in pcms.items():
        for snr in SNRS_DB:
            mixed = mix_at_snr(segs[1], amb, snr, seed=1000 + int(snr))
            ok, reason = locks[key].admit(mixed)
            sim = sim_of(locks[key], mixed)
            results["arms"]["same_noisy"].append(
                {"voice": key, "snr_db": snr, "sim": round(sim, 3), "reason": reason}
            )
            print(f"  {key:7s} snr={snr:4.0f}dB  sim={sim:.3f}  {reason}")
    false_drop += sum(1 for r in results["arms"]["same_noisy"] if r["reason"] == "drop")

    # ---- C 异人全矩阵（informational：v1 已实证不分人）----
    print("\n== C. 异人全矩阵（informational——v1 嵌入不分人，只报分布）==")
    cross_total = cross_drop = 0
    keys = list(pcms)
    for anchor in keys:
        for other in keys:
            if other == anchor:
                continue
            for i, seg in enumerate(pcms[other]):
                ok, reason = locks[anchor].admit(seg)
                sim = sim_of(locks[anchor], seg)
                cross_total += 1
                cross_drop += 1 if reason == "drop" else 0
                results["arms"]["cross"].append(
                    {"anchor": anchor, "other": other, "utt": i, "sim": round(sim, 3), "reason": reason}
                )
                print(f"  {anchor:7s}×{other:7s} utt{i}  sim={sim:.3f}  {reason}")
    cross_rate = cross_drop / cross_total

    # ---- D 纯环境音（硬判据：v1 真实能力面=环境音滤除，须全判丢）----
    print("\n== D. 纯环境音段（期望全部 drop）==")
    rng = np.random.default_rng(7)
    amb_leak = 0
    for i in range(3):
        start = int(rng.integers(0, len(amb) - 2 * SR))
        seg = _f_to_pcm(amb[start : start + 2 * SR])
        ok, reason = locks["meijia"].admit(seg)
        sim = sim_of(locks["meijia"], seg)
        amb_leak += 0 if reason == "drop" else 1
        results["arms"]["amb_only"].append({"i": i, "sim": round(sim, 3), "reason": reason})
        print(f"  amb#{i}  sim={sim:.3f}  {reason}")

    # ---- 接线件端到端（enroll→hit(带噪同人)→drop(纯环境音段)）----
    print("\n== E. SegmentSpeakerGate 端到端 ==")
    gate = SegmentSpeakerGate(SpeakerLock())
    gate.segment_start()
    for j in range(0, len(pcms["meijia"][0]), 6400):
        gate.feed(pcms["meijia"][0][j : j + 6400])
    gate.segment_end("你好我叫美佳请问有什么可以帮到你")
    assert gate.lock.enrolled, "enroll failed"
    noisy = mix_at_snr(pcms["meijia"][2], amb, 6.0, seed=42)
    gate.segment_start()
    v_hit = [gate.feed(noisy[j : j + 6400]) for j in range(0, len(noisy), 6400)][-1]
    gate.segment_end("麻烦你稍等一下")
    amb_seg = _f_to_pcm(amb[16000 : 16000 + 2 * SR])
    gate.segment_start()
    v_drop = [gate.feed(amb_seg[j : j + 6400]) for j in range(0, len(amb_seg), 6400)][-1]
    gate.segment_end("")
    print(f"  同人带噪 6dB → {'放行' if v_hit else '误杀!'}；纯环境音段 → {'判丢' if not v_drop else '漏放!'}")
    gate_ok = v_hit is True and v_drop is False

    verdict = "PASS" if (false_drop == 0 and amb_leak == 0 and gate_ok) else "FAIL"
    summary = {
        "false_drop_same_voice": false_drop,
        "amb_leak": amb_leak,
        "cross_drop_rate_informational": round(cross_rate, 3),
        "gate_end_to_end_ok": gate_ok,
        "verdict": verdict,
    }
    (REPORTS_DIR / "result.json").write_text(
        json.dumps({"summary": summary, **results}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"\n[probe] {summary}  → {REPORTS_DIR / 'result.json'}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
