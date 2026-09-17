"""P1-C /api/token 收编测试（2026-09-17 全量 debug）。

契约：
- 无通话记录的房间名：照发 token 但降为 subscribe-only（can_publish=False、
  can_publish_data=False、can_subscribe=True），不挂 RoomAgentDispatch（不拉
  agent、不空转资源），并留 token.issued 审计行；
- 有记录房间：行为不变（publish grants + bok-voice dispatch）；
- per-identity 频控 30/min，超限 429，身份间互不挤占。

E2E 依赖核查（改闸前 grep scripts/ 全量）：三套 E2E 与全部 probe/soak/acceptance
都是先 POST /api/calls 建单、再拿 call["id"] 当房名取 token——记录恒存在；唯一
无记录用例是 scripts/load_cp_concurrency.py 场景 D（load-{i}×50），其自起 CP 无
LiveKit 凭据、依赖缺凭据 503（该检查位于频控之前，行为不变）。
"""
from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-token-dispatch")
PW = "Passw0rd!x"  # 测试夹具口令(与 test_auth.py 同源),非真实凭据

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import hash_password


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app
    from control_plane.nodes_store import NodeStore

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    # 熔断窒息点中间件对带 Bearer 的 POST /api/token 解 node_token——app 是模块级
    # 单例、startup 未必在本会话跑过，钉内存 NodeStore（NodeStore(None) 双模）。
    monkeypatch.setattr(app.state, "node_store", NodeStore(None), raising=False)
    return TestClient(app), repo


def _mk_user(repo, username, role="user", account="acc-001", password=PW):
    return repo.create_user(
        username=username, password_hash=hash_password(password),
        role=role, org_id="org-t", account_id=account,
    )


def _login(client, username, password=PW):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _claims(participant_token: str) -> dict:
    seg = participant_token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))


def _fresh_limiter(monkeypatch):
    import control_plane.main as cp_main

    monkeypatch.setattr(cp_main, "_token_issue_times", {})  # 隔离进程内已有窗口


# ---- 无记录房间：subscribe-only + 无 dispatch + 审计 ----


