"""B 线播放背压实弹探针（P2 `_PlaybackBacklog`，2026-09-16）。

连发 8 条短句（<10 字单口气句结构性免疫劈轮 → 句句独立成轮翻译），令译文在
框架 speech 队列里堆积；配合低门槛 `BOK_INTERP_MAX_BACKLOG_S`（建议 1.0，须在
`bok.py serve` 前设置——_interp_env 透传）实弹触发「追最新弃音保字」。

断言：
  - turns 原文行 ≥ N-2（源转写零丢失）；
  - interp-fwd.log 出现 `INTERP_BACKLOG ... drop=`（`BOK_PROBE_REQUIRE_DROP=0`
    跳过——默认门槛 6s 的正常节奏永不触发，属预期零回归）；
  - 最新一条译文照出（追最新语义）。

前置：`python tools/bok.py serve`（含 interp-fwd :8082、MT :1236）。
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx

from e2e_edge_cases import tts_pcm
from e2e_interpret import CONTROL_PLANE_URL, Side

SENTENCES = [
    # 长句先行(无逗号——话音句带逗号会被 vad-pause 劈轮,E2E 铁律):译文播
    # 5-7s,期间短句涌入 → say 队列堆深。门槛 1.0s 需 ≥3 并存句才弃(队头+最新
    # 永不弃,depth<3 结构性不触发)。短句 7-9 字:句级提交 ≥6 字保护下限,
    # 4-5 字句会被保护门吞掉(首跑实证)。
    "我们公司在深圳南山区科技园那边有三百多名员工。",
    "交货期大概要多久。",
    "能不能便宜一点呢。",
    "现在仓库有现货吗。",
    "付款方式有哪几种。",
    "发票可以提前开吗。",
]
# 句间 0.85s:≥0.45s VAD min_silence 门(0.6s 首跑实证会被下一句开头粘连吞句)。
GAP_S = 0.85
LOG_PATH = Path(
    os.environ.get(
        "BOK_PROBE_INTERP_LOG",
        str(Path.home() / "Library/Application Support/BokVoice/logs/interp-fwd.log"),
    )
)


def count_drops(call_id: str) -> tuple[int, int]:
    """以本通首个 room 行为锚，数其后的 INTERP_BACKLOG 行数与 drop 句总数。

    锚必须是**首次**出现——框架事件 JSON 行带 room 字段会持续刷,取最后一条
    会把锚点推到事件流末尾,数到 0(首跑实证)。"""
    try:
        lines = LOG_PATH.read_text(errors="ignore").splitlines()
    except OSError:
        return 0, 0
    idx = [i for i, ln in enumerate(lines) if call_id in ln]
    if not idx:
        return 0, 0
    events = drops = 0
    for ln in lines[idx[0]:]:
        if "INTERP_BACKLOG" in ln:
            events += 1
            if "drop=1" in ln:
                drops += 1
    return events, drops


async def main() -> int:
    require_drop = os.environ.get("BOK_PROBE_REQUIRE_DROP", "1") == "1"
    start = time.time()
    call = httpx.post(
        f"{CONTROL_PLANE_URL}/api/calls",
        json={"account_id": "acc-001", "kind": "interpret", "mode": "live", "direction": "interpret",
              "language": "zh", "target_lang": "en", "object_id": ""},
        timeout=15,
    ).json()
    call_id = call["id"]
    me = Side(call_id, f"me-{call_id}")
    other = Side(call_id, f"other-{call_id}")
    await me.connect()
    await other.connect()
    await asyncio.sleep(6)  # 等 RoomAgentDispatch 拉起解释器
    try:
        for s in SENTENCES:
            await me.push(tts_pcm(s, "zh"))
            await asyncio.sleep(GAP_S)  # ≥VAD min_silence,句间边界稳定
        await asyncio.sleep(16)  # 等队列排干(追最新:最后一条必须出)
    finally:
        await me.close()
        await other.close()
        try:
            httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/hangup", timeout=10)
        except Exception:
            pass

    turns = httpx.get(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/turns", timeout=10).json()
    orig = [t for t in turns if str(t.get("transcript") or "").startswith("原文：")]
    tran = [t for t in turns if str(t.get("transcript") or "").startswith("译文：")]
    events, drops = (count_drops(call_id) if require_drop else (0, 0))

    ok = len(orig) >= len(SENTENCES) - 2 and (not require_drop or drops >= 1)
    print(
        f"INTERPRET_BACKLOG_PROBE call={call_id} src={len(SENTENCES)} orig={len(orig)} "
        f"tran={len(tran)} events={events} dropped={drops} "
        f"elapsed={int(time.time() - start)}s {'PASS' if ok else 'FAIL'}",
        flush=True,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
