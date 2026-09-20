"""F1(2026-09-20,A_LINE_LOGIC §8.4)回归:人设账号作用域 + pregen 不静默回落。

三层可测面(全部离线:不连云、不真合成、子进程/HTTP 全部打桩):
- CP 人设端点:POST/PUT(body /api/personas 与 by-ID PUT)body 缺 account_id 时
  兜底 acc-001(与列表端点默认口径一致)——此前落 "" 令 /api/personas(acc-001)
  看不到它 → pregen_tts --persona 找不到 → 静默回落默认音色 moss_audio_*,物化
  缓存键与运行时错位(「看起来录好了、通话里永远不播」)。显式 account_id 维持
  原优先级;admin 身份强制本账号;by-ID PUT 未显式携带 account_id 不洗账号。
- scripts/pregen_tts.py:--persona 指定的人设找不到 → rc=3 响亮失败、零 provider
  构造、零缓存写,绝不静默回落默认音色。
- CP 罐头状态端点:voice_source 顶层信息位透传;statuses 三态语义不变(web 按
  三态渲染,改语义会静默破 UI)。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-persona-account-scope!")

ROOT = Path(__file__).resolve().parents[1]
for _p in ("apps/agent", "packages/core", "scripts"):
    _sp = str(ROOT / _p)
    if _sp not in sys.path:
        sys.path.insert(0, _sp)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import pregen_tts  # noqa: E402
from control_plane import auth as cp_auth  # noqa: E402
from control_plane import pregen as pregen_mod  # noqa: E402
from control_plane.main import app  # noqa: E402


class _FakePopen:
    """记录 spawn;poll() 缺省 None=在跑(单飞判定用)。绝不真跑子进程。"""

    def __init__(self, cmd, **kw):
        self.cmd = list(cmd)
        self.kw = kw
        self.pid = -1
        self.rc = None

    def poll(self):
        return self.rc

    def wait(self):
        return self.rc


@pytest.fixture()
def spawns(monkeypatch):
    rec: list[_FakePopen] = []
    monkeypatch.setattr(
        pregen_mod.subprocess, "Popen", lambda cmd, **kw: rec.append(_FakePopen(cmd, **kw)) or rec[-1]
    )
    pregen_mod._PREGEN_PROCS.clear()
    yield rec
    pregen_mod._PREGEN_PROCS.clear()


# ---- CP 人设端点:账号作用域(body 缺省兜底,显式直通,身份强制) ----


def test_post_persona_without_account_defaults_acc001(spawns):
    with TestClient(app) as client:
        body = client.post("/api/personas", json={"name": "缺账号"}).json()
        listed = client.get("/api/personas").json()
    assert body["account_id"] == "acc-001"  # 不再落 ""
    assert body["id"] in {p["id"] for p in listed}  # 默认口径(acc-001)列表可见


def test_post_persona_explicit_account_honored(spawns):
    with TestClient(app) as client:
        body = client.post(
            "/api/personas", json={"account_id": "acc-777", "name": "显式账号"}
        ).json()
        in_777 = {p["id"] for p in client.get("/api/personas?account_id=acc-777").json()}
        in_001 = {p["id"] for p in client.get("/api/personas").json()}
    assert body["account_id"] == "acc-777"  # 显式值维持原优先级
    assert body["id"] in in_777 and body["id"] not in in_001


def test_post_persona_admin_identity_forced_to_own_account(spawns):
    token = cp_auth.create_token(
        cp_auth.Identity(user_id="u1", username="adm", role="admin", account_id="acc-777")
    )
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        body = client.post(
            "/api/personas", json={"account_id": "acc-999", "name": "越权"}, headers=headers
        ).json()
    assert body["account_id"] == "acc-777"  # 既有语义:admin 强制本账号


def test_put_persona_without_account_preserves_account(spawns):
    # F1 同病面:by-ID PUT 整包 dump 曾把未显式携带的 account_id 洗成 "",
    # 人设从 acc-001 列表消失(同一条「补录后永远不播」链)。只改名字,
    # 音色不变 → tts_pregen=unchanged,不触发 spawn。
    with TestClient(app) as client:
        created = client.post(
            "/api/personas",
            json={"account_id": "acc-001", "name": "改名前", "reference_audio": "v1"},
        ).json()
        body = client.put(
            f"/api/personas/{created['id']}",
            json={"name": "改名后", "language": created.get("language", "zh"),
                  "reference_audio": "v1"},
        ).json()
        listed = {p["id"] for p in client.get("/api/personas").json()}
    assert body["account_id"] == "acc-001"  # 未显式携带=保留原归属
    assert body["tts_pregen"]["status"] == "unchanged"
    assert created["id"] in listed


def test_put_persona_explicit_account_moves(spawns):
    with TestClient(app) as client:
        created = client.post(
            "/api/personas", json={"account_id": "acc-001", "name": "搬家前"}
        ).json()
        body = client.put(
            f"/api/personas/{created['id']}",
            json={"account_id": "acc-888", "name": "搬家后", "language": created.get("language", "zh"),
                  "reference_audio": created.get("reference_audio", "")},
        ).json()
        in_888 = {p["id"] for p in client.get("/api/personas?account_id=acc-888").json()}
    assert body["account_id"] == "acc-888"  # 显式携带(root 等价/无身份)仍可挪
    assert created["id"] in in_888
    assert body["tts_pregen"]["status"] == "no_voice"  # 无音色:提醒面短路,不 spawn


# ---- pregen_tts:--persona 找不到 → rc=3 响亮失败,零 provider 构造零落盘 ----


class _BoomProvider:
    def __init__(self, *a, **kw):
        raise AssertionError("MiniMaxTTS must not be constructed on PERSONA_MISSING abort")


def _run_pregen(monkeypatch, argv: list[str], personas: list[dict], capsys, cache_root: Path):
    """打桩跑 main():_fetch_cp/_cp_get 定死返回,缓存根指 tmp,MiniMaxTTS 构造即炸。"""
    monkeypatch.setattr(pregen_tts, "MiniMaxTTS", _BoomProvider)
    monkeypatch.setattr(
        pregen_tts, "_fetch_cp", lambda *a, **k: ({}, personas, [], [])
    )
    monkeypatch.setattr(pregen_tts, "_cp_get", lambda *a, **k: [])  # qa/fillers 拉取不触网
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    monkeypatch.setenv("BOK_TTS_CACHE_DIR", str(cache_root))
    monkeypatch.setattr("sys.argv", argv)
    rc = pregen_tts.main()
    return rc, capsys.readouterr()


def test_pregen_persona_missing_aborts_without_fallback(monkeypatch, tmp_path, capsys):
    rc, out = _run_pregen(
        monkeypatch,
        ["pregen_tts.py", "--greetings", "--persona", "nope", "--cp", "http://cp.test"],
        personas=[],
        capsys=capsys,
        cache_root=tmp_path,
    )
    assert rc == 3  # 非零且区别于「合成失败」的 2
    assert "not found in CP /api/personas" in out.err
    assert "PERSONA_MISSING" in out.err
    # 零落盘:守卫先于 cache 构造,缓存目录都不应被创建
    assert not (tmp_path / "tts-cache").exists()


def test_pregen_persona_missing_aborts_status_modes_too(monkeypatch, tmp_path, capsys):
    # --branch-status / --qa-status 带 --persona 同样不回落:状态面对错键报
    # 「假 ok」比不报更糟。stdout 不出 JSON(纯 JSON 契约只对成功路径成立)。
    for mode in ("--branch-status", "--qa-status"):
        rc, out = _run_pregen(
            monkeypatch,
            ["pregen_tts.py", mode, "--persona", "nope", "--cp", "http://cp.test"],
            personas=[],
            capsys=capsys,
            cache_root=tmp_path,
        )
        assert rc == 3, mode
        assert "PERSONA_MISSING" in out.err, mode
        assert not out.out.strip(), mode  # 无 JSON 输出


def test_pregen_persona_found_materializes_with_persona_voice(monkeypatch, tmp_path, capsys):
    # 反向钉住:人设在场时走原路径(此处 dry-run 断言计划 persona 归属),
    # 不被「一律 rc=3」误伤。
    persona = {"id": "p1", "language": "zh", "reference_audio": json.dumps({"zh": "Vzh"})}
    rc, out = _run_pregen(
        monkeypatch,
        ["pregen_tts.py", "--qa", "--all-personas", "--dry-run", "--persona", "p1",
         "--cp", "http://cp.test"],
        personas=[persona],
        capsys=capsys,
        cache_root=tmp_path,
    )
    assert rc == 0
    assert "PERSONA_MISSING" not in out.err


# ---- 状态面 voice_source:纯函数输出 → 子进程 JSON → CP 端点透传 ----


def _tpl_zh() -> dict:
    return {
        "language": "zh",
        "steps_json": json.dumps(
            [{"goal": "告知", "ref": "您好。\n如果客户说没收到→包裹到了。"}],
            ensure_ascii=False,
        ),
    }


def test_branch_status_json_carries_voice_source(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BOK_TTS_CACHE_DIR", str(tmp_path))
    personas = [{"id": "pzh", "language": "zh", "reference_audio": json.dumps({"zh": "Vzh"})}]
    monkeypatch.setattr(
        pregen_tts, "_fetch_cp", lambda *a, **k: ({}, personas, [], [_tpl_zh()])
    )
    monkeypatch.setattr("sys.argv", ["pregen_tts.py", "--branch-status", "--cp", "http://cp.test"])
    assert pregen_tts.main() == 0
    data = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert data["voice_source"]["zh"] == "persona:pzh"  # 有人设:与运行时同源
    assert data["voice_source"]["cantonese"] == "personas_default"  # 无人设语言


def test_branch_status_json_default_when_no_persona(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BOK_TTS_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(
        pregen_tts, "_fetch_cp", lambda *a, **k: ({}, [], [], [_tpl_zh()])
    )
    monkeypatch.setattr("sys.argv", ["pregen_tts.py", "--branch-status", "--cp", "http://cp.test"])
    assert pregen_tts.main() == 0
    data = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert data["voice_source"]["zh"] == "personas_default"


def test_branch_canned_status_endpoint_voice_source_passthrough(monkeypatch):
    pregen_mod._branch_status_cache.clear()
    try:
        monkeypatch.setattr(
            "control_plane.pregen.branch_status_json",
            lambda base_url, account_id="": {
                "available": True,
                "branch_status": {"分支resp": "ok"},
                "voice_source": {"zh": "persona:p1", "cantonese": "personas_default"},
            },
        )
        with TestClient(app) as client:
            body = client.get("/api/tts/branch-canned-status?account_id=acc-001").json()
    finally:
        pregen_mod._branch_status_cache.clear()
    # 三态语义不变(statuses 原样),信息位只加顶层
    assert body["statuses"] == {"分支resp": "ok"}
    assert body["available"] is True
    assert body["voice_source"] == {"zh": "persona:p1", "cantonese": "personas_default"}


def test_qa_canned_status_endpoint_voice_source_passthrough(monkeypatch):
    pregen_mod._status_cache = (0.0, None)
    try:
        monkeypatch.setattr(
            "control_plane.pregen.qa_status_json",
            lambda base_url: {
                "available": True,
                "qa_status": {"qa:1": {"state": "missing", "voice": "", "key": ""}},
                "voice_source": {"zh": "personas_default"},
            },
        )
        with TestClient(app) as client:
            body = client.get("/api/qa/canned-status?account_id=acc-001").json()
    finally:
        pregen_mod._status_cache = (0.0, None)
    assert body["statuses"] == {"qa:1": {"state": "missing", "voice": "", "key": ""}}
    assert body["voice_source"] == {"zh": "personas_default"}


def test_status_endpoint_degraded_still_has_voice_source(monkeypatch):
    # spawn 失败降级面:端点不炸,voice_source 兜底空表。
    pregen_mod._branch_status_cache.clear()
    try:
        def boom(base_url: str, account_id: str = "") -> dict:
            raise RuntimeError("spawn fail")

        monkeypatch.setattr("control_plane.pregen.branch_status_json", boom)
        with TestClient(app) as client:
            body = client.get("/api/tts/branch-canned-status?account_id=acc-001").json()
    finally:
        pregen_mod._branch_status_cache.clear()
    assert body["available"] is False and body["statuses"] == {}
    assert body["voice_source"] == {}
