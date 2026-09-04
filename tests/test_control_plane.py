import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

from fastapi.testclient import TestClient

from control_plane.main import app


def test_idempotent_migration_adds_missing_columns(tmp_path, monkeypatch):
    """对旧版建出的 SQLite（缺新列）启动时须自动补列，且幂等可重复。

    SQLite 不支持 `ADD COLUMN IF NOT EXISTS`（曾导致迁移静默失败、建通话 500）。
    """
    import sqlite3

    from sqlalchemy import text

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE call_sessions (id VARCHAR(64) PRIMARY KEY, account_id VARCHAR(64), object_id VARCHAR(64), persona_id VARCHAR(64), mode VARCHAR(32), status VARCHAR(32));"
        "CREATE TABLE object_profiles (id VARCHAR(64) PRIMARY KEY, account_id VARCHAR(64), display_name VARCHAR(255));"
        "CREATE TABLE settlements (id VARCHAR(64) PRIMARY KEY, call_id VARCHAR(64));"
    )
    conn.close()

    from control_plane.deps import build_engine

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    # 首启：补列
    build_engine()
    # 二启：幂等不报错
    build_engine()

    import sqlite3 as s3

    c = s3.connect(db)
    cols = {t: [r[1] for r in c.execute(f"PRAGMA table_info({t})")] for t in ("call_sessions", "object_profiles", "settlements")}
    c.close()
    assert "template_id" in cols["call_sessions"]
    assert "template_id" in cols["object_profiles"]
    assert "summary" in cols["settlements"]


