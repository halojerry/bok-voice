"""建站端点 `POST /api/sip/sites`（spec 2026-09-13 sip-edge-thin-node-v2 §7 P1.5 T7）。

钉死四件事：

1. **建行链路**——最小 body（name + livekit_url）即可建出站点行，id 形如 `site-*`，
   其余字段（sip_edge/trunk_id/numbers/region）按入参或默认落库；建出后
   `GET /api/sip/sites` 立即可见（面板下拉数据源）。
2. **幂等**（T7 定案）：同 account+name 重复 POST 返回**同一行**（不重复建、不覆盖
   ——已注册的 trunk_id 不能被重名提交静默清掉）；同名不同 account 各自成行。
3. **失败面**：name 空=400；sip_edge 越界（值域 none|local|cloud）=400（空=local）；
   numbers 非 list[str]=422（Pydantic 严格类型，T1 审查同款防线）。
4. **审计** `sip.site_created`（建站可追溯；幂等命中不重复审计）。

TestClient + InMemory repo 姿势照 `tests/test_sip_trunk_api.py`。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from bok_voice_business_db.repository import InMemoryBusinessRepository
    from control_plane.main import app

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    return TestClient(app), repo


def _post_site(client, **over):
    body = {"name": "hk-edge", "livekit_url": "ws://vps.example:7880"}
    body.update(over)
    return client.post("/api/sip/sites", json=body)


# ---- 成功路径 ----


def test_create_site_minimal(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)

    r = _post_site(client)

    assert r.status_code == 200
    site = r.json()
    assert site["id"].startswith("site-") and site["id"] != "site-local"
    assert site["name"] == "hk-edge"
    assert site["livekit_url"] == "ws://vps.example:7880"
    assert site["sip_edge"] == "local"  # 缺省值
    assert site["trunk_id"] == "" and site["numbers"] == [] and site["region"] == ""
    assert repo.get_site(site["id"])["name"] == "hk-edge"
    # 列表立即可见（面板下拉数据源）
    rows = client.get("/api/sip/sites", params={"account_id": "acc-001"}).json()
    assert [row["id"] for row in rows] == [site["id"]]


def test_create_site_full_fields(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)

    r = _post_site(client, name="hk-cloud", livekit_url="wss://lk.example",
                   sip_edge="cloud", trunk_id="ST_manual",
                   numbers=["+12025550123"], region="hk")

    assert r.status_code == 200
    site = r.json()
    assert site["sip_edge"] == "cloud" and site["trunk_id"] == "ST_manual"
    assert site["numbers"] == ["+12025550123"] and site["region"] == "hk"


def test_create_site_empty_sip_edge_defaults_local(monkeypatch):
    """`sip_edge=""` 视同缺省（面板空值不该 400）——与 repo 默认值同向。"""
    client, _repo = _client_and_repo(monkeypatch)

    r = _post_site(client, sip_edge="")

    assert r.status_code == 200 and r.json()["sip_edge"] == "local"


def test_create_site_trims_whitespace(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)

    r = _post_site(client, name="  hk-edge  ", livekit_url=" ws://vps:7880 ",
                   numbers=[" +12025550123 ", "  "])

    assert r.status_code == 200
    site = r.json()
    assert site["name"] == "hk-edge" and site["livekit_url"] == "ws://vps:7880"
    assert site["numbers"] == ["+12025550123"]


# ---- 幂等 ----


def test_create_site_idempotent_same_account_name(monkeypatch):
    """同 account+name 重复 POST=返回既有行；不改写字段（trunk_id 保守住）。"""
    client, repo = _client_and_repo(monkeypatch)
    first = _post_site(client, name="hk-edge", trunk_id="ST_keep").json()

    again = _post_site(client, name="hk-edge", livekit_url="wss://changed.example")

    assert again.status_code == 200
    assert again.json()["id"] == first["id"]
    assert again.json()["trunk_id"] == "ST_keep"  # 幂等不覆盖
    assert again.json()["livekit_url"] == "ws://vps.example:7880"
    assert len(repo.list_sites("acc-001")) == 1


def test_create_site_same_name_other_account_separate_rows(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)

    a = _post_site(client, account_id="acc-001").json()
    b = _post_site(client, account_id="acc-002").json()

    assert a["id"] != b["id"]
    assert len(repo.list_sites("acc-001")) == 1 and len(repo.list_sites("acc-002")) == 1


# ---- 失败面 ----


def test_create_site_blank_name_400(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)

    r = _post_site(client, name="   ")

    assert r.status_code == 400 and "name" in r.json()["detail"]
    assert repo.list_sites("acc-001") == []


def test_create_site_missing_name_422(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    r = client.post("/api/sip/sites", json={"livekit_url": "ws://vps:7880"})
    assert r.status_code == 422


def test_create_site_bad_sip_edge_400(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)

    r = _post_site(client, sip_edge="vps")

    assert r.status_code == 400 and "sip_edge" in r.json()["detail"]
    assert repo.list_sites("acc-001") == []


def test_create_site_numbers_must_be_list_str(monkeypatch):
    """`numbers` 严格 `list[str]`：字符串/数字/对象一律 422（T1 审查同款防线）。"""
    client, repo = _client_and_repo(monkeypatch)

    for bad in ("+12025550123", 123, {"n": 1}, ["+12025550123", 4]):
        r = _post_site(client, numbers=bad)
        assert r.status_code == 422, bad

    assert repo.list_sites("acc-001") == []


# ---- 审计 ----


def test_create_site_audited(monkeypatch):
    """审计 `sip.site_created`；幂等命中不重复审计（建站动作一次一条）。"""
    client, _repo = _client_and_repo(monkeypatch)

    with client:
        first = _post_site(client, name="hk-edge").json()
        _post_site(client, name="hk-edge")  # 幂等命中
        rows = client.get("/api/audit", params={"action": "sip.site_created"}).json()

    assert len(rows) == 1
    assert rows[0]["subject_id"] == first["id"]
    assert rows[0]["account_id"] == "acc-001"
    assert rows[0]["detail"]["name"] == "hk-edge"
