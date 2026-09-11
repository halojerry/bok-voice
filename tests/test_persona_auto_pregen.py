"""人设保存点自动罐头物化(W3)单测:状态机矩阵 + spawn 契约 + fail-dead。

用户 mandate(2026-09-10):上线新人设没有 QA 罐头/垫话要提醒,并自动触发
全部生成。响应 `tts_pregen.status` = 提醒面;任何失败绝不阻人设保存。
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

import pytest
from fastapi.testclient import TestClient

from control_plane import pregen
from control_plane.main import app


class _FakePopen:
    """记录 spawn;poll() 缺省 None=在跑(单飞判定用)。"""

    def __init__(self, cmd, **kw):
        self.cmd = list(cmd)
        self.kw = kw
        self.pid = -1
        self.rc = None

    def poll(self):
        return self.rc


@pytest.fixture()
def spawns(monkeypatch):
    rec: list[_FakePopen] = []
    monkeypatch.setattr(
        pregen.subprocess, "Popen", lambda cmd, **kw: rec.append(_FakePopen(cmd, **kw)) or rec[-1]
    )
    pregen._PREGEN_PROCS.clear()
    yield rec
    pregen._PREGEN_PROCS.clear()


def test_create_persona_with_voice_queues_pregen(spawns):
    with TestClient(app) as client:
        resp = client.post(
            "/api/personas",
            json={
                "account_id": "acc-001",
                "name": "小罐",
                "language": "cantonese",
                "reference_audio": "Cantonese_GentleLady",
            },
        )
    body = resp.json()
    assert resp.status_code == 200
    assert body["tts_pregen"]["status"] == "queued"
    assert len(spawns) == 1
    cmd = spawns[0].cmd
    for token in ("--greetings", "--fillers", "--qa", "--persona", body["id"]):
        assert token in cmd
    # 子进程经 CP HTTP API 取人设/设置,base_url 必须带对
    assert spawns[0].kw["env"]["BOK_CP_URL"] == "http://testserver"


def test_create_persona_without_voice_alerts_no_voice(spawns):
    with TestClient(app) as client:
        body = client.post(
            "/api/personas", json={"account_id": "acc-001", "name": "無音"}
        ).json()
    assert body["tts_pregen"]["status"] == "no_voice"
    assert "hint" in body["tts_pregen"]
    assert spawns == []


def test_voice_map_any_nonempty_value_counts(spawns):
    with TestClient(app) as client:
        body = client.post(
            "/api/personas",
            json={
                "account_id": "acc-001",
                "name": "映射",
                "reference_audio": json.dumps({"zh": "", "cantonese": "Cantonese_GentleLady"}),
            },
        ).json()
        assert body["tts_pregen"]["status"] == "queued"
        empty = client.post(
            "/api/personas",
            json={
                "account_id": "acc-001",
                "name": "空映射",
                "reference_audio": json.dumps({"zh": "", "en": ""}),
            },
        ).json()
        assert empty["tts_pregen"]["status"] == "no_voice"


def test_update_name_only_is_unchanged(spawns):
    with TestClient(app) as client:
        created = client.post(
            "/api/personas",
            json={"account_id": "acc-001", "name": "小罐", "reference_audio": "v1"},
        ).json()
        assert len(spawns) == 1
        body = client.put(
            f"/api/personas/{created['id']}",
            json={
                "account_id": "acc-001",
                "name": "小罐改名",
                "reference_audio": "v1",
                "language": created.get("language", "zh"),
            },
        ).json()
    assert body["tts_pregen"]["status"] == "unchanged"
    assert len(spawns) == 1  # 没有二次 spawn


def test_update_voice_change_requeues(spawns):
    with TestClient(app) as client:
        created = client.post(
            "/api/personas",
            json={"account_id": "acc-001", "name": "换声", "reference_audio": "v1"},
        ).json()
        spawns[0].rc = 0  # 上一发已跑完
        body = client.put(
            f"/api/personas/{created['id']}",
            json={"account_id": "acc-001", "name": "换声", "reference_audio": "v2"},
        ).json()
    assert body["tts_pregen"]["status"] == "queued"
    assert len(spawns) == 2


def test_single_flight_while_running(spawns):
    with TestClient(app) as client:
        created = client.post(
            "/api/personas",
            json={"account_id": "acc-001", "name": "单飞", "reference_audio": "v1"},
        ).json()
        # 上一发 poll()=None 仍在跑
        body = client.put(
            f"/api/personas/{created['id']}",
            json={"account_id": "acc-001", "name": "单飞", "reference_audio": "v2"},
        ).json()
    assert body["tts_pregen"]["status"] == "already_running"
    assert len(spawns) == 1


def test_kill_switch_disables(spawns, monkeypatch):
    monkeypatch.setenv("BOK_PERSONA_AUTO_PREGEN", "0")
    with TestClient(app) as client:
        body = client.post(
            "/api/personas",
            json={"account_id": "acc-001", "name": "关闸", "reference_audio": "v1"},
        ).json()
    assert body["tts_pregen"]["status"] == "disabled"
    assert spawns == []


def test_spawn_failure_never_breaks_save(spawns, monkeypatch):
    def _boom(cmd, **kw):
        raise OSError("noexec")

    monkeypatch.setattr(pregen.subprocess, "Popen", _boom)
    with TestClient(app) as client:
        resp = client.post(
            "/api/personas",
            json={"account_id": "acc-001", "name": "崩而不阻", "reference_audio": "v1"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"]
    assert body["tts_pregen"]["status"] == "failed"


def test_script_missing_reports(spawns, monkeypatch, tmp_path):
    monkeypatch.setattr(pregen, "_repo_root", lambda: tmp_path)
    with TestClient(app) as client:
        body = client.post(
            "/api/personas",
            json={"account_id": "acc-001", "name": "无脚本", "reference_audio": "v1"},
        ).json()
    assert body["tts_pregen"]["status"] == "script_missing"
    assert spawns == []
