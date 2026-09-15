"""SIP trunk 注册端点（spec 2026-09-13 sip-edge-thin-node-v2 §7 P1.5 Task 3）。

钉死五件事：

1. **一次性引导链路**——`POST /api/sip/sites/{site_id}/trunk` 用面板给的地址/主叫号/
   鉴权调 LiveKit SIP 服务建 outbound trunk，成功把返回的 `sip_trunk_id` 回填
   `site.trunk_id`（T2 的 campaign 按 site 优先取 trunk 靠这个字段）。
2. **SDK 真实契约**（venv `livekit-api 1.2.1` 核实）——非弃用的
   `sip.create_outbound_trunk(CreateSIPOutboundTrunkRequest(trunk=SIPOutboundTrunkInfo(...)))`；
   字段名 address/numbers/auth_username/auth_password 逐个透传（计划草案里的
   `SIPOutboundTrunkConfig`/`dispatch_ruleless` 在本 SDK 版本不存在）。
3. **失败面**——site 不存在=404、address/numbers 缺=400、LiveKit 凭据缺=502、
   SIP 调用炸=502（detail 带「livekit-sip 未部署或不可达」提示+原始错误）、
   LiveKit 返回空 trunk id=502；失败路径**绝不写** `site.trunk_id`。
4. **auth_password 不回显**（凭据安全，T8 掩码先例）：响应体任何位置不出现明文/键名。
5. **`numbers` 严格 `list[str]`**（T1 审查定案）——CP 层 Pydantic 类型是唯一防线，
   字符串/数字/对象等畸形入参一律 422 挡在 repo 之外（repo 层畸形会被静默清空号码池）。

fake lkapi 姿势照 `tests/test_dispatch_utils.py`：monkeypatch
`control_plane.main._lkapi_client` 返回记录入参的假客户端（aclose 必须 awaitable）。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

from types import SimpleNamespace

import pytest


def _client_and_repo(monkeypatch):
    """TestClient + InMemory repo（照 test_campaign_api 姿势）。"""
    from fastapi.testclient import TestClient

    from bok_voice_business_db.repository import InMemoryBusinessRepository
    from control_plane.main import app

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    return TestClient(app), repo


class _FakeSipService:
    """记录 `create_outbound_trunk` 入参的假 SIP 服务。"""

    def __init__(self, *, trunk_id: str = "ST_fake", raises: Exception | None = None) -> None:
        self.calls: list[object] = []
        self._trunk_id = trunk_id
        self._raises = raises

    async def create_outbound_trunk(self, request: object) -> SimpleNamespace:
        self.calls.append(request)
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(sip_trunk_id=self._trunk_id)


class _FakeLkApi:
    """假 LiveKitAPI：`sip` 服务 + `aclose()`（一次性客户端用完必须关）。"""

    def __init__(self, *, trunk_id: str = "ST_fake", raises: Exception | None = None) -> None:
        self.sip = _FakeSipService(trunk_id=trunk_id, raises=raises)
        self.aclose_calls = 0

    async def aclose(self) -> None:
        self.aclose_calls += 1


def _fake_lkapi(monkeypatch, *, trunk_id: str = "ST_fake", raises: Exception | None = None) -> _FakeLkApi:
    fake = _FakeLkApi(trunk_id=trunk_id, raises=raises)
    monkeypatch.setattr("control_plane.main._lkapi_client", lambda: fake)
    return fake


def _make_site(repo, **over) -> dict:
    fields = {"name": "hk-edge", "livekit_url": "ws://vps.example:7880"}
    fields.update(over)
    return repo.create_site("acc-001", **fields)


def _post_trunk(client, site_id: str, **over):
    body = {"address": "sip.telnyx.com", "numbers": ["+12025550123"]}
    body.update(over)
    return client.post(f"/api/sip/sites/{site_id}/trunk", json=body)


# ---- 成功路径 ----


def test_register_trunk_stores_site_trunk_id(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    site = _make_site(repo)
    fake = _fake_lkapi(monkeypatch, trunk_id="ST_fake")

    r = _post_trunk(client, site["id"], auth_username="user1", auth_password="sekret")

    assert r.status_code == 200
    body = r.json()
    assert body["trunk_id"] == "ST_fake"
    assert body["site"]["id"] == site["id"]
    assert repo.get_site(site["id"])["trunk_id"] == "ST_fake"
    assert fake.aclose_calls == 1


def test_register_trunk_passes_fields_to_livekit_sip(monkeypatch):
    """入参逐字段透传 + name 可辨识（`outbound-<siteid8>-<epoch>`）。"""
    client, repo = _client_and_repo(monkeypatch)
    site = _make_site(repo)
    fake = _fake_lkapi(monkeypatch)

    r = _post_trunk(
        client, site["id"],
        address="sip.telnyx.com", numbers=["+12025550123", "+12025550124"],
        auth_username="user1", auth_password="sekret",
    )

    assert r.status_code == 200
    assert len(fake.sip.calls) == 1
    trunk = fake.sip.calls[0].trunk
    assert trunk.address == "sip.telnyx.com"
    assert list(trunk.numbers) == ["+12025550123", "+12025550124"]
    assert trunk.auth_username == "user1"
    assert trunk.auth_password == "sekret"
    assert trunk.name.startswith("outbound-")
    assert site["id"][:8] in trunk.name


def test_register_trunk_auth_optional_ip_whitelist_mode(monkeypatch):
    """鉴权留空=IP 白名单模式：不传 auth 字段也照常注册。"""
    client, repo = _client_and_repo(monkeypatch)
    site = _make_site(repo)
    fake = _fake_lkapi(monkeypatch)

    r = _post_trunk(client, site["id"])

    assert r.status_code == 200
    assert fake.sip.calls[0].trunk.auth_username == ""
    assert fake.sip.calls[0].trunk.auth_password == ""


def test_register_trunk_audited(monkeypatch):
    """审计 `sip.trunk_registered`（站点生命周期可追溯）；`with` 触发 startup 挂
    audit tap（JSONL → repo），/api/audit 才查得到。"""
    client, repo = _client_and_repo(monkeypatch)
    site = _make_site(repo)
    _fake_lkapi(monkeypatch)

    with client:
        assert _post_trunk(client, site["id"]).status_code == 200
        rows = client.get("/api/audit", params={"action": "sip.trunk_registered"}).json()

    assert any(r["subject_id"] == site["id"] for r in rows)
    assert rows[0]["account_id"] == "acc-001"
    assert rows[0]["detail"]["trunk_id"] == "ST_fake"


# ---- 凭据不回显 ----


def test_register_trunk_response_never_echoes_auth_password(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    site = _make_site(repo)
    _fake_lkapi(monkeypatch)

    r = _post_trunk(client, site["id"], auth_username="user1", auth_password="sekret")

    assert r.status_code == 200
    assert "sekret" not in r.text
    assert "auth_password" not in r.text


# ---- 失败面（一律不写 site.trunk_id） ----


def test_register_trunk_unknown_site_is_404(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    fake = _fake_lkapi(monkeypatch)

    r = _post_trunk(client, "site-missing")

    assert r.status_code == 404
    assert fake.sip.calls == []
    assert fake.aclose_calls == 0


@pytest.mark.parametrize(
    "over",
    [
        {"address": ""},
        {"address": "   "},
        {"numbers": []},
        {"numbers": ["", "  "]},
    ],
)
def test_register_trunk_requires_address_and_numbers(monkeypatch, over):
    client, repo = _client_and_repo(monkeypatch)
    site = _make_site(repo)
    fake = _fake_lkapi(monkeypatch)

    r = _post_trunk(client, site["id"], **over)

    assert r.status_code == 400
    assert fake.sip.calls == []
    assert repo.get_site(site["id"])["trunk_id"] == ""


def test_register_trunk_without_livekit_credentials_is_502(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    site = _make_site(repo)
    monkeypatch.setattr("control_plane.main._lkapi_client", lambda: None)

    r = _post_trunk(client, site["id"])

    assert r.status_code == 502
    assert repo.get_site(site["id"])["trunk_id"] == ""


def test_register_trunk_livekit_failure_is_502(monkeypatch):
    """livekit-sip 未部署/不可达 → 502，detail 带提示与原始错误；站点不被污染。"""
    client, repo = _client_and_repo(monkeypatch)
    site = _make_site(repo, trunk_id="ST_old")
    fake = _fake_lkapi(monkeypatch, raises=RuntimeError("no sip server"))

    r = _post_trunk(client, site["id"])

    assert r.status_code == 502
    assert "livekit-sip" in r.json()["detail"]
    assert "no sip server" in r.json()["detail"]
    assert repo.get_site(site["id"])["trunk_id"] == "ST_old"  # 旧 trunk 不被清空
    assert fake.aclose_calls == 1  # 失败也要关客户端


def test_register_trunk_empty_trunk_id_from_livekit_is_502(monkeypatch):
    """LiveKit 返回空 trunk id（异常形状）→ 502，不透支站点 trunk（宁可报错不清池）。"""
    client, repo = _client_and_repo(monkeypatch)
    site = _make_site(repo, trunk_id="ST_old")
    _fake_lkapi(monkeypatch, trunk_id="")

    r = _post_trunk(client, site["id"])

    assert r.status_code == 502
    assert repo.get_site(site["id"])["trunk_id"] == "ST_old"


# ---- numbers 严格 list[str]（T1 审查定案：CP 层类型=唯一防线） ----


@pytest.mark.parametrize(
    "numbers",
    [
        "abc",  # 字符串：repo 层 _normalize_site_numbers 会归一成 []（静默清池）
        [1, 2],  # 数字：Pydantic v2 不做 int→str 回填
        {"a": 1},
        None,
    ],
)
def test_register_trunk_rejects_malformed_numbers(monkeypatch, numbers):
    client, repo = _client_and_repo(monkeypatch)
    site = _make_site(repo)
    fake = _fake_lkapi(monkeypatch)

    r = _post_trunk(client, site["id"], numbers=numbers)

    assert r.status_code == 422
    assert fake.sip.calls == []


# ---- 站点列表（面板站点下拉数据源） ----


def test_list_sites_returns_account_sites(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    other = repo.create_site("acc-002", name="other")
    first = _make_site(repo, name="hk-edge")
    second = _make_site(repo, name="us-edge", trunk_id="ST_us")

    r = client.get("/api/sip/sites", params={"account_id": "acc-001"})

    assert r.status_code == 200
    rows = r.json()
    assert [row["id"] for row in rows] == [first["id"], second["id"]]
    assert rows[1]["trunk_id"] == "ST_us"
    assert all(row["id"] != other["id"] for row in rows)
    assert "auth_password" not in r.text


def test_list_sites_empty_account(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    r = client.get("/api/sip/sites", params={"account_id": "acc-nobody"})
    assert r.status_code == 200 and r.json() == []
