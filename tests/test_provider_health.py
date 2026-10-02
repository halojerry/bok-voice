"""M-11（fix-wave-3，task-13 F-M1）：MiniMax 云 TTS 配额死可见性面。

task-13 坐实：配额风暴（2056×56）期间全部健康面全绿、无任何程序消费 agent.log
打点族——「配额死 N=∞ 不可见，唯一探测是人工 grep」。修法（只做可见性）：

1. 纯函数扫描器 `bok_voice_obs/provider_health.py`（stdlib-only，双端共用）：
   tail 读 worker 日志，从结构化行（`ts`/`timestamp`）取时基给裸 print 打点行
   （MINIMAX_TTS_*/MINIMAX_BIDI_RATE_LIMIT 族）配时，按窗口聚合 2056（配额死）
   与 1002/1039/2205（限流族）计数 + 最近命中时间。
2. bok.py `status`/`doctor` 出 cloud-tts 行（按文件路径加载模块——包 __init__
   链 starlette，编排器保持零第三方依赖）。
3. CP `GET /api/stats/provider-health`（reports 页键闸，同 llm-gaps/qa-drift 家族）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import pytest

from bok_voice_obs.provider_health import scan_provider_health  # noqa: E402

_NOW = 1_800_000_000.0  # 固定 now（epoch 秒）
_TS_FMT = "%Y-%m-%dT%H:%M:%S+00:00"


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(_TS_FMT)


def _structured_ts(epoch: float) -> str:
    return json.dumps({"ts": _iso(epoch), "level": "INFO", "message": "heartbeat"})


def _write(log_dir: Path, name: str, lines: list[str]) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture()
def log_dir(tmp_path):
    return tmp_path / "logs"


def test_missing_log_dir_is_clean_not_error(log_dir):
    out = scan_provider_health(log_dir, window_s=300.0, now=_NOW)
    assert out["available"] is False
    assert out["degraded"] is False
    assert out["quota_2056"]["count"] == 0


def test_quota_2056_counted_with_structured_line_timebase(log_dir):
    _write(log_dir, "agent.log", [
        _structured_ts(_NOW - 30),
        "MINIMAX_TTS_RETRY 1 RuntimeError(\"minimax empty audio: {'status_code': 2056, 'status_msg': '已达到 Token Plan 用量上限'}\")",
        "MINIMAX_TTS_BIDI_STATUS 2056 {'status_code': 2056}",
        _structured_ts(_NOW - 10),
        "MINIMAX_BIDI_RATE_LIMIT status=1002 event=task_failed streak=1",
    ])
    out = scan_provider_health(log_dir, window_s=300.0, now=_NOW)
    assert out["available"] is True
    assert out["degraded"] is True
    assert out["quota_2056"]["count"] == 2
    assert out["quota_2056"]["last_hit"] == _iso(_NOW - 30)
    assert out["rate_limit"]["count"] == 1
    assert out["rate_limit"]["statuses"] == {"2056": 2, "1002": 1}


def test_hits_outside_window_excluded_from_count_kept_in_last_hit(log_dir):
    _write(log_dir, "agent.log", [
        _structured_ts(_NOW - 3600),  # 1h 前（窗外）
        "MINIMAX_TTS_ERROR RuntimeError(\"minimax empty audio: {'status_code': 2056}\")",
        _structured_ts(_NOW - 20),
        "MINIMAX_TTS_BIDI_STATUS 2056 {'status_code': 2056}",
    ])
    out = scan_provider_health(log_dir, window_s=300.0, now=_NOW)
    assert out["quota_2056"]["count"] == 1  # 只数窗内
    assert out["quota_2056"]["last_hit"] == _iso(_NOW - 20)


def test_interp_logs_scanned_too(log_dir):
    _write(log_dir, "interp-fwd.log", [
        _structured_ts(_NOW - 60),
        "MINIMAX_BIDI_RATE_LIMIT status=2205 event=task_failed streak=2",
    ])
    out = scan_provider_health(log_dir, window_s=300.0, now=_NOW)
    assert out["degraded"] is True
    assert out["rate_limit"]["count"] == 1


def test_undated_markers_not_counted_in_window(log_dir):
    """扫描窗起点之前的裸标记行（无前导结构化行可配时）不入窗内计数。"""
    _write(log_dir, "agent.log", [
        "MINIMAX_TTS_BIDI_STATUS 2056 {'status_code': 2056}",  # 无时基
        _structured_ts(_NOW - 5),
    ])
    out = scan_provider_health(log_dir, window_s=300.0, now=_NOW)
    assert out["quota_2056"]["count"] == 0
    assert out["undated"] == 1


def test_clean_log_reports_healthy(log_dir):
    _write(log_dir, "agent.log", [
        _structured_ts(_NOW - 5),
        "AGENT_METRICS tts ttfb=644ms audio=1.20s",
        "[call-x] greeting_playout_done",
    ])
    out = scan_provider_health(log_dir, window_s=300.0, now=_NOW)
    assert out["degraded"] is False
    assert out["quota_2056"]["count"] == 0
    assert out["rate_limit"]["count"] == 0


def test_tail_bytes_bound_large_file(log_dir):
    """tail 有界读：大文件只扫尾部（agent.log 实盘 30MB+，全读=状态命令卡顿）。"""
    p = log_dir / "agent.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    filler = (_structured_ts(_NOW - 4000) + "\n" + "x" * 100 + "\n") * 20000  # ~4MB+
    p.write_text(filler + _structured_ts(_NOW - 5) + "\n"
                 "MINIMAX_TTS_BIDI_STATUS 2056 {'status_code': 2056}\n", encoding="utf-8")
    out = scan_provider_health(log_dir, window_s=300.0, now=_NOW, tail_bytes=1_000_000)
    # 尾部 1MB 内：结构与标记行都在 → 命中
    assert out["quota_2056"]["count"] == 1


def test_bok_status_and_doctor_use_shared_scanner(tmp_path, monkeypatch, capsys):
    """bok.py 出 cloud-tts 行：按文件路径加载共享模块（不触发包 __init__）。"""
    import bok

    logs = tmp_path / "logs"
    _write(logs, "agent.log", [
        _structured_ts(_NOW - 30),
        "MINIMAX_TTS_ERROR RuntimeError(\"minimax empty audio: {'status_code': 2056}\")",
    ])
    summary = bok._provider_health_summary(log_dir=logs, now=_NOW)
    assert summary is not None
    assert summary["degraded"] is True

    # fail 消息生成器（doctor 用）：配额死进 fails、干净不出行
    fails = bok._provider_health_fails(summary)
    assert len(fails) == 1 and "2056" in fails[0]
    clean = scan_provider_health(tmp_path / "empty-logs", window_s=300.0, now=_NOW)
    assert bok._provider_health_fails(clean) == []


def test_bok_provider_health_summary_none_when_module_missing(tmp_path, monkeypatch):
    """模块文件缺失（半打包形态）→ None，status/doctor 打 n/a 不炸。"""
    import bok

    calls = []
    real_exists = Path.exists

    def fake_exists(self):
        if self.name == "provider_health.py":
            calls.append(self)
            return False
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)
    assert bok._provider_health_summary(log_dir=tmp_path) is None
    assert calls


def test_cp_provider_health_endpoint(monkeypatch):
    """CP /api/stats/provider-health（reports 页键闸）出扫描结果。"""
    from fastapi.testclient import TestClient

    import control_plane.main as cp_main
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)

    def fake_scan(log_dir, window_s=300.0, now=None, **kw):
        return {"available": True, "degraded": True, "window_s": window_s,
                "quota_2056": {"count": 3, "last_hit": "ts", "undated": 0},
                "rate_limit": {"count": 0, "last_hit": None, "statuses": {}},
                "scanned": {}}

    monkeypatch.setattr(cp_main, "scan_provider_health", fake_scan)
    client = TestClient(cp_main.app)
    r = client.get("/api/stats/provider-health")
    assert r.status_code == 200
    body = r.json()
    assert body["degraded"] is True and body["quota_2056"]["count"] == 3
