"""B 线全双工争用探针（2026-09-16）——两向同时说话，量双向延迟与零丢句。

全双工形态（虚拟声卡/音频线物理隔离 + 关自动半双工）下，fwd 与 rev 两个
worker 同时收音、同时翻译——两路的 ASR(:8787)/MT(:1236) 共享同一块 GPU，
请求可能排队。本探针量化争用代价：

  - fwd lag：me 推 zh 两段，other 侧 trans-en 音轨语音起点 − 源段结束
    （与 probe_interpret_latency 同口径；单路无争用基线 ~2.5-2.7s）
  - rev 落库延迟：other 同时推 en，me 侧译文行（纯字幕）出现在 turns 的时刻
    − 源段结束（轮询粒度 0.3s，数值偏保守）
  - 零丢句：双向原文/译文行数齐（manual 管线零整轮丢弃的并发回归）

用法:
  .venv312/bin/python scripts/probe_interp_duplex.py
env:
  BOK_PROBE_LAG_BUDGET_MS   fwd 逐句/平均 lag 预算(默认 3500)
  BOK_PROBE_REV_BUDGET_MS   rev 落库延迟预算(默认 6000;轮询粒度粗,给足余量)
前置:python tools/bok.py serve(含 interp-fwd/rev 与 MT :1236)。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import e2e_interpret as e2e  # noqa: E402
from probe_interpret_latency import TimedSide, speech_onset_after  # noqa: E402

# auth-on 栈(2026-09-15 标准姿势)要求 CP 请求带机器通道 token——E2E 建单/取
# token/收线/读 turns 全是机器语义,Bearer BOK_CP_TOKEN 直通(与 agent worker 同源)。
# 未设 env(老 auth-off 栈)零变化。CP 之外(asr sidecar)不带。
_CP_HEADERS: dict[str, str] = {}
if os.environ.get("BOK_CP_TOKEN", "").strip():
    _CP_HEADERS["Authorization"] = f"Bearer {os.environ['BOK_CP_TOKEN'].strip()}"

FWD_SEG_S = 4.0
GAP_S = 0.7
REV_SEG_S = 4.5
REV_GAP_S = 0.5


async def _rev_feeder(other: TimedSide, pcm_rev: bytes) -> None:
    """对方侧持续说话(与 fwd 完全重叠):3 段 en,段间 0.5s。"""
    for i in range(3):
        await other.push(pcm_rev)
        if i < 2:
            await asyncio.sleep(REV_GAP_S)


async def main() -> int:
    lag_budget = int(os.environ.get("BOK_PROBE_LAG_BUDGET_MS", "3500"))
    rev_budget = int(os.environ.get("BOK_PROBE_REV_BUDGET_MS", "6000"))

    pcm_fwd = e2e.read_wav_pcm(e2e.AUDIO_DIR / "zh.wav")[: int(16000 * FWD_SEG_S * 2) * 2]
    pcm_rev = e2e.read_wav_pcm(e2e.AUDIO_DIR / "en.wav")[: int(16000 * REV_SEG_S * 2) * 2]

    created = e2e.httpx.post(
        f"{e2e.CONTROL_PLANE_URL}/api/calls",
        json={"account_id": "acc-001", "kind": "interpret", "mode": "live", "direction": "interpret",
              "language": "zh", "target_lang": "en", "object_id": ""},
        headers=_CP_HEADERS,
        timeout=15,
    ).json()
    call_id = created["id"]
    me = TimedSide(call_id, f"me-{call_id}")
    other = TimedSide(call_id, f"other-{call_id}")
    await me.connect()
    await other.connect()
    await asyncio.sleep(6)  # 等 RoomAgentDispatch 拉起 fwd/rev 解释器

    segs: list[dict] = []
    try:
        # 两向同时开跑:gather 保证 fwd 推流与 rev 持续说话完全重叠。
        async def fwd_speak() -> None:
            for i in range(2):
                t0 = time.monotonic()
                await me.push(pcm_fwd)
                segs.append({"src_end": t0 + FWD_SEG_S, "onset": None})
                if i == 0:
                    await asyncio.sleep(GAP_S)

        await asyncio.gather(fwd_speak(), _rev_feeder(other, pcm_rev))

        # 等两向都收敛:fwd 音频 4s 无增长
        deadline = time.monotonic() + 60
        last_len, last_change = len(other.captured), time.monotonic()
        while time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            if len(other.captured) != last_len:
                last_len, last_change = len(other.captured), time.monotonic()
            elif time.monotonic() - last_change > 4:
                break
        for seg in segs:
            seg["onset"] = speech_onset_after(other.timelog, other.captured, seg["src_end"])
    finally:
        await me.close()
        await other.close()
        try:
            e2e.httpx.post(f"{e2e.CONTROL_PLANE_URL}/api/calls/{call_id}/hangup", headers=_CP_HEADERS, timeout=10)
        except Exception:
            pass

    rows = e2e.httpx.get(f"{e2e.CONTROL_PLANE_URL}/api/calls/{call_id}/turns", headers=_CP_HEADERS, timeout=10).json()

    def _row_epoch(r: dict) -> float:
        # CP created_at 是 UTC naive ISO(实证:与 utcnow 同域)→ 补 UTC 归一 epoch。
        return datetime.fromisoformat(str(r.get("created_at"))).replace(tzinfo=timezone.utc).timestamp()

    fwd_orig = sum(1 for r in rows if str(r.get("transcript") or "").startswith("原文：") and (r.get("language") or "") == "zh")
    rev_src_rows = sorted(
        (r for r in rows if str(r.get("transcript") or "").startswith("原文：") and (r.get("language") or "") == "en"),
        key=_row_epoch,
    )
    rev_orig = len(rev_src_rows)
    fwd_tran = sum(1 for r in rows if str(r.get("transcript") or "").startswith("译文：") and (r.get("language") or "") == "en")
    rev_tran_rows = sorted(
        (r for r in rows if str(r.get("transcript") or "").startswith("译文：") and (r.get("language") or "") == "zh"),
        key=_row_epoch,
    )

    lags = [round((s["onset"] - s["src_end"]) * 1000) for s in segs if s["onset"] is not None]
    # rev 生成耗时:rev 原文行(STT 提交)→ 对应译文行的 created_at 差(按序配对)。
    # 首跑教训①:轮询计数迟到一次性计数产出假 60s+——created_at 才是可信时刻源;
    # 教训②:译文行在推流中途(子句提交)就落库,早于「推完」锚,负差全被滤掉——
    # 原文行→译文行的配对差才是纯生成耗时,与推流节奏无关。
    rev_lags = [
        round((_row_epoch(t) - _row_epoch(s)) * 1000)
        for s, t in zip(rev_src_rows, rev_tran_rows)
        if _row_epoch(t) >= _row_epoch(s)
    ]
    avg = round(sum(lags) / len(lags)) if lags else -1
    rev_avg = round(sum(rev_lags) / len(rev_lags)) if rev_lags else -1

    ok = (
        len(lags) == len(segs)
        and all(l <= lag_budget for l in lags)
        and rev_lags
        and rev_avg <= rev_budget
        and fwd_orig >= 2 and fwd_tran >= 2 and rev_orig >= 3 and len(rev_tran_rows) >= 3
    )
    print(
        f"INTERPRET_DUPLEX_PROBE fwd_lag_ms={lags} fwd_avg_ms={avg} fwd_budget={lag_budget} | "
        f"rev_lag_ms={rev_lags} rev_avg_ms={rev_avg} rev_budget={rev_budget} | "
        f"fwd(orig={fwd_orig},tran={fwd_tran}) rev(orig={rev_orig},tran={len(rev_tran_rows)}) "
        f"{'PASS' if ok else 'FAIL'}",
        flush=True,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
