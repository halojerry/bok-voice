"""单发外呼「立即外呼」（spec 2026-09-13 sip-edge-thin-node-v2 §7 P1.5 Task 4）。

钉死三件事：

1. **`campaign.build_dial_block` 纯函数**——dial 块 10 键**键序逐键与 campaign
   旧实现一致**（agent 侧按键读，键序是有意的形状契约；campaign 自 T2 起按
   站点优先/settings 兜底解析 trunk）、数字字段 `or 默认` 兜底（0/空不得静默
   变「无保险丝/零振铃窗」）、mode 走 `_dial_mode`（env BOK_SIP_MODE 优先）。
2. **campaign 建通链仍走同一函数**——`campaign_tick` 实际派发的 dial 块与直接
   调 `build_dial_block` 的结果逐字段相等（`_start_call` 委托的回归锚）。
3. **`POST /api/objects/{object_id}/dial-now`**——对象无 phone=400、对象不存在
   =404；成功=建一通 `direction=outbound`/`mode=live` 通话 + 直接派 agent
   （metadata JSON 的 `dial.to`=对象电话、`campaign_item_id` 空）+ 审计
   `call.dial_now`；语言缺省链=请求 language > 对象 language > zh；站点 trunk
   优先、未知站点/无 site_id 回退 settings；派发失败=502（通话仍建，便于排查）。

fake dispatcher 姿势：monkeypatch `control_plane.campaign._default_dispatcher`
（端点内是函数体延迟 import，测试注入即生效，照 `test_campaign_loop.py` 的
注入 dispatcher 形状）。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "control-plane"))

from bok_voice_business_db import models  # noqa: E402
from bok_voice_business_db.repository import (  # noqa: E402
    InMemoryBusinessRepository,
    SqlAlchemyBusinessRepository,
)
from control_plane.campaign import build_dial_block  # noqa: E402

SETTINGS_TRUNK = "ST_settings_trunk"
SITE_TRUNK = "ST_site_trunk"
# 旧 dial 块键序（spec 钉死：逐键一致，勿增删/重排）。
LEGACY_DIAL_KEYS = [
    "to",
    "mode",
    "scenario",
    "script",
    "speak_interval_s",
    "language",
    "trunk_id",
    "campaign_item_id",
    "max_call_duration_s",
    "ringing_timeout_s",
]


def _client_and_repo(monkeypatch, repo=None):
    """TestClient + repo（照 test_sip_trunk_api 姿势）。

    默认 InMemory；断言 `direction/language/contact_phone` 这类**只有 SQL 仓才有
    的列**时传 `sql_repo` fixture（内存替身的 create_call 不落这些键）。
    """
    from fastapi.testclient import TestClient

    from control_plane.main import app

    repo = repo if repo is not None else InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    return TestClient(app), repo


@pytest.fixture()
def sql_repo():
    """真 sqlite 后端：StaticPool=全线程共享同一内存库（照 test_campaign_site 姿势）。"""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    yield SqlAlchemyBusinessRepository(session)
    session.close()
    engine.dispose()


class _FakeDispatch:
    """记录 dispatcher 调用（形状照 test_campaign_loop 的 fake_dispatch）。"""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._raises = raises

    async def __call__(self, room: str, metadata: str) -> None:
        self.calls.append((room, metadata))
        if self._raises is not None:
            raise self._raises

    def dial(self, index: int = 0) -> dict:
        return json.loads(self.calls[index][1])["dial"]


def _patch_dispatcher(monkeypatch, *, raises: Exception | None = None) -> _FakeDispatch:
    fake = _FakeDispatch(raises=raises)
    monkeypatch.setattr("control_plane.campaign._default_dispatcher", fake)
    return fake


def _make_object(repo, **over) -> dict:
    fields = {"display_name": "A", "phone": "+85291112222", "language": "zh"}
    fields.update(over)
    return repo.create_object("acc-001", fields)


def _post_dial_now(client, object_id: str, **body):
    return client.post(f"/api/objects/{object_id}/dial-now", json=body)


# ---- ① build_dial_block 纯函数 ----


def test_build_dial_block_key_order_matches_legacy_dial_block():
    """键序=旧 dial 块逐键一致（agent 侧读键，顺序是有意的形状契约）。"""
    dial = build_dial_block(number="+8529", language="zh", sip={})
    assert list(dial.keys()) == LEGACY_DIAL_KEYS


def test_build_dial_block_defaults():
    """空站点/空 sip：mock 档、空 trunk、空 item、数字兜默认。"""
    dial = build_dial_block(number="+85291112222", language="cantonese", sip={})
    assert dial["to"] == "+85291112222"
    assert dial["mode"] == "mock"
    assert dial["scenario"] == "" and dial["script"] == []
    assert dial["speak_interval_s"] == 0.0
    assert dial["language"] == "cantonese"
    assert dial["trunk_id"] == "" and dial["campaign_item_id"] == ""
    assert dial["max_call_duration_s"] == 600
    assert dial["ringing_timeout_s"] == 30


def test_build_dial_block_trunk_prefers_site_over_settings():
    dial = build_dial_block(
        number="+8529", language="zh",
        sip={"trunk_id": SETTINGS_TRUNK}, site={"trunk_id": SITE_TRUNK},
    )
    assert dial["trunk_id"] == SITE_TRUNK


@pytest.mark.parametrize(
    "site",
    [None, {}, {"trunk_id": ""}, {"trunk_id": None}],
    ids=["no-site", "empty-site", "site-trunk-empty", "site-trunk-none"],
)
def test_build_dial_block_trunk_falls_back_to_settings(site):
    """无站点/站点未注册 trunk（空/None）→ 逐字回退 settings（单站点旧行为零变化）。"""
    dial = build_dial_block(
        number="+8529", language="zh",
        sip={"trunk_id": SETTINGS_TRUNK}, site=site,
    )
    assert dial["trunk_id"] == SETTINGS_TRUNK


def test_build_dial_block_site_trunk_wins_when_settings_missing():
    dial = build_dial_block(number="+8529", language="zh", sip={},
                            site={"trunk_id": SITE_TRUNK})
    assert dial["trunk_id"] == SITE_TRUNK


@pytest.mark.parametrize("zero", [0, None, ""], ids=["0", "none", "empty"])
def test_build_dial_block_numeric_fields_fall_back(zero):
    """0/空一律兜默认（`or 默认` 语义）——不得静默变「无保险丝/零振铃窗」。"""
    dial = build_dial_block(
        number="+8529", language="zh",
        sip={"max_call_duration_s": zero, "ringing_timeout_s": zero},
    )
    assert dial["max_call_duration_s"] == 600
    assert dial["ringing_timeout_s"] == 30


def test_build_dial_block_numeric_fields_pass_configured_values():
    dial = build_dial_block(
        number="+8529", language="zh",
        sip={"max_call_duration_s": 120, "ringing_timeout_s": 45},
    )
    assert dial["max_call_duration_s"] == 120 and dial["ringing_timeout_s"] == 45


def test_build_dial_block_narrowband_omitted_by_default():
    """窄带档默认关：键**不出现**（旧 dial 块逐字节零变化；agent 侧缺键=False）。"""
    dial = build_dial_block(number="+8529", language="zh", sip={})
    assert "narrowband" not in dial
    assert list(dial.keys()) == LEGACY_DIAL_KEYS
    # 显式 False 与缺省同语义（不发键）。
    assert "narrowband" not in build_dial_block(number="+8529", language="zh", sip={},
                                                narrowband=False)


def test_build_dial_block_narrowband_appended_when_enabled():
    """窄带档开：键追加在尾部（键序=旧 10 键 + narrowband，前面键位不动）。"""
    dial = build_dial_block(number="+8529", language="zh", sip={}, narrowband=True)
    assert dial["narrowband"] is True
    assert list(dial.keys()) == [*LEGACY_DIAL_KEYS, "narrowband"]


def test_build_dial_block_mode_env_kill_switch(monkeypatch):
    """env BOK_SIP_MODE 合法值压过 settings；非法值回落 settings（`_dial_mode` 语义）。"""
    monkeypatch.setenv("BOK_SIP_MODE", "real")
    assert build_dial_block(number="+8529", language="zh",
                            sip={"mode": "mock"})["mode"] == "real"
    monkeypatch.setenv("BOK_SIP_MODE", "bogus")
    assert build_dial_block(number="+8529", language="zh",
                            sip={"mode": "real"})["mode"] == "real"


def test_build_dial_block_script_is_copied_and_defaults_empty():
    """script 入参 None → []；传入列表按值拷贝（调用方后续改动不外溢进 dial 块）。"""
    lines = ["你好", "我WhatsApp係"]
    dial = build_dial_block(number="+8529", language="zh", sip={}, script=lines,
                            scenario="answer", speak_interval_s=6.5,
                            campaign_item_id="item-1")
    assert dial["script"] == lines and dial["script"] is not lines
    assert dial["scenario"] == "answer"
    assert dial["speak_interval_s"] == 6.5
    assert dial["campaign_item_id"] == "item-1"
    assert build_dial_block(number="+8529", language="zh", sip={}, script=None)["script"] == []


def test_campaign_start_call_delegates_to_build_dial_block(monkeypatch):
    """campaign 建通链的 dial 块=同一函数的输出（抽函数的回归锚，键序/取值同源）。"""
    import asyncio

    from control_plane.campaign import campaign_tick

    repo = InMemoryBusinessRepository()
    obj = _make_object(repo, display_name="A", phone="+85211111111", language="cantonese")
    camp = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                                language="cantonese", gap_seconds=5, object_ids=[obj["id"]])
    repo.update_campaign(camp["id"], status="running")
    repo.save_settings({"sip": {"mode": "real", "trunk_id": SETTINGS_TRUNK}})
    fake = _FakeDispatch()
    asyncio.run(campaign_tick(repo, dispatcher=fake))
    item = repo.list_items(camp["id"])[0]
    assert fake.dial() == build_dial_block(
        number=item["phone"], language="cantonese",
        sip={"mode": "real", "trunk_id": SETTINGS_TRUNK},
        campaign_item_id=str(item["id"]),
    )


def test_campaign_narrowband_flag_rides_scripts_json_into_dial_block(monkeypatch):
    """战役窄带档：`__narrowband__` 与句间隔同源（scripts_json 保留键），进 dial 块。

    8kHz 重验探针要按战役开窄带（mock 测试床专属；real 档无消费）。
    """
    import asyncio

    from control_plane.campaign import campaign_tick

    repo = InMemoryBusinessRepository()
    obj = _make_object(repo, display_name="A", phone="+85211111111")
    camp = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                                language="zh", gap_seconds=5, object_ids=[obj["id"]],
                                scripts={"__narrowband__": True,
                                         "__speak_interval__": 8.0})
    repo.update_campaign(camp["id"], status="running")
    fake = _FakeDispatch()
    asyncio.run(campaign_tick(repo, dispatcher=fake))
    dial = fake.dial()
    assert dial["narrowband"] is True
    assert dial["speak_interval_s"] == 8.0


# ---- ② 端点：成功路径 ----


def test_dial_now_creates_outbound_call_and_dispatches(monkeypatch, sql_repo):
    """核心链路：建 outbound/live 通话 + 直接派 agent，metadata 的 dial.to=对象电话。"""
    client, repo = _client_and_repo(monkeypatch, sql_repo)
    fake = _patch_dispatcher(monkeypatch)
    obj = _make_object(repo, phone="+85291112222")

    r = _post_dial_now(client, obj["id"])

    assert r.status_code == 200
    body = r.json()
    call_id = body["call_id"]
    assert call_id.startswith("call-")  # manifest 生成的通话 id
    assert body["status"] == "ringing"
    # 通话落库：outbound / live / 快照电话
    call = repo.get_call(call_id)
    assert call["direction"] == "outbound" and call["mode"] == "live"
    assert call["object_id"] == obj["id"] and call["contact_phone"] == obj["phone"]
    # 派发：room=call_id，metadata.dial.to=对象 phone、无 campaign item（单发）
    assert len(fake.calls) == 1
    room, metadata = fake.calls[0]
    assert room == call_id
    payload = json.loads(metadata)
    assert payload["call_id"] == call_id
    dial = payload["dial"]
    assert list(dial.keys()) == LEGACY_DIAL_KEYS
    assert dial["to"] == obj["phone"]
    assert dial["campaign_item_id"] == "" and dial["scenario"] == ""
    assert dial["mode"] == "mock"  # settings 未配 sip → mock 档
    assert repo.find_item_by_call(call_id) is None  # 单发外呼不进任何战役名单


def test_dial_now_uses_settings_trunk_and_sip_numbers(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    fake = _patch_dispatcher(monkeypatch)
    repo.save_settings({"sip": {"mode": "real", "trunk_id": SETTINGS_TRUNK,
                                "ringing_timeout_s": 45, "max_call_duration_s": 120}})
    obj = _make_object(repo)

    assert _post_dial_now(client, obj["id"]).status_code == 200

    dial = fake.dial()
    assert dial["mode"] == "real" and dial["trunk_id"] == SETTINGS_TRUNK
    assert dial["ringing_timeout_s"] == 45 and dial["max_call_duration_s"] == 120


def test_dial_now_site_id_wires_site_trunk(monkeypatch):
    """带 site_id：站点的 trunk 压过 settings（T2 同一条解析规则）。"""
    client, repo = _client_and_repo(monkeypatch)
    fake = _patch_dispatcher(monkeypatch)
    repo.save_settings({"sip": {"mode": "real", "trunk_id": SETTINGS_TRUNK}})
    site = repo.create_site(name="hk", trunk_id=SITE_TRUNK)
    obj = _make_object(repo)

    assert _post_dial_now(client, obj["id"], site_id=site["id"]).status_code == 200
    assert fake.dial()["trunk_id"] == SITE_TRUNK


def test_dial_now_unknown_site_falls_back_to_settings(monkeypatch):
    """site_id 指向不存在的站点（含虚拟 `site-local`）→ 安全回退 settings，不报错。"""
    client, repo = _client_and_repo(monkeypatch)
    fake = _patch_dispatcher(monkeypatch)
    repo.save_settings({"sip": {"mode": "real", "trunk_id": SETTINGS_TRUNK}})
    obj = _make_object(repo)

    assert _post_dial_now(client, obj["id"], site_id="site-local").status_code == 200
    assert fake.dial()["trunk_id"] == SETTINGS_TRUNK


@pytest.mark.parametrize(
    "object_language,req_language,expected",
    [
        ("zh", "", "zh"),
        ("cantonese", "", "cantonese"),
        ("en", "", "en"),
        ("zh", "cantonese", "cantonese"),   # 请求 language 最高优先
        ("cantonese", "en", "en"),
        ("", "", "zh"),                     # 对象语言缺失 → zh
    ],
)
def test_dial_now_language_precedence(monkeypatch, sql_repo, object_language, req_language, expected):
    """语言缺省链：请求 language > 对象 language > zh（dial 块与通话快照同值）。"""
    client, repo = _client_and_repo(monkeypatch, sql_repo)
    fake = _patch_dispatcher(monkeypatch)
    obj = _make_object(repo, language=object_language)

    body = {} if not req_language else {"language": req_language}
    resp = _post_dial_now(client, obj["id"], **body)

    assert resp.status_code == 200
    assert fake.dial()["language"] == expected
    assert repo.get_call(resp.json()["call_id"])["language"] == expected


def test_dial_now_passes_persona_id(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    _patch_dispatcher(monkeypatch)
    obj = _make_object(repo)

    resp = _post_dial_now(client, obj["id"], persona_id="persona-9")

    assert resp.status_code == 200
    assert repo.get_call(resp.json()["call_id"])["persona_id"] == "persona-9"


def test_dial_now_template_snapshot(monkeypatch):
    """话术来源优先级：显式 template_id > 对象绑定模板（call_sessions.template_id 建单快照）。"""
    client, repo = _client_and_repo(monkeypatch)
    _patch_dispatcher(monkeypatch)
    obj = _make_object(repo, template_id="tpl-object")

    plain = _post_dial_now(client, obj["id"])
    assert repo.get_call(plain.json()["call_id"])["template_id"] == "tpl-object"

    over = _post_dial_now(client, obj["id"], template_id="tpl-explicit")
    assert repo.get_call(over.json()["call_id"])["template_id"] == "tpl-explicit"


# ---- ② 端点：失败面 ----


def test_dial_now_object_without_phone_is_400(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    fake = _patch_dispatcher(monkeypatch)
    obj = _make_object(repo, phone="")

    r = _post_dial_now(client, obj["id"])

    assert r.status_code == 400
    assert "电话" in r.json()["detail"]
    assert fake.calls == []                      # 不派发
    assert repo.list_calls("acc-001") == []      # 不建通话


def test_dial_now_blank_phone_is_400(monkeypatch):
    """空白号等同无号（strip 后判定），不得拿 "   " 去拨。"""
    client, repo = _client_and_repo(monkeypatch)
    fake = _patch_dispatcher(monkeypatch)
    obj = _make_object(repo, phone="   ")

    r = _post_dial_now(client, obj["id"])

    assert r.status_code == 400 and fake.calls == []


def test_dial_now_unknown_object_is_404(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    fake = _patch_dispatcher(monkeypatch)

    r = _post_dial_now(client, "obj-missing")

    assert r.status_code == 404
    assert fake.calls == [] and repo.list_calls("acc-001") == []


def test_dial_now_dispatch_failure_is_502(monkeypatch, sql_repo):
    """派发炸（LiveKit 凭据缺/不可达）→ 502；通话仍建好便于排查（campaign 同语义）。"""
    client, repo = _client_and_repo(monkeypatch, sql_repo)
    _patch_dispatcher(monkeypatch, raises=RuntimeError("livekit credentials missing"))
    obj = _make_object(repo)

    r = _post_dial_now(client, obj["id"])

    assert r.status_code == 502
    assert "livekit credentials missing" in r.json()["detail"]
    calls = repo.list_calls("acc-001")
    assert len(calls) == 1 and calls[0]["direction"] == "outbound"


def test_dial_now_audited(monkeypatch):
    """审计 `call.dial_now`（单发外呼可追溯）；`with` 触发 startup 挂 audit tap。"""
    client, repo = _client_and_repo(monkeypatch)
    _patch_dispatcher(monkeypatch)
    obj = _make_object(repo)

    with client:
        resp = _post_dial_now(client, obj["id"])
        assert resp.status_code == 200
        rows = client.get("/api/audit", params={"action": "call.dial_now"}).json()

    assert any(r["subject_id"] == resp.json()["call_id"] for r in rows)
    assert rows[0]["account_id"] == "acc-001"
    assert rows[0]["detail"]["object"] == obj["id"]
