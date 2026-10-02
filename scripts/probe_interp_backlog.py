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


def _dither_silence_pcm(seconds: float, sr: int = 16000) -> bytes:
    """低幅噪声「静音」:±1 会被 Opus 编码器 VAD 判静音照样 DTX 丢弃(实测),
    ±500(≈-36dBFS 白噪,人耳轻嘶声)能过 DTX,而 silero(activation 0.75)
    仍判静音——间隙静音对 VAD 可见。"""
    import random  # 非加密:探针噪声种子固定系可复现要求(Mimosa 弱随机提示不适用)

    rng = random.Random(0x5EED)
    n = int(sr * seconds)
    return b"".join(rng.randrange(-500, 501).to_bytes(2, "little", signed=True) for _ in range(n))
from e2e_interpret import CONTROL_PLANE_URL, Side

# auth-on 栈(2026-09-15 标准姿势)要求 CP 请求带机器通道 token——E2E 建单/取
# token/收线/读 turns 全是机器语义,Bearer BOK_CP_TOKEN 直通(与 agent worker 同源)。
# 未设 env(老 auth-off 栈)零变化。CP 之外(asr sidecar)不带。
_CP_HEADERS: dict[str, str] = {}
if os.environ.get("BOK_CP_TOKEN", "").strip():
    _CP_HEADERS["Authorization"] = f"Bearer {os.environ['BOK_CP_TOKEN'].strip()}"

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


def count_drops(call_id: str) -> tuple[int, int, int]:
    """以本通首个 room 行为锚，数其后的背压事件与两类弃句。

    锚必须是**首次**出现——框架事件 JSON 行带 room 字段会持续刷,取最后一条
    会把锚点推到事件流末尾,数到 0(首跑实证)。

    fix round 1（评审 I-2）扩列：M-31 摘译路径日志是 `INTERP_BACKLOG source-skip
    xN`（无 `drop=` 字样），旧版只数 `drop=1` 行 → 摘译不进判据。现分开口径：
    - drops  = 译文弃句（门日志 `drop=1` 行数,即该窗弃了几句译文音）
    - skips  = 源句摘译（`source-skip` 消费行数,MT worker 真跳过的待译源句）
    require_drop 判据取并集（drops+skips≥1）——「追最新弃音保字」与「摘译保文」
    都是积压门生效的落地形态。"""
    try:
        lines = LOG_PATH.read_text(errors="ignore").splitlines()
    except OSError:
        return 0, 0, 0
    idx = [i for i, ln in enumerate(lines) if call_id in ln]
    if not idx:
        return 0, 0, 0
    events = drops = skips = 0
    for ln in lines[idx[0]:]:
        if "INTERP_BACKLOG" in ln:
            events += 1
            if "drop=1" in ln:
                drops += 1
            if "source-skip" in ln:
                skips += 1
    return events, drops, skips


async def main() -> int:
    require_drop = os.environ.get("BOK_PROBE_REQUIRE_DROP", "1") == "1"
    start = time.time()
    call = httpx.post(
        f"{CONTROL_PLANE_URL}/api/calls",
        json={"account_id": "acc-001", "kind": "interpret", "mode": "live", "direction": "interpret",
              "language": "zh", "target_lang": "en", "object_id": ""},
        headers=_CP_HEADERS,
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
            # 间隙必须推静音帧(2026-10-02 根因定案):官方 VAD 缺帧=静音不可见
            # ——只 sleep 唔推帧时 EOS 永不触发,'。'尾句(流式 partial 只出逗号)
            # 无强标点可提交=整串粘进一个永不 finish 的会话、断线才蒸发
            # (orig=4/6 与 drops/skips=0 的双重 FAIL 根因;A 线台架
            # silence_pcm 同款纪律)。⚠ 不能用纯零(silence_pcm):发布端 Opus
            # DTX 把数字静音整段丢弃(实测纯零间隙零效果),A 线刺激 WAV 能过
            # 是因为真合成音带本底噪声——±1 抖动同理:过 DTX、VAD 读作静音。
            await me.push(_dither_silence_pcm(GAP_S))
        await asyncio.sleep(16)  # 等队列排干(追最新:最后一条必须出)
    finally:
        await me.close()
        await other.close()
        try:
            httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/hangup", headers=_CP_HEADERS, timeout=10)
        except Exception:
            pass

    turns = httpx.get(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/turns", headers=_CP_HEADERS, timeout=10).json()
    orig = [t for t in turns if str(t.get("transcript") or "").startswith("原文：")]
    tran = [t for t in turns if str(t.get("transcript") or "").startswith("译文：")]
    events, drops, skips = (count_drops(call_id) if require_drop else (0, 0, 0))

    ok = len(orig) >= len(SENTENCES) - 2 and (not require_drop or drops + skips >= 1)
    print(
        f"INTERPRET_BACKLOG_PROBE call={call_id} src={len(SENTENCES)} orig={len(orig)} "
        f"tran={len(tran)} events={events} dropped={drops} skipped={skips} "
        f"elapsed={int(time.time() - start)}s {'PASS' if ok else 'FAIL'}",
        flush=True,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
