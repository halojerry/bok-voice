from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

from bok_voice_business_db.repository import InMemoryBusinessRepository


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    return TestClient(app), repo


def _create_call_via(repo, obj) -> dict:
    """照 test_dial_result_api 同款：直接用 repo.create_call(manifest) 造一通。"""
    from bok_voice_core.types import CallMode, SessionManifest

    return repo.create_call(
        SessionManifest(
            session_id=f"call-{obj['id']}",
            account_id="acc-001",
            object_id=obj["id"],
            persona_id="",
            mode=CallMode.LIVE,
            direction="outbound",
            language="zh",
            providers={},
        )
    )


def _campaign_body(obj, **over) -> dict:
    body = {
        "name": "波次一",
        "object_ids": [obj["id"]],
        "language": "zh",
    }
    body.update(over)
    return body


def test_create_and_detail_defaults(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    r = client.post("/api/campaigns", json=_campaign_body(obj))
    assert r.status_code == 200
    camp = r.json()
    assert camp["status"] == "draft"
    assert camp["id"] and camp["account_id"] == "acc-001"
    assert camp["language"] == "zh" and camp["gap_seconds"] == 5
    assert camp["template_id"] == "" and camp["persona_id"] == ""
    assert camp["name"] == "波次一"


def test_create_requires_object_ids(monkeypatch):
    client, _ = _client_and_repo(monkeypatch)
    r = client.post("/api/campaigns", json={"name": "空波次", "language": "zh"})
    assert r.status_code == 400


def test_create_filters_scenario_whitelist(monkeypatch):
    """scenarios 值只收 answer/no_answer/reject/hangup_mid，其余（含拼错）丢弃。"""
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    r = client.post(
        "/api/campaigns",
        json=_campaign_body(
            obj,
            scenarios={obj["id"]: "no_answer"},
        ),
    )
    assert r.status_code == 200
    assert repo.list_items(r.json()["id"])[0]["scenario"] == "no_answer"

    obj2 = repo.create_object("acc-001", {"display_name": "B", "phone": "+85222222222"})
    r2 = client.post(
        "/api/campaigns",
        json=_campaign_body(obj2, scenarios={obj2["id"]: "bogus"}),
    )
    assert r2.status_code == 200
    assert repo.list_items(r2.json()["id"])[0]["scenario"] == ""


def test_lifecycle_transitions(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    cid = client.post("/api/campaigns", json=_campaign_body(obj)).json()["id"]

    r = client.post(f"/api/campaigns/{cid}/start")
    assert r.status_code == 200 and r.json()["status"] == "running"
    r = client.post(f"/api/campaigns/{cid}/pause")
    assert r.status_code == 200 and r.json()["status"] == "paused"
    r = client.post(f"/api/campaigns/{cid}/start")
    assert r.json()["status"] == "running"
    r = client.post(f"/api/campaigns/{cid}/stop")
    assert r.status_code == 200 and r.json()["status"] == "stopped"
    # stopped 是终态：不可再 start/pause
    assert client.post(f"/api/campaigns/{cid}/start").status_code == 409
    assert client.post(f"/api/campaigns/{cid}/pause").status_code == 409


def test_lifecycle_illegal_transitions_are_409(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    cid = client.post("/api/campaigns", json=_campaign_body(obj)).json()["id"]
    # draft 不能 pause（未开始）
    assert client.post(f"/api/campaigns/{cid}/pause").status_code == 409
    # draft 可以 stop
    assert client.post(f"/api/campaigns/{cid}/stop").status_code == 200


def test_lifecycle_missing_campaign_is_404(monkeypatch):
    client, _ = _client_and_repo(monkeypatch)
    for verb in ("start", "pause", "stop"):
        assert client.post(f"/api/campaigns/camp-nope/{verb}").status_code == 404


def test_progress_counts_and_answered(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    objs = [
        repo.create_object("acc-001", {"display_name": f"A{i}", "phone": f"+852111111{i}"})
        for i in range(4)
    ]
    cid = client.post(
        "/api/campaigns", json=_campaign_body(objs[0], object_ids=[o["id"] for o in objs])
    ).json()["id"]
    items = repo.list_items(cid)
    repo.update_item(items[0]["id"], status="done")
    repo.update_item(items[1]["id"], status="no_answer")
    repo.update_item(items[2]["id"], status="rejected")
    # items[3] 保持 pending

    detail = client.get(f"/api/campaigns/{cid}").json()
    p = detail["progress"]
    assert p["total"] == 4
    assert (p["pending"], p["done"], p["no_answer"], p["rejected"]) == (1, 1, 1, 1)
    assert p["dialing"] == p["in_call"] == p["failed"] == p["skipped"] == 0
    assert p["answered"] == 3  # done + no_answer + rejected
    assert len(detail["items"]) == 4

    rows = client.get("/api/campaigns").json()
    row = next(c for c in rows if c["id"] == cid)
    assert row["progress"] == p


def test_list_filters_account_and_detail_404(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    cid = client.post("/api/campaigns", json=_campaign_body(obj)).json()["id"]
    assert [c["id"] for c in client.get("/api/campaigns").json()] == [cid]
    assert client.get("/api/campaigns", params={"account_id": "acc-002"}).json() == []
    assert client.get("/api/campaigns/camp-nope").status_code == 404


def test_campaign_crud_and_dial_result(monkeypatch):
    """Brief 主链：建战役 → 启停 → 详情/列表 → dial-result 联动 item 状态。"""
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    r = client.post("/api/campaigns", json=_campaign_body(obj))
    assert r.status_code == 200
    cid = r.json()["id"]
    assert r.json()["status"] == "draft"
    r = client.post(f"/api/campaigns/{cid}/start")
    assert r.json()["status"] == "running"
    r = client.post(f"/api/campaigns/{cid}/pause")
    assert r.json()["status"] == "paused"
    detail = client.get(f"/api/campaigns/{cid}").json()
    assert detail["progress"]["total"] == 1 and detail["items"][0]["status"] == "pending"
    assert client.get("/api/campaigns").json()[0]["id"] == cid

    # dial-result 联动：answered → item in_call
    call = _create_call_via(repo, obj)
    repo.update_item(detail["items"][0]["id"], status="dialing", call_id=call["id"])
    r = client.post(f"/api/calls/{call['id']}/dial-result", json={"status": "answered"})
    assert r.status_code == 200
    assert repo.get_item(detail["items"][0]["id"])["status"] == "in_call"
    r = client.post(f"/api/calls/{call['id']}/dial-result", json={"status": "no_answer"})
    assert r.status_code == 200
    assert repo.get_item(detail["items"][0]["id"])["status"] == "no_answer"


def test_create_campaign_narrowband_flag_rides_scripts_json(monkeypatch):
    """窄带档（8kHz 重验测试床）：`narrowband: true` → scripts_json 保留键，缺省不发。"""
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    cid = client.post("/api/campaigns", json=_campaign_body(obj)).json()["id"]
    assert repo.get_campaign_scripts(cid) == {}

    obj2 = repo.create_object("acc-001", {"display_name": "B", "phone": "+85222222222"})
    cid2 = client.post(
        "/api/campaigns", json=_campaign_body(obj2, narrowband=True)
    ).json()["id"]
    scripts = repo.get_campaign_scripts(cid2)
    assert scripts["__narrowband__"] is True
    # 假值（false/缺省）不污染 scripts_json——旧战役行为零变化。
    obj3 = repo.create_object("acc-001", {"display_name": "C", "phone": "+85233333333"})
    cid3 = client.post(
        "/api/campaigns", json=_campaign_body(obj3, narrowband=False)
    ).json()["id"]
    assert repo.get_campaign_scripts(cid3) == {}


def test_dial_result_links_all_failure_states(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    cid = client.post("/api/campaigns", json=_campaign_body(obj)).json()["id"]
    item_id = repo.list_items(cid)[0]["id"]

    for status in ("no_answer", "rejected", "failed"):
        call = _create_call_via(repo, obj)
        repo.update_call(call["id"], status="ringing")
        repo.update_item(item_id, status="dialing", call_id=call["id"], last_error="")
        r = client.post(
            f"/api/calls/{call['id']}/dial-result",
            json={"status": status, "detail": f"edge {status}"},
        )
        assert r.status_code == 200, status
        item = repo.get_item(item_id)
        assert item["status"] == status, status
        assert "edge" in item["last_error"], status


def test_dial_result_ignores_items_outside_dialing(monkeypatch):
    """不在 dialing/in_call 的 item 不被 dial-result 覆写（pending 不得跳到终态）。"""
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    cid = client.post("/api/campaigns", json=_campaign_body(obj)).json()["id"]
    item_id = repo.list_items(cid)[0]["id"]
    call = _create_call_via(repo, obj)
    repo.update_item(item_id, call_id=call["id"])  # 仍 pending

    r = client.post(f"/api/calls/{call['id']}/dial-result", json={"status": "no_answer"})
    assert r.status_code == 200
    assert repo.get_item(item_id)["status"] == "pending"


def test_dial_result_without_campaign_item_is_noop(monkeypatch):
    """普通通话（无任何 item 指向它）上报 dial-result 照常改通话，不报错。"""
    from bok_voice_core.types import CallMode, SessionManifest

    client, repo = _client_and_repo(monkeypatch)
    call = repo.create_call(
        SessionManifest(
            session_id="call-plain", account_id="acc-001", object_id="",
            persona_id="", mode=CallMode.LIVE, direction="outbound",
            language="zh", providers={},
        )
    )
    r = client.post(f"/api/calls/{call['id']}/dial-result", json={"status": "no_answer"})
    assert r.status_code == 200
    assert repo.get_call(call["id"])["disposition"] == "no_answer"


def test_campaign_audit_events(monkeypatch):
    """全链审计：create/start/pause/stop 各自落一条 audit 事件。"""
    from bok_voice_obs.audit import AuditStore, audit_store

    events: list[str] = []
    original = audit_store()
    monkeypatch.setattr(
        "bok_voice_obs.audit._STORE",
        AuditStore(original.directory, tap=lambda e: events.append(e.action)),
    )

    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    cid = client.post("/api/campaigns", json=_campaign_body(obj)).json()["id"]
    client.post(f"/api/campaigns/{cid}/start")
    client.post(f"/api/campaigns/{cid}/pause")
    client.post(f"/api/campaigns/{cid}/stop")

    for expected in ("campaign.create", "campaign.running", "campaign.paused",
                     "campaign.stopped"):
        assert expected in events, expected


def test_campaign_sql_backend_parity(monkeypatch):
    """SQL 后端同契约：create/list/detail/transition 全链在 SQL 仓可用。"""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from bok_voice_business_db import models
    from bok_voice_business_db.repository import SqlAlchemyBusinessRepository

    from control_plane.main import app

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.create_all(engine)
    repo = SqlAlchemyBusinessRepository(
        sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    )
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    client = TestClient(app)

    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    r = client.post("/api/campaigns", json=_campaign_body(obj))
    assert r.status_code == 200
    cid = r.json()["id"]
    assert r.json()["status"] == "draft"
    assert client.post(f"/api/campaigns/{cid}/start").json()["status"] == "running"
    detail = client.get(f"/api/campaigns/{cid}").json()
    assert detail["progress"]["total"] == 1
    assert detail["items"][0]["status"] == "pending"
    assert client.get("/api/campaigns").json()[0]["id"] == cid

    call = _create_call_via(repo, obj)
    repo.update_item(detail["items"][0]["id"], status="dialing", call_id=call["id"])
    client.post(f"/api/calls/{call['id']}/dial-result", json={"status": "answered"})
    assert repo.get_item(detail["items"][0]["id"])["status"] == "in_call"


def test_delete_campaign_removes_items_and_audits(monkeypatch):
    """删战役：连带名单项；审计 campaign.delete；再删 404。"""
    from bok_voice_obs.audit import AuditStore, audit_store

    events: list[str] = []
    original = audit_store()
    monkeypatch.setattr(
        "bok_voice_obs.audit._STORE",
        AuditStore(original.directory, tap=lambda e: events.append(e.action)),
    )
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    cid = client.post("/api/campaigns", json=_campaign_body(obj)).json()["id"]
    assert len(repo.list_items(cid)) == 1

    r = client.delete(f"/api/campaigns/{cid}")
    assert r.status_code == 200
    assert r.json() == {"campaign_id": cid, "deleted": True, "items_removed": 1}
    assert repo.get_campaign(cid) is None
    assert repo.list_items(cid) == []
    assert "campaign.delete" in events
    assert client.delete(f"/api/campaigns/{cid}").status_code == 404


def test_delete_running_campaign_is_409(monkeypatch):
    """在跑波次拒删——先停止再删，防误删名单。"""
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    cid = client.post("/api/campaigns", json=_campaign_body(obj)).json()["id"]
    client.post(f"/api/campaigns/{cid}/start")
    assert client.delete(f"/api/campaigns/{cid}").status_code == 409
    assert repo.get_campaign(cid)["status"] == "running"


def test_create_call_explicit_template_overrides_object_binding(monkeypatch):
    """显式话术（战役/话务员自选）建单即快照；缺省仍回落对象卡绑定。"""
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "template_id": "tpl-object"})
    explicit = client.post("/api/calls", json={
        "account_id": "acc-001", "object_id": obj["id"], "template_id": "tpl-explicit",
    }).json()
    assert explicit["template_id"] == "tpl-explicit"
    fallback = client.post("/api/calls", json={
        "account_id": "acc-001", "object_id": obj["id"],
    }).json()
    assert fallback["template_id"] == "tpl-object"


def _create_campaign(client, repo, **over) -> dict:
    """最小建波 helper：单对象 + `_campaign_body` 透传覆盖字段。"""
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    return client.post("/api/campaigns", json=_campaign_body(obj, **over)).json()


def _create_and_start(client, repo, **over) -> dict:
    camp = _create_campaign(client, repo, **over)
    r = client.post(f"/api/campaigns/{camp['id']}/start")
    assert r.status_code == 200
    return camp


def test_create_campaign_with_scheduling_payload(monkeypatch):
    """调度三字段（2026-09-17）：时段窗归一（非法窗丢/超 3 截断）、并发 0=不限、
    重拨策略 on 白名单剔除。"""
    client, repo = _client_and_repo(monkeypatch)
    body = _campaign_body(
        repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"}),
        call_windows=[{"days": [1, 2, 3, 4, 5], "start": "08:00", "end": "18:00"},
                      {"days": [6], "start": "09:00", "end": "12:00"},
                      {"days": [7], "start": "bad", "end": "x"},
                      {"days": [1], "start": "10:00", "end": "11:00"}],
        max_concurrency=0,
        redispatch={"max_attempts": 2, "interval_minutes": 30,
                    "on": ["no_answer", "junk"]},
    )
    camp = client.post("/api/campaigns", json=body).json()
    assert len(camp["call_windows"]) == 3  # 非法窗丢、超 3 截断
    assert camp["max_concurrency"] == 0
    assert camp["redispatch"]["on"] == ["no_answer"]  # 非法结果名剔除


def test_create_campaign_explicit_null_max_concurrency_defaults_to_one(monkeypatch):
    """T1-M1 防呆：显式 `"max_concurrency": null` 不落 0（不限），按缺省 1（串行）。"""
    client, repo = _client_and_repo(monkeypatch)
    camp = _create_campaign(client, repo, max_concurrency=None)
    assert camp["max_concurrency"] == 1


def test_update_campaign_rejects_running(monkeypatch):
    """运行中锁定（对齐竞品语义）：running 时段/并发不可改，先 pause。"""
    client, repo = _client_and_repo(monkeypatch)
    camp = _create_and_start(client, repo)
    resp = client.put(f"/api/campaigns/{camp['id']}", json={"gap_seconds": 9})
    assert resp.status_code == 409
    assert repo.get_campaign(camp["id"])["gap_seconds"] == 5  # 拒改未落库


def test_update_campaign_edits_paused(monkeypatch):
    """draft/paused 可改：调度字段+基础字段白名单落库，响应回解析后形状。"""
    client, repo = _client_and_repo(monkeypatch)
    camp = _create_campaign(client, repo)
    resp = client.put(
        f"/api/campaigns/{camp['id']}",
        json={"max_concurrency": 3,
              "call_windows": [{"days": [1], "start": "08:00", "end": "12:00"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["max_concurrency"] == 3
    assert resp.json()["call_windows"] == [{"days": [1], "start": "08:00", "end": "12:00"}]
    row = repo.get_campaign(camp["id"])
    assert row["max_concurrency"] == 3


def test_update_campaign_missing_is_404_and_audits(monkeypatch):
    """PUT 404 同既有端点；成功编辑落 campaign.update 审计。"""
    from bok_voice_obs.audit import AuditStore, audit_store

    events: list[str] = []
    original = audit_store()
    monkeypatch.setattr(
        "bok_voice_obs.audit._STORE",
        AuditStore(original.directory, tap=lambda e: events.append(e.action)),
    )
    client, repo = _client_and_repo(monkeypatch)
    assert client.put("/api/campaigns/camp-nope", json={"gap_seconds": 9}).status_code == 404
    camp = _create_campaign(client, repo)
    resp = client.put(f"/api/campaigns/{camp['id']}", json={"name": "改名波次"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "改名波次"
    assert "campaign.update" in events


# ---- 全局外呼时段窗 settings.campaign 段（2026-09-17 T3b）----

def test_settings_campaign_section_roundtrip_and_tick_gating(monkeypatch):
    """/api/settings 收 campaign 段：归一落库、GET 回显、不传保留既有；
    保存的全局窗对 campaign_tick 生效（窗内起拨/窗外不起拨）。"""
    import asyncio
    from datetime import datetime, timezone

    from control_plane.campaign import campaign_tick

    client, repo = _client_and_repo(monkeypatch)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # 窗内窗（覆盖 now）+ 一条垃圾窗：归一落库应剔除垃圾、保留正常窗。
    # end 用同小时 :59（恒 start<end）：旧 (hour+1)%24 在 23 点档变跨零点窗
    # 被 parse_call_windows 静默丢弃，断言确定性红（T3b-Important flake 修复）。
    inside = [{"days": [now.isoweekday()], "start": f"{now.hour:02d}:00",
               "end": f"{now.hour:02d}:59"},
              {"days": [1], "start": "08:00", "end": "99:99"}]
    resp = client.put("/api/settings", json={"campaign": {"call_windows": inside}})
    assert resp.status_code == 200
    assert resp.json()["campaign"]["call_windows"] == [inside[0]]
    # GET 照常回显 campaign 段
    assert client.get("/api/settings").json()["campaign"]["call_windows"] == [inside[0]]

    # 行为：全局窗覆盖 now、任务窗空（不限）→ tick 起拨
    dispatched: list[str] = []

    async def fake_dispatch(room: str, metadata: str) -> None:
        dispatched.append(room)
        repo.update_call(room, status="ended", disposition="no_answer")

    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    camp = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                                language="zh", gap_seconds=0, object_ids=[obj["id"]])
    repo.update_campaign(camp["id"], status="running")
    out = asyncio.run(campaign_tick(repo, dispatcher=fake_dispatch, now=now))
    assert out["started"] == 1

    # 换窗外窗（days=明天，任一时刻都不命中今天）→ 新战役 tick 不起拨
    tomorrow = (now.isoweekday() % 7) + 1
    outside = [{"days": [tomorrow], "start": "00:00", "end": "23:59"}]
    resp2 = client.put("/api/settings", json={"campaign": {"call_windows": outside}})
    assert resp2.status_code == 200
    obj2 = repo.create_object("acc-001", {"display_name": "B", "phone": "+85222222222"})
    camp2 = repo.create_campaign("acc-001", name="t2", template_id="", persona_id="",
                                 language="zh", gap_seconds=0, object_ids=[obj2["id"]])
    repo.update_campaign(camp2["id"], status="running")
    out2 = asyncio.run(campaign_tick(repo, dispatcher=fake_dispatch, now=now))
    assert out2["started"] == 0
    assert repo.list_items(camp2["id"])[0]["status"] == "pending"

    # 不传 campaign 键的 PUT → 既有段保留（旧行为零变化）
    resp3 = client.put("/api/settings", json={"policy": "offline_first"})
    assert resp3.status_code == 200
    assert resp3.json()["campaign"]["call_windows"] == outside
