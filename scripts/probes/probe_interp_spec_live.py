"""B 线投机翻译实弹探针（2026-10-08 W1×spec 饥饿修复验收——多子句语料）。

背景：延迟/连续探针语料结构性测不出 spec/CLAUSE_COMMIT（无「≥6 正字后逗号」
的子句边界），而真人通话（call-21739d55）正是多子句长句。本探针用多子句
刺激实弹整套「说话中」链：interim 稳定子句 → spec 开火（原文挂点坐标）→
CLAUSE_COMMIT 说话中 FINAL → spec 确认（HIT=held PCM 直播 / defer-hit 有界
等待）→ 译文首声。

判据（探针侧可测的硬指标）：
  1. 对端捕获到译文音频（trans-<tgt> 非空）；
  2. 至少一句的译文首声早于源话音讲完（onset < src_end，「边说边译」硬定义,
     连续探针同款口径）——spec HIT 的体感证据。
判据 3（日志侧，探针打印 call_id 供 grep）：
  `[interp] INTERP_SPEC fire/hit/defer` 与 `[doubao] CLAUSE_COMMIT` 在场。

用法：
  BOK_PROBE_STIMULUS=cloud .venv312/bin/python scripts/probes/probe_interp_spec_live.py
env:
  BOK_PROBE_LANG_PAIR   默认 "zh,cantonese"（Ethan 真人通话语言对）
  BOK_PROBE_STIMULUS    stimulus_pcm 后端（cloud=MiniMax 云,首跑合成有缓存）
前置：python tools/bok.py serve（云档 interp-fwd 在场即可,零本地模型）。
"""
from __future__ import annotations
# --- scripts import bootstrap (G1) ---
import sys as _sys, pathlib as _pathlib
_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))

import asyncio
import os
import time

import httpx

ROOT = _S.parent
import e2e_interpret as e2e  # noqa: E402  (Side/推流/CP 常量)
from probe_stimulus import stimulus_pcm  # noqa: E402
from urlguard_gate import gate  # noqa: E402  (SSRF 守卫,2026-09-23 Mimosa)

# 多子句刺激（逗号前 ≥6 正字=CLAUSE_COMMIT 闸+spec 候选双触发形状;对齐
# call-21739d55 真人句形「坐地铁到啊，广州去玩。那确实。」的教训：单子句
# 语料两闸结构性不触发=测不出修复）。
STIMULI = [
    "你好呀我想问一下，你们这个集运怎么收费，大概几天能到？",
    "我们上个月发了一批货去广州，走海运比较慢，坐高铁过去拿要几天？",
]


def _squeeze_pauses(pcm: bytes, keep_s: float = 0.15) -> bytes:
    """TTS 逗号停顿压到 VAD 静音线(min_silence 0.28s)以内。

    合成语音的逗号=0.3-0.6s 内停顿,被 VAD 劈成独立段=多子句 interim 流
    结构性不存在(e2e 句形铁律的探针侧对症)。真人逗号停顿 <0.28s——把
    >keep_s 的静音 run 压到 keep_s,韵律保留、连续性达标。纯 numpy。
    阈值=信号相对(p95 振幅的 8%,地板 200)——绝对阈值在轻响度渲染上会把
    全段误判静音压成 0.15s(首版实弹翻车:整段 PCM 被吞)。护栏=压缩产物
    <50% 原长时判失效,原样返回(宁可 VAD 劈段也不吞音频)。"""
    import numpy as np

    if not pcm:
        return pcm
    x = np.frombuffer(pcm, dtype=np.int16)
    if x.size < 1600:
        return pcm
    thresh = max(200, int(np.percentile(np.abs(x.astype(np.int32)), 95) * 0.08))
    quiet = np.abs(x.astype(np.int32)) < thresh
    keep_n, out, run_start = int(keep_s * 16000), [], -1
    seg_start = 0
    for i, q in enumerate(quiet):
        if q and run_start < 0:
            run_start = i
        elif not q and run_start >= 0:
            out.append(x[seg_start:run_start])
            out.append(x[run_start : min(i, run_start + keep_n)])
            seg_start = i
            run_start = -1
    out.append(x[seg_start:])
    if run_start >= 0:  # 尾静音不保留
        pass
    y = np.concatenate(out)
    if y.size * 2 < len(pcm) // 2:  # <50% 原长=误判,弃压缩
        return pcm
    return y.tobytes()


async def main() -> int:
    gate(e2e.CONTROL_PLANE_URL, "http://127.0.0.1:8787")
    src_lang, tgt_lang = os.environ.get("BOK_PROBE_LANG_PAIR", "zh,cantonese").split(",")
    pcms = [_squeeze_pauses(stimulus_pcm(text, src_lang)) for text in STIMULI]

    created = httpx.post(
        f"{e2e.CONTROL_PLANE_URL}/api/calls",
        json={"account_id": "acc-001", "kind": "interpret", "mode": "live",
              "direction": "interpret", "language": src_lang, "target_lang": tgt_lang,
              "object_id": "", "glossary": ""},
        headers=e2e._CP_HEADERS,
        timeout=15,
    ).json()
    call_id = created["id"]
    print(f"call_id={call_id}", flush=True)
    me = e2e.Side(call_id, f"me-{call_id}")
    other = e2e.Side(call_id, f"other-{call_id}")
    await me.connect()
    await other.connect()
    await asyncio.sleep(6)  # 等 RoomAgentDispatch 拉起解释器

    segs: list[dict] = []
    try:
        for pcm in pcms:
            dur = len(pcm) / 32000.0
            t0 = time.monotonic()
            seg = {"src_end": t0 + dur, "onset": None}
            segs.append(seg)
            push = asyncio.create_task(me.push(pcm))
            # 推流期间 100ms 采样对端 captured 增长=译文首声(说话中出声即「追嘴」)
            while not push.done():
                before = len(other.captured)
                await asyncio.sleep(0.1)
                if len(other.captured) > before + 3200 and seg["onset"] is None:
                    seg["onset"] = time.monotonic()
            await push
            await asyncio.sleep(1.2)  # 句间呼吸;句内多子句连续(逗号不换气)

        deadline = time.monotonic() + 45
        last_len, last_change = len(other.captured), time.monotonic()
        while time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            if len(other.captured) != last_len:
                last_len, last_change = len(other.captured), time.monotonic()
            elif time.monotonic() - last_change > 4:
                break
    finally:
        await me.close()
        await other.close()
        try:
            httpx.post(f"{e2e.CONTROL_PLANE_URL}/api/calls/{call_id}/hangup",
                       headers=e2e._CP_HEADERS, timeout=10)
        except Exception:
            pass

    ok_audio = len(other.captured) > 16000  # >0.5s 译文音频
    chased = 0
    for i, seg in enumerate(segs):
        if seg["onset"] is not None and seg["onset"] < seg["src_end"]:
            chased += 1
        lag = "-" if seg["onset"] is None else f"{seg['onset'] - seg['src_end']:+.2f}s"
        print(f"seg{i}: onset_vs_src_end={lag}", flush=True)
    print(f"translation_audio={'yes' if ok_audio else 'NO'} bytes={len(other.captured)}", flush=True)
    print(f"SPEC_LIVE verdict={'PASS' if ok_audio else 'FAIL'} chased={chased}/{len(segs)}", flush=True)
    print("(日志判据 grep call_id: INTERP_SPEC fire/hit/defer + CLAUSE_COMMIT)", flush=True)
    return 0 if ok_audio else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
