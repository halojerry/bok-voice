"""建单并发容量准入（第一性重写，2026-10-01 计划档 §二）。

第一性公式::

    max_active_calls = clamp(floor, (available − headroom) / workset, ceiling)

三条纪律：

1. **准入不创造容量**——公式只往下压（内存紧→少放行），绝不上抬；
   硬件档案（profile）只给 floor/ceiling 初值，floor=已验证下限、
   ceiling=实测物理上限（mac=2 是 6 通 Metal OOM 实弹的物理现实，
   reports/mac-concurrency-2026-09-24/BATTERY-FINAL.md；cuda=48 为
   硬件档案上限初值）。
2. **常驻脚印并入安全垫**：定案档案只有四个旋钮（floor/ceiling/workset/
   headroom），常驻服务余量算进 headroom，不单列（mac 2GB 已覆盖）。
3. **显式 ``BOK_MAX_ACTIVE_CALLS`` = legacy 钉死**：显式设了就不探测不
   计算，返回值逐字节等于旧语义（0/负=不限），作为 ceiling 硬覆盖的
   钉死档——要动态档就别设它，要固定档就设它。未设 → 动态路径。

环境键（全部 CP 面，经 tools/bok.py ``_control_plane_env`` 下发；
prod launchd 封闭 env 面不注入即死门，先例 BOK_MAX_ACTIVE_CALLS）：

- ``BOK_DEPLOY_PROFILE`` = ``auto``(默认) | ``mac`` | ``cuda``；auto =
  macOS→mac、Linux 且 nvidia-smi 可用→cuda，其余 unknown（按 mac 档保守）。
- ``BOK_MAX_CALLS_FLOOR`` / ``BOK_MAX_CALLS_CEILING``：覆盖档案初值
  （仅 ≥1 的整数生效，非法/缺省回落档案）。
- ``BOK_MAX_ACTIVE_CALLS``：见上（legacy 钉死）。

``available_bytes`` 取值口径（**零新依赖**——psutil 未在 requirements
声明，不引入可选依赖；全部平台原生探针，口径可复述可复核）：

- macOS：解析 ``vm_stat`` 头行 page size × (free + inactive + purgeable)
  页数（与 Activity Monitor 口径同族：inactive/purgeable 可即刻回收）。
- Linux：``/proc/meminfo`` 的 ``MemAvailable``。
- cuda：先取 ``nvidia-smi --query-gpu=memory.free`` 显存余量（求和），
  失败回退系统内存（概档在 Mac 上显式设 cuda 时即走此回退）。

探测失败（None）fail-open 回**档案 ceiling**（mac=2=旧缺省逐字节等价，
绝不因探测异常 500 建单）。30s 缓存（建单高频，vm_stat/nvidia-smi 有
子进程开销）；缓存键=四个 env 键，改键立刻生效（测试确定复现）。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_GB = 1024 ** 3

# 硬件档案初值（floor=已验证下限 / ceiling=实测物理上限 / workset=单通工作集 /
# headroom=安全垫）。mac 档数字直接来自双通实弹与 6 通 OOM 对照；cuda 档为
# 计划档定案的初始档案（首跑校准后凭 BOK_MAX_CALLS_* 覆盖，勿在代码里猜）。
PROFILES: dict[str, dict[str, float]] = {
    "mac": {"floor": 1, "ceiling": 2, "workset_gb": 2.5, "headroom_gb": 2.0},
    "cuda": {"floor": 10, "ceiling": 48, "workset_gb": 1.5, "headroom_gb": 4.0},
    # 未知平台（非 mac / 非 cuda-Linux）：按 mac 档保守兜底（准入不创造容量，
    # 宁可少放；要更多显式设 BOK_MAX_CALLS_CEILING）。
    "unknown": {"floor": 1, "ceiling": 2, "workset_gb": 2.5, "headroom_gb": 2.0},
}

_CACHE_TTL_S = 30.0
_CACHE: dict[str, Any] = {"key": None, "at": 0.0, "snap": None}

_ENV_KEYS = (
    "BOK_DEPLOY_PROFILE",
    "BOK_MAX_ACTIVE_CALLS",
    "BOK_MAX_CALLS_FLOOR",
    "BOK_MAX_CALLS_CEILING",
)


# ---------------------------------------------------------------------------
# 档案探测
# ---------------------------------------------------------------------------


def detect_profile() -> str:
    """部署档案:env BOK_DEPLOY_PROFILE 优先;auto 按平台探测。

    auto=macOS→mac;Linux 且 nvidia-smi 可用→cuda;其余 unknown。
    """
    v = str(os.environ.get("BOK_DEPLOY_PROFILE", "") or "auto").strip().lower()
    if v == "mac":
        return "mac"
    if v == "cuda":
        return "cuda"
    # auto / 非法值:平台探测
    if sys.platform == "darwin":
        return "mac"
    if sys.platform.startswith("linux") and shutil.which("nvidia-smi"):
        return "cuda"
    return "unknown"


def available_bytes(profile: str | None = None) -> int | None:
    """可用内存/显存字节数;探测失败 None（调用方 fail-open 回档案 ceiling）。

    口径见模块 docstring（macOS vm_stat free+inactive+purgeable / Linux
    MemAvailable / cuda nvidia-smi memory.free 求和失败回系统内存）。
    """
    prof = profile or detect_profile()
    if prof == "cuda":
        gpu = _nvidia_free_bytes()
        if gpu is not None and gpu > 0:
            return gpu
    return _system_available_bytes()


def _system_available_bytes() -> int | None:
    if sys.platform == "darwin":
        return _vm_stat_available_bytes()
    if sys.platform.startswith("linux"):
        return _linux_meminfo_available_bytes()
    return None


_VM_STAT_PAGE_RE = re.compile(r"^Pages (free|inactive|purgeable):\s+(\d+)\.?\s*$", re.M)
_VM_STAT_PAGESIZE_RE = re.compile(r"page size of (\d+) bytes")


def _vm_stat_available_bytes() -> int | None:
    """macOS: vm_stat 头行 page size × (free+inactive+purgeable) 页数。"""
    try:
        out = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=5
        )
        if out.returncode != 0:
            return None
        m = _VM_STAT_PAGESIZE_RE.search(out.stdout or "")
        if not m:
            return None
        page = int(m.group(1))
        pages = {
            key: int(val)
            for key, val in _VM_STAT_PAGE_RE.findall(out.stdout or "")
        }
        if not pages:
            return None
        return page * sum(pages.values())
    except Exception:  # noqa: BLE001 - 探测失败=数据,不是异常
        return None


_MEMINFO_AVAIL_RE = re.compile(r"^MemAvailable:\s+(\d+) kB", re.M)


def _linux_meminfo_available_bytes(path: str = "/proc/meminfo") -> int | None:
    """Linux: /proc/meminfo MemAvailable（内核可用内存估算,含可回收页）。"""
    try:
        txt = Path(path).read_text(encoding="utf-8", errors="replace")
        m = _MEMINFO_AVAIL_RE.search(txt)
        if not m:
            return None
        return int(m.group(1)) * 1024
    except Exception:  # noqa: BLE001
        return None


def _nvidia_free_bytes() -> int | None:
    """cuda: nvidia-smi 显存余量求和（MiB→字节）;失败/空 None。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return None
        vals = [int(line.strip()) for line in (out.stdout or "").splitlines() if line.strip()]
        if not vals:
            return None
        return sum(vals) * 1024 * 1024
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 公式与准入快照
# ---------------------------------------------------------------------------