def test_data_migration_yue_to_cantonese(tmp_path, monkeypatch):
    """存量 language='yue' 行 + reference_audio/global_settings 的 yue 键 → cantonese（幂等）。

    字段统一：新代码只产出 cantonese；旧数据在 control-plane 启动时一次性迁移。
    """
    import json
    import sqlite3

    from sqlalchemy import text

    db = tmp_path / "migrate.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE persona_profiles (id VARCHAR(64) PRIMARY KEY, account_id VARCHAR(64) DEFAULT '', name VARCHAR(255) DEFAULT '', company VARCHAR(255) DEFAULT '', tone TEXT DEFAULT '', language VARCHAR(16) DEFAULT 'zh', reference_audio VARCHAR(512) DEFAULT '', tts_provider VARCHAR(32) DEFAULT '');"
        "CREATE TABLE object_profiles (id VARCHAR(64) PRIMARY KEY, account_id VARCHAR(64) DEFAULT '', display_name VARCHAR(255) DEFAULT '', role_template VARCHAR(64) DEFAULT '', language VARCHAR(16) DEFAULT 'zh', background TEXT DEFAULT '', phone VARCHAR(64) DEFAULT '', tracking_no VARCHAR(64) DEFAULT '', courier VARCHAR(64) DEFAULT '', address VARCHAR(255) DEFAULT '', template_id VARCHAR(64) DEFAULT '', status VARCHAR(32) DEFAULT 'active');"
        "CREATE TABLE call_sessions (id VARCHAR(64) PRIMARY KEY, account_id VARCHAR(64) DEFAULT '', object_id VARCHAR(64) DEFAULT '', persona_id VARCHAR(64) DEFAULT '', mode VARCHAR(32) DEFAULT '', status VARCHAR(32) DEFAULT '', language TEXT DEFAULT '', template_id VARCHAR(64) DEFAULT '', whatsapp_status VARCHAR(16) DEFAULT '', customer_whatsapp VARCHAR(64) DEFAULT '');"
        "CREATE TABLE conversation_templates (id VARCHAR(64) PRIMARY KEY, account_id VARCHAR(64) DEFAULT '', name VARCHAR(255) DEFAULT '', opening TEXT DEFAULT '', core TEXT DEFAULT '', objection TEXT DEFAULT '', closing TEXT DEFAULT '', tone_override VARCHAR(255) DEFAULT '', language VARCHAR(16) DEFAULT 'zh', steps_json TEXT DEFAULT '');"
        "CREATE TABLE global_settings (id VARCHAR(64) PRIMARY KEY, asr_json TEXT DEFAULT '{}', llm_json TEXT DEFAULT '{}', tts_json TEXT DEFAULT '{}', vad_json TEXT DEFAULT '{}', policy VARCHAR(64) DEFAULT 'offline_first');"
    )
    conn.execute(
        "INSERT INTO persona_profiles (id, language, reference_audio) VALUES ('p1','yue', ?)",
        (json.dumps({"zh": "male-qn-qingse", "yue": "Cantonese_GentleLady"}, ensure_ascii=False),),
    )
    conn.execute("INSERT INTO persona_profiles (id, language, reference_audio) VALUES ('p2','zh','{}')")
    conn.execute("INSERT INTO object_profiles (id, language) VALUES ('o1','yue')")
    conn.execute("INSERT INTO call_sessions (id, language) VALUES ('c1','yue')")
    conn.execute("INSERT INTO conversation_templates (id, language) VALUES ('t1','yue')")
    conn.execute(
        "INSERT INTO global_settings (id, tts_json, vad_json) VALUES ('global', ?, ?)",
        (
            json.dumps({"provider": "minimax", "speaker_zh": "x", "speaker_yue": "Cantonese_crisp_news_anchor_vv2"}),
            json.dumps({"provider": "silero", "min_silence_duration": 0.45, "min_speech_duration": 0.15}),
        ),
    )
    conn.commit()
    conn.close()

    from control_plane.deps import build_engine

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    build_engine()
    build_engine()  # 幂等：重跑不报错、不重复改写

    c = sqlite3.connect(db)
    _ids = {"persona_profiles": "p1", "object_profiles": "o1", "call_sessions": "c1", "conversation_templates": "t1"}
    langs = {
        t: c.execute(f"SELECT language FROM {t} WHERE id='{_ids[t]}'").fetchone()[0]
        for t in ("persona_profiles", "object_profiles", "call_sessions", "conversation_templates")
    }
    assert all(v == "cantonese" for v in langs.values()), langs
    ref = c.execute("SELECT reference_audio FROM persona_profiles WHERE id='p1'").fetchone()[0]
    assert json.loads(ref) == {"zh": "male-qn-qingse", "cantonese": "Cantonese_GentleLady"}
    # 未含 yue 的行/值不动（voice ID Cantonese_* 保持）。
    ref_zh = c.execute("SELECT reference_audio FROM persona_profiles WHERE id='p2'").fetchone()[0]
    assert json.loads(ref_zh) == {}
    tts = json.loads(c.execute("SELECT tts_json FROM global_settings WHERE id='global'").fetchone()[0])
    assert "speaker_yue" not in tts and tts["speaker_cantonese"] == "Cantonese_crisp_news_anchor_vv2"
    # 迁移只改键名，不改 vad 数值（用户调过的 VAD 值不被启动迁移覆盖）。
    vad = json.loads(c.execute("SELECT vad_json FROM global_settings WHERE id='global'").fetchone()[0])
    assert vad["min_silence_duration"] == 0.45
    assert vad["min_speech_duration"] == 0.15
    c.close()


def test_control_plane_flow():
    with TestClient(app) as client:
        assert client.get("/health").json() == {"ok": True, "service": "bok-voice-control-plane"}
        token = client.post("/api/token", json={"account_id": "acc-001"}).json()
        assert token["roomName"]
        import jwt

        claims = jwt.decode(token["token"], options={"verify_signature": False})
        assert claims["video"]["room"] == token["roomName"]
        assert claims["video"]["roomJoin"] is True
        assert token["url"] == "ws://127.0.0.1:7880"
        created = client.post(
            "/api/calls",
            json={"account_id": "acc-001", "object_id": "obj-1", "persona_id": "p-1", "mode": "simulation"},
        ).json()
        call_id = created["id"]
        client.post(f"/api/calls/{call_id}/turns", params={"role": "user", "transcript": "嗯 然后 优惠", "emotion": "neutral"})
        settled = client.post(f"/api/calls/{call_id}/settle").json()
        assert settled["status"] == "done"
        assert "summary" in settled  # settle 结果应携带总结正文（无 LLM 时回退纯指标摘要）


