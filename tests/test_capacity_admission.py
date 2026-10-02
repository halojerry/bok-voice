"""容量准入模块契约（2026-10-01 第一性重写）。

第一性公式 ``max_active_calls = clamp(floor, (available − headroom)/workset,
ceiling)``；准入不创造容量（公式只往下压），硬件档案只给 floor/ceiling 初值。
覆盖：

- 公式/clamp 数学（mock 探测，不碰真机内存/不跑子进程）
- 档案常量：mac ceiling=2 不可被内存上抬；cuda floor=10 / ceiling=48
- legacy ``BOK_MAX_ACTIVE_CALLS`` 钉死（零探测；0/负=不限）
- ``BOK_DEPLOY_PROFILE`` env 覆盖 + auto 平台探测（darwin/linux+cuda/unknown）
- floor/ceiling env 覆盖（仅 ≥1 生效）
- 30s 缓存（探测一次；env 改键立刻失效；TTL 到期重探）
- 探测失败/异常 fail-open 回档案 ceiling（绝不 500）
- 平台解析口径：macOS vm_stat 三页求和 / Linux MemAvailable / cuda nvidia-smi
- 409 detail 含计算明细（TestClient + 内存仓，沿用 test_lifecycle_guards 姿势）
- 源级 pin：main.py 建单闸走 capacity（静态 _max_active_calls_env 已退役）、
  bok.py CP 面登记三键
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-capacity")

import pytest  # noqa: E402

from control_plane import capacity  # noqa: E402

GB = 1024 ** 3

_ENV_KEYS = (
    "BOK_DEPLOY_PROFILE",
    "BOK_MAX_ACTIVE_CALLS",
    "BOK_MAX_CALLS_FLOOR",
    "BOK_MAX_CALLS_CEILING",
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """清四个 env 键 + 缓存：同进程其他测试文件的模块级 setenv 不能串进来
    （test_dial_now_api / test_campaign_loop 会常驻 BOK_MAX_ACTIVE_CALLS=0）。"""
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    capacity.reset_cache()
    yield
    capacity.reset_cache()


def _pin_available(monkeypatch, value: int | None, *, record: list | None = None):
    """固定 available_bytes 返回值（record 传入时记录调用次数/参数）。"""
    def _fake(profile=None):
        if record is not None:
            record.append(profile)
        return value
    monkeypatch.setattr(capacity, "available_bytes", _fake)


# ---------------------------------------------------------------------------
# 公式与 clamp 数学
# ---------------------------------------------------------------------------


def test_formula_and_clamp_math_mac(monkeypatch):
    """mac 档案(floor1/ceiling2/workset2.5/headroom8)各内存档位：
    公式 computed=(avail-headroom)//workset，结果=clamp 且恒 ≥1。
    headroom=8 是 2026-10-02 对齐双 4GB prompt-cache 时代的定值。"""
    monkeypatch.setenv("BOK_DEPLOY_PROFILE", "mac")
    cases = {
        1 * GB: (-3, 1),      # (1-8)/2.5 → 商 -3 → floor 1
        4 * GB: (-2, 1),      # -1.6 → -2 → floor 1
        6 * GB: (-1, 1),      # -0.8 → -1 → floor 1
        10 * GB: (0, 1),      # 0.8 → 0 → floor 1
        16 * GB: (3, 2),      # 3.2 → 3 → clamp ceiling 2（fake 16GB 机也压回物理上限）
        128 * GB: (48, 2),    # 48 → ceiling 2（内存再多也不上抬）
    }
    for avail, (want_computed, want_max) in cases.items():
        _pin_available(monkeypatch, avail)
        capacity.reset_cache()
        snap = capacity.capacity_snapshot()
        assert snap["computed"] == want_computed, (avail, snap)
        assert snap["max"] == want_max, (avail, snap)
        assert snap["probed"] is True and snap["legacy"] is False


def test_profiles_headroom_pins_cache_era(monkeypatch):
    """headroom 常量钉（2026-10-02 orch2-D）：mac/unknown=8.0=双 4GB prompt-cache
    顶满的最坏增长面（旧值 2.0 是 2GB cap 年代）；cuda 档不动（计划档初始值）。
    可用内存 8GB 的机器此前算出 2（clamp ceiling），现压到 floor=1——准入不
    创造容量，宁可少放。"""
    assert capacity.PROFILES["mac"]["headroom_gb"] == 8.0
    assert capacity.PROFILES["unknown"]["headroom_gb"] == 8.0
    assert capacity.PROFILES["cuda"]["headroom_gb"] == 4.0
    # 8GB 可用：headroom 未回本 → computed=0 → floor 1（旧档为 clamp ceiling 2）
    monkeypatch.setenv("BOK_DEPLOY_PROFILE", "mac")
    _pin_available(monkeypatch, 8 * GB)
    snap = capacity.capacity_snapshot()
    assert snap["computed"] == 0 and snap["max"] == 1


def test_mac_ceiling_two_is_physical_limit(monkeypatch):
    """mac 档 ceiling=2 是 6 通 OOM 实弹的物理上限——公式只往下压，不可上抬。"""
    monkeypatch.setenv("BOK_DEPLOY_PROFILE", "mac")
    _pin_available(monkeypatch, 1024 * GB)
    assert capacity.compute_max_calls() == 2
    # 要 3+ 只能显式改 ceiling/legacy 钉死（另测），内存信号本身永远给不到 3。
    _pin_available(monkeypatch, 3 * GB)
    capacity.reset_cache()
    assert capacity.compute_max_calls() == 1


def test_cuda_profile_floor_and_ceiling(monkeypatch):
    """cuda 档案(floor10/ceiling48/workset1.5/headroom4)：内存紧→floor=10，
    充裕→上限 48；两者之间按公式取整。"""
    monkeypatch.setenv("BOK_DEPLOY_PROFILE", "cuda")
    _pin_available(monkeypatch, 6 * GB)          # (6-4)/1.5=1 → floor 10
    assert capacity.compute_max_calls() == 10
    _pin_available(monkeypatch, 25 * GB)         # 21/1.5=14 → 14
    capacity.reset_cache()
    assert capacity.compute_max_calls() == 14
    _pin_available(monkeypatch, 1024 * GB)       # → ceiling 48
    capacity.reset_cache()
    assert capacity.compute_max_calls() == 48


def test_env_floor_ceiling_overrides(monkeypatch):
    """BOK_MAX_CALLS_FLOOR/CEILING 覆盖档案初值；非法/0/负忽略回落档案。"""
    monkeypatch.setenv("BOK_DEPLOY_PROFILE", "mac")
    _pin_available(monkeypatch, 1024 * GB)
    monkeypatch.setenv("BOK_MAX_CALLS_CEILING", "4")
    assert capacity.compute_max_calls() == 4          # ceiling 上抬（显式授权）
    monkeypatch.setenv("BOK_MAX_CALLS_FLOOR", "3")
    _pin_available(monkeypatch, 1 * GB)
    capacity.reset_cache()
    assert capacity.compute_max_calls() == 3          # floor 兜底
    # 非法覆盖=忽略（回落档案初值）
    monkeypatch.setenv("BOK_MAX_CALLS_CEILING", "abc")
    monkeypatch.setenv("BOK_MAX_CALLS_FLOOR", "0")
    capacity.reset_cache()
    assert capacity.compute_max_calls() == 1


# ---------------------------------------------------------------------------
# 档案探测
# ---------------------------------------------------------------------------


def test_detect_profile_env_override(monkeypatch):
    monkeypatch.setenv("BOK_DEPLOY_PROFILE", "mac")
    assert capacity.detect_profile() == "mac"
    monkeypatch.setenv("BOK_DEPLOY_PROFILE", "CUDA")
    assert capacity.detect_profile() == "cuda"
    # 非法值=auto 平台探测——钉 darwin(2026-10-02 批3 合流修:原注释「本机
    # darwin→mac」是 fp 线 mac 宿主假设,CI Linux+无 nvidia-smi 会得 unknown)。
    monkeypatch.setenv("BOK_DEPLOY_PROFILE", "garbage")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert capacity.detect_profile() == "mac"


def test_detect_profile_auto_platform(monkeypatch):
    monkeypatch.delenv("BOK_DEPLOY_PROFILE", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert capacity.detect_profile() == "mac"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(capacity, "shutil", types.SimpleNamespace(which=lambda name: "/usr/bin/nvidia-smi"))
    assert capacity.detect_profile() == "cuda"
    monkeypatch.setattr(capacity, "shutil", types.SimpleNamespace(which=lambda name: None))
    assert capacity.detect_profile() == "unknown"
    # unknown 档按 mac 数字保守兜底（准入不创造容量）
    assert capacity.PROFILES["unknown"]["ceiling"] == 2


# ---------------------------------------------------------------------------
# legacy 钉死
# ---------------------------------------------------------------------------


def test_legacy_env_pins_and_skips_probe(monkeypatch):
    """显式 BOK_MAX_ACTIVE_CALLS=钉死：不探测不计算，结果=显式值（旧语义）。"""
    monkeypatch.setenv("BOK_DEPLOY_PROFILE", "mac")
    monkeypatch.setenv("BOK_MAX_ACTIVE_CALLS", "7")
    calls: list = []
    _pin_available(monkeypatch, None, record=calls)
    snap = capacity.capacity_snapshot()
    assert snap["max"] == 7 and snap["legacy"] is True
    assert calls == [], "legacy 档必须零探测（子进程开销零）"
    assert capacity.compute_max_calls() == 7
    # 0/负=不限哨兵（调用方按 >0 判限）
    monkeypatch.setenv("BOK_MAX_ACTIVE_CALLS", "0")
    capacity.reset_cache()
    assert capacity.compute_max_calls() == 0
    monkeypatch.setenv("BOK_MAX_ACTIVE_CALLS", "-1")
    capacity.reset_cache()
    assert capacity.compute_max_calls() == -1
    # 非法值=非钉死，落动态路径（探测被调用）
    monkeypatch.setenv("BOK_MAX_ACTIVE_CALLS", "abc")
    capacity.reset_cache()
    calls.clear()
    capacity.capacity_snapshot()
    assert calls == ["mac"]


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------


def test_cache_single_probe_and_env_key(monkeypatch):
    monkeypatch.setenv("BOK_DEPLOY_PROFILE", "mac")
    calls: list = []
    _pin_available(monkeypatch, 16 * GB, record=calls)
    assert capacity.compute_max_calls() == 2  # (16-8)/2.5=3 → clamp ceiling 2
    assert capacity.compute_max_calls() == 2
    assert len(calls) == 1, "30s 内高频建单只探测一次"
    # env 改键立刻失效（缓存键含 profile/legacy/floor/ceiling）
    monkeypatch.setenv("BOK_MAX_CALLS_CEILING", "5")
    assert capacity.compute_max_calls() == 3  # 同 16GB → (16-8)/2.5=3 < ceiling 5
    assert len(calls) == 2
    # 显式 now 推过 TTL → 重探
    capacity.reset_cache()
    _pin_available(monkeypatch, 4 * GB, record=calls)
    capacity.capacity_snapshot(now=1000.0)
    capacity.capacity_snapshot(now=1020.0)
    assert len(calls) == 3, "TTL 内命中缓存"
    capacity.capacity_snapshot(now=1000.0 + capacity._CACHE_TTL_S + 1)
    assert len(calls) == 4, "TTL 到期重探"


def test_probe_failure_and_exception_fail_open_to_ceiling(monkeypatch):
    """探测 None/抛异常都 fail-open 回档案 ceiling（mac=2=旧缺省等价），
    绝不向上抛（建单绝不因探测异常 500）。"""
    monkeypatch.setenv("BOK_DEPLOY_PROFILE", "mac")
    _pin_available(monkeypatch, None)
    snap = capacity.capacity_snapshot()
    assert snap["max"] == 2 and snap["probed"] is False and snap["computed"] is None
    # 会抛的探针（未来重构防范）也被兜住
    def _boom(profile=None):
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(capacity, "available_bytes", _boom)
    capacity.reset_cache()
    assert capacity.capacity_snapshot()["max"] == 2
    # cuda 档同理回 48（档案 ceiling）
    monkeypatch.setenv("BOK_DEPLOY_PROFILE", "cuda")
    capacity.reset_cache()
    assert capacity.capacity_snapshot()["max"] == 48


# ---------------------------------------------------------------------------
# available_bytes 平台解析口径
# ---------------------------------------------------------------------------

_VM_STAT_OUT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               10000.
Pages active:                            200000.
Pages inactive:                           40000.
Pages speculative:                         5000.
Pages wired down:                         50000.
Pages purgeable:                           2000.
"""


