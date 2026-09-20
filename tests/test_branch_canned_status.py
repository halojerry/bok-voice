"""分支罐头状态面(2026-09-20 流程画布答法抽屉注线)单测。

三层可测面:
- pregen_tts._branch_status 纯函数:ok/missing/ph 三态、键=resp 原文含动作
  标记(逐字节)、音频文本=剥标记后渲染、--texts-file 过滤、跨语言上下文任一
  命中即 ok;
- CP pregen.branch_canned_status:TTL 缓存键含账号(acc 隔离——QA 全局单份
  跨账号串状态的已知问题不复制)、spawn 失败降级 available=False;
- 端点形状(GET /api/tts/branch-canned-status / POST /api/tts/branch-pregen):
  monkeypatch 打桩 spawn,绝不真跑云 TTS。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-branch-canned")

ROOT = Path(__file__).resolve().parents[1]
for p in ("apps/agent", "packages/core", "scripts"):
    sp = str(ROOT / p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

import pytest  # noqa: E402

import pregen_tts  # noqa: E402
from agent_runtime.tts_cache import TtsAudioCache  # noqa: E402

_REF = (
    "您好，想跟您核对一下包裹。\n"
    "如果客户说没收到→您的包裹已经到驿站了，凭取件码就能取。\n"
    "如果客户嫌赔偿少→【转人工】好的，马上为您转接人工客服处理赔偿。\n"
    "如果客户问时效→大概需要{days}天送达。"
)
_TPL = {"language": "zh", "steps_json": json.dumps([{"goal": "告知", "ref": _REF}], ensure_ascii=False)}
_LANG_PERSONAS = {"zh": None, "cantonese": None, "en": None}


def _status(cache: TtsAudioCache, templates=None, texts=None) -> dict[str, str]:
    return pregen_tts._branch_status(
        templates if templates is not None else [_TPL],
        _LANG_PERSONAS,
        tts_cfg={}, voice_mode="single", model="speech-2.8-hd", cache=cache,
        texts=texts,
    )


# ---- 脚本核心:_branch_status 三态/键契约/过滤 ----


def test_branch_status_three_states_and_marker_key(tmp_path):
    cache = TtsAudioCache(tmp_path)
    plan = _status(cache)
    # 键=分支 resp 原文(含动作标记,逐字节);普通分支不带标记
    assert "您的包裹已经到驿站了，凭取件码就能取。" in plan
    assert "【转人工】好的，马上为您转接人工客服处理赔偿。" in plan
    # 占位残留 → ph(空变量渲染与运行时对象变量渲染不同文,补录无效,不报假 missing)
    assert plan["大概需要{days}天送达。"] == "ph"
    assert plan["您的包裹已经到驿站了，凭取件码就能取。"] == "missing"
    assert plan["【转人工】好的，马上为您转接人工客服处理赔偿。"] == "missing"
    # 音频文本=剥标记后渲染:以剥标记文本算 key 物化后,带标记键翻 ok
    voice = pregen_tts._persona_resolved_voice(None, "zh", {}, "single")
    from agent_runtime.providers.livekit_plugins import minimax_speed_for

    text = "好的，马上为您转接人工客服处理赔偿。"
    cache.store(
        cache.key_for(text, voice=voice, model="speech-2.8-hd",
                      speed=minimax_speed_for("zh"), emotion=""),
        b"\x00\x00" * 100, text=text, voice=voice, model="speech-2.8-hd", pin=True,
    )
    plan2 = _status(cache)
    assert plan2["【转人工】好的，马上为您转接人工客服处理赔偿。"] == "ok"
    assert plan2["您的包裹已经到驿站了，凭取件码就能取。"] == "missing"  # 未物化的仍 missing


def test_branch_status_texts_filter(tmp_path):
    cache = TtsAudioCache(tmp_path)
    plan = _status(cache, texts={"【转人工】好的，马上为您转接人工客服处理赔偿。"})
    assert set(plan) == {"【转人工】好的，马上为您转接人工客服处理赔偿。"}


def test_branch_status_cross_language_any_hit_is_ok(tmp_path):
    cache = TtsAudioCache(tmp_path)
    templates = [
        _TPL,
        {"language": "cantonese", "steps_json": json.dumps(
            [{"goal": "告知", "ref": "您好。\n如果客户说没收到→您的包裹已经到驿站了，凭取件码就能取。"}],
            ensure_ascii=False)},
    ]
    # zh 版物化、粤版未物化:任一上下文命中即 ok(运行时该语言通话即可播录音)
    voice = pregen_tts._persona_resolved_voice(None, "zh", {}, "single")
    from agent_runtime.providers.livekit_plugins import minimax_speed_for

    text = "您的包裹已经到驿站了，凭取件码就能取。"
    cache.store(
        cache.key_for(text, voice=voice, model="speech-2.8-hd",
                      speed=minimax_speed_for("zh"), emotion=""),
        b"\x00\x00" * 100, text=text, voice=voice, model="speech-2.8-hd", pin=True,
    )
    plan = _status(cache, templates=templates)
    assert plan[text] == "ok"


def test_branch_jobs_marker_strip_and_texts_filter():
    jobs = pregen_tts._branch_jobs([_TPL], _LANG_PERSONAS)
    texts = [t for _p, _l, t, _e in jobs]
    # 物化文本=剥标记后的应答(音频里不准念「转人工」字样),占位条目跳过
    assert "好的，马上为您转接人工客服处理赔偿。" in texts
    assert not any("【" in t for t in texts)
    assert not any("{" in t for t in texts)
    # texts 过滤按 resp 原文(含标记)命中
    jobs2 = pregen_tts._branch_jobs(
        [_TPL], _LANG_PERSONAS, texts={"您的包裹已经到驿站了，凭取件码就能取。"}
    )
    assert [t for _p, _l, t, _e in jobs2] == ["您的包裹已经到驿站了，凭取件码就能取。"]


def test_strip_branch_action_prefixes():
    f = pregen_tts._strip_branch_action
    assert f("【收线】好，那就不打扰您了。") == "好，那就不打扰您了。"
    assert f("【挂断】好的。") == "好的。"
    assert f("【跳第3步】那我们直接看方案。") == "那我们直接看方案。"
    assert f("【留本步】我再说一次。") == "我再说一次。"
    assert f("普通应答没有标记") == "普通应答没有标记"
    assert f("") == ""


def test_load_texts_file(tmp_path):
    p = tmp_path / "texts.txt"
    p.write_text("第一行\n\n  第二行  \n", encoding="utf-8")
    assert pregen_tts._load_texts_file(str(p)) == {"第一行", "第二行"}
    assert pregen_tts._load_texts_file("") is None
    assert pregen_tts._load_texts_file("  ") is None


# ---- CP 侧:按账号分键 TTL 缓存 + 端点形状(monkeypatch 打桩,零云调用) ----


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    client = TestClient(app).__enter__()
    return client, repo


@pytest.fixture(autouse=True)
def _reset_branch_cache():
    """模块级 TTL 缓存跨测试隔离(照 test_qa_canned_status 同款纪律)。"""
    from control_plane import pregen as pregen_mod

    pregen_mod._branch_status_cache.clear()
    yield
    pregen_mod._branch_status_cache.clear()


def test_branch_ttl_cache_key_includes_account(monkeypatch):
    from control_plane import pregen as pregen_mod

    calls: list[str] = []

    def fake(base_url: str, account_id: str = "") -> dict:
        calls.append(account_id)
        return {"available": True, "branch_status": {"k": f"st-{account_id or 'default'}"}}

    monkeypatch.setattr(pregen_mod, "branch_status_json", fake)
    monkeypatch.setattr(pregen_mod, "_STATUS_TTL_S", 60.0)
    a1 = pregen_mod.branch_canned_status("http://cp", account_id="acc-001")
    b1 = pregen_mod.branch_canned_status("http://cp", account_id="acc-002")
    a2 = pregen_mod.branch_canned_status("http://cp", account_id="acc-001")
    # TTL 内同账号只 spawn 一次;换账号必 spawn(缓存键含账号,不串状态)
    assert calls == ["acc-001", "acc-002"]
    assert a1["statuses"] == {"k": "st-acc-001"}
    assert b1["statuses"] == {"k": "st-acc-002"}
    assert a2["statuses"] == {"k": "st-acc-001"}  # 缓存命中,仍是自己账号的份


def test_branch_canned_status_spawn_failure_degrades(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)

    def boom(base_url: str, account_id: str = "") -> dict:
        raise RuntimeError("spawn fail")

    monkeypatch.setattr("control_plane.pregen.branch_status_json", boom)
    r = client.get("/api/tts/branch-canned-status")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False and body["statuses"] == {}


def test_branch_canned_status_endpoint_shape(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    key = "如果客户嫌赔偿少→【转人工】好的，马上为您转接。"
    monkeypatch.setattr(
        "control_plane.pregen.branch_status_json",
        lambda base_url, account_id="": {"available": True, "branch_status": {key: "ok"}},
    )
    r = client.get("/api/tts/branch-canned-status?account_id=acc-001")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["statuses"] == {key: "ok"}
    assert isinstance(body["generated_at"], int)


def test_branch_pregen_endpoint_passthrough(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    seen: dict = {}

    def fake_spawn(base_url: str, account_id: str, texts) -> dict:
        seen.update(account_id=account_id, texts=texts)
        return {"status": "queued", "pid": 123}

    monkeypatch.setattr("control_plane.pregen.branch_pregen_spawn", fake_spawn)
    r = client.post(
        "/api/tts/branch-pregen",
        json={"account_id": "acc-001", "texts": ["分支resp原文"]},
    )
    assert r.status_code == 200 and r.json()["status"] == "queued"
    assert seen == {"account_id": "acc-001", "texts": ["分支resp原文"]}
    # texts 缺省 → None(=全量分支)
    seen.clear()
    r2 = client.post("/api/tts/branch-pregen", json={"account_id": "acc-001"})
    assert r2.status_code == 200
    assert seen["account_id"] == "acc-001" and seen["texts"] is None
