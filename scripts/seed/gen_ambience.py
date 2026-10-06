#!/usr/bin/env python3
"""W6 场景底噪资产生成器(2026-10-06 demo-quality-wave):确定性种子化合成三场景无缝循环 wav。

产出(与 fillers 同款「随源码分发资产」模式,改场景=改本脚本重跑一个 PR):
  apps/agent/agent_runtime/assets/ambience/{office,callcenter,car}.wav
  apps/agent/agent_runtime/assets/ambience/manifest.json
      字段 scene/file/loop_s/gain_db/license="procedural-generated (seeded)"。

规格:每场景 loop_s 秒、24kHz、mono、16-bit PCM wav,峰值归一 -28dBFS
(底噪是铺底不是前景;运行时再叠 manifest gain_db 缺省 -28dB 播放衰减)。
无缝循环=生成 loop_s+xfade_s 原料,头 X 秒做「尾→首」crossfade:
  out[:X] = raw[N:N+X]*(1-w) + raw[:X]*w   (w=等功率正弦窗)
拼接点 out[N-1]=raw[N-1] → out[0]=raw[N] 天然连续;跨缝事件尾巴经 (1-w)
自然淡出,无爆音。稳态底噪用 FFT 周期化频谱整形(整段缓冲周期信号,缝外
双保险);音调/LFO 全部整周期数(整数 cycle/loop 总长),幅度包络在缝上连续。

场景配方(全部 numpy 种子化,default_rng(scene_seed),同 seed 逐字节可复现):
  office:     褐噪声底(深 rumble)+ 宽带空调洗底 + 120Hz HVAC 嗡(+240/360 谐波)
              + 极低幅度慢 LFO 起伏;
  callcenter: office 式底 + 稀疏远场人声嗡爆发(带通噪声 × 语速幅度调制,
              模拟多人低语)+ 偶发轻键盘敲击(簇发短促高通点击);
  car:        低频隆隆(<120Hz 能量为主)+ 缓慢幅度 LFO + 轻微路噪嘶声。

用法:
  .venv312/bin/python scripts/seed/gen_ambience.py [--scenes office,car] [--out DIR]
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


import argparse
import json
import wave
from pathlib import Path

import numpy as np

# 运行时消费单点=apps/agent/agent_runtime/ambience.py(DEFAULT_GAIN_DB/SCENES);
# 本脚本保持零仓内 import(纯生成器),两处 -28.0 以注释对齐,test_ambience 钉一致性。
DEFAULT_GAIN_DB = -28.0
DEFAULT_OUT = (
    _pathlib.Path(__file__).resolve().parents[2]
    / "apps" / "agent" / "agent_runtime" / "assets" / "ambience"
)
LICENSE = "procedural-generated (seeded)"
# 每场景固定 seed(拍板日 2026-10-06 派生;换配方不改 seed——同底保谱形可 A/B)。
SCENE_SEEDS = {"office": 20261061, "callcenter": 20261062, "car": 20261063}
SCENE_ALL = ("office", "callcenter", "car")

SR = 24000
LOOP_S = 40.0
XFADE_S = 2.0
PEAK_DBFS = -28.0


# ---- 频谱整形基元(FFT 周期化:整段缓冲视为周期信号,天然无缝) ----


def _shape(x: np.ndarray, sr: int, fn) -> np.ndarray:
    """白噪声 → 频域整形 → RMS 归一(输出单位方差量级)。"""
    n = len(x)
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, d=1.0 / sr)
    f_safe = np.where(f > 0.0, f, 1.0)
    y = np.fft.irfft(spec * fn(f_safe), n)
    rms = float(np.sqrt(np.mean(y * y)))
    return y / max(rms, 1e-12)


def _lp(fc: float, order: float = 2.0):
    return lambda f: 1.0 / np.sqrt(1.0 + (f / fc) ** (2.0 * order))


def _hp(fc: float, order: float = 2.0):
    return lambda f: 1.0 / np.sqrt(1.0 + (fc / f) ** (2.0 * order))


def _bp(lo: float, hi: float, order: float = 2.0):
    return lambda f: _lp(hi, order)(f) * _hp(lo, order)(f)


def _tone(sr: int, n: int, total_s: float, freq_hz: float, phase: float = 0.0) -> np.ndarray:
    """整周期数正弦(freq 映射到最近的整数 cycle/total,缝上幅度连续)。"""
    k = max(1, round(freq_hz * total_s))
    t = np.arange(n) / n  # 归一相位基,整周期数 k → 周期边界连续
    return np.sin(2.0 * np.pi * k * t + phase)


def _lfo(n: int, total_s: float, cycles: int, depth: float, phase: float = 0.0) -> np.ndarray:
    t = np.arange(n) / n
    return 1.0 + depth * np.cos(2.0 * np.pi * cycles * t + phase)


def _env_ad(n: int, sr: int, attack_s: float, release_s: float) -> np.ndarray:
    """raise-cosine 攻降包络(平台段恒 1)。"""
    e = np.ones(n)
    na = min(n, int(attack_s * sr))
    nr = min(n, int(release_s * sr))
    if na > 0:
        e[:na] = 0.5 - 0.5 * np.cos(np.pi * np.arange(na) / na)
    if nr > 0:
        e[n - nr:] = 0.5 + 0.5 * np.cos(np.pi * np.arange(nr) / nr)
    return e


def _add(buf: np.ndarray, piece: np.ndarray, at: int) -> None:
    end = min(len(buf), at + len(piece))
    if at < end:
        buf[at:end] += piece[: end - at]


# ---- 场景配方(全部相对幅度;末级统一峰值归一) ----


def _office_base(rng: np.random.Generator, n: int, sr: int, total_s: float) -> tuple[np.ndarray, np.ndarray]:
    """office 底 = 深褐 rumble + 宽带空调洗底 + HVAC 嗡;返回 (bed, hum)。"""
    brown = _shape(rng.standard_normal(n), sr, _lp(150.0, 2.0)) * 0.85
    wash = _shape(rng.standard_normal(n), sr, _lp(600.0, 2.0)) * 1.0
    bed = brown + wash
    bed *= _lfo(n, total_s, cycles=3, depth=0.12, phase=rng.uniform(0, 2 * np.pi))
    hum = (
        _tone(sr, n, total_s, 120.0)
        + 0.45 * _tone(sr, n, total_s, 240.0, phase=rng.uniform(0, 2 * np.pi))
        + 0.18 * _tone(sr, n, total_s, 360.0, phase=rng.uniform(0, 2 * np.pi))
    ) * 0.06
    return bed, hum


def synth_office(rng: np.random.Generator, n: int, sr: int, total_s: float) -> np.ndarray:
    bed, hum = _office_base(rng, n, sr, total_s)
    return bed + hum


def synth_callcenter(rng: np.random.Generator, n: int, sr: int, total_s: float) -> np.ndarray:
    loop_s = total_s  # 事件只摆进原料首 loop_s 段(尾部留 crossfade 续料)
    bed, hum = _office_base(rng, n, sr, total_s)
    out = bed * 0.9 + hum * 0.6
    # 远场人声嗡:稀疏爆发,每发=带通噪声 × 语速(3-4Hz)幅度调制 × 攻降包络,
    # 中心频率/时长/幅度逐发抖动(多人低语不重样)。
    for _ in range(11):
        dur = rng.uniform(1.2, 2.8)
        m = int(dur * sr)
        at = int(rng.uniform(0.0, loop_s - 0.2) * sr)
        lo = rng.uniform(150.0, 240.0)
        hi = rng.uniform(650.0, 950.0)
        piece = _shape(rng.standard_normal(m), sr, _bp(lo, hi, 2.0))
        syll = 0.65 + 0.35 * np.abs(
            np.sin(2.0 * np.pi * rng.uniform(2.8, 4.2) * np.arange(m) / sr + rng.uniform(0, 2 * np.pi))
        )
        piece *= syll * _env_ad(m, sr, 0.35, 0.5) * rng.uniform(0.35, 0.8)
        _add(out, piece, at)
    # 轻键盘敲击:簇发(每簇 4-7 击,70-180ms 间距),单击=高通化的 4-8ms 突刺。
    for _ in range(7):
        at = int(rng.uniform(0.0, loop_s - 1.0) * sr)
        for _ in range(int(rng.integers(4, 8))):
            m = int(rng.uniform(0.004, 0.008) * sr)
            click = np.diff(rng.standard_normal(m + 1))  # 一阶差分=高通
            click *= np.exp(-np.arange(m) / (0.0015 * sr))  # 快衰减瞬态
            _add(out, click * rng.uniform(0.25, 0.55), at)
            at += int(rng.uniform(0.07, 0.18) * sr)
    return out


def synth_car(rng: np.random.Generator, n: int, sr: int, total_s: float) -> np.ndarray:
    rumble = _shape(rng.standard_normal(n), sr, _lp(110.0, 3.0))
    rumble *= _lfo(n, total_s, cycles=2, depth=0.22, phase=rng.uniform(0, 2 * np.pi))
    road = _shape(rng.standard_normal(n), sr, _bp(120.0, 900.0, 1.0)) * 0.35
    road *= _lfo(n, total_s, cycles=5, depth=0.15, phase=rng.uniform(0, 2 * np.pi))
    hiss = _shape(rng.standard_normal(n), sr, _hp(2000.0, 1.0)) * 0.10
    return rumble + road + hiss


SYNTH = {"office": synth_office, "callcenter": synth_callcenter, "car": synth_car}


def render_loop(scene: str, *, sr: int, loop_s: float, xfade_s: float, peak_dbfs: float) -> np.ndarray:
    """整场景 → 无缝循环 int16(头 X 秒尾→首等功率 crossfade + 峰值归一)。"""
    rng = np.random.default_rng(SCENE_SEEDS[scene])
    xfade_n = int(xfade_s * sr)
    n_total = int((loop_s + xfade_s) * sr)
    raw = SYNTH[scene](rng, n_total, sr, loop_s + xfade_s)
    n = n_total - xfade_n
    out = raw[:n].copy()
    # 尾→首 crossfade:out[0]=raw[n](天然续 raw[n-1]),w 0→1 淡入真头;跨缝
    # 事件的尾巴经 (1-w) 自然续出后淡出——拼接处零爆音。
    w = np.sin(np.linspace(0.0, np.pi / 2.0, xfade_n)).astype(np.float64)  # 等功率
    out[:xfade_n] = raw[n : n + xfade_n] * (1.0 - w) + raw[:xfade_n] * w
    peak = float(np.max(np.abs(out)))
    if peak > 0:
        out = out * (10.0 ** (peak_dbfs / 20.0)) / peak
    return np.clip(np.round(out * 32767.0), -32768, 32767).astype(np.int16)


def write_wav(path: Path, pcm: np.ndarray, sr: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scenes", default=",".join(SCENE_ALL), help="逗号分隔场景名(缺省全三档)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help=f"输出目录(缺省 {DEFAULT_OUT})")
    ap.add_argument("--sr", type=int, default=SR)
    ap.add_argument("--loop-s", type=float, default=LOOP_S)
    ap.add_argument("--xfade-s", type=float, default=XFADE_S)
    ap.add_argument("--peak-dbfs", type=float, default=PEAK_DBFS)
    args = ap.parse_args()

    scenes = [s.strip().lower() for s in str(args.scenes).split(",") if s.strip()]
    unknown = [s for s in scenes if s not in SYNTH]
    if unknown:
        ap.error(f"unknown scenes: {unknown} (known: {list(SCENE_ALL)})")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    entries = []
    for scene in scenes:
        pcm = render_loop(
            scene, sr=args.sr, loop_s=args.loop_s, xfade_s=args.xfade_s, peak_dbfs=args.peak_dbfs
        )
        path = out_dir / f"{scene}.wav"
        write_wav(path, pcm, args.sr)
        dur = len(pcm) / args.sr
        peak_db = 20.0 * np.log10(max(1, int(np.max(np.abs(pcm)))) / 32767.0)
        rms_db = 20.0 * np.log10(max(1e-9, float(np.sqrt(np.mean((pcm / 32767.0) ** 2)))))
        entries.append(
            {
                "scene": scene,
                "file": path.name,
                "loop_s": round(dur, 2),
                "gain_db": DEFAULT_GAIN_DB,
                "license": LICENSE,
            }
        )
        print(
            f"[gen_ambience] {scene}: {dur:.1f}s peak={peak_db:.1f}dBFS rms={rms_db:.1f}dBFS"
            f" size={path.stat().st_size / 1024:.0f}KB seed={SCENE_SEEDS[scene]}"
        )

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps({"scenes": entries}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[gen_ambience] manifest → {manifest_path} ({len(entries)} scenes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
