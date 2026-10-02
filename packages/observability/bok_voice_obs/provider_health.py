"""MiniMax 云 TTS 配额/限流健康面（fix-wave-3 M-11，task-13 F-M1）。

背景：配额风暴（2026-09-22T19:36 UTC 起 `MINIMAX_TTS_BIDI_STATUS 2056` 连发）期间
全部健康面全绿——status/doctor 只探本地端口（tts sidecar :8788 死的是云端配额不是
进程）、CP 无云配额端点、agent.log 打点族无任何程序消费。「配额死 N=∞ 不可见，
唯一探测是人工 grep」。

本模块是**纯消费聚合**（stdlib-only，不 import 包内兄弟模块——编排器 bok.py 按
文件路径直接加载本文件，包 `__init__` 的 starlette 链不得被牵起）：

- tail 有界读 worker 日志（agent.log / interp-fwd.log / interp-rev.log）；
- worker 的打点行是**无时间戳的裸 print**（MINIMAX_TTS_RETRY/TTS_ERROR/
  TTS_BIDI_STATUS/MINIMAX_BIDI_RATE_LIMIT 族），时基取自同文件中最近一条前置
  结构化行（`ts`/`timestamp` 键，bok 服务格式与 livekit-agents JSON 双格式）；
- 按窗口聚合：2056（Token Plan 配额死）与 1002/1039/2205（RPM/TPM 限流族，
  与 livekit_plugins._MINIMAX_BIDI_RATE_LIMIT_STATUSES 同集）计数 + 最近命中时间。

只做可见性，不做通知渠道；读路径永不抛（日志怪形状一律吞成计数 0）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

# 与 apps/agent/agent_runtime/providers/livekit_plugins.py
# _MINIMAX_BIDI_RATE_LIMIT_STATUSES 同集（1002=RPM / 1039=TPM / 2205=请求超限族）。
RATE_LIMIT_STATUSES: frozenset[int] = frozenset({1002, 1039, 2205})
# MiniMax 云配额死：Token Plan 用量上限（task-13 F-M1 主角）。
QUOTA_STATUSES: frozenset[int] = frozenset({2056})

DEFAULT_LOG_FILES: tuple[str, ...] = ("agent.log", "interp-fwd.log", "interp-rev.log")
DEFAULT_WINDOW_S = 300.0  # 5 分钟内可见（brief 判据）
DEFAULT_TAIL_BYTES = 4 * 1024 * 1024

# 裸 print 打点行里的状态码形状（三族覆盖既有打点格式）。
_STATUS_RES = (
    re.compile(r"'status_code':\s*(\d+)"),  # RuntimeError repr / base_resp repr
    re.compile(r"MINIMAX_TTS_BIDI_STATUS\s+(\d+)"),
    re.compile(r"MINIMAX_BIDI_RATE_LIMIT\s+status=(\d+)"),
)


def _parse_line_ts(line: str) -> float | None:
    """结构化行取时间戳；非 JSON / 无 ts / 解析失败一律 None。"""
    s = line.strip()
    if not s.startswith("{"):
        return None
    try:
        obj = json.loads(s)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    raw = obj.get("ts") or obj.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _scan_file(path: Path, window_start: float, tail_bytes: int) -> dict:
    """单文件扫描：返回窗内各族计数 + 全尾最近命中（epoch）+ 无时基标记计数。"""
    out: dict = {
        "quota_2056": 0,
        "rate_limit": 0,
        "undated": 0,
        "statuses": {},
        "last_hits": {"quota_2056": None, "rate_limit": None},
    }
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()  # 丢掉截断的半行
            blob = fh.read().decode("utf-8", "replace")
    except Exception:
        return out

    last_ts: float | None = None
    for line in blob.splitlines():
        ts = _parse_line_ts(line)
        if ts is not None:
            last_ts = ts
            continue
        if "MINIMAX_" not in line and "status_code" not in line:
            continue
        statuses = {int(m.group(1)) for pat in _STATUS_RES for m in pat.finditer(line)}
        statuses.discard(0)
        if not statuses:
            continue
        if last_ts is None:
            # 扫描窗起点之前落盘的裸标记（tail 截去了其前导结构化行）：无时基，
            # 不入窗内计数与 last_hit（诚实少报优于错报）。
            out["undated"] += 1
            continue
        in_window = last_ts > window_start
        if in_window:
            for st in statuses:
                if st in QUOTA_STATUSES:
                    out["quota_2056"] += 1
                elif st in RATE_LIMIT_STATUSES:
                    out["rate_limit"] += 1
                out["statuses"][str(st)] = out["statuses"].get(str(st), 0) + 1
        fam = "quota_2056" if statuses & QUOTA_STATUSES else (
            "rate_limit" if statuses & RATE_LIMIT_STATUSES else None)
        if fam is not None:
            prev = out["last_hits"][fam]
            if prev is None or last_ts > prev:
                out["last_hits"][fam] = last_ts
    return out


def scan_provider_health(
    log_dir: Path | str,
    window_s: float = DEFAULT_WINDOW_S,
    now: float | None = None,
    files: tuple[str, ...] = DEFAULT_LOG_FILES,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
) -> dict:
    """聚合 provider 云健康（2056/限流族近窗计数 + 最近命中时间）。

    返回结构（读路径永不抛）：
    ```
    {"available": bool,        # 日志目录在场（缺席=跨机部署等场景的诚实降级）
     "degraded": bool,         # 窗内有配额死/限流命中
     "window_s": float,
     "undated": int,           # 无时基标记行数（诚实少报注记）
     "quota_2056": {"count", "last_hit"(iso|None)},
     "rate_limit": {"count", "last_hit"(iso|None), "statuses"},
     "scanned": {file: {...每文件明细...}}}
    ```
    """
    import time as _time

    log_dir = Path(log_dir)
    now = _time.time() if now is None else now
    window_start = now - window_s
    available = log_dir.is_dir()
    total = {"quota_2056": 0, "rate_limit": 0, "undated": 0, "statuses": {}}
    last_hits: dict[str, float | None] = {"quota_2056": None, "rate_limit": None}
    scanned: dict[str, dict] = {}
    for name in files:
        per = _scan_file(log_dir / name, window_start, tail_bytes)
        scanned[name] = per
        total["quota_2056"] += per["quota_2056"]
        total["rate_limit"] += per["rate_limit"]
        total["undated"] += per["undated"]
        for st, n in per["statuses"].items():
            total["statuses"][st] = total["statuses"].get(st, 0) + n
        for fam in ("quota_2056", "rate_limit"):
            hit = per["last_hits"][fam]
            if hit is not None and (last_hits[fam] is None or hit > last_hits[fam]):
                last_hits[fam] = hit

    def _fmt_hit(epoch: float | None) -> str | None:
        if epoch is None:
            return None
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()

    return {
        "available": available,
        "degraded": total["quota_2056"] > 0 or total["rate_limit"] > 0,
        "window_s": window_s,
        "undated": total["undated"],
        "quota_2056": {
            "count": total["quota_2056"],
            "last_hit": _fmt_hit(last_hits["quota_2056"]),
        },
        "rate_limit": {
            "count": total["rate_limit"],
            "last_hit": _fmt_hit(last_hits["rate_limit"]),
            "statuses": total["statuses"],
        },
        "scanned": scanned,
    }
