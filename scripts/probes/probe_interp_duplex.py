"""B 线全双工探针（2026-09-16 立项；2026-10-09 W8-A3 判据升级=无闭麦全双工）。

拓扑定案（Ethan 拍板）：远程双方各戴耳机各自设备——meHeld 半双工闭麦闸退位
（web 侧缺省关，同机演示档才开），fwd 与 rev 两线天然独立、无串译环，全双工
天然成立。本探针是全双工验收仪器（worker 级；web 侧缺省档由
apps/web/test/interp-duplex.test.mjs 钉）：

  ①零丢句：两向同时推语料 ~30s（段窗按 10s 步进错开——echo-dedup 同文 8s 窗
    会丢弃重推的同文段，段间内容必须互异），双向 finals 计数对账：每向原文行
    ≥ 推送段数（ASR 劈句放行、丢句/并句即红），上限 3 倍碎句护栏；
  ②双向译文独立出声（双轨音频块到达）：me 侧 trans-<我方语言>（rev 译员耳语）
    与 other 侧 trans-<对方语言>（fwd 译文）各自到达字节 ≥1s 音频——两条 TTS
    轨都真出声，而非只剩单向（rev 轨缺席=BOK_INTERP_REV_AUDIO 被关或订阅
    白名单断， FAIL）；
  ③无串译环：任一方向「原文」行与对向「译文」行高相似（归一后 ≥0.85 或互为
    子串）=译文回灌为源文=串译死循环证据；
  ④双向延迟判据保留：fwd 语音起点 lag 预算（与 probe_interpret_latency 同口径）
    + rev 原文行→译文行 turns 配对生成延迟预算。

用法:
  .venv312/bin/python scripts/probes/probe_interp_duplex.py
env:
  BOK_PROBE_LAG_BUDGET_MS    fwd 逐句/平均 lag 预算(默认 3500)
  BOK_PROBE_REV_BUDGET_MS    rev 落库延迟预算(默认 6000;轮询粒度粗,给足余量)
  BOK_PROBE_DUPLEX_SPAN_S    两向同时推语料时长(默认 30)
前置:python tools/bok.py serve(含 interp-fwd/rev 与 MT :1236)。
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
import difflib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import e2e_interpret as e2e  # noqa: E402
from probe_interpret_latency import TimedSide, speech_onset_after  # noqa: E402

# auth-on 栈(2026-09-15 标准姿势)要求 CP 请求带机器通道 token——E2E 建单/取
# token/收线/读 turns 全是机器语义,Bearer BOK_CP_TOKEN 直通(与 agent worker 同源)。
# 未设 env(老 auth-off 栈)零变化。CP 之外(asr sidecar)不带。
_CP_HEADERS: dict[str, str] = {}
if os.environ.get("BOK_CP_TOKEN", "").strip():
    _CP_HEADERS["Authorization"] = f"Bearer {os.environ['BOK_CP_TOKEN'].strip()}"

SEG_FWD_S = 4.0
GAP_FWD_S = 1.2
SEG_REV_S = 4.5
GAP_REV_S = 0.8
_WIN_STRIDE_S = 10.0  # 相邻推送窗错开步进:段内容必须互异(echo-dedup 同文 8s 窗会丢重推段)
_MIN_TRACK_BYTES = 16000 * 2 * 1  # 1s 音频:双轨「真出声」字节下限
_LOOP_SIM = 0.85


def _win(pcm: bytes, i: int, dur_s: float) -> bytes:
    """第 i 个互异内容窗:同一条长语料按 stride 错开取窗,段间文本天然不同。"""
    start = int(16000 * _WIN_STRIDE_S * i) * 2
    end = min(start + int(16000 * dur_s) * 2, len(pcm))
    return pcm[start:end]


class DuplexSide(TimedSide):
    """TimedSide + 按轨名记账(双轨独立出声判据):父类 captured 是全部 trans-* 混流,
    这里另挂 per-name 读取器,统计每条译文轨的到达字节/帧批事件。"""

    def __init__(self, call_id: str, identity: str):
        super().__init__(call_id, identity)
        self.track_bytes: dict[str, int] = {}
        self.track_events: dict[str, int] = {}

    async def connect(self) -> None:
        await super().connect()
        from livekit import rtc

        def watch(track, pub, participant):
            if int(track.kind) != int(rtc.TrackKind.KIND_AUDIO):
                return
            name = str(getattr(track, "name", "") or "")
            if not name.startswith("trans-"):
                return

            async def _read():
                try:
                    stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
                    async for event in stream:
                        frame = getattr(event, "frame", event)
                        data = bytes(frame.data)
                        self.track_bytes[name] = self.track_bytes.get(name, 0) + len(data)
                        self.track_events[name] = self.track_events.get(name, 0) + 1
                except Exception:
                    pass

            self._tasks.append(asyncio.get_running_loop().create_task(_read()))

        self.room.on("track_subscribed", watch)
        for participant in self.room.remote_participants.values():
            for pub in participant.track_publications.values():
                t = getattr(pub, "track", None)
                if t is not None:
                    watch(t, pub, participant)


def _norm_txt(s: str) -> str:
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def _loop_hits(src_texts: list[str], tran_texts: list[str]) -> list[str]:
    """串译环检测:源文行 ≈ 对向译文行(归一相等/互为子串/sim≥0.85)=回灌证据。"""
    hits: list[str] = []
    trans_norms = [t for t in (_norm_txt(x) for x in tran_texts) if t]
    for s in src_texts:
        ns = _norm_txt(s)
        if not ns:
            continue
        for nt in trans_norms:
            if ns == nt or nt in ns or ns in nt or difflib.SequenceMatcher(a=ns, b=nt).ratio() >= _LOOP_SIM:
                hits.append(s)
                break
    return hits


async def _feeder(side, pcm: bytes, seg_s: float, gap_s: float, span_s: float, sink: list) -> None:
    """一侧持续说话:互异窗逐段推流直到 span 耗尽,段间留提交空隙。"""
    i = 0
    t_end = time.monotonic() + span_s
    while time.monotonic() < t_end:
        t0 = time.monotonic()
        await side.push(_win(pcm, i, seg_s))
        sink.append({"src_end": t0 + seg_s, "onset": None})
        i += 1
        remain = t_end - time.monotonic()
        if remain > 0:
            await asyncio.sleep(min(gap_s, remain))


async def _settle(*captures: bytearray, quiet_s: float = 4.0, deadline_s: float = 90.0) -> None:
    """等两向译文音频都收敛:所有捕获流 4s 无增长。"""
    deadline = time.monotonic() + deadline_s
    last = [len(c) for c in captures]
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        await asyncio.sleep(0.3)
        cur = [len(c) for c in captures]
        if cur != last:
            last, last_change = cur, time.monotonic()
        elif time.monotonic() - last_change > quiet_s:
            break


async def main() -> int:
    lag_budget = int(os.environ.get("BOK_PROBE_LAG_BUDGET_MS", "3500"))
    rev_budget = int(os.environ.get("BOK_PROBE_REV_BUDGET_MS", "6000"))
    span_s = float(os.environ.get("BOK_PROBE_DUPLEX_SPAN_S", "30") or 30)

    pcm_fwd = e2e.read_wav_pcm(e2e.AUDIO_DIR / "zh.wav")
    pcm_rev = e2e.read_wav_pcm(e2e.AUDIO_DIR / "en.wav")

    created = e2e.httpx.post(
        f"{e2e.CONTROL_PLANE_URL}/api/calls",
        json={"account_id": "acc-001", "kind": "interpret", "mode": "live", "direction": "interpret",
              "language": "zh", "target_lang": "en", "object_id": ""},
        headers=_CP_HEADERS,
        timeout=15,
    ).json()
    call_id = created["id"]
    me = DuplexSide(call_id, f"me-{call_id}")    # fwd:推 zh,收 trans-zh 耳语(+trans-en 加听)
    other = DuplexSide(call_id, f"other-{call_id}")  # rev:推 en,收 trans-en 译文
    await me.connect()
    await other.connect()
    await asyncio.sleep(6)  # 等 RoomAgentDispatch 拉起 fwd/rev 解释器

    fwd_segs: list[dict] = []
    rev_segs: list[dict] = []
    try:
        # 两向同时开跑:gather 保证 fwd 推流与 rev 持续说话完全重叠(全双工争用)。
        await asyncio.gather(
            _feeder(me, pcm_fwd, SEG_FWD_S, GAP_FWD_S, span_s, fwd_segs),
            _feeder(other, pcm_rev, SEG_REV_S, GAP_REV_S, span_s, rev_segs),
        )
        await _settle(me.captured, other.captured)
        for seg in fwd_segs:
            seg["onset"] = speech_onset_after(other.timelog, other.captured, seg["src_end"])
    finally:
        await me.close()
        await other.close()
        try:
            e2e.httpx.post(f"{e2e.CONTROL_PLANE_URL}/api/calls/{call_id}/hangup", headers=_CP_HEADERS, timeout=10)
        except Exception:
            pass

    # turns 对账:挂断 flush 后轮询,双向 finals 齐了早停;否则等满窗拿最终账。
    rows: list[dict] = []
    deadline = time.monotonic() + 20
    stable = 0
    last_key = None
    while time.monotonic() < deadline:
        await asyncio.sleep(1.0)
        rows = e2e.httpx.get(
            f"{e2e.CONTROL_PLANE_URL}/api/calls/{call_id}/turns", headers=_CP_HEADERS, timeout=10
        ).json()
        key = (len(rows), sum(1 for r in rows if str(r.get("transcript") or "").startswith("原文：")))
        if key == last_key:
            stable += 1
        else:
            stable = 0
        last_key = key
        fwd_zh = sum(
            1 for r in rows
            if str(r.get("transcript") or "").startswith("原文：") and (r.get("language") or "") == "zh"
        )
        rev_en = sum(
            1 for r in rows
            if str(r.get("transcript") or "").startswith("原文：") and (r.get("language") or "") == "en"
        )
        if (fwd_zh >= len(fwd_segs) and rev_en > 0) or stable >= 3:
            break

    def _row_epoch(r: dict) -> float:
        # CP created_at 是 UTC naive ISO(实证:与 utcnow 同域)→ 补 UTC 归一 epoch。
        return datetime.fromisoformat(str(r.get("created_at"))).replace(tzinfo=timezone.utc).timestamp()

    fwd_src_rows = sorted(
        (r for r in rows if str(r.get("transcript") or "").startswith("原文：") and (r.get("language") or "") == "zh"),
        key=_row_epoch,
    )
    fwd_finals = len(fwd_src_rows)
    rev_src_rows = sorted(
        (r for r in rows if str(r.get("transcript") or "").startswith("原文：") and (r.get("language") or "") == "en"),
        key=_row_epoch,
    )
    rev_finals = len(rev_src_rows)
    fwd_tran = sum(
        1 for r in rows
        if str(r.get("transcript") or "").startswith("译文：") and (r.get("language") or "") == "en"
    )
    rev_tran_rows = sorted(
        (r for r in rows if str(r.get("transcript") or "").startswith("译文：") and (r.get("language") or "") == "zh"),
        key=_row_epoch,
    )

    def _body(r: dict) -> str:
        return str(r.get("transcript") or "").replace("原文：", "", 1).replace("译文：", "", 1).strip()

    # ①零丢句:双向 finals 计数对账(下限=推送段数:劈句放行、丢句/并句即红;
    # 上限=3 倍:碎句形状护栏)。
    zero_drop = (
        fwd_finals >= len(fwd_segs) and fwd_finals <= len(fwd_segs) * 3
        and rev_finals >= len(rev_segs) and rev_finals <= len(rev_segs) * 3
    )
    # ②双向译文独立出声:两条译文轨各 ≥1s 到达字节(rev 耳语轨缺席=单向化回退)。
    me_bytes = me.track_bytes.get("trans-zh", 0)
    oth_bytes = other.track_bytes.get("trans-en", 0)
    dual_audio = me_bytes >= _MIN_TRACK_BYTES and oth_bytes >= _MIN_TRACK_BYTES
    # ③无串译环:双向原文行 vs 对向译文行。
    loop_zh = _loop_hits([_body(r) for r in fwd_src_rows], [_body(r) for r in rev_tran_rows])
    loop_en = _loop_hits([_body(r) for r in rev_src_rows], [
        _body(r) for r in rows
        if str(r.get("transcript") or "").startswith("译文：") and (r.get("language") or "") == "en"
    ])
    no_loop = not loop_zh and not loop_en

    lags = [round((s["onset"] - s["src_end"]) * 1000) for s in fwd_segs if s["onset"] is not None]
    # rev 生成耗时:rev 原文行(STT 提交)→ 对应译文行的 created_at 差(按序配对)。
    # 轮询计数迟到会出假 60s+——created_at 才是可信时刻源;译文行在推流中途
    # (子句提交)就落库,原文行→译文行的配对差才是纯生成耗时。
    rev_lags = [
        round((_row_epoch(t) - _row_epoch(s)) * 1000)
        for s, t in zip(rev_src_rows, rev_tran_rows)
        if _row_epoch(t) >= _row_epoch(s)
    ]
    avg = round(sum(lags) / len(lags)) if lags else -1
    rev_avg = round(sum(rev_lags) / len(rev_lags)) if rev_lags else -1

    lag_ok = (
        len(lags) == len(fwd_segs)
        and all(ms <= lag_budget for ms in lags)
        and rev_lags
        and rev_avg <= rev_budget
    )
    ok = zero_drop and dual_audio and no_loop and lag_ok and fwd_tran >= 1 and len(rev_tran_rows) >= 1
    print(
        f"INTERPRET_DUPLEX_PROBE fwd_lag_ms={lags} fwd_avg_ms={avg} fwd_budget={lag_budget} | "
        f"rev_lag_ms={rev_lags} rev_avg_ms={rev_avg} rev_budget={rev_budget} | "
        f"fwd(push={len(fwd_segs)},finals={fwd_finals},tran={fwd_tran}) "
        f"rev(push={len(rev_segs)},finals={rev_finals},tran={len(rev_tran_rows)}) | "
        f"dual_audio(me trans-zh={me_bytes}B,other trans-en={oth_bytes}B) | "
        f"loop_hits(zh={len(loop_zh)},en={len(loop_en)}) | "
        f"zero_drop={zero_drop} dual_audio={dual_audio} no_loop={no_loop} lag_ok={lag_ok} "
        f"{'PASS' if ok else 'FAIL'}",
        flush=True,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