def test_interpret_session_token_roles_and_dispatch():
    """同传会话(kind=interpret):me/other 角色签发对应 identity;me 端 token
    挂 RoomAgentDispatch 显式分发两个方向的 interpreter;object_id 允许为空。"""
    with TestClient(app) as client:
        import json

        import jwt

        created = client.post(
            "/api/calls",
            json={
                "account_id": "acc-001",
                "object_id": "",
                "kind": "interpret",
                "mode": "live",
                "direction": "interpret",
                "language": "zh",
                "target_lang": "en",
            },
        ).json()
        assert created["kind"] == "interpret" and created["target_lang"] == "en"
        room = created["id"]

        me = client.post("/api/token", json={"account_id": "acc-001", "call_id": room, "role": "me"}).json()
        other = client.post("/api/token", json={"account_id": "acc-001", "call_id": room, "role": "other"}).json()
        me_claims = jwt.decode(me["token"], options={"verify_signature": False})
        other_claims = jwt.decode(other["token"], options={"verify_signature": False})
        assert me_claims["sub"] == f"me-{room}"
        assert other_claims["sub"] == f"other-{room}"

        # me 端(房间创建者)token 携带 agent 分发:两方向 + 语言对 metadata。
        room_config = me_claims.get("roomConfig") or me_claims.get("room_config") or {}
        agents = room_config.get("agents") or []
        names = {a.get("agentName") or a.get("agent_name") for a in agents}
        assert {"bok-interp-fwd", "bok-interp-rev"} <= names
        metas = [json.loads(a.get("metadata") or "{}") for a in agents]
        fwd = next(m for m in metas if m.get("listen") == "me-")
        assert fwd["deliver"] == "other-" and fwd["source_lang"] == "zh" and fwd["target_lang"] == "en"

        # 对方端 token 不挂 agent 分发(只有首个建房者生效)。
        assert not (other_claims.get("roomConfig") or other_claims.get("room_config"))


def test_token_requires_livekit_credentials(monkeypatch):
    """缺少 LiveKit 凭据时必须显式 503，绝不能回退 sha256 假 token。"""
    from control_plane import main as m

    monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)
    old_key = m.app.state.lk_key
    old_secret = m.app.state.lk_secret
    m.app.state.lk_key = ""
    m.app.state.lk_secret = ""
    try:
        with TestClient(m.app) as client:
            resp = client.post("/api/token", json={"account_id": "acc-001"})
            assert resp.status_code == 503
            assert "credentials" in resp.json()["detail"].lower()
    finally:
        m.app.state.lk_key = old_key
        m.app.state.lk_secret = old_secret


def test_settle_writes_transcript_docs(tmp_path, monkeypatch):
    """结算必须把 transcript.md / settlement.md 真实落盘到 vault（按对象/通话留存）。"""
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path / "vault"))
    with TestClient(app) as client:
        created = client.post(
            "/api/calls",
            json={"account_id": "acc-001", "object_id": "obj-1", "persona_id": "p-1", "mode": "simulation"},
        ).json()
        call_id = created["id"]
        client.post(f"/api/calls/{call_id}/turns", params={"role": "user", "transcript": "你好，我想了解套餐"}).json()
        settled = client.post(f"/api/calls/{call_id}/settle").json()
        assert settled["status"] == "done"
        base = tmp_path / "vault" / "accounts" / "acc-001" / "objects" / "obj-1" / "calls" / call_id
        transcript = base / "transcript.md"
        settlement = base / "settlement.md"
        assert transcript.exists()
        assert settlement.exists()
        assert "你好，我想了解套餐" in transcript.read_text(encoding="utf-8")
        assert "通话结算" in settlement.read_text(encoding="utf-8")