def test_mac_vm_stat_parse(monkeypatch):
    """macOS 口径 = page_size × (free + inactive + purgeable)，其余页型不算。"""
    monkeypatch.setattr(
        capacity, "subprocess",
        types.SimpleNamespace(run=lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=_VM_STAT_OUT)),
    )
    assert capacity._vm_stat_available_bytes() == 16384 * (10000 + 40000 + 2000)
    # 探测失败（rc!=0 / 格式坏）→ None（调用方 fail-open）
    monkeypatch.setattr(
        capacity, "subprocess",
        types.SimpleNamespace(run=lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="")),
    )
    assert capacity._vm_stat_available_bytes() is None


def test_linux_meminfo_parse(tmp_path: Path):
    """Linux 口径 = /proc/meminfo MemAvailable。"""
    p = tmp_path / "meminfo"
    p.write_text("MemTotal:       65536000 kB\nMemAvailable:   12345678 kB\n", encoding="utf-8")
    assert capacity._linux_meminfo_available_bytes(str(p)) == 12345678 * 1024
    p.write_text("MemTotal:       65536000 kB\n", encoding="utf-8")
    assert capacity._linux_meminfo_available_bytes(str(p)) is None


def test_cuda_nvidia_parse_and_system_fallback(monkeypatch):
    """cuda 口径 = nvidia-smi memory.free 求和；失败回退系统内存。"""
    monkeypatch.setattr(
        capacity, "subprocess",
        types.SimpleNamespace(run=lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="8192\n8192\n")),
    )
    assert capacity._nvidia_free_bytes() == 16384 * 1024 * 1024
    # nvidia-smi 失败 → available_bytes 回系统内存（由 _system_available_bytes 决定）
    monkeypatch.setattr(
        capacity, "subprocess",
        types.SimpleNamespace(run=lambda *a, **k: types.SimpleNamespace(returncode=127, stdout="")),
    )
    monkeypatch.setattr(capacity, "_system_available_bytes", lambda: 5 * GB)
    assert capacity.available_bytes("cuda") == 5 * GB


