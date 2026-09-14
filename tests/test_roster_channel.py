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


def test_report_whatsapp_summary_falls_back_to_last_customer_turn():
    """spec §4.3：无 settlement（结算未生成）时 summary 退「末轮客户转写」。

    captured 通常发生在通话进行中，settlement 由收线后异步产出 → 届时摘要恒空、
    名册条目只剩号码。兜底取最后一轮 speaker=customer 的 transcript（截 300 字）。
    """
    with TestClient(app) as client:
        created = client.post(
            "/api/calls",
            json={"account_id": "acc-001", "object_id": "obj-1", "persona_id": "p-1", "mode": "simulation"},
        ).json()
        call_id = created["id"]
        # 无 settlement；两轮客户转写 + 一轮 AI（末轮客户=第二条）
        client.post(f"/api/calls/{call_id}/turns",
                    params={"role": "user", "transcript": "我想問快遞",
                            "speaker": "customer"})
        client.post(f"/api/calls/{call_id}/turns",
                    params={"role": "assistant", "transcript": "好嘅",
                            "speaker": "agent_ai"})
        client.post(f"/api/calls/{call_id}/turns",
                    params={"role": "user", "transcript": "我WhatsApp係六四三二零一一一",
                            "speaker": "customer"})

        assert _repo().get_settlement(call_id) in (None, {}, "")
        r = client.post(f"/api/calls/{call_id}/whatsapp",
                        json={"number": "64320111"}).json()
        assert r["whatsapp_status"] == "captured"

        hit = _roster_for(call_id)
        assert len(hit) == 1, hit
        assert hit[0]["summary"] == "我WhatsApp係六四三二零一一一"


def test_report_whatsapp_summary_prefers_settlement_over_transcript():
    """settlement 有 summary 时优先用它，不被末轮转写兜底覆盖。"""
    with TestClient(app) as client:
        created = client.post(
            "/api/calls",
            json={"account_id": "acc-001", "object_id": "obj-1", "persona_id": "p-1", "mode": "simulation"},
        ).json()
        call_id = created["id"]
        client.post(f"/api/calls/{call_id}/turns",
                    params={"role": "user", "transcript": "末轮客户话",
                            "speaker": "customer"})
        _repo().append_settlement(call_id, {"summary": "结算摘要正文"})

        client.post(f"/api/calls/{call_id}/whatsapp", json={"number": "64320111"})
        hit = _roster_for(call_id)
        assert len(hit) == 1, hit
        assert hit[0]["summary"] == "结算摘要正文"