def test_settings_object_persona_knowledge_and_reports():
    with TestClient(app) as client:
        settings = client.get("/api/settings").json()
        assert settings["policy"] == "offline_first"
        saved = client.put(
            "/api/settings",
            json={
                "asr": {"provider": "sherpa_sensevoice", "model": "sensevoice"},
                "llm": {"provider": "local_openai", "model": "local", "api_key": "mlx"},
                "tts": {"provider": "volcano_streaming", "access_token": "secret"},
                "vad": {"provider": "silero"},
                "policy": "offline_first",
            },
        ).json()
        assert saved["llm"]["has_api_key"] is True

        obj = client.post(
            "/api/objects",
            params={"account_id": "acc-001"},
            json={"display_name": "Nguyen", "role_template": "buyer", "language": "vi", "background": "test"},
        ).json()
        obj_id = obj["id"]
        updated = client.patch(f"/api/objects/{obj_id}", json={"display_name": "Nguyen V2"}).json()
        assert updated["display_name"] == "Nguyen V2"

        persona = client.post("/api/personas", json={"account_id": "acc-001", "name": "小博"}).json()
        persona_id = persona["id"]
        assert client.put(f"/api/personas/{persona_id}", json={"account_id": "acc-001", "name": "小博2"}).json()["name"] == "小博2"

        imported = client.post(
            "/api/knowledge/import",
            json={"account_id": "acc-001", "path": "p.md", "content": "产品知识"},
        ).json()
        assert imported["indexed"] >= 1
        docs = client.get("/api/knowledge", params={"account_id": "acc-001"}).json()
        assert any(d["path"].endswith("p.md") for d in docs)


def test_knowledge_delete_removes_vault_file_and_does_not_resurrect(tmp_path, monkeypatch):
    """删除知识必须同步删除 vault 源文件——否则重启重建索引时「已删除知识复活」。"""
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path / "vault"))
    from control_plane import main as m

    with TestClient(m.app) as client:
        imported = client.post(
            "/api/knowledge/import",
            json={"account_id": "acc-001", "path": "del-me.md", "content": "这段知识稍后删除"},
        ).json()
        assert imported["indexed"] >= 1
        docs = client.get("/api/knowledge", params={"account_id": "acc-001"}).json()
        target = next(d for d in docs if str(d.get("path", "")).endswith("del-me.md"))
        vault_file = tmp_path / "vault" / "accounts" / "acc-001" / "knowledge" / "del-me.md"
        assert vault_file.exists()

        resp = client.delete("/api/knowledge", params={"knowledge_id": target["id"], "account_id": "acc-001"})
        assert resp.status_code == 200
        assert not vault_file.exists(), "删除后 vault 源文件必须被移除"
        remaining = client.get("/api/knowledge", params={"account_id": "acc-001"}).json()
        assert not any(str(d.get("path", "")).endswith("del-me.md") for d in remaining)

        # 模拟进程重启时的重建：文件已删，文档不应复活。
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(m._rebuild_in_memory_knowledge(m.app.state.knowledge.vector, str(tmp_path / "vault")))
        finally:
            loop.close()
        after = client.get("/api/knowledge", params={"account_id": "acc-001"}).json()
        assert not any(str(d.get("path", "")).endswith("del-me.md") for d in after)


def test_supervisor_pause_resume_roundtrip():
    """主管台暂停/恢复要真实改通话状态（agent 轮询到后生效）。"""
    with TestClient(app) as client:
        created = client.post(
            "/api/calls",
            json={"account_id": "acc-001", "object_id": "obj-1", "persona_id": "p-1", "mode": "live"},
        ).json()
        call_id = created["id"]
        assert created["status"] == "ringing"

        paused = client.post(f"/api/supervisor/{call_id}/pause-agent").json()
        assert paused["status"] == "paused"

        takeover = client.post(f"/api/supervisor/{call_id}/takeover").json()
        assert takeover["status"] == "paused"
        assert client.get(f"/api/calls/{call_id}").json()["escalated_to_human"] is True

        resumed = client.post(f"/api/supervisor/{call_id}/resume-agent").json()
        assert resumed["status"] == "active"
        assert client.get(f"/api/calls/{call_id}").json()["escalated_to_human"] is False


