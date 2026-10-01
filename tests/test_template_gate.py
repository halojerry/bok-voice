"""实时通话强绑话术模板闸契约（2026-09-27）。

`_create_call_in` 是 POST /api/calls / dial-now / campaign 循环的唯一建单汇聚点。
规则：请求 `mode == live` 且 `kind != "interpret"` 且解析后 template_id 为空
（显式 template_id 优先，缺省回落对象卡绑定）→ 400；simulation / realtime_demo
与 kind=interpret 豁免。kill-switch `BOK_REQUIRE_TEMPLATE=0` 回旧行为。

所有用例显式 setenv —— 兄弟测试文件（test_dial_now_api / test_campaign_loop）
为隔离在模块级把该 env 设成 "0"，进程内共享 os.environ，不能假设缺省态。

测试全走内存仓 + TestClient / fake dispatcher，不连真 LiveKit。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-template-gate")

from bok_voice_business_db.repository import InMemoryBusinessRepository  # noqa: E402

import control_plane.main as cp_main  # noqa: E402


class _FakeDispatch:
    """记录 dial-now 派发调用（照 test_dial_now_api 姿势）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, room: str, metadata: str) -> None:
        self.calls.append((room, metadata))


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    audits: list[tuple] = []
    monkeypatch.setattr(cp_main, "_audit", lambda action, **kw: audits.append((action, kw)))
    client = TestClient(app).__enter__()
    return client, repo, audits


def _make_object(repo, account_id: str = "acc-001", **over) -> dict:
    fields = {"display_name": "A", "phone": "+85291112222", "language": "zh"}
    fields.update(over)
    return repo.create_object(account_id, fields)


# ---------------------------------------------------------------------------
# POST /api/calls
# ---------------------------------------------------------------------------


def test_live_without_template_is_rejected(monkeypatch):
    """live + 无 template_id + 无 object（无对象卡绑定）→ 400，不落库。"""
    client, repo, audits = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_REQUIRE_TEMPLATE", "1")
    try:
        r = client.post("/api/calls", json={"account_id": "acc-001", "mode": "live"})
        assert r.status_code == 400, r.text
        assert "话术模板" in r.json()["detail"]
        assert repo.list_calls("") == []
        assert any(a[0] == "call.reject_no_template" for a in audits)
    finally:
        client.__exit__(None, None, None)


def test_live_object_without_template_is_rejected(monkeypatch):
    """live + object 卡未绑定 template_id → 400（解析后仍为空）。"""
    client, repo, _audits = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_REQUIRE_TEMPLATE", "1")
    obj = _make_object(repo)
    try:
        r = client.post("/api/calls",
                        json={"account_id": "acc-001", "object_id": obj["id"], "mode": "live"})
        assert r.status_code == 400, r.text
        assert repo.list_calls("") == []
    finally:
        client.__exit__(None, None, None)


def test_live_with_explicit_template_passes(monkeypatch):
    """live + 显式 template_id → 200，快照落通话。"""
    client, repo, _audits = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_REQUIRE_TEMPLATE", "1")
    try:
        r = client.post("/api/calls", json={"account_id": "acc-001", "mode": "live",
                                            "template_id": "tpl-explicit"})
        assert r.status_code == 200, r.text
        assert r.json()["template_id"] == "tpl-explicit"
    finally:
        client.__exit__(None, None, None)


def test_live_with_object_bound_template_passes(monkeypatch):
    """live + 对象卡绑定 template_id（无显式）→ 200，快照取对象绑定。"""
    client, repo, _audits = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_REQUIRE_TEMPLATE", "1")
    obj = _make_object(repo, template_id="tpl-object")
    try:
        r = client.post("/api/calls",
                        json={"account_id": "acc-001", "object_id": obj["id"], "mode": "live"})
        assert r.status_code == 200, r.text
        assert r.json()["template_id"] == "tpl-object"
    finally:
        client.__exit__(None, None, None)


def test_kind_interpret_without_template_passes(monkeypatch):
    """kind=interpret（B 线同传，无话术语义）+ live + 无模板 → 200。"""
    client, repo, _audits = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_REQUIRE_TEMPLATE", "1")
    try:
        r = client.post("/api/calls", json={
            "account_id": "acc-001", "kind": "interpret", "mode": "live",
            "direction": "interpret", "language": "zh", "target_lang": "en",
        })
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "interpret"
    finally:
        client.__exit__(None, None, None)


