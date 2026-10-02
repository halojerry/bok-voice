"""W5-T1 通知域骨架：settings sms 段 + POST /api/notify/sms + 挂断自动短信钩子
+ SIP REFER 试点（POST /api/supervisor/{call_id}/transfer-sip）。

钉死九组断言（docs/superpowers/plans/2026-09-19-ai-studio.md §8 T1）：
1. settings sms 段默认形状（双后端）。
2. GET /api/settings 掩码——secret 永不回显原文（has_secret 标记）。
3. PUT 空 secret 保留旧值（web 卡片掩码回显后直接保存不丢凭据）。
4. PUT 不带 sms 键=段不动（不清已配 webhook，照 campaign 先例）。
5. /api/notify/sms 未配置 503 / 参数缺失 400。
6. /api/notify/sms 200 + HMAC 签名头 + 审计 sms.send（fake httpx）。
7. 挂断钩子默认关——默认 settings 下 settle 不触发 webhook。
8. 挂断钩子开启触发 + {contact} 渲染 + 二次 settle 幂等不重发。
9. transfer-sip：mock 短路（默认安全）/ 真路径 identity+transfer_to+aclose
   / 凭据缺 502 / 参数缺失 400（fake lkapi 照 test_sip_trunk_api 手法）。

假 httpx 姿势：helper 走 async `httpx.AsyncClient`（与端点/钩子共用同一内核），
测试 monkeypatch `httpx.AsyncClient` 为记录入参的假类（Summarizer 走同步
httpx.post，不受影响——settle 蒸馏在默认 settings 下本就回落纯指标）。
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests

from types import SimpleNamespace

import httpx
import pytest

from bok_voice_business_db.models import Base
from bok_voice_business_db.repository import (
    InMemoryBusinessRepository,
    SqlAlchemyBusinessRepository,
)

SMS_DEFAULTS = {
    "webhook_url": "",
    "secret": "",
    "enabled": False,
    "hangup_enabled": False,
    "hangup_template": "",
    "allow_private_webhook": False,
}

WEBHOOK_URL = "https://sms.example.invalid/hook"
WEBHOOK_SECRET = "devsecret-not-a-real-credential"


def _dns_ok(monkeypatch):
    """保存期全验的 DNS 桩（2026-09-23 SSRF 守卫配套）。

    WEBHOOK_URL 用 RFC .invalid 域（真解析必失败），守卫 resolve=True 会拒——
    测试桩把它解析成公网 IP，保存路径照常走通（与假 httpx 同款手法）。
    """
    import socket as _socket

    def _resolve(host, *a, **kw):
        if host == "sms.example.invalid":
            return [(_socket.AF_INET, None, None, "", ("93.184.216.34", 0))]
        raise OSError(f"unexpected host in test: {host}")

    monkeypatch.setattr(_socket, "getaddrinfo", _resolve)


# ---- 脚手架 ----


def _client_and_repo(monkeypatch):
    """TestClient + InMemory repo（照 test_sip_trunk_api 姿势）。

    不跑 lifespan（无 `with`）——settle 路径的 `app.state.settlement` 就地补上
    （startup 里唯一硬依赖；knowledge 走 getattr 缺省 None，已安全）。审计 tap
    同理：照 test_campaign_api 手法显式挂 `AuditStore(tap=repo.append_audit)`，
    否则 `_audit` 只进 JSONL 文件 sink，`repo.list_audit_events` 查不到。
    """
    from bok_voice_obs.audit import AuditStore, audit_store
    from fastapi.testclient import TestClient

    from bok_voice_core.settlement import SettlementTrigger
    from control_plane.main import app

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    app.state.settlement = SettlementTrigger()
    original = audit_store()
    monkeypatch.setattr(
        "bok_voice_obs.audit._STORE",
        AuditStore(original.directory, tap=lambda e: repo.append_audit(e.to_dict())),
    )
    return TestClient(app), repo


@pytest.fixture()
def sql_repo(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    # 文件库（tmp_path 每用例一个）而非 `sqlite://`+StaticPool：后者的「全线程共用
    # 一条连接」在 dispose 与在用并发时 SIGSEGV（2026-09-21 机制级复现 3/3）。
    engine = create_engine(
        f"sqlite:///{tmp_path}/bok_test.db",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    yield SqlAlchemyBusinessRepository(session)
    session.close()
    engine.dispose()


def _enable_sms(repo, **over) -> dict:
    cfg = {
        "webhook_url": WEBHOOK_URL,
        "secret": WEBHOOK_SECRET,
        "enabled": True,
        "hangup_enabled": False,
        "hangup_template": "",
    }
    cfg.update(over)
    s = repo.get_settings()
    s["sms"] = cfg
    repo.save_settings(s)
    return cfg


def _fake_async_client(monkeypatch, *, raises: Exception | None = None, status_code: int = 200):
    """假 httpx.AsyncClient：记录每次 post 的 url/content/headers + init timeout。"""
    calls: list[dict] = []

    class _Client:
        def __init__(self, **kwargs):
            calls.append({"timeout": kwargs.get("timeout")})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, content=None, headers=None):
            calls.append({"url": url, "content": content, "headers": headers})
            if raises is not None:
                raise raises
            resp = SimpleNamespace(status_code=status_code)
            resp.raise_for_status = lambda: None
            return resp

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return calls


class _FakeSipService:
    """记录 `transfer_sip_participant` 入参的假 SIP 服务。"""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[object] = []
        self._raises = raises

    async def transfer_sip_participant(self, request: object) -> SimpleNamespace:
        self.calls.append(request)
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace()


class _FakeLkApi:
    """假 LiveKitAPI（照 test_sip_trunk_api 手法）：sip 服务 + aclose 计数。"""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.sip = _FakeSipService(raises=raises)
        self.aclose_calls = 0

    async def aclose(self) -> None:
        self.aclose_calls += 1


def _make_call(client, repo, *, phone: str = "") -> str:
    created = client.post(
        "/api/calls",
        json={"account_id": "acc-001", "object_id": "obj-1", "mode": "simulation"},
    ).json()
    call_id = str(created["id"])
    if phone:
        repo.update_call(call_id, contact_phone=phone)
    return call_id


# ---- 1. settings sms 段默认形状（双后端） ----


def test_default_settings_has_sms_segment():
    s = InMemoryBusinessRepository().get_settings()
    assert s["sms"] == SMS_DEFAULTS
    assert SqlAlchemyBusinessRepository.default_settings()["sms"] == SMS_DEFAULTS


def test_sql_settings_without_row_and_empty_blob_fall_back_to_sms_defaults(sql_repo):
    """空库与空 blob（老库补列后未保存）都回落默认段。"""
    assert sql_repo.get_settings()["sms"] == SMS_DEFAULTS
    sql_repo.save_settings(sql_repo.get_settings())
    from bok_voice_business_db import models

    row = sql_repo.session.get(models.GlobalSetting, "global")
    row.sms_json = ""
    sql_repo.session.commit()
    assert sql_repo.get_settings()["sms"] == SMS_DEFAULTS


def test_sms_roundtrip_both_backends(sql_repo):
    assert "sms_json" in Base.metadata.tables["global_settings"].columns
    for repo in (InMemoryBusinessRepository(), sql_repo):
        s = repo.get_settings()
        s["sms"] = {**SMS_DEFAULTS, "enabled": True, "webhook_url": WEBHOOK_URL,
                    "secret": WEBHOOK_SECRET, "hangup_template": "您好，我是 {contact}"}
        repo.save_settings(s)
        got = repo.get_settings()["sms"]
        assert got["enabled"] is True
        assert got["webhook_url"] == WEBHOOK_URL
        assert got["secret"] == WEBHOOK_SECRET
        assert got["hangup_template"] == "您好，我是 {contact}"
        # 同列其它段不被牵连。
        assert repo.get_settings()["sip"]["mode"] == "mock"


def test_save_without_sms_key_keeps_segment():
    """不带 sms 键的旧调用（老 web 构建/外部脚本）保存后不得把 sms 段清空。"""
    repo = InMemoryBusinessRepository()
    _enable_sms(repo)
    # 拷贝后再去键：InMemory get_settings 返回本体 dict（别名），真实调用方
    # （SQL 仓/PUT 端点）拿到的都是独立 dict。
    s = dict(repo.get_settings())
    s.pop("sms")
    repo.save_settings(s)
    assert repo.get_settings()["sms"]["enabled"] is True


# ---- 2. GET /api/settings 掩码 ----


def test_get_settings_masks_sms_secret(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    _enable_sms(repo)
    body = client.get("/api/settings").json()
    assert body["sms"]["secret"] == ""  # 原文永不回显
    assert body["sms"]["has_secret"] is True
    assert body["sms"]["webhook_url"] == WEBHOOK_URL  # 非 secret 原样回显
    assert WEBHOOK_SECRET not in json.dumps(body)


# ---- 3. PUT 空 secret 保留旧值 / 4. 段缺省不动 ----


def test_put_sms_empty_secret_keeps_old(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    _enable_sms(repo)
    _dns_ok(monkeypatch)
    r = client.put("/api/settings", json={
        "sms": {"webhook_url": WEBHOOK_URL, "secret": "", "enabled": True,
                "hangup_enabled": True, "hangup_template": "x"},
    })
    assert r.status_code == 200
    got = repo.get_settings()["sms"]
    assert got["secret"] == WEBHOOK_SECRET  # 空=保留旧值
    assert got["hangup_enabled"] is True


def test_put_without_sms_key_leaves_segment_untouched(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    _enable_sms(repo, hangup_enabled=True)
    r = client.put("/api/settings", json={"policy": "offline_first"})
    assert r.status_code == 200
    got = repo.get_settings()["sms"]
    assert got["enabled"] is True and got["webhook_url"] == WEBHOOK_URL
    assert got["hangup_enabled"] is True  # 段未动


# ---- 5. /api/notify/sms 503 / 400 ----


def test_notify_sms_unconfigured_503(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    # 默认 settings（disabled）→ 503。
    r = client.post("/api/notify/sms", json={"to": "+8613800138000", "text": "hi"})
    assert r.status_code == 503
    # 配了 URL/secret 但总闸关 → 仍 503。
    _enable_sms(repo, enabled=False)
    assert client.post("/api/notify/sms", json={"to": "+8613800138000", "text": "hi"}).status_code == 503


def test_notify_sms_missing_params_400(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    _enable_sms(repo)
    assert client.post("/api/notify/sms", json={"text": "hi"}).status_code == 400  # to/call_id 都空
    assert client.post("/api/notify/sms", json={"to": "+8613800138000"}).status_code == 400  # text 缺
    assert client.post("/api/notify/sms", json={"text": "hi", "call_id": "call-nope"}).status_code == 404
    # 通话存在但无 contact_phone → 反查落空 400。
    cid = _make_call(client, repo)
    assert client.post("/api/notify/sms", json={"text": "hi", "call_id": cid}).status_code == 400


# ---- 6. /api/notify/sms 200 + 签名 + 审计 ----


def test_notify_sms_sends_signed_webhook_and_audits(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    _enable_sms(repo)
    calls = _fake_async_client(monkeypatch)

    r = client.post("/api/notify/sms", json={"to": "+8613800138000", "text": "您的订单已发货"})
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "provider": "webhook", "status_code": 200}

    assert len(calls) == 2
    assert calls[0]["timeout"] == 10
    send = calls[1]
    assert send["url"] == WEBHOOK_URL
    payload = json.loads(send["content"])
    assert payload == {"to": "+8613800138000", "text": "您的订单已发货"}
    expected_sig = hmac_mod.new(
        WEBHOOK_SECRET.encode("utf-8"), send["content"], hashlib.sha256
    ).hexdigest()
    assert send["headers"]["X-Bok-Signature"] == expected_sig
    assert "secret" not in json.dumps(send["headers"])  # 凭据零外泄

    audits = repo.list_audit_events(action="sms.send")
    assert len(audits) == 1
    detail = audits[0]["detail"]
    assert detail["chars"] == len("您的订单已发货")
    assert detail["status_code"] == 200
    assert WEBHOOK_SECRET not in json.dumps(audits[0])


def test_notify_sms_upstream_failure_502(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    _enable_sms(repo)
    _fake_async_client(monkeypatch, raises=httpx.ConnectError("boom"))
    r = client.post("/api/notify/sms", json={"to": "+8613800138000", "text": "hi"})
    assert r.status_code == 502


def test_notify_sms_to_from_call_contact_phone(monkeypatch):
    """to 空=按 call_id 反查 contact_phone。"""
    client, repo = _client_and_repo(monkeypatch)
    _enable_sms(repo)
    cid = _make_call(client, repo, phone="+8613800138000")
    calls = _fake_async_client(monkeypatch)
    r = client.post("/api/notify/sms", json={"call_id": cid, "text": "hi"})
    assert r.status_code == 200
    assert json.loads(calls[1]["content"])["to"] == "+8613800138000"


# ---- 7/8. 挂断钩子：默认关 / 开启触发 / 二次 settle 幂等 ----


def test_settle_sms_hook_default_off(monkeypatch):
    """默认 settings（enabled=False）→ settle 不触发 webhook。"""
    client, repo = _client_and_repo(monkeypatch)
    cid = _make_call(client, repo, phone="+8613800138000")
    calls = _fake_async_client(monkeypatch)
    r = client.post(f"/api/calls/{cid}/settle")
    assert r.status_code == 200
    assert calls == []  # 钩子默认关，零 webhook 调用


def test_settle_sms_hook_disabled_gate(monkeypatch):
    """enabled=True 但 hangup_enabled=False（默认）→ 也不发。"""
    client, repo = _client_and_repo(monkeypatch)
    cid = _make_call(client, repo, phone="+8613800138000")
    _enable_sms(repo)  # hangup_enabled=False
    calls = _fake_async_client(monkeypatch)
    client.post(f"/api/calls/{cid}/settle")
    assert calls == []


def test_settle_sms_hook_fires_and_renders_template(monkeypatch):
    """开启后挂断结算触发：{contact} 渲染为收件号码 + 审计带 source=hangup。"""
    client, repo = _client_and_repo(monkeypatch)
    cid = _make_call(client, repo, phone="+8613800138000")
    _enable_sms(repo, hangup_enabled=True, hangup_template="回访短信，联系号码 {contact}")
    calls = _fake_async_client(monkeypatch)
    r = client.post(f"/api/calls/{cid}/settle")
    assert r.status_code == 200
    assert len(calls) == 2
    send = calls[1]
    assert send["url"] == WEBHOOK_URL
    payload = json.loads(send["content"])
    assert payload["to"] == "+8613800138000"
    assert payload["text"] == "回访短信，联系号码 +8613800138000"  # {contact} 已渲染
    audits = repo.list_audit_events(action="sms.send")
    assert len(audits) == 1 and audits[0]["detail"]["source"] == "hangup"


def test_settle_sms_hook_object_phone_fallback(monkeypatch):
    """call.contact_phone 空 → object.phone 兜底（同 settle digest 段读法）。"""
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "客户甲", "phone": "+8613900139000"})
    created = client.post(
        "/api/calls",
        json={"account_id": "acc-001", "object_id": obj["id"], "mode": "simulation"},
    ).json()
    _enable_sms(repo, hangup_enabled=True, hangup_template="hi {contact}")
    calls = _fake_async_client(monkeypatch)
    client.post(f"/api/calls/{created['id']}/settle")
    assert len(calls) == 2
    assert json.loads(calls[1]["content"])["to"] == "+8613900139000"


def test_settle_sms_hook_no_phone_no_send(monkeypatch):
    """call 与 object 都无号码 → 不发（不炸）。"""
    client, repo = _client_and_repo(monkeypatch)
    cid = _make_call(client, repo)
    _enable_sms(repo, hangup_enabled=True, hangup_template="hi")
    calls = _fake_async_client(monkeypatch)
    r = client.post(f"/api/calls/{cid}/settle")
    assert r.status_code == 200
    assert calls == []


def test_second_settle_does_not_resend_sms(monkeypatch):
    """二次 settle 被 existing 幂等短路 → 天然不重发。"""
    client, repo = _client_and_repo(monkeypatch)
    cid = _make_call(client, repo, phone="+8613800138000")
    _enable_sms(repo, hangup_enabled=True, hangup_template="hi")
    calls = _fake_async_client(monkeypatch)
    client.post(f"/api/calls/{cid}/settle")
    assert len(calls) == 2  # 首次：1 init + 1 send
    client.post(f"/api/calls/{cid}/settle")
    assert len(calls) == 2  # 二次：零新增
    assert len(repo.list_audit_events(action="sms.send")) == 1


def test_settle_sms_hook_failure_never_breaks_settle(monkeypatch):
    """webhook 炸 → settle 照常返回（must not break settle 先例）。"""
    client, repo = _client_and_repo(monkeypatch)
    cid = _make_call(client, repo, phone="+8613800138000")
    _enable_sms(repo, hangup_enabled=True, hangup_template="hi")
    _fake_async_client(monkeypatch, raises=httpx.ConnectError("boom"))
    r = client.post(f"/api/calls/{cid}/settle")
    assert r.status_code == 200
    assert repo.get_settlement(cid)  # 结算落库不受影响


# ---- 9. transfer-sip：mock 短路 / 真路径 / 502 / 400 ----


def test_transfer_sip_mock_mode_short_circuits(monkeypatch):
    """默认 sip.mode=mock → 短路 {"mocked": True}，不触 LiveKit（默认安全）。"""
    client, repo = _client_and_repo(monkeypatch)
    cid = _make_call(client, repo, phone="+8613800138000")
    fake = _FakeLkApi()
    monkeypatch.setattr("control_plane.main._lkapi_client", lambda: fake)
    r = client.post(f"/api/supervisor/{cid}/transfer-sip", json={"transfer_to": "+861500015000"})
    assert r.status_code == 200
    assert r.json() == {"call_id": cid, "mocked": True, "transfer_to": "+861500015000"}
    assert fake.sip.calls == [] and fake.aclose_calls == 0  # mock 档不触 LiveKit
    audits = repo.list_audit_events(action="supervisor.transfer_sip")
    assert audits and audits[0]["detail"]["mocked"] is True


def test_transfer_sip_real_path_identity_and_aclose(monkeypatch):
    """real 档真路径：room_name=call_id / identity=sip-{phone} / transfer_to 透传
    / 一次性客户端 finally aclose / 审计 mocked=False。"""
    client, repo = _client_and_repo(monkeypatch)
    s = repo.get_settings()
    s["sip"] = {**s["sip"], "mode": "real"}
    repo.save_settings(s)
    cid = _make_call(client, repo, phone="+8613800138000")
    fake = _FakeLkApi()
    monkeypatch.setattr("control_plane.main._lkapi_client", lambda: fake)
    r = client.post(f"/api/supervisor/{cid}/transfer-sip", json={"transfer_to": "+861500015000"})
    assert r.status_code == 200
    assert r.json()["mocked"] is False
    assert len(fake.sip.calls) == 1 and fake.aclose_calls == 1
    req = fake.sip.calls[0]
    assert str(req.room_name) == cid
    assert str(req.participant_identity) == "sip-+8613800138000"
    assert str(req.transfer_to) == "+861500015000"
    audits = repo.list_audit_events(action="supervisor.transfer_sip")
    assert audits[-1]["detail"]["mocked"] is False


def test_transfer_sip_real_mode_livekit_failure_502(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    s = repo.get_settings()
    s["sip"] = {**s["sip"], "mode": "real"}
    repo.save_settings(s)
    cid = _make_call(client, repo, phone="+8613800138000")
    fake = _FakeLkApi(raises=RuntimeError("sip down"))
    monkeypatch.setattr("control_plane.main._lkapi_client", lambda: fake)
    r = client.post(f"/api/supervisor/{cid}/transfer-sip", json={"transfer_to": "+861500015000"})
    assert r.status_code == 502
    assert fake.aclose_calls == 1  # 失败路径也要关一次性客户端
    # 502 不出审计（动作没做成；outcome=ok 的审计只记成功）。
    assert repo.list_audit_events(action="supervisor.transfer_sip") == []


def test_transfer_sip_real_mode_no_credentials_502(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    s = repo.get_settings()
    s["sip"] = {**s["sip"], "mode": "real"}
    repo.save_settings(s)
    cid = _make_call(client, repo, phone="+8613800138000")
    monkeypatch.setattr("control_plane.main._lkapi_client", lambda: None)
    r = client.post(f"/api/supervisor/{cid}/transfer-sip", json={"transfer_to": "+861500015000"})
    assert r.status_code == 502


def test_transfer_sip_missing_params_400(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    cid = _make_call(client, repo, phone="+8613800138000")
    bare = _make_call(client, repo)  # 无 contact_phone
    assert client.post(f"/api/supervisor/{cid}/transfer-sip", json={}).status_code == 400
    assert client.post(
        f"/api/supervisor/{bare}/transfer-sip", json={"transfer_to": "+861500015000"}
    ).status_code == 400
    assert client.post(
        "/api/supervisor/call-nope/transfer-sip", json={"transfer_to": "+861500015000"}
    ).status_code == 404


# ---- 10. SSRF 守卫（2026-09-23，Mimosa 修复：保存期全验 + 发送期字面量复验） ----


def test_put_sms_enabled_webhook_guard_rejects_private(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    r = client.put("/api/settings", json={
        "sms": {"webhook_url": "http://192.168.1.9/hook", "secret": "s", "enabled": True},
    })
    assert r.status_code == 400
    assert "不合规" in r.json()["detail"]


def test_put_sms_metadata_rejected_even_with_allow_private(monkeypatch):
    # 云元数据段无口子：allow_private_webhook 也不放行
    client, repo = _client_and_repo(monkeypatch)
    r = client.put("/api/settings", json={
        "sms": {"webhook_url": "http://169.254.169.254/latest", "secret": "s",
                "enabled": True, "allow_private_webhook": True},
    })
    assert r.status_code == 400


def test_put_sms_allow_private_flag_permits_lab_gateway(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    r = client.put("/api/settings", json={
        "sms": {"webhook_url": "http://10.1.2.3/sms", "secret": "s",
                "enabled": True, "allow_private_webhook": True},
    })
    assert r.status_code == 200
    assert repo.get_settings()["sms"]["allow_private_webhook"] is True


def test_put_sms_disabled_draft_url_not_validated(monkeypatch):
    # 未启用的草稿 URL 不拦——保存摩擦留给启用那一刻
    client, repo = _client_and_repo(monkeypatch)
    r = client.put("/api/settings", json={
        "sms": {"webhook_url": "http://192.168.1.9/hook", "secret": "s", "enabled": False},
    })
    assert r.status_code == 200


def test_send_time_guard_blocks_repo_tampered_webhook(monkeypatch):
    # 绕过 PUT 直接改库（保存面被绕过/被改库场景）→ 发送期字面量复验拦截（502）
    client, repo = _client_and_repo(monkeypatch)
    s = repo.get_settings()
    s["sms"] = {"webhook_url": "http://169.254.169.254/latest", "secret": "s",
                "enabled": True, "hangup_enabled": False, "hangup_template": "",
                "allow_private_webhook": True}
    repo.save_settings(s)
    r = client.post("/api/notify/sms", json={"to": "+8613800138000", "text": "hi"})
    assert r.status_code == 502
