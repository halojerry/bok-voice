"""doctor 盲区补面（2026-10-02 编排审计第二波 · PR-E item 6）。

两处盲区：
① a_reply 专线（:1237，P2 翻档后是通话回复主脑）doctor 从不做功能探针——
   它 wedge 时通话回复直接灭而 doctor 全绿（此前只探 :1235/:1236）；
② queue_proxy 租约看门狗（lease-timeout forced-reclaim）打点在 llm-proxy.log
   里，doctor 从不做计数面——槽泄漏/断连僵尸的历史证据无人汇总。

约定：urlopen/healthy 全打桩，不碰真栈（照 test_doctor_minimax_probe /
test_health_surface 约定）。
"""

from __future__ import annotations

import inspect
import io
import json
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import bok  # noqa: E402
from _bokpatch import patch_bok  # noqa: E402

_LEASE_LINE = (
    "[llm-queue] lease-timeout forced-reclaim hold_s>60 "
    "active=1 queued(reply=0,bg=0)\n"
)


class _FakeResp(io.BytesIO):
    status = 200


def test_lease_timeout_census(tmp_path: Path) -> None:
    log = tmp_path / "llm-proxy.log"
    log.write_text("boot\n" + _LEASE_LINE * 3 + "other\n", encoding="utf-8")
    assert bok.doctor._doctor_queue_proxy_lease_timeouts(tmp_path) == 3
    assert bok.doctor._doctor_queue_proxy_lease_timeouts(tmp_path / "missing") is None


def test_lease_marker_matches_queue_proxy_source() -> None:
    """计数标记必须与 queue_proxy.py 真打点字面量一致（防字面量漂移假绿）。"""
    src = (ROOT / "services" / "llm-mlx" / "queue_proxy.py").read_text(encoding="utf-8")
    assert "lease-timeout forced-reclaim" in src
    assert bok.doctor._LEASE_TIMEOUT_MARKER == "lease-timeout forced-reclaim"


def test_doctor_source_pins() -> None:
    src = inspect.getsource(bok.doctor.cmd_doctor)
    assert "_doctor_queue_proxy_lease_timeouts(" in src, "doctor 必须汇总租约超时"
    assert "queue_proxy lease_timeouts=" in src
    assert "a_reply" in src, "doctor 必须为 a_reply 专线打功能探针行"


def test_doctor_a_reply_probe_line(monkeypatch, tmp_path: Path, capsys) -> None:
    """端到端（全打桩）：:1237 在听 → a_reply 功能探针行出现且 ok。"""
    model_dir = tmp_path / "settle-model"
    model_dir.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "llm-proxy.log").write_text(_LEASE_LINE * 2, encoding="utf-8")

    patch_bok(monkeypatch, "app_data_dir", lambda: tmp_path)
    patch_bok(monkeypatch, "platform_key", lambda: "mac")
    patch_bok(monkeypatch, "is_packaged", lambda: False)
    patch_bok(monkeypatch, "_warn_memory_posture", lambda fails, packaged: None)
    patch_bok(monkeypatch, "sidecar_python", lambda name: tmp_path / "py")
    patch_bok(monkeypatch, "_embedded_livekit", lambda: None)
    patch_bok(monkeypatch, "bundled_node", lambda: None)
    patch_bok(monkeypatch, "bundled_llama", lambda: None)
    patch_bok(monkeypatch, "_virtual_audio_present", lambda: False)
    patch_bok(monkeypatch, "_doctor_gpu_gate", lambda packaged, fails: None)
    patch_bok(monkeypatch, "_doctor_draft_warning", lambda cur: None)
    patch_bok(monkeypatch, "_model_present", lambda repo: True)
    patch_bok(monkeypatch, "_settle_llm_model", lambda cur: str(model_dir))
    patch_bok(monkeypatch, "_probe_worker", lambda port, timeout=3.0: (True, "ok"))
    patch_bok(monkeypatch, "_provider_health_summary", lambda: None)
    patch_bok(monkeypatch, "_doctor_minimax_tts", lambda data, fails: None)
    # :1237 只在听（1235/1236 等不在），CORE_PORTS 判定全走此桩。
    patch_bok(monkeypatch, "healthy", lambda port: port == 1237)

    def fake_urlopen(arg, timeout=None):
        url = arg if isinstance(arg, str) else arg.full_url
        if url.endswith("/v1/models"):
            return _FakeResp(json.dumps({"data": [{"id": str(model_dir)}]}).encode())
        if url.endswith("/chat/completions"):
            return _FakeResp(json.dumps({"choices": []}).encode())
        raise urllib.error.URLError(f"unexpected url {url}")

    monkeypatch.setattr(bok.urllib.request, "urlopen", fake_urlopen)
    rc = bok.doctor.cmd_doctor()
    assert rc == 0
    out = capsys.readouterr().out
    assert "a_reply 功能探针" in out and "ok " in out
    assert "doctor: queue_proxy lease_timeouts=2" in out
