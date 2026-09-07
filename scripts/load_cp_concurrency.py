"""CP API 并发压测：独立 CP 实例(:8001)+临时 DB，零污染真实数据。

场景：
  A. 50 并发 × 混合读 200 req（objects/knowledge/search/reports/audit）
  B. 20 并发 × 对象 CRUD 全周期（create→patch→get→delete）
  C. 单 call × 30 并发 turns 写（SQLite 写锁行为）
  D. 50 并发 × /api/token（缺 LiveKit 凭据时应稳定 503 而非 500/挂起）
输出：各场景 p50/p95/max/错误率。
运行：python scripts/load_cp_concurrency.py   （自动拉起/复用 :8001 独立 CP）
"""

from __future__ import annotations

import asyncio
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("LOAD_CP_BASE", "http://127.0.0.1:8001")
ACC = "acc-001"


def _start_cp() -> subprocess.Popen | None:
    try:
        r = httpx.get(f"{BASE}/health", timeout=2)
        if r.status_code == 200:
            print("[load] reusing CP at :8001")
            return None
    except Exception:
        pass  # 未起 → 走启动分支
    db = tempfile.mktemp(suffix=".db")
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db}"
    env["PYTHONPATH"] = (
        f"{ROOT}/apps/agent:{ROOT}/apps/control-plane:{ROOT}/packages/business-db:"
        f"{ROOT}/packages/core:{ROOT}/packages/knowledge:{ROOT}/packages/observability"
    )
    py = str(ROOT / "runtime" / "python" / "bin" / "python3")
    if not Path(py).exists():
        py = sys.executable
    proc = subprocess.Popen(
        [py, "-m", "uvicorn", "control_plane.main:app", "--host", "127.0.0.1", "--port", "8001"],
        cwd=str(ROOT / "apps" / "control-plane"),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        time.sleep(0.5)
        try:
            if httpx.get(f"{BASE}/health", timeout=2).status_code == 200:
                print(f"[load] CP started pid={proc.pid} db={db}")
                return proc
        except Exception:
            continue
    proc.kill()
    raise SystemExit("[load] CP failed to start")


def _percentile(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    if not xs:
        return 0.0
    return xs[min(int(len(xs) * p), len(xs) - 1)]


async def _fire(client: httpx.AsyncClient, method: str, url: str, **kw) -> tuple[float, int]:
    t0 = time.perf_counter()
    try:
        r = await client.request(method, url, **kw)
        return (time.perf_counter() - t0) * 1000, r.status_code
    except Exception:
        return (time.perf_counter() - t0) * 1000, 0


async def _run(label: str, tasks_coro) -> dict:
    t0 = time.perf_counter()
    results = await asyncio.gather(*tasks_coro)
    wall = (time.perf_counter() - t0) * 1000
    lat = [r[0] for r in results]
    codes = [r[1] for r in results]
    ok = [c for c in codes if 200 <= c < 500 and c != 0]
    err = [c for c in codes if c >= 500 or c == 0]
    summary = {
        "label": label,
        "n": len(codes),
        "p50_ms": round(_percentile(lat, 0.5), 1),
        "p95_ms": round(_percentile(lat, 0.95), 1),
        "max_ms": round(max(lat), 1),
        "ok": len(ok),
        "err5xx": len(err),
        "codes": sorted(set(codes)),
        "wall_ms": round(wall, 1),
    }
    print(
        f"LOAD {summary['label']:<28} n={summary['n']} p50={summary['p50_ms']}ms "
        f"p95={summary['p95_ms']}ms max={summary['max_ms']}ms ok={summary['ok']} "
        f"err5xx={summary['err5xx']} codes={summary['codes']} wall={summary['wall_ms']}ms",
        flush=True,
    )
    return summary


async def scenario_a(client):
    paths = [
        ("GET", f"/api/objects?account_id={ACC}"),
        ("GET", f"/api/knowledge?account_id={ACC}"),
        ("GET", f"/api/knowledge/search?account_id={ACC}&query=集运"),
        ("GET", f"/api/reports/summary?account_id={ACC}"),
        ("GET", f"/api/audit?account_id={ACC}"),
    ]
    coros = []
    for i in range(200):
        m, u = paths[i % len(paths)]
        coros.append(_fire(client, m, u))
    return await _run("A.mixed-read-200", coros)


async def scenario_b(client):
    async def one_full(i):
        r = await client.post(
            f"/api/objects?account_id={ACC}",
            json={"display_name": f"LOAD-{i}-{time.time()}", "role_template": "buyer", "language": "zh"},
        )
        if r.status_code != 200:
            return (0.0, r.status_code)
        oid = r.json()["id"]
        lat2, code2 = await _fire(
            client, "PATCH", f"/api/objects/{oid}?account_id={ACC}",
            json={"display_name": f"LOAD-{i}-patched"},
        )
        lat3, code3 = await _fire(client, "DELETE", f"/api/objects/{oid}?account_id={ACC}")
        return (lat2 + lat3, 200 if (code2 == 200 and code3 == 200) else code2 or code3)

    results = await _run("B.object-crud-20", [one_full(i) for i in range(20)])
    return results


async def scenario_c(client):
    call = (
        await client.post(
            f"/api/calls",
            json={"account_id": ACC, "mode": "live", "direction": "webrtc", "language": "zh"},
        )
    ).json()
    cid = call["id"]
    coros = [
        _fire(
            client, "POST", f"/api/calls/{cid}/turns",
            params={"role": "customer", "transcript": f"并发轮 {i}"},
        )
        for i in range(30)
    ]
    out = await _run("C.concurrent-turns-30", coros)
    turns = (await client.get(f"/api/calls/{cid}/turns")).json()
    n = len(turns) if isinstance(turns, list) else len(turns.get("turns", []))
    print(f"LOAD C.verifier turns_in_db={n}/30", flush=True)
    out["turns_in_db"] = n
    await _fire(client, "DELETE", f"/api/calls/{cid}")
    return out


async def scenario_d(client):
    coros = [
        _fire(client, "POST", "/api/token", json={"account_id": ACC, "call_id": f"load-{i}"})
        for i in range(50)
    ]
    return await _run("D.token-50-nocreds", coros)


async def main() -> None:
    proc = _start_cp()
    try:
        async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
            out = {
                "A": await scenario_a(client),
                "B": await scenario_b(client),
                "C": await scenario_c(client),
                "D": await scenario_d(client),
            }
        bad = []
        if out["A"]["err5xx"]:
            bad.append("A has 5xx")
        if out["B"]["err5xx"]:
            bad.append("B has 5xx")
        if out["C"]["err5xx"] or out["C"].get("turns_in_db", 0) < 30:
            bad.append("C turns lost/5xx")
        if out["D"].get("codes") != [503]:
            bad.append(f"D expected all-503 got {out['D'].get('codes')} (500/0=真失败)")
        print(f"CP_LOAD {'PASS' if not bad else 'FAIL'} {'; '.join(bad)}", flush=True)
        if bad:
            raise SystemExit(1)
    finally:
        if proc:
            proc.kill()


if __name__ == "__main__":
    asyncio.run(main())
