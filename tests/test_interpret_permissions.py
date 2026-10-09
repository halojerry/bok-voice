"""B 线权限错配回归（2026-10-02 刀2，RC-5 权限矩阵）。

修复契约：
- TTS 读/试听面（GET /api/tts/voices、GET /api/tts/cloud-voices、
  POST /api/tts/preview）闸 settings|interpret 任一页键——同传页「克隆音色列表 +
  输出设备指认试听」对 interpret-only 话务员不再静默空列表/403。
- 通话资源端点按行 kind 分闸：kind=interpret 行放行 calls|interpret 任一页键
  （interpret-only 操作员建单→进房→读回→结束全链 200）；其余行（含行缺失）照旧
  只认 calls——非 interpret 行为与旧 _gate_page(request, "calls") 逐字节一致，
  403 先于 404 的旧序保留。
- /api/calls 列表与 /api/calls/{id} DELETE（管理面删单）不随本刀放宽。
- 容灾面板服务注册表不再为已退役的 v1 POC（:8790 b-line）常驻红点。

权限矩阵口径（见 control_plane/permissions.py）：settings 是管理面键，user 角色
拿不到（effective_permissions 对非 grantable 键静默丢弃）；「settings-only」唯一
可构造形态=被 root 逐键下发到只剩 settings 的 admin——页面闸家族（_gate_page/
_gate_page_any）对 admin/root 恒直通，故 admin 全路径 200。

测试口令走模块常量 PW（测试夹具，非真实凭据）。
"""
from __future__ import annotations

import os
import types

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-interpret-perms")

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import hash_password

PW = "Passw0rd!x"


def _client_and_repo(monkeypatch):
    """内存仓 + 打开 startup 的 TestClient（test_permissions.py 同款夹具）。"""
    from fastapi.testclient import TestClient

    from control_plane.main import app

    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    client = TestClient(app).__enter__()
    return client, repo


def _mk_user(repo, username, role="user", permissions="", account_id="acc-001"):
    return repo.create_user(
        username=username, password_hash=hash_password(PW), role=role,
        org_id="org-t", account_id=account_id, permissions_json=permissions,
    )


def _login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _create_call(client, headers, *, kind="", mode="simulation", account_id="acc-001"):
    body: dict = {"account_id": account_id, "object_id": "", "mode": mode}
    if kind:
        body.update({"kind": kind, "direction": "interpret", "language": "zh",
                     "target_lang": "en"})
    r = client.post("/api/calls", headers=headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()


class _FakeResponse:
    def __init__(self, payload=None, content: bytes = b""):
        self._payload = payload
        self.content = content
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """httpx.AsyncClient 替身：本地 TTS sidecar 缺席时给最小成功应答面，
    证明闸后请求真达「sidecar 代理」段（修前此处恒 403，根本到不了）。"""

    def __init__(self, *args, **kwargs): ...
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, *args, **kwargs):
        return _FakeResponse([{"voice_id": "agent-probe", "language": "cantonese"}])

    async def post(self, url, *args, **kwargs):
        return _FakeResponse(content=b"\x00\x00" * 240)  # 240 样本 PCM → 合法 wav


def _stub_tts(monkeypatch) -> None:
    import control_plane.main as cp_main

    monkeypatch.setattr(cp_main, "httpx", types.SimpleNamespace(AsyncClient=_FakeAsyncClient))


def _noop_room_delete(monkeypatch) -> None:
    import control_plane.main as cp_main

    async def _noop(room_name: str) -> None:
        return None

    monkeypatch.setattr(cp_main, "_disconnect_livekit_room", _noop)


# ---- interpret-only 话务员：TTS 共用面 ----


