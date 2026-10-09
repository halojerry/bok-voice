"""B 线 vad-pause 提交字数门槛实弹 A/B 探针（2026-10-06 延迟压刀遗留真杠杆评估）。

刺激形状（标准延迟探针造不出的「子句+真停顿」）：子句 A「你好呀我想问一下」
（8 字）+ 0.7s 真静音（≥ VAD min_silence → END_OF_SPEECH = vad-pause 提交判定
点）+ 子句 B「你们这个集运怎么收费的」（11 字）+ 尾静音，一次推流。被测档位
来自 serve env（QWEN3_ASR_PAUSE_COMMIT_MIN_CHARS，A/B 两臂各 serve 一次）——
本探针只测量不设阈值，--threshold-env 仅在判定行记录台账。

量法（主判据=译文首声对停顿起点的 lag；三层证据）：
  ① segs = 本通 CP「译文：」轮数（与 worker INTERP_LAG 行 1:1——每条 ledger
    配对落一条译文 turn；日志窗口另抓 SENTENCE_COMMIT/INTERP_LAG/BACKLOG 原行，
    source=vad-pause|partial-len|partial-punct 分辨哪条事件源抢到边界）；
  ② first_lag_ms = 译文首声 − pause_start（push(A) 返回时刻；实时推流口径下
    即 A 尾离嘴，推流前摄突发会令锚偏早、lag 偏保守——两臂同偏，A/B 公平）。
    ≤2000ms = clause-chase 档（~1.2s class），否则 sentence-end 档（~3.5s class）；
  ③ chase_b = 译文首声早于 B 讲完 = A 的译文在 B 说完前已出声（真同传语义）。

用法（前置 BOK_LOCAL_TTS=1 serve，刺激源走 :8788 本地 TTS）：
  .venv312/bin/python scripts/probes/probe_interp_pause_commit.py --threshold-env 10
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
import random
import struct
import sys
import time

import e2e_interpret as e2e  # noqa: E402  (建单/token/hangup/turns 常量与回读)
import probe_interpret_latency as base  # noqa: E402  (TimedSide + 语音起点检测)
import probe_interp_continuous as cont  # noqa: E402  (strip_silence)
from e2e_real_customer import _default_log_dir  # noqa: E402  (worker 日志平台路径)
from probe_stimulus import stimulus_pcm  # noqa: E402  (客户话音合成单点)

CLAUSE_A = "你好呀我想问一下"  # 8 字——门槛 8 的正靶（10 时够不着 vad-pause 门）
CLAUSE_B = "你们这个集运怎么收费的"  # 11 字——停顿后续讲
PAUSE_S = 0.7  # ≥ VAD min_silence → 停顿即 END_OF_SPEECH（vad-pause 判定点）
TRAIL_S = 0.6  # 尾静音：让 B 也有干净停嘴兜底
CHASE_MS = 2000  # class 分界：≤2s=clause-chase，>2s=sentence-end


def _silence(seconds: float) -> bytes:
    """抖动静音（±250 噪声，固定种子可复现）：纯零静音在 rtc 信道被当 DTX 缺
    帧，VAD「静音不可见」→ B 的 START 迟到、句头被吃（2026-10-02 duplex 探针
    同款修法：±500 抖动静音）。"""
    rng = random.SystemRandom()  # 抖动静音（非加密语境，SystemRandom 系 Mimosa 弱随机清零）
    n = int(16000 * seconds)
    return struct.pack(f"<{n}h", *(rng.randint(-250, 250) for _ in range(n)))


async def main() -> int:
    thr = sys.argv[sys.argv.index("--threshold-env") + 1] if "--threshold-env" in sys.argv else ""
    pcm_a = cont.strip_silence(stimulus_pcm(CLAUSE_A, "zh"))
    pcm_b = cont.strip_silence(stimulus_pcm(CLAUSE_B, "zh"))
    a_s, b_s = len(pcm_a) / 32000, len(pcm_b) / 32000

    fwd_log = _default_log_dir() / "interp-fwd.log"
    mark = fwd_log.stat().st_size if fwd_log.exists() else 0

    created = e2e.httpx.post(
        f"{e2e.CONTROL_PLANE_URL}/api/calls",
        json={"account_id": "acc-001", "kind": "interpret", "mode": "live", "direction": "interpret",
              "language": "zh", "target_lang": "en", "object_id": "", "glossary": ""},
        headers=e2e._CP_HEADERS,
        timeout=15,
    ).json()
    call_id = created["id"]
    me = base.TimedSide(call_id, f"me-{call_id}")
    other = base.TimedSide(call_id, f"other-{call_id}")
    await me.connect()
    await other.connect()
    await asyncio.sleep(6)  # 等 RoomAgentDispatch 拉起解释器

    onset = None
    try:
        await me.push(pcm_a)
        pause_start = time.monotonic()
        b_done = pause_start + PAUSE_S + b_s  # 实时推流口径下 B 的讲完时刻
        await asyncio.sleep(PAUSE_S)
        await me.push(pcm_b + _silence(TRAIL_S))
        # 等译文音频收敛：captured 4s 无增长或总窗 60s（与 base 探针同款）
        deadline = time.monotonic() + 60
        last_len, last_change = len(other.captured), time.monotonic()
        while time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            if len(other.captured) != last_len:
                last_len, last_change = len(other.captured), time.monotonic()
            elif time.monotonic() - last_change > 4:
                break
        onset = base.speech_onset_after(other.timelog, other.captured, pause_start)
    finally:
        await me.close()
        await other.close()
        try:
            e2e.httpx.post(f"{e2e.CONTROL_PLANE_URL}/api/calls/{call_id}/hangup",
                           headers=e2e._CP_HEADERS, timeout=10)
        except Exception:
            pass

    await asyncio.sleep(2)  # 落库账本 spawn 任务收尾，防译文行漏读
    segs, perceived, readback = 0, [], ""
    try:
        turns = e2e.httpx.get(f"{e2e.CONTROL_PLANE_URL}/api/calls/{call_id}/turns",
                              headers=e2e._CP_HEADERS, timeout=10).json()
        tran = [t for t in turns if str(t.get("transcript") or "").startswith("译文：")]
        segs = len(tran)
        perceived = [t.get("perceived_ms") for t in tran if t.get("perceived_ms")]
    except Exception:
        pass
    if onset is not None:
        lo = min((s for t, s, _ in other.timelog if t >= onset - 0.1), default=None)
        if lo is not None:
            _, readback = e2e.asr_transcribe(bytes(other.captured[lo:]))

    window = b""
    try:
        window = fwd_log.read_bytes()[mark:]
    except Exception:
        pass
    _keep = (b"QWEN3_ASR_SENTENCE_COMMIT", b"INTERP_LAG", b"BACKLOG")
    log_lines = [ln.decode("utf-8", "replace").strip() for ln in window.splitlines() if any(k in ln for k in _keep)]

    first_lag_ms = None if onset is None else round((onset - pause_start) * 1000)
    klass = "-" if first_lag_ms is None else ("clause-chase" if first_lag_ms <= CHASE_MS else "sentence-end")
    chase = onset is not None and onset < b_done
    print(f"call={call_id} a_s={a_s:.2f} b_s={b_s:.2f} segs={segs} perceived_ms={perceived}", flush=True)
    print(f"readback={readback[:80] or '-'}", flush=True)
    for ln in log_lines[:12]:
        print(f"  log| {ln}", flush=True)
    print(
        f"PAUSE_COMMIT_EVAL threshold={thr or 'serve-env'} segs={segs} "
        f"first_lag_ms={'-' if first_lag_ms is None else first_lag_ms} (anchor=pause_start) "
        f"budget_class={klass} chase_b={'yes' if chase else 'no'} "
        f"{'PASS' if segs >= 1 and first_lag_ms is not None else 'FAIL'}",
        flush=True,
    )
    return 0 if segs >= 1 and first_lag_ms is not None else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
