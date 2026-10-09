"""时域变速（OLA/SOLA 保音高时长压缩）单源。

2026-10-09 自 ``scripts/probes/probe_fast_speech.py`` 上收共享包（W8-B interp_lite
auto_tempo 复用同一实现——A 线探针与 B 线追播共用一份算法，禁止第二份拷贝）：
常速率 s16le mono PCM 上的 OLA/SOLA 时域叠加。窗口 ~60ms（宽度随 ``sample_rate``
缩放，16k 时 960 样本与探针旧常量逐字节一致），合成步 ``hop_out = window//4``，
分析步 ``hop_in = round(hop_out * factor)``（factor>1 → 输入推进更快 → 时长压缩）；
每帧在 ±hop_out//2 内用归一化互相关找与已写输出的最佳相位对齐偏移（SOLA）后
Hann 窗交叠相加并按窗能量归一（保 COLA 增益）。factor≈1 逐字节原样返回；
相位不保真可接受——语音 1.0-1.5 档听感自然（探针实弹验证过 1.4 档）。

``numpy`` 惰性导入（函数体内 import）——本包其余模块零三方依赖的纪律不破，
无 numpy 环境 import 本模块不炸（调用时才要求）。
"""

from __future__ import annotations


def _best_ola_shift(x, nominal: int, out, out_start: int, corr_len: int, search: int) -> int:
    """在 ±search 内找使 x[nominal+s] 与已写输出最对齐的 s（归一化互相关）。"""
    import numpy as np

    seg_out = out[out_start:out_start + corr_len]
    if seg_out.size < corr_len:
        seg_out = np.concatenate(
            [seg_out, np.zeros(corr_len - seg_out.size, dtype=np.float64)]
        )
    ob = seg_out - seg_out.mean()
    onorm = float(np.sqrt(np.dot(ob, ob))) + 1e-9
    best_s, best_v = 0, -2.0
    n = int(x.size)
    for s in range(-search, search + 1):
        a = nominal + s
        if a < 0 or a + corr_len > n:
            continue
        seg = x[a:a + corr_len]
        ab = seg - seg.mean()
        anorm = float(np.sqrt(np.dot(ab, ab))) + 1e-9
        v = float(np.dot(ab, ob)) / (anorm * onorm)
        if v > best_v:
            best_v, best_s = v, s
    return best_s


def speedup_pcm(pcm: bytes, factor: float, sample_rate: int = 16000) -> bytes:
    """保音高时长压缩（时间尺度拉伸/压缩，音调不变）。

    factor>1=压短（更快）；``sample_rate`` 决定窗口宽度（60ms 档，16k=960 样本
    与探针历史常量一致，24k=1440）。实现=OLA/SOLA 时域叠加（算法细节见模块
    docstring）；factor≈1 或空输入原样/空返回。
    """
    import numpy as np

    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    n = int(x.size)
    if n == 0 or factor <= 0:
        return b""
    w = int(round(sample_rate * 0.06))  # 60ms 窗（16k=960：探针历史常量不变）
    hop_out = w // 4      # 合成步（输出每帧前进）
    hop_in = max(1, int(round(hop_out * factor)))  # 分析步（输入每帧前进）
    if hop_in == hop_out:
        return bytes(pcm)
    n_frames = int(np.ceil(max(0, n - w) / hop_in)) + 1 if n > w else 1
    out_len = (n_frames - 1) * hop_out + w
    corr_len = min(hop_out, w)  # 新帧头与已写输出重叠的相关窗
    search = hop_out // 2       # 对齐搜索半径

    win = np.hanning(w)
    out = np.zeros(out_len, dtype=np.float64)
    wsum = np.zeros(out_len, dtype=np.float64)

    for k in range(n_frames):
        nominal = k * hop_in
        out_start = k * hop_out
        if k == 0 or nominal + corr_len > n:
            shift = 0
        else:
            shift = _best_ola_shift(x, nominal, out, out_start, corr_len, search)
        a = nominal + shift
        if a < 0:
            a = 0
        seg = x[a:a + w]
        if seg.size < w:  # 尾帧补齐零
            seg = np.concatenate([seg, np.zeros(w - seg.size, dtype=np.float64)])
        end = min(out_start + w, out_len)
        take = end - out_start
        out[out_start:end] += seg[:take] * win[:take]
        wsum[out_start:end] += win[:take]

    wsum = np.maximum(wsum, 1e-3)
    out = out / wsum
    out = np.clip(np.rint(out), -32768, 32767).astype(np.int16)
    return out.tobytes()