def test_interpret_only_user_tts_surfaces_open(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    _mk_user(repo, "interp", permissions='["interpret"]')
    h = _login(client, "interp")
    _stub_tts(monkeypatch)

    r = client.get("/api/tts/voices", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()[0]["voice_id"] == "agent-probe"

    r = client.get("/api/tts/cloud-voices", headers=h)
    assert r.status_code == 200, r.text

    # 试听（同传控制台输出设备指认走本端点）：闸后真达 sidecar 代理段。
    r = client.post(
        "/api/tts/preview",
        headers=h,
        json={"provider": "qwen3_tts", "voice": "Vivian", "language": "zh", "text": "试听"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("audio/wav")


def test_keyless_user_tts_surfaces_stay_403(monkeypatch):
    """多键闸不是旁路：两键都没有的 user 照旧 403（test_diag_gates 契约保留）。"""
    client, repo = _client_and_repo(monkeypatch)
    _mk_user(repo, "peon", permissions="[]")
    h = _login(client, "peon")
    assert client.get("/api/tts/voices", headers=h).status_code == 403
    assert client.get("/api/tts/cloud-voices", headers=h).status_code == 403
    r = client.post("/api/tts/preview", headers=h, json={"provider": "qwen3_tts", "text": "x"})
    assert r.status_code == 403


# ---- interpret-only 话务员：通话资源全链 ----


def test_interpret_only_user_call_lifecycle(monkeypatch):
    """建单→进房→读回→结束：interpret-only 操作员全链不再 403（RC-5 验收面 5）。"""
    client, repo = _client_and_repo(monkeypatch)
    _noop_room_delete(monkeypatch)
    _mk_user(repo, "interp", permissions='["interpret"]')
    h = _login(client, "interp")

    call = _create_call(client, h, kind="interpret", mode="live")
    cid = call["id"]
    assert call["kind"] == "interpret"

    # 进房（/api/token 本就按 kind 派生 interpret 键，本刀未动——负回归位）。
    r = client.post("/api/token", headers=h,
                    json={"account_id": "acc-001", "call_id": cid, "role": "me"})
    assert r.status_code in (200, 201), r.text

    # 读回同一通话：修前 403（calls 键缺失），修后 200。
    r = client.get(f"/api/calls/{cid}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "interpret"

    # 结束按钮：修前 403，修后 200 + ended。
    r = client.post(f"/api/calls/{cid}/hangup", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ended"

    # 列表不随本刀放宽（混合 kind 面，仍 calls-only）；管理面删单仍 role 闸。
    assert client.get("/api/calls", headers=h).status_code == 403
    assert client.delete(f"/api/calls/{cid}", headers=h).status_code == 403


def test_interpret_only_user_denied_on_a_line_call(monkeypatch):
    """非 interpret 行照旧 calls-only：interpret-only 用户 403（行为逐字节不变）。"""
    client, repo = _client_and_repo(monkeypatch)
    _mk_user(repo, "interp", permissions='["interpret"]')
    _mk_user(repo, "boss", role="admin")
    h = _login(client, "interp")
    ah = _login(client, "boss")

    call = _create_call(client, ah, mode="simulation")
    cid = call["id"]
    assert not str(call.get("kind") or "")  # A 线行（kind=''）

    assert client.get(f"/api/calls/{cid}", headers=h).status_code == 403
    assert client.post(f"/api/calls/{cid}/hangup", headers=h).status_code == 403


def test_keyless_user_denied_on_interpret_call(monkeypatch):
    """两键都无的 user 对 interpret 行仍 403（多键闸不放大权限面）。"""
    client, repo = _client_and_repo(monkeypatch)
    _mk_user(repo, "peon", permissions='["qa"]')
    _mk_user(repo, "boss", role="admin")
    h = _login(client, "peon")
    ah = _login(client, "boss")

    cid = _create_call(client, ah, kind="interpret", mode="live")["id"]
    assert client.get(f"/api/calls/{cid}", headers=h).status_code == 403
    assert client.post(f"/api/calls/{cid}/hangup", headers=h).status_code == 403


def test_missing_call_row_keeps_403_before_404(monkeypatch):
    """行缺失仍走 calls-only 闸：无键者 403（不是 404），旧序保留。"""
    client, repo = _client_and_repo(monkeypatch)
    _mk_user(repo, "interp", permissions='["interpret"]')
    h = _login(client, "interp")
    assert client.get("/api/calls/call-not-exist", headers=h).status_code == 403
    assert client.post("/api/calls/call-not-exist/hangup", headers=h).status_code == 403
    # 有 calls 键（或 admin）时行缺失才是 404——归属/存在性语义未动。
    _mk_user(repo, "boss", role="admin")
    ah = _login(client, "boss")
    assert client.get("/api/calls/call-not-exist", headers=ah).status_code == 404
    assert client.post("/api/calls/call-not-exist/hangup", headers=ah).status_code == 404


def test_interpret_call_cross_account_still_404(monkeypatch):
    """跨账号闸保留：interpret 行通过页键闸后仍走 deny_cross_account → 404。"""
    client, repo = _client_and_repo(monkeypatch)
    _noop_room_delete(monkeypatch)
    _mk_user(repo, "iproot", role="root", account_id="")
    _mk_user(repo, "interp", permissions='["interpret"]')
    rh = _login(client, "iproot")
    h = _login(client, "interp")

    cid = _create_call(client, rh, kind="interpret", mode="live", account_id="acc-002")["id"]
    assert client.get(f"/api/calls/{cid}", headers=h).status_code == 404
    assert client.post(f"/api/calls/{cid}/hangup", headers=h).status_code == 404


def test_admin_and_root_unaffected(monkeypatch):
    """admin/root 页面闸家族恒直通（含只剩 settings 管理键的下发制 admin）。"""
    client, repo = _client_and_repo(monkeypatch)
    _noop_room_delete(monkeypatch)
    _stub_tts(monkeypatch)
    _mk_user(repo, "boss", role="admin", permissions='["settings"]')
    _mk_user(repo, "iproot", role="root", account_id="")
    ah = _login(client, "boss")
    rh = _login(client, "iproot")

    for h in (ah, rh):
        assert client.get("/api/tts/voices", headers=h).status_code == 200
        assert client.get("/api/tts/cloud-voices", headers=h).status_code == 200
        r = client.post("/api/tts/preview", headers=h,
                        json={"provider": "qwen3_tts", "voice": "Vivian", "text": "试听"})
        assert r.status_code == 200, r.text

    cid = _create_call(client, ah, kind="interpret", mode="live")["id"]
    for h in (ah, rh):
        assert client.get(f"/api/calls/{cid}", headers=h).status_code == 200


# ---- 容灾面：退役服务不再亮红灯 ----


def test_server_registry_drops_retired_b_line(monkeypatch):
    """:8790 v1 POC 已退役：服务注册表不得再含 b-line 行（面板红点清零）。"""
    from control_plane.ops_metrics import server_registry

    rows = server_registry({})
    assert all(port != 8790 for _name, _host, port in rows), rows
    assert all(name != "b-line" for name, _host, _port in rows), rows
    # 保底：其余已知服务仍在（防误删整表/误伤同族端口）。
    names = {name for name, _host, _port in rows}
    assert {"llm", "llm-9b", "mt-llm", "asr", "tts", "livekit", "laya", "csc"} <= names, rows
