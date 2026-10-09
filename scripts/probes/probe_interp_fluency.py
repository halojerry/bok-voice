"""B 线流畅度探针（W6×interp-lite 合流验收仪器，2026-10-09）。

与既有两针的分工：
  - probe_interp_continuous：只验「边说边译硬定义」（首声早于讲完）——短句语料；
  - probe_interpret_latency：逐句带停顿的「说完→出声」口径（边说边译模式下
    语义错位，onset 锚在 src_end 之后而译声常已在播）；
  - 本针验 **W6 流畅度四判据**（docs/superpowers/plans/2026-10-09-bline-fluency.md
    §5 口径修正版——perceived 含整段播完与「单段 ≥4s」内部矛盾，故不设门只报告）：
      1. 段间天窗：译文轨静音 >800ms 计数 ≤1/句（主判据——「断断续续」的量化）；
      2. 单段出声时长 p50 ≥4s（碎片→长段：碎片档每段只 ~2s 声）；
      3. 首声 onset 早于**第一句讲完**（边说边译；句档语料首个提交单元=保险丝/
         句号,结构性在语流深处——绝对 onset 值仅报告,短句预算不适用）；
      4. 原文提交单元 p50 ≥15 字（句档生效的账本侧证据；碎片档 p50=10）。

语料=≥40 字长句 ×3（句内带逗号——逗号档若在必被切碎；句号收尾），butt-join
0.12s 连续推流。判据可 env 调（BOK_PROBE_GAP_MS/SEG_MIN_S）。

用法：
  .venv312/bin/python scripts/probes/probe_interp_fluency.py
前置：BOK_INTERP_LITE=1 python tools/bok.py serve（薄线句档）+ :8788 刺激源。
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


import asyncio
import contextlib
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "apps" / "agent"))

import httpx  # noqa: E402

import e2e_interpret as e2e  # noqa: E402
import probe_interp_continuous as cont  # noqa: E402  (tts_pcm/strip_silence/gap)
import probe_interpret_latency as base  # noqa: E402  (TimedSide/onset)

_CP_HEADERS: dict = {}
if os.environ.get("BOK_CP_TOKEN", "").strip():
    _CP_HEADERS["Authorization"] = f"Bearer {os.environ['BOK_CP_TOKEN'].strip()}"

# ≥40 字长句，句内带逗号（逗号档在则必碎），句号收尾——句档提交的靶形语料。
SENTENCES = [
    "我想请你们帮我查一下上周三在你们平台下的那笔订单，当时下单的时候页面显示四十八小时之内发货，但是到现在已经过去了五天，订单状态一直显示已经发货可是物流信息完全是空的。",
    "我去年十月份在你们店里买的那台咖啡机，用了不到三个月就开始漏水，我当时联系客服你们说给我安排师傅上门维修，结果等了两个星期都没有人来，现在机器彻底不能用了。",
    "麻烦帮我把我账户里剩的三百多块钱余额退回到原来的支付卡里面，我不打算继续用你们的服务了，请你们在三个工作日之内处理完成，并且把确认邮件发到我注册的邮箱。",
]


def _speech_timeline(timelog, captured: bytearray) -> list[tuple[float, bool]]:
    """timelog 逐条目转 (墙钟, 该条目是否语音)——RMS≥150 判据与 onset 同源。"""
    step = 320
    out: list[tuple[float, bool]] = []
    for t, start, end in timelog:
        chunk = bytes(captured[start:end])
        if not chunk:
            continue
        n_speech = sum(
            1 for i in range(0, len(chunk) - step + 1, step) if e2e.frame_rms(chunk[i : i + step]) >= 150
        )
        n_all = max(1, (len(chunk) - step) // step + 1)
        out.append((t, n_speech / n_all > 0.3))
    return out


def _segments(tl: list[tuple[float, bool]], gap_budget: float) -> tuple[list[float], list[tuple[float, float]]]:
    """语音时间线 → (段时长秒列表, [(天窗起点墙钟, 时长秒)] )。首前/尾后静音不计。"""
    segs: list[float] = []
    gaps: list[tuple[float, float]] = []
    run_start = None
    last_speech_end = None
    for t, is_speech in tl:
        if is_speech:
            if run_start is None:
                run_start = t
                if last_speech_end is not None and t - last_speech_end > gap_budget:
                    gaps.append((last_speech_end, t - last_speech_end))
            last_speech_end = t
        else:
            if run_start is not None and t - last_speech_end > gap_budget:
                segs.append(last_speech_end - run_start)
                run_start = None
    if run_start is not None and last_speech_end is not None:
        segs.append(last_speech_end - run_start)
    return segs, gaps


def _p50(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[len(s) // 2]


async def main() -> int:
    src_lang, tgt_lang = os.environ.get("BOK_PROBE_LANG_PAIR", "zh,en").split(",")
    seg_min_s = float(os.environ.get("BOK_PROBE_SEG_MIN_S", "4.0"))

    parts = [cont.strip_silence(cont.tts_pcm(s, src_lang)) for s in SENTENCES]
    gap = b"\x00\x00" * int(16000 * cont.JOIN_GAP_S)
    combined = gap.join(parts)
    audio_s = len(combined) / 16000 / 2
    print(f"fluency audio: {audio_s:.1f}s ({len(parts)} long sentences butt-joined)", flush=True)

    created = e2e.httpx.post(
        f"{e2e.CONTROL_PLANE_URL}/api/calls",
        json={"account_id": "acc-001", "kind": "interpret", "mode": "live", "direction": "interpret",
              "language": src_lang, "target_lang": tgt_lang, "object_id": "", "glossary": ""},
        headers=_CP_HEADERS,
        timeout=15,
    ).json()
    call_id = created["id"]
    me = base.TimedSide(call_id, f"me-{call_id}")
    other = base.TimedSide(call_id, f"other-{call_id}")
    await me.connect()
    await other.connect()
    await asyncio.sleep(6)

    onset: float | None = None
    try:
        t0 = time.monotonic()
        await me.push(combined)
        deadline = time.monotonic() + 75
        last_len, last_change = len(other.captured), time.monotonic()
        while time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            if len(other.captured) != last_len:
                last_len, last_change = len(other.captured), time.monotonic()
            elif time.monotonic() - last_change > 5:
                break
        onset = base.speech_onset_after(other.timelog, other.captured, t0)
    finally:
        await me.close()
        await other.close()
        with contextlib.suppress(Exception):
            e2e.httpx.post(f"{e2e.CONTROL_PLANE_URL}/api/calls/{call_id}/hangup", headers=_CP_HEADERS, timeout=10)

    tl = _speech_timeline(other.timelog, other.captured)
    gap_budget = float(os.environ.get("BOK_PROBE_GAP_MS", "800")) / 1000.0
    segs, gaps_all = _segments(tl, gap_budget)
    onset_ms = None if onset is None else round((onset - t0) * 1000)
    # 天窗只数「讲完之前」起点的窗（尾部收敛期噪声块/挂断杂音不算段间天窗）；
    # onset 判据=首声早于第一句讲完（句档语料首个提交单元=保险丝/句号,结构性
    # 在语流深处——短句语料校准的绝对 onset 预算在此不适用,绝对值仍报告）。
    src_end = t0 + audio_s / 0.8
    first_sent_end = t0 + (audio_s / len(SENTENCES)) / 0.8
    gaps = [(t, d) for (t, d) in gaps_all if t < src_end]
    # 单窗上限（防「≤N/句 计数」放过巨型天窗——首版实弹 26.3s 窗混过 PASS 的教训：
    # 句 2+3 并成一单 commit + 播报衔接饿窗；根因修在 provider 长度保险丝）。
    max_gap_s = float(os.environ.get("BOK_PROBE_MAX_GAP_S", "8"))

    src_lens: list[int] = []
    try:
        with httpx.Client(timeout=10) as client:
            turns = client.get(
                f"{e2e.CONTROL_PLANE_URL}/api/calls/{call_id}/turns", headers=_CP_HEADERS
            ).json()
        for t in turns:
            tr = str(t.get("transcript", ""))
            if tr.startswith("原文") and t.get("speaker") == "me":
                src_lens.append(len(tr.split("：", 1)[-1]))
    except Exception:  # noqa: BLE001 - turns 读失败不阻音频判据
        pass
    src_p50 = _p50([float(x) for x in src_lens])
    seg_p50 = _p50(segs)

    ok = (
        onset is not None
        and onset < first_sent_end  # 边说边译（首声早于第一句讲完）
        and len(gaps) <= len(SENTENCES)  # ≤1 天窗/句
        and all(d <= max_gap_s for _, d in gaps)  # 单窗上限
        and seg_p50 >= seg_min_s
        and src_p50 >= 15
    )
    print(
        f"INTERPRET_FLUENCY_PROBE audio_s={audio_s:.1f} "
        f"onset_ms={'-' if onset_ms is None else onset_ms} "
        f"first_sent_end_s={round((first_sent_end - t0), 1)} "
        f"segments={len(segs)} seg_p50_s={seg_p50:.1f} "
        f"gaps_gt800ms={len(gaps)} gap_list={[round(d, 2) for _, d in gaps]} "
        f"max_gap_budget_s={max_gap_s:g} "
        f"src_turns={len(src_lens)} src_p50_chars={src_p50:.0f} "
        f"{'PASS' if ok else 'FAIL'}",
        flush=True,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