def _parse_int(raw: Any) -> int | None:
    """宽松整数解析:非法/空 → None（调用方回落缺省）。"""
    s = str(raw if raw is not None else "").strip()
    if not s:
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _env_override(name: str) -> int | None:
    """floor/ceiling 覆盖:仅 ≥1 整数生效（0/负/非法=忽略,回落档案初值）。"""
    n = _parse_int(os.environ.get(name, ""))
    if n is None or n < 1:
        return None
    return n


def _env_cache_key() -> tuple[str, ...]:
    return tuple(str(os.environ.get(k, "") or "").strip() for k in _ENV_KEYS)


def _compute_snapshot() -> dict[str, Any]:
    profile = detect_profile()
    params = PROFILES.get(profile) or PROFILES["unknown"]
    floor = _env_override("BOK_MAX_CALLS_FLOOR") or int(params["floor"])
    ceiling = _env_override("BOK_MAX_CALLS_CEILING") or int(params["ceiling"])
    base: dict[str, Any] = {
        "profile": profile,
        "floor": floor,
        "ceiling": ceiling,
        "workset_gb": float(params["workset_gb"]),
        "headroom_gb": float(params["headroom_gb"]),
        "computed": None,
        "free_gb": None,
        "probed": False,
        "legacy": False,
    }
    # legacy 钉死:显式设了 BOK_MAX_ACTIVE_CALLS 就不探测不计算（旧语义逐字节,
    # 含 0/负=不限的哨兵值,调用方按 >0 判限）。零探测=零子进程开销。
    legacy = _parse_int(os.environ.get("BOK_MAX_ACTIVE_CALLS", ""))
    if legacy is not None:
        return {**base, "legacy": True, "ceiling": legacy, "max": legacy}
    # 动态档:探测→公式→clamp（floor ≤ 结果 ≤ ceiling,并恒 ≥1）。
    # 探测异常也 fail-open（契约=绝不因探测异常 500 建单;各探针内部已 catch,
    # 这里再兜一层,防未来重构引入会抛的探针）。
    try:
        avail = available_bytes(profile)
    except Exception:  # noqa: BLE001
        avail = None
    if avail is None:
        # 探测失败 fail-open 回档案 ceiling（mac=2=旧缺省等价;绝不 500）。
        return {**base, "max": max(1, ceiling)}
    headroom = int(base["headroom_gb"] * _GB)
    workset = int(base["workset_gb"] * _GB)
    computed = (avail - headroom) // workset
    allowed = max(1, min(ceiling, max(floor, int(computed))))
    return {
        **base,
        "computed": int(computed),
        "free_gb": round(avail / _GB, 1),
        "probed": True,
        "max": allowed,
    }