# ---------------------------------------------------------------------------
# 409 明细（TestClient + 内存仓）
# ---------------------------------------------------------------------------


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from bok_voice_business_db.repository import InMemoryBusinessRepository

    import control_plane.main as cp_main
    from control_plane.main import app

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    audits: list[tuple] = []
    monkeypatch.setattr(cp_main, "_audit", lambda action, **kw: audits.append((action, kw)))
    client = TestClient(app).__enter__()
    return client, repo, audits


def test_gate_rejects_with_capacity_breakdown(monkeypatch):
    """达动态上限 409：detail 带 profile/floor/computed/ceiling/free_gb；
    审计同款结构字段；被拒通话不落库。"""
    monkeypatch.setenv("BOK_DEPLOY_PROFILE", "mac")
    monkeypatch.setenv("BOK_REQUIRE_TEMPLATE", "0")
    _pin_available(monkeypatch, 64 * GB)  # (64-8)/2.5=22.4 → 22 → clamp 到 ceiling 2
    capacity.reset_cache()
    client, repo, audits = _client_and_repo(monkeypatch)
    try:
        for _ in range(2):
            r = client.post("/api/calls", json={"account_id": "acc-001", "mode": "live"})
            assert r.status_code == 200, r.text
        r = client.post("/api/calls", json={"account_id": "acc-001", "mode": "live"})
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert isinstance(detail, str)
        for frag in ("profile=mac", "floor=1", "computed=22", "ceiling=2", "free_gb=64.0"):
            assert frag in detail, (frag, detail)
        hit = [a for a in audits if a[0] == "call.reject_concurrency"]
        assert hit, audits
        assert hit[0][1]["detail"]["computed"] == 22
        assert hit[0][1]["detail"]["profile"] == "mac"
        assert len(repo.list_calls("")) == 2
    finally:
        client.__exit__(None, None, None)


