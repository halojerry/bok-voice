"""MiniMax 云端克隆音色（路线 B）settings 面：minimax_clones_json 防蒸发回归。

克隆清单由 /api/tts/minimax-voices 直写 settings.tts.minimax_clones_json（绕过
ProviderSettings 模型）；PUT /api/settings 走 req.tts.model_dump() 重建整段——
字段未在模型上声明时该键被静默蒸发 = 已克隆音色清单全丢（2026-09-18
wt-saas-delivery 会话发现，落地前 main 实锤）。本文件钉死：字段声明后
GET→PUT 真实保存链路克隆清单原样保留。
"""

from __future__ import annotations

import json

from bok_voice_business_db.repository import InMemoryBusinessRepository

CLONES = [{"voice_id": "bokcloneabc12345", "label": "小美", "sample_lang": "zh",
           "created_at": "2026-09-18T01:00:00", "activated": False}]


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    return TestClient(app), repo


def _seed_clones(repo) -> None:
    s = repo.get_settings()
    s["tts"] = {**s["tts"], "minimax_clones_json": json.dumps(CLONES, ensure_ascii=False)}
    repo.save_settings(s)


def test_settings_put_preserves_minimax_clones(monkeypatch):
    """web 设置页真实保存形状：GET 回显整段 tts 原样 PUT 回，克隆清单不丢。"""
    client, repo = _client_and_repo(monkeypatch)
    _seed_clones(repo)

    got = client.get("/api/settings").json()["tts"]
    assert json.loads(got["minimax_clones_json"])[0]["voice_id"] == "bokcloneabc12345"

    resp = client.put("/api/settings", json={"tts": got})
    assert resp.status_code == 200
    after = json.loads(repo.get_settings()["tts"]["minimax_clones_json"])
    assert after[0]["voice_id"] == "bokcloneabc12345"
    assert after[0]["label"] == "小美"


def test_clone_list_endpoint_reads_settings_blob(monkeypatch):
    """GET /api/tts/minimax-voices 读 settings blob（本地面板数据，非云调用）。"""
    client, repo = _client_and_repo(monkeypatch)
    _seed_clones(repo)

    resp = client.get("/api/tts/minimax-voices")
    assert resp.status_code == 200
    rows = resp.json()
    assert rows[0]["voice_id"] == "bokcloneabc12345"


def test_clone_list_endpoint_empty_default(monkeypatch):
    """未克隆过 → 空数组（非 404/500），web 下拉静默降级。"""
    client, repo = _client_and_repo(monkeypatch)
    resp = client.get("/api/tts/minimax-voices")
    assert resp.status_code == 200
    assert resp.json() == []