def capacity_snapshot(now: float | None = None) -> dict[str, Any]:
    """准入快照(30s 缓存;缓存键=四个 env 键,改键立刻失效)。

    字段:max/profile/floor/ceiling/computed/free_gb/probed/legacy/
    workset_gb/headroom_gb——409 明细与审计共用。
    """
    now = time.monotonic() if now is None else now
    key = _env_cache_key()
    snap = _CACHE.get("snap")
    if (
        snap is not None
        and _CACHE.get("key") == key
        and (now - float(_CACHE.get("at") or 0.0)) < _CACHE_TTL_S
    ):
        return snap
    snap = _compute_snapshot()
    _CACHE.update({"key": key, "at": now, "snap": snap})
    return snap


def compute_max_calls(now: float | None = None) -> int:
    """准入上限（legacy ≤0=不限,调用方按 >0 判限）。"""
    return int(capacity_snapshot(now=now)["max"])


def reset_cache() -> None:
    """清缓存（测试/配置热更后强制重探）。"""
    _CACHE.update({"key": None, "at": 0.0, "snap": None})


def format_limit_detail(snap: dict[str, Any]) -> str:
    """409 detail 人话串:保留旧文案 + 计算明细（运维归因一眼可读）。"""
    def _show(v: Any) -> str:
        return "na" if v is None else str(v)

    parts = [
        f"profile={_show(snap.get('profile'))}",
        f"floor={_show(snap.get('floor'))}",
        f"computed={_show(snap.get('computed'))}",
        f"ceiling={_show(snap.get('ceiling'))}",
        f"free_gb={_show(snap.get('free_gb'))}",
    ]
    if snap.get("legacy"):
        parts.append("legacy=1")
    return "并发已达上限，请稍后重试（" + " ".join(parts) + "）"