def test_gate_probe_failure_still_admits_mac_ceiling(monkeypatch):
    """探测失败 fail-open：mac 档仍放行至档案 ceiling=2，绝不 500。"""
    monkeypatch.setenv("BOK_DEPLOY_PROFILE", "mac")
    monkeypatch.setenv("BOK_REQUIRE_TEMPLATE", "0")
    _pin_available(monkeypatch, None)
    capacity.reset_cache()
    client, repo, _audits = _client_and_repo(monkeypatch)
    try:
        for _ in range(2):
            r = client.post("/api/calls", json={"account_id": "acc-001", "mode": "live"})
            assert r.status_code == 200, r.text
        r = client.post("/api/calls", json={"account_id": "acc-001", "mode": "live"})
        assert r.status_code == 409, r.text
    finally:
        client.__exit__(None, None, None)


def test_format_detail_legacy_marker():
    """legacy 档明细打 legacy=1，未探测字段显式 na（可归因）。"""
    snap = {"max": 7, "profile": "mac", "floor": 1, "computed": None,
            "ceiling": 7, "free_gb": None, "legacy": True}
    s = capacity.format_limit_detail(snap)
    assert "legacy=1" in s and "computed=na" in s and "free_gb=na" in s


# ---------------------------------------------------------------------------
# 源级 pin（防回退）
# ---------------------------------------------------------------------------


def test_wiring_source_pins():
    root = Path(__file__).resolve().parents[1]
    main_src = (root / "apps" / "control-plane" / "control_plane" / "main.py").read_text(encoding="utf-8")
    # 建单闸走 capacity 快照 + 明细格式化；静态 env 读取函数已退役（防回退）。
    assert "capacity_snapshot()" in main_src
    assert "format_limit_detail(_limit)" in main_src
    assert "_max_active_calls_env" not in main_src
    # bok CP 面登记三个容量键（prod launchd 封闭 env 面下发点）。
    bok_src = (root / "tools" / "bok.py").read_text(encoding="utf-8")
    for k in ("BOK_DEPLOY_PROFILE", "BOK_MAX_CALLS_FLOOR", "BOK_MAX_CALLS_CEILING"):
        assert f'"{k}"' in bok_src, k
