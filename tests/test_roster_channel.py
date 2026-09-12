from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.flow import channel_from_text


def test_channel_from_text():
    assert channel_from_text("我微信是一二三四五六七八") == "wechat"
    assert channel_from_text("加我WeChat好朋友") == "wechat"
    assert channel_from_text("我WhatsApp係六四三二零一一") == "whatsapp"
    assert channel_from_text("冇講渠道，净係报数") == "whatsapp"
    assert channel_from_text("") == "whatsapp"


# ---- CP 端点：captured 带号码 → 自动入册名册 ----

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

from fastapi.testclient import TestClient

from control_plane.main import _repo, app


def _roster_for(call_id: str) -> list[dict]:
    return [e for e in _repo().list_roster(account_id="acc-001") if e.get("call_id") == call_id]


def test_report_whatsapp_captured_upserts_roster():
    """captured 带号码 → roster_entries upsert；channel 优先 agent 上报值。"""
    with TestClient(app) as client:
        created = client.post(
            "/api/calls",
            json={"account_id": "acc-001", "object_id": "obj-1", "persona_id": "p-1", "mode": "simulation"},
        ).json()
        call_id = created["id"]
        # offered(无号码)不入册
        client.post(f"/api/calls/{call_id}/whatsapp", json={"number": ""})
        assert _roster_for(call_id) == []

        r = client.post(
            f"/api/calls/{call_id}/whatsapp",
            json={"number": "6868123456", "channel": "wechat"},
        ).json()
        assert r["whatsapp_status"] == "captured"

        hit = _roster_for(call_id)
        assert len(hit) == 1, hit
        assert hit[0]["channel"] == "wechat"
        assert hit[0]["number"] == "6868123456"


def test_report_whatsapp_channel_defaults_from_object():
    """channel 缺省(空/非法) → 按对象 contact_channel 推断（微信→wechat，否则 whatsapp）。"""
    with TestClient(app) as client:
        obj = client.post(
            "/api/objects",
            params={"account_id": "acc-001"},
            json={"display_name": "微信对象", "contact_channel": "微信"},
        ).json()
        created = client.post(
            "/api/calls",
            json={"account_id": "acc-001", "object_id": obj["id"], "persona_id": "p-1", "mode": "simulation"},
        ).json()
        call_id = created["id"]
        client.post(f"/api/calls/{call_id}/whatsapp", json={"number": "12345678"})
        hit = _roster_for(call_id)
        assert len(hit) == 1, hit
        assert hit[0]["channel"] == "wechat"

        # 非法 channel 同样回退推断，不落怪值
        created2 = client.post(
            "/api/calls",
            json={"account_id": "acc-001", "object_id": obj["id"], "persona_id": "p-1", "mode": "simulation"},
        ).json()
        call_id2 = created2["id"]
        client.post(f"/api/calls/{call_id2}/whatsapp", json={"number": "87654321", "channel": "telegram"})
        hit2 = _roster_for(call_id2)
        assert len(hit2) == 1, hit2
        assert hit2[0]["channel"] == "wechat"

