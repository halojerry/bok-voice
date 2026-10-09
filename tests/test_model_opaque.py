"""W③（2026-10-09）模型面 API 收口：厂商名/端点对非 root 零出仓。

契约：
- GET /api/settings、/api/setup 族、/api/asr|tts health·speakers、克隆 CRUD、
  /api/calls/{id}/logs → 平台专属（root/机器/auth-off 直通；admin/user 403）。
- GET /api/tts/voices 保持 interpret|settings-any（负载=不透明音色 id）。
- POST /api/tts/preview：客户端不传 provider——服务端按 voice_id 前缀解析
  （bokclone*/Cantonese_*/English_/moss_* → 云端，其余 → 本地）。
- canned-status 两端点 tts_provider 出仓=cloud/local 不透明档。
- GET /api/calls(+/{id})：非 root 的 session_report.usage 剥 provider/model。
- /api/audit：非 root 的 settings.*/model_routing.* 行整段红action，其余行
  detail 的 provider/base_url/model 键值红action。
- **非 root 任何 200 响应体递归零厂商字符串**（minimax/deepseek/doubao/豆包/
  火山/volcano/bytedance/qwen——qwen 仅在 provider 语义位断言，音色 id 除外）。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "x" * 40)

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import clear_account_expiry_cache, hash_password

PW = "Passw0rd" + "!x"

# 厂商字面量（响应体递归断言用；qwen 作为本地引擎名也收——客户面不区分本地/云端）。
VENDOR_LITERALS = ("minimax", "deepseek", "doubao", "豆包", "火山", "volcano", "bytedance", "qwen")


def _client_and_repo(monkeypatch):
    from control_plane.main import app
    from control_plane.nodes_store import NodeStore
    from fastapi.testclient import TestClient

    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    monkeypatch.setattr(
        app.state, "account_lookup",
        lambda account_id: repo.get_account(account_id), raising=False,
    )
    monkeypatch.setattr(app.state, "node_store", NodeStore(None), raising=False)
    # 测试隔离：auth._ACCOUNT_EXPIRY_CACHE 模块级 TTL 缓存跨测试存活——
    # 换仓即清（前一测试把同名账号缓存成 expired 会污染本文件全部 403）。
    clear_account_expiry_cache()
    _mk(repo, "op-admin", "admin", "acc-a")
    _mk(repo, "op-peon", "user", "acc-a")
    _mk(repo, "op-root", "root", "")
    return TestClient(app), repo


def _mk(repo, username, role, account):
    repo.create_user(
        username=username, password_hash=hash_password(PW),
        role=role, org_id="", account_id=account,
    )


def _login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def _assert_no_vendor_literals(obj, path="$"):
    import json

    blob = json.dumps(obj, ensure_ascii=False).lower()
    hits = [v for v in VENDOR_LITERALS if v in blob]
    assert not hits, f"{path} 响应含厂商字面量 {hits}: {blob[:300]}"


def test_platform_only_surfaces(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    at = _login(client, "op-admin")
    ut = _login(client, "op-peon")
    rt = _login(client, "op-root")
    platform_only = [
        "/api/settings",
        "/api/setup",
        "/api/asr/health",
        "/api/tts/health",
        "/api/tts/speakers",
        "/api/calls/none/logs",
    ]
    for path in platform_only:
        assert client.get(path, headers=_h(at)).status_code == 403, path
        assert client.get(path, headers=_h(ut)).status_code == 403, path
        # root 过闸即通过：health 类探针打本地 sidecar，栈起/没起 200/503 皆业务态
        # （环境敏感断言会抖——只判「不是 401/403 闸错」）。
        code_root = client.get(path, headers=_h(rt)).status_code
        assert code_root not in (401, 403), (path, code_root)
    # 克隆 CRUD 平台专属（GET 清单仍 interpret|settings-any）；POST 须带合法
    # multipart 让闸先于 422 生效。W④：端点路径中性化（cloud-voices）。
    assert client.post("/api/tts/cloud-voices", headers=_h(at),
                       files={"file": ("a.wav", b"RIFF")}, data={"label": "t"}).status_code == 403
    assert client.delete("/api/tts/cloud-voices/x", headers=_h(at)).status_code == 403
    # 旧厂商名路径不复存在（404=路由本身没了，抓包面零厂商词）
    assert client.get("/api/tts/minimax-voices", headers=_h(at)).status_code == 404


def test_settings_masked_view_gone_for_admin(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    at = _login(client, "op-admin")
    r = client.get("/api/settings", headers=_h(at))
    assert r.status_code == 403
    assert "minimax" not in r.text.lower()


def test_preview_provider_resolution(monkeypatch):
    client, _ = _client_and_repo(monkeypatch)
    at = _login(client, "op-admin")  # interpret 页键默认有（admin 直通页闸）
    # 云端前缀 → 平台专属闸（admin 403；不透传 provider 字段也判得出）
    r = client.post("/api/tts/preview", headers=_h(at),
                    json={"voice_id": "bokcloneabcdef12", "text": "x"})
    assert r.status_code == 403
    r = client.post("/api/tts/preview", headers=_h(at),
                    json={"voice": "Cantonese_crisp_news_anchor_vv2", "text": "x"})
    assert r.status_code == 403
    # 本地（无前缀）→ interpret 页闸放行（sidecar 不在 → 5xx 但绝非 403/429 之外的闸错）
    r = client.post("/api/tts/preview", headers=_h(at),
                    json={"voice_id": "some-local-voice", "text": "x"})
    assert r.status_code != 403
    # root 云端前缀 → 过平台闸（api_key 缺 → 503 业务错，非 403）
    rt = _login(client, "op-root")
    r = client.post("/api/tts/preview", headers=_h(rt),
                    json={"voice_id": "bokcloneabcdef12", "text": "x"})
    assert r.status_code != 403


def test_canned_status_opaque(monkeypatch):
    client, _ = _client_and_repo(monkeypatch)
    ut = _login(client, "op-peon")
    import control_plane.pregen as pregen_mod

    fake = {"available": 0, "statuses": {}, "generated_at": "",
            "voice_source": {}, "tts_provider": {"zh": "minimax", "cantonese": "qwen3_tts"}}
    monkeypatch.setattr(pregen_mod, "qa_canned_status", lambda *a, **k: fake)
    r = client.get("/api/qa/canned-status", params={"account_id": "acc-a"}, headers=_h(ut))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tts_provider"] == {"zh": "cloud", "cantonese": "local"}
    _assert_no_vendor_literals(body)


def test_calls_session_report_sanitized(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    at = _login(client, "op-admin")
    rt = _login(client, "op-root")
    from bok_voice_core.types import CallMode, SessionManifest

    manifest = SessionManifest(
        session_id="call-x1", account_id="acc-a", object_id="", persona_id="",
        mode=CallMode.SIMULATION, direction="outbound", language="zh",
        providers={}, template_id="", created_by="",
    )
    row = repo.create_call(manifest)
    repo.update_call(row["id"], **{
        "status": "ended",
        "session_report": {
            "session_id": "call-x1",
            "usage": [
                {"provider": "minimax", "model": "speech-2.8-hd", "label": "tts"},
                {"provider": "deepseek", "model": "deepseek-chat", "label": "llm"},
            ],
            "duration": 12.5,
        },
    })
    # admin 视角：usage 无 provider/model，其余聚合保留
    body = client.get(f"/api/calls/{row['id']}", headers=_h(at)).json()
    usage = body["session_report"]["usage"]
    assert all("provider" not in u and "model" not in u for u in usage)
    assert body["session_report"]["duration"] == 12.5
    _assert_no_vendor_literals(body)
    # root 视角：原文保留
    body_root = client.get(f"/api/calls/{row['id']}", headers=_h(rt)).json()
    assert body_root["session_report"]["usage"][0]["provider"] == "minimax"


def test_persona_engine_three_tier_opaque(monkeypatch):
    """W④：persona tts_provider 出仓三态（cloud/local/""），真值仅 root 可见。"""
    client, repo = _client_and_repo(monkeypatch)
    at = _login(client, "op-admin")
    rt = _login(client, "op-root")
    # 平台（root）设真值的 persona
    created = client.post("/api/personas", headers=_h(rt), json={
        "account_id": "acc-a", "name": "云引擎人设", "language": "zh",
        "tts_provider": "minimax",
    }).json()
    pid = created["id"]
    assert created["tts_provider"] == "minimax"  # root 视角=真值

    rows = client.get("/api/personas", params={"account_id": "acc-a"}, headers=_h(at)).json()
    row = next(r for r in rows if r["id"] == pid)
    assert row["tts_provider"] == "cloud"
    _assert_no_vendor_literals(rows)

    detail = client.get(f"/api/personas/{pid}", headers=_h(at)).json()
    assert detail["tts_provider"] == "cloud"

    # admin PUT 真值=越权尝试 → 保留现值；三态 cloud=合法（映射到平台云端档）
    put = client.put(f"/api/personas/{pid}", headers=_h(at), json={
        "account_id": "acc-a", "name": "云引擎人设", "language": "zh",
        "tts_provider": "minimax_streaming",
    }).json()
    assert put["tts_provider"] == "cloud"  # 出仓口径恒三态
    raw = repo.get_persona(pid)
    assert raw["tts_provider"] == "minimax"  # 真值未被越权改写（保留现值）


def test_audit_redaction(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    at = _login(client, "op-admin")
    rt = _login(client, "op-root")
    # 直接塞审计事件（绕过 _audit 的 correlation 依赖）
    repo.append_audit({
        "action": "settings.save", "account_id": "acc-a",
        "detail": {"llm_provider": "deepseek", "base_url": "https://api.deepseek.com/v1"},
    })
    repo.append_audit({
        "action": "call.create", "account_id": "acc-a",
        "detail": {"provider": "graph-jump", "mode": "simulation"},
    })
    # admin 需 audit 管理键——给账号 admin 补键后可读，但红action生效
    admin = repo.get_user_by_username("op-admin")
    repo.update_user(admin["id"], permissions_json='["calls","audit","users"]')
    rows = client.get("/api/audit", headers=_h(at)).json()
    by_action = {r["action"]: r for r in rows}
    assert by_action["settings.save"]["detail"] == {"redacted": "platform-only"}
    assert by_action["call.create"]["detail"]["provider"] == "[redacted]"
    rows_root = client.get("/api/audit", headers=_h(rt)).json()
    by_action_root = {r["action"]: r for r in rows_root}
    assert by_action_root["settings.save"]["detail"]["base_url"].startswith("https://")