def test_recordless_room_subscribe_only_no_dispatch(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    _fresh_limiter(monkeypatch)
    audits = []
    monkeypatch.setattr("control_plane.main._audit", lambda action, **kw: audits.append((action, kw)))
    r = client.post("/api/token", json={"account_id": "acc-001", "room_name": "no-such-room"})
    assert r.status_code == 201, r.text
    claims = _claims(r.json()["participantToken"])
    assert claims["video"]["canPublish"] is False
    assert claims["video"]["canPublishData"] is False
    assert claims["video"]["canSubscribe"] is True
    assert "roomConfig" not in claims  # 不挂 agent dispatch
    issued = [kw for action, kw in audits if action == "token.issued"]
    assert len(issued) == 1
    assert issued[0]["subject_id"] == "no-such-room"
    assert issued[0]["detail"]["recordless"] is True


def test_recordless_call_id_alias_same_treatment(monkeypatch):
    """旧 call_id 兼容路径同样收口：无记录的 call_id 房间=subscribe-only。"""
    client, repo = _client_and_repo(monkeypatch)
    _fresh_limiter(monkeypatch)
    r = client.post("/api/token", json={"account_id": "acc-001", "call_id": "ghost-call"})
    assert r.status_code == 201
    claims = _claims(r.json()["participantToken"])
    assert claims["video"]["canPublish"] is False
    assert "roomConfig" not in claims


# ---- 有记录房间：行为不变 ----


def test_existing_record_full_publish_and_dispatch(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    _fresh_limiter(monkeypatch)
    call = client.post("/api/calls", json={"account_id": "acc-001"}).json()
    r = client.post("/api/token", json={"account_id": "acc-001", "call_id": call["id"]})
    assert r.status_code == 201, r.text
    claims = _claims(r.json()["participantToken"])
    assert claims["video"]["canPublish"] is True
    assert claims["video"]["canPublishData"] is True
    agents = claims.get("roomConfig", {}).get("agents") or []
    assert agents and agents[0]["agentName"] == "bok-voice"
    assert json.loads(agents[0]["metadata"])["call_id"] == call["id"]


def test_existing_interpret_record_dispatch_unchanged(monkeypatch):
    """B 线（kind=interpret）me 端照挂双向 dispatch——recordless 闸不误伤有记录路径。"""
    client, repo = _client_and_repo(monkeypatch)
    _fresh_limiter(monkeypatch)
    call = client.post(
        "/api/calls",
        json={"account_id": "acc-001", "kind": "interpret", "language": "zh", "target_lang": "en"},
    ).json()
    r = client.post(
        "/api/token",
        json={"account_id": "acc-001", "call_id": call["id"], "role": "me"},
    )
    assert r.status_code == 201, r.text
    claims = _claims(r.json()["participantToken"])
    names = {a["agentName"] for a in (claims.get("roomConfig", {}).get("agents") or [])}
    assert names == {"bok-interp-fwd", "bok-interp-rev"}


# ---- B 线会话级音色（voices_json → dispatch metadata 透传，2026-09-17）----


def test_interpret_session_voices_metadata_passthrough(monkeypatch):
    """同传建单 voices_json → call_sessions 落列 → fwd/rev 双 dispatch metadata
    各带 "voices"（同份 JSON map，worker 按自己 target_lang 取键）。"""
    client, repo = _client_and_repo(monkeypatch)
    _fresh_limiter(monkeypatch)
    voices = '{"zh":"moss_audio_zh","en":"English_magnetic_voiced_man"}'
    call = client.post(
        "/api/calls",
        json={
            "account_id": "acc-001", "kind": "interpret",
            "language": "zh", "target_lang": "en", "voices_json": voices,
        },
    ).json()
    assert repo.get_call(call["id"])["voices_json"] == voices
    r = client.post(
        "/api/token",
        json={"account_id": "acc-001", "call_id": call["id"], "role": "me"},
    )
    assert r.status_code == 201, r.text
    agents = {
        a["agentName"]: json.loads(a["metadata"])
        for a in (_claims(r.json()["participantToken"]).get("roomConfig", {}).get("agents") or [])
    }
    assert set(agents) == {"bok-interp-fwd", "bok-interp-rev"}
    assert agents["bok-interp-fwd"]["voices"] == voices
    assert agents["bok-interp-rev"]["voices"] == voices
    # 语言对互换语义不回归：fwd 译对方语言、rev 译我方语言
    assert agents["bok-interp-fwd"]["target_lang"] == "en"
    assert agents["bok-interp-rev"]["target_lang"] == "zh"


def test_interpret_voices_json_truncated_to_512(monkeypatch):
    """voices_json 建单 512 字硬截（防 metadata 膨胀,同 glossary 1000 字先例）。"""
    client, repo = _client_and_repo(monkeypatch)
    _fresh_limiter(monkeypatch)
    call = client.post(
        "/api/calls",
        json={
            "account_id": "acc-001", "kind": "interpret",
            "language": "zh", "target_lang": "en",
            "voices_json": '{"zh":"' + "v" * 600 + '"}',
        },
    ).json()
    stored = repo.get_call(call["id"])["voices_json"]
    assert len(stored) == 512
    assert stored.startswith('{"zh":"vvvv')


# ---- per-identity 频控 ----


def test_token_rate_limit_429_and_per_identity(monkeypatch):
    import control_plane.main as cp_main

    client, repo = _client_and_repo(monkeypatch)
    _fresh_limiter(monkeypatch)
    cp_main._token_issue_times["anon"] = [time.monotonic()] * cp_main._TOKEN_RATE_LIMIT
    r = client.post("/api/token", json={"account_id": "acc-001", "room_name": "rate-x"})
    assert r.status_code == 429
    # user 身份独立窗口：anon 打满不挤占 user
    _mk_user(repo, "tk-user")
    tok = _login(client, "tk-user")
    r2 = client.post(
        "/api/token",
        json={"account_id": "acc-001", "room_name": "rate-x"}, headers=_auth(tok),
    )
    assert r2.status_code == 201, r2.text
    assert "roomConfig" not in _claims(r2.json()["participantToken"])  # recordless 降级不变
    # 窗口滑出后 anon 恢复
    cp_main._token_issue_times["anon"] = []
    r3 = client.post("/api/token", json={"account_id": "acc-001", "room_name": "rate-x"})
    assert r3.status_code == 201
