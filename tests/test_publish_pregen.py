"""模板发布 → 自动罐头物化(EX-3,2026-09-27)契约 + 直念步变量渲染同源。

两块被测面:
- POST /api/templates/{id}/publish 冻结 published_json 后 detached 跑
  `pregen_tts.py --greetings --branches --account-id <acct>`;任何 spawn 失败
  只回 `tts_pregen` 状态键,绝不阻发布响应(镜像 personas 自动物化姿势);
  kill-switch `BOK_PUBLISH_AUTO_PREGEN=0` 全关。
- scripts/runtime/pregen_tts.py `_say_step_lines(tpl, obj)` 与运行时
  FlowController.step_say_text 对**同一个对象**逐字节同源——同一份 object_vars()+
  render_template_text(),缓存键(文本+音色+模型)才对得上。

所有用例显式 setenv —— 进程内共享 os.environ,本文件模块级置
`BOK_PUBLISH_AUTO_PREGEN=0`(兄弟文件 test_template_publish 也会发布模板,
不能让它真 spawn 子进程;与 test_template_gate 注记的同类隔离惯例一致)。
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
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-publish-pregen!")

# 模块级默认关:见 docstring。需要的用例显式 setenv("...","1")。
os.environ["BOK_PUBLISH_AUTO_PREGEN"] = "0"

ROOT = Path(__file__).resolve().parents[1]
for _p in ("apps/agent", "scripts"):
    _sp = str(ROOT / _p)
    if _sp not in sys.path:
        sys.path.insert(0, _sp)

import pytest  # noqa: E402

from bok_voice_business_db.repository import InMemoryBusinessRepository  # noqa: E402

import control_plane.main as cp_main  # noqa: E402
from control_plane import pregen as pregen_mod  # noqa: E402

_LIVE = {
    "account_id": "acc-001",
    "name": "发布物化话术",
    "language": "cantonese",
    "steps_json": json.dumps(
        [{"goal": "确认身份", "ref": "你好，請問係{姓名}嗎？"},
         {"goal": "通知", "ref": "我哋會經{聯絡方式}聯絡您。", "say": 1}],
        ensure_ascii=False,
    ),
    "opening": "你好，請問係{姓名}嗎？",
}


class _FakePopen:
    """记录 spawn;poll()/wait() 模拟单飞判定+reaper。绝不真跑子进程。"""

    def __init__(self, cmd, **kw):
        self.cmd = list(cmd)
        self.kw = kw
        self.pid = 424242
        self.rc = None

    def poll(self):
        return self.rc

    def wait(self):
        return self.rc


@pytest.fixture()
def spawns(monkeypatch, tmp_path):
    """拦截 Popen + 把日志路径改到 tmp(不碰真实 app-data)。"""
    rec: list[_FakePopen] = []
    monkeypatch.setattr(
        cp_main.subprocess, "Popen",
        lambda cmd, **kw: rec.append(_FakePopen(cmd, **kw)) or rec[-1],
    )
    monkeypatch.setattr(pregen_mod, "_log_path", lambda: tmp_path / "tts-pregen.log")
    yield rec


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app

    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    monkeypatch.setattr(cp_main, "_audit", lambda action, **kw: None)
    return TestClient(app).__enter__(), repo


def _make_template(client, **overrides) -> dict:
    body = {**_LIVE, **overrides}
    r = client.post("/api/templates", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# (a) 发布 → detached spawn 一次,argv/flags 正确
# ---------------------------------------------------------------------------


def test_publish_spawns_detached_pregen(spawns, monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_PUBLISH_AUTO_PREGEN", "1")
    try:
        tpl = _make_template(client)
        resp = client.post(f"/api/templates/{tpl['id']}/publish")
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["published"] is True
    assert body["tts_pregen"]["status"] == "queued"
    # 冻结已在 spawn 之前提交(发布态完好)
    assert repo.get_template(tpl["id"])["published_json"]

    assert len(spawns) == 1
    proc = spawns[0]
    assert proc.cmd[0] == sys.executable
    assert proc.cmd[1] == str(ROOT / "scripts" / "runtime" / "pregen_tts.py")
    assert proc.cmd[2:] == ["--greetings", "--branches", "--account-id", "acc-001"]
    kw = proc.kw
    assert kw["start_new_session"] is True           # detached
    assert kw["cwd"] == str(ROOT)
    assert kw["stderr"] == cp_main.subprocess.STDOUT
    assert kw["stdout"] is not None
    assert kw["env"]["BOK_CP_URL"] == "http://testserver"


# ---------------------------------------------------------------------------
# (b) spawn 抛错 → 发布仍 200
# ---------------------------------------------------------------------------


def test_spawn_failure_never_breaks_publish(spawns, monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_PUBLISH_AUTO_PREGEN", "1")

    def _boom(cmd, **kw):
        raise OSError("noexec")

    monkeypatch.setattr(cp_main.subprocess, "Popen", _boom)
    try:
        tpl = _make_template(client)
        resp = client.post(f"/api/templates/{tpl['id']}/publish")
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    assert resp.json()["tts_pregen"]["status"] == "failed"
    # 发布态照常落库
    assert repo.get_template(tpl["id"])["published_json"]
    assert spawns == []


# ---------------------------------------------------------------------------
# (c) kill-switch BOK_PUBLISH_AUTO_PREGEN=0 → 不 spawn
# ---------------------------------------------------------------------------


def test_kill_switch_disables_spawn(spawns, monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_PUBLISH_AUTO_PREGEN", "0")
    try:
        tpl = _make_template(client)
        resp = client.post(f"/api/templates/{tpl['id']}/publish")
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    assert resp.json()["tts_pregen"]["status"] == "disabled"
    assert spawns == []
    assert repo.get_template(tpl["id"])["published_json"]  # 关闸不影响发布


# ---------------------------------------------------------------------------
# (d) 直念步渲染与运行时逐字节同源
# ---------------------------------------------------------------------------


def _runtime_say_lines(tpl: dict, obj: dict) -> list[tuple[str, str]]:
    """运行时口径(flow.py 真身):FlowController.from_template → step_say_text。"""
    from agent_runtime.flow import FlowController

    fc = FlowController.from_template(tpl, obj)
    out: list[tuple[str, str]] = []
    for i, s in enumerate(fc.steps):
        if not s.say:
            continue
        text = fc.step_say_text(i)
        if text:
            out.append((text, s.emotion))
    return out


def test_say_step_lines_byte_identical_to_runtime():
    import pregen_tts

    obj = {
        "id": "obj-1",
        "display_name": "陳大文",
        "language": "cantonese",
        "contact_channel": "",  # 空=按对象语言缺省 → cantonese=WhatsApp
        "tracking_no": "SF7890",
    }
    tpl = {
        "id": "tpl-1",
        "language": "cantonese",
        "steps_json": json.dumps(
            [
                # 含 {姓名}+{聯絡方式} 两个变量的直念步 → 必须按对象变量渲染
                {"goal": "通知", "ref": "你好{姓名}，我哋會經{聯絡方式}發送退款連結。", "say": 1},
                # 无变量直念步 → 照常物化(与对象无关)
                {"goal": "收尾", "ref": "再見，多謝您耐心等候。", "say": 1},
                # 未定义变量 → 运行时渲染后仍残留 {占位} → 直念线宁可退 LLM,不物化
                {"goal": "坏步", "ref": "訂單編號 {不存在的變量} 已處理。", "say": 1},
            ],
            ensure_ascii=False,
        ),
    }

    actual = pregen_tts._say_step_lines(tpl, obj)
    expected = _runtime_say_lines(tpl, obj)

    assert actual == expected
    # 逐字节钉死(文本+情绪)
    for (a_text, a_emo), (e_text, e_emo) in zip(actual, expected):
        assert a_text.encode("utf-8") == e_text.encode("utf-8")
        assert a_emo == e_emo

    texts = [t for t, _ in actual]
    # 变量真被替换(非残留跳过):姓名 + 粤语缺省渠道 WhatsApp
    assert any("陳大文" in t and "WhatsApp" in t for t in texts)
    # 未定义变量行被跳过(不进物化计划)
    assert all("{不存在的變量}" not in t for t in texts)
    # 无变量行照常物化
    assert "再見，多謝您耐心等候。" in texts


def test_say_step_lines_empty_vars_keeps_legacy_skip():
    """无对象(obj=None)=旧行为:含 {占位符} 的行渲染后仍有残留 → 跳过。"""
    import pregen_tts

    tpl = {
        "language": "cantonese",
        "steps_json": json.dumps(
            [{"goal": "通知", "ref": "你好{姓名}，我哋會經{聯絡方式}聯絡您。", "say": 1},
             {"goal": "收尾", "ref": "再見。", "say": 1}],
            ensure_ascii=False,
        ),
    }
    assert pregen_tts._say_step_lines(tpl, None) == [("再見。", "")]
    # 显式给对象 → 变量行现在能物化(修复点)
    obj = {"display_name": "陳大文", "language": "cantonese"}
    lines = pregen_tts._say_step_lines(tpl, obj)
    assert any("陳大文" in t and "WhatsApp" in t for t, _ in lines)


def test_resolve_say_object_explicit_and_fallback():
    import pregen_tts

    objs = [
        {"id": "o1", "display_name": "一"},
        {"id": "o2", "display_name": "二"},
    ]
    # 显式命中
    assert pregen_tts._resolve_say_object(objs, object_id="o1")["id"] == "o1"
    # 显式未命中 = None(绝不静默换对象)
    assert pregen_tts._resolve_say_object(objs, object_id="nope") is None
    # 缺省 = 账号列表末条(最近更新代理)
    assert pregen_tts._resolve_say_object(objs, object_id="")["id"] == "o2"
    # 空表 = None(退回空变量旧行为)
    assert pregen_tts._resolve_say_object([], object_id="") is None