def test_whatsapp_capture_and_handled_roundtrip():
    """Agent 偵測客戶 WhatsApp → offered→captured→handled,唔覆寫已知號碼。"""
    with TestClient(app) as client:
        created = client.post(
            "/api/calls",
            json={"account_id": "acc-001", "object_id": "obj-1", "persona_id": "p-1", "mode": "simulation"},
        ).json()
        call_id = created["id"]
        assert created.get("whatsapp_status", "") in ("", None)

        # offered(客戶應承加,無號碼)
        r = client.post(f"/api/calls/{call_id}/whatsapp", json={"number": ""}).json()
        assert r["whatsapp_status"] == "offered"
        # captured(客戶俾號碼)
        r = client.post(f"/api/calls/{call_id}/whatsapp", json={"number": "6868123456"}).json()
        assert r["whatsapp_status"] == "captured"
        assert r["customer_whatsapp"] == "6868123456"
        # captured 後空 offered 唔會降級 / 同號碼唔重寫
        r = client.post(f"/api/calls/{call_id}/whatsapp", json={"number": ""}).json()
        assert r["whatsapp_status"] == "captured"
        # handled 後唔再降級(專員已對接)
        r = client.post(f"/api/calls/{call_id}/whatsapp/handled", json={"handled": True}).json()
        assert r["whatsapp_status"] == "handled"
        r = client.post(f"/api/calls/{call_id}/whatsapp", json={"number": "90000000"}).json()
        assert r["whatsapp_status"] == "handled"
        assert r["customer_whatsapp"] == "6868123456"
        # 404
        assert client.post("/api/calls/nope/whatsapp", json={"number": "1"}).status_code == 404


def test_conversation_template_crud_and_object_binding():
    with TestClient(app) as client:
        tpl = client.post(
            "/api/templates",
            json={
                "account_id": "acc-001",
                "name": "采购异议",
                "opening": "您好，很高兴为您服务。",
                "core": "我们支持越南语与粤语实时通话。",
                "objection": "价格方面我们可以给出阶梯报价。",
                "closing": "感谢您的咨询，再见。",
                "language": "zh",
            },
        ).json()
        tpl_id = tpl["id"]
        assert client.get("/api/templates", params={"account_id": "acc-001"}).json()[0]["id"] == tpl_id
        assert client.put(f"/api/templates/{tpl_id}", json={"name": "采购异议V2"}).json()["name"] == "采购异议V2"

        obj = client.post(
            "/api/objects",
            params={"account_id": "acc-001"},
            json={"display_name": "Nguyen", "template_id": tpl_id},
        ).json()
        assert obj["template_id"] == tpl_id
        assert client.get(f"/api/objects/{obj['id']}").json()["template_id"] == tpl_id

        assert client.delete(f"/api/templates/{tpl_id}").json()["deleted"] is True
        assert client.get("/api/templates", params={"account_id": "acc-001"}).json() == []

        report = client.get("/api/reports/summary", params={"account_id": "acc-001"}).json()
        assert isinstance(report["total_calls"], int)


def test_sidecar_health_routes_exist():
    with TestClient(app) as client:
        asr = client.get("/api/asr/health")
        tts = client.get("/api/tts/health")
        assert asr.status_code in {200, 503}
        assert tts.status_code in {200, 503}


def test_audit_trail_records_and_queries():
    with TestClient(app) as client:
        # A template create is an audited action -> it should land in /api/audit.
        tpl = client.post(
            "/api/templates",
            json={"account_id": "acc-001", "name": "审计模板", "opening": "您好", "core": "介绍"},
        ).json()
        rows = client.get("/api/audit", params={"action": "template.create"}).json()
        assert any(r["subject_id"] == tpl["id"] for r in rows)
        assert rows[0]["request_id"]  # correlation id is propagated
        # Filtering by template id returns the same row.
        by_obj = client.get("/api/audit", params={"action": "template.create", "account_id": "acc-001"}).json()
        assert any(r["subject_id"] == tpl["id"] for r in by_obj)


def test_setup_status_reports_model_readiness():
    with TestClient(app) as client:
        resp = client.get("/api/setup")
        assert resp.status_code == 200
        body = resp.json()
        assert "ready" in body
        assert isinstance(body.get("models"), list)
        # In CI/dev the endpoint returns a structured shape even if models absent.
        for m in body["models"]:
            assert set(["name", "repo", "present", "required"]).issubset(m.keys())