def test_simulation_without_template_passes(monkeypatch):
    """mode=simulation（训练/画布试跑）+ 无模板 → 200（不吃话术漏斗）。"""
    client, repo, _audits = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_REQUIRE_TEMPLATE", "1")
    try:
        r = client.post("/api/calls", json={"account_id": "acc-001", "mode": "simulation"})
        assert r.status_code == 200, r.text
    finally:
        client.__exit__(None, None, None)


def test_realtime_demo_without_template_passes(monkeypatch):
    """mode=realtime_demo（云端 S2S 演示）+ 无模板 → 200（不吃本地话术漏斗）。"""
    client, repo, _audits = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_REQUIRE_TEMPLATE", "1")
    try:
        r = client.post("/api/calls", json={"account_id": "acc-001", "mode": "realtime_demo",
                                            "object_id": "E2E-probe-1"})
        assert r.status_code == 200, r.text
    finally:
        client.__exit__(None, None, None)


def test_kill_switch_restores_old_behavior(monkeypatch):
    """BOK_REQUIRE_TEMPLATE=0 → live + 无模板照建（旧行为）。"""
    client, repo, _audits = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_REQUIRE_TEMPLATE", "0")
    try:
        r = client.post("/api/calls", json={"account_id": "acc-001", "mode": "live"})
        assert r.status_code == 200, r.text
        assert len(repo.list_calls("")) == 1
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# POST /api/objects/{id}/dial-now（同一建单汇聚点）
# ---------------------------------------------------------------------------


def _patch_dispatcher(monkeypatch) -> _FakeDispatch:
    fake = _FakeDispatch()
    monkeypatch.setattr("control_plane.campaign._default_dispatcher", fake)
    return fake


def test_dial_now_without_template_is_rejected(monkeypatch):
    """dial-now：对象无模板 + 无显式 template_id → 400（闸口拦在建单前）。"""
    client, repo, _audits = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_REQUIRE_TEMPLATE", "1")
    _patch_dispatcher(monkeypatch)
    obj = _make_object(repo)
    try:
        r = client.post(f"/api/objects/{obj['id']}/dial-now", json={})
        assert r.status_code == 400, r.text
        assert repo.list_calls("") == []
    finally:
        client.__exit__(None, None, None)


def test_dial_now_with_object_bound_template_passes(monkeypatch):
    """dial-now：对象卡绑定模板 → 200，快照取对象绑定。"""
    client, repo, _audits = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_REQUIRE_TEMPLATE", "1")
    fake = _patch_dispatcher(monkeypatch)
    obj = _make_object(repo, template_id="tpl-object")
    try:
        r = client.post(f"/api/objects/{obj['id']}/dial-now", json={})
        assert r.status_code == 200, r.text
        call_id = r.json()["call_id"]
        assert repo.get_call(call_id)["template_id"] == "tpl-object"
        assert len(fake.calls) == 1
    finally:
        client.__exit__(None, None, None)


def test_dial_now_with_explicit_template_passes(monkeypatch):
    """dial-now：显式 template_id 覆盖对象（旧版先建后改会绕过闸口）→ 200 且快照=显式。"""
    client, repo, _audits = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_REQUIRE_TEMPLATE", "1")
    _patch_dispatcher(monkeypatch)
    obj = _make_object(repo)  # 对象无模板——旧序必被误拒
    try:
        r = client.post(f"/api/objects/{obj['id']}/dial-now", json={"template_id": "tpl-explicit"})
        assert r.status_code == 200, r.text
        assert repo.get_call(r.json()["call_id"])["template_id"] == "tpl-explicit"
    finally:
        client.__exit__(None, None, None)


def test_dial_now_kill_switch_restores_old_behavior(monkeypatch):
    """BOK_REQUIRE_TEMPLATE=0 → dial-now 无模板照建。"""
    client, repo, _audits = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_REQUIRE_TEMPLATE", "0")
    _patch_dispatcher(monkeypatch)
    obj = _make_object(repo)
    try:
        r = client.post(f"/api/objects/{obj['id']}/dial-now", json={})
        assert r.status_code == 200, r.text
    finally:
        client.__exit__(None, None, None)
