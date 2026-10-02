"""DR 容灾+可观测波 B 路（feat/dr-observability）· GET /api/calls/{id}/logs 游标尾读。

契约 §5（docs/DR-WAVE-CONTRACT.md 冻结）：数据源 agent.log；过滤=行内含 call_id
（结构行天然带）+ 原始 print 行按「最近一次结构行归属」跟随（同 call 窗口的原始行
跟结构行走）；``after`` 字节游标续读、``next_offset`` 回填、``eof`` 尾标；limit
默认 200 上限 1000；权限=``_gate_page("calls")``。

零端口零服务：日志走 tmp_path + BOK_AGENT_LOG，通话行走内存仓。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "")  # 强制内存仓
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")

from bok_voice_business_db.repository import InMemoryBusinessRepository  # noqa: E402
from control_plane import main as cp_main  # noqa: E402
from control_plane import ops_metrics  # noqa: E402
from control_plane.auth import hash_password  # noqa: E402

PW = "Passw0rd!x"
MACHINE_TOKEN = "machine-token"

CALL_A = "call-aaaaaaaa"
CALL_B = "call-bbbbbbbb"

# 结构行=含 call id 的日志行（agent.log 的 JSON 行天然带）；原始 print 行无 call id。
LOG_INITIAL = (
    "INFO raw line before any structural line\n"  # 无归属（current=''）→ 不入任何 call
    f"STRUCT worker opened room {CALL_A}\n"
    "print raw line for A\n"
    f"STRUCT worker opened room {CALL_B}\n"
    "print raw line for B\n"
    f"STRUCT {CALL_A} turn done\n"
    "print raw line for A again\n"
)

EXPECTED_A = [
    f"STRUCT worker opened room {CALL_A}",
    "print raw line for A",
    f"STRUCT {CALL_A} turn done",
    "print raw line for A again",
]
EXPECTED_B = [
    f"STRUCT worker opened room {CALL_B}",
    "print raw line for B",
]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """状态隔离：饥荒状态机复位 + 鉴权 env 清场（auth-off 默认形态）。"""
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    ops_metrics.reset_state()
    yield
    ops_metrics.reset_state()


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr(cp_main, "_repo", lambda: repo)
    return TestClient(cp_main.app), repo


def _mk_call(repo, call_id: str, account_id: str = "acc-001"):
    from bok_voice_core.policies import select_session_manifest
    from bok_voice_core.types import CallMode

    return repo.create_call(select_session_manifest(
        session_id=call_id, account_id=account_id, object_id="",
        persona_id="", mode=CallMode.SIMULATION,
    ))


def _log_file(tmp_path, text: str = LOG_INITIAL, monkeypatch=None):
    path = tmp_path / "agent.log"
    path.write_text(text, encoding="utf-8")
    if monkeypatch is not None:
        monkeypatch.setenv("BOK_AGENT_LOG", str(path))
    return path


# ---- 纯函数层：归属 / 游标 / eof ----


def test_full_read_filters_and_attributes(tmp_path):
    path = _log_file(tmp_path)
    out = ops_metrics.read_call_log(CALL_A, path=path)
    assert out["lines"] == EXPECTED_A
    assert out["eof"] is True
    assert out["next_offset"] == path.stat().st_size
    # 另一通只拿自己的结构行 + 中间原始行
    out_b = ops_metrics.read_call_log(CALL_B, path=path)
    assert out_b["lines"] == EXPECTED_B


def test_cursor_reconstructs_without_loss_or_duplication(tmp_path):
    path = _log_file(tmp_path)
    cursor = 0
    collected: list[str] = []
    eof = False
    for _ in range(20):
        page = ops_metrics.read_call_log(CALL_A, after=cursor, limit=1, path=path)
        collected.extend(page["lines"])
        cursor = page["next_offset"]
        eof = page["eof"]
        if eof:
            break
    assert collected == EXPECTED_A  # 零丢行零重复
    assert eof is True
    # 到 EOF 后继续轮询：空页 + 游标不动
    again = ops_metrics.read_call_log(CALL_A, after=cursor, path=path)
    assert again["lines"] == [] and again["eof"] is True
    assert again["next_offset"] == cursor


def test_lookback_keeps_attribution_across_cursor(tmp_path):
    """游标停在中段后，原始 print 行的归属靠回看窗重建（不回发已读行）。"""
    path = _log_file(tmp_path)
    # 先读到 struct A（limit=1）→ 游标 = 「print raw line for A」行首
    first = ops_metrics.read_call_log(CALL_A, after=0, limit=1, path=path)
    assert first["lines"] == [f"STRUCT worker opened room {CALL_A}"]
    assert first["eof"] is False
    second = ops_metrics.read_call_log(CALL_A, after=first["next_offset"], limit=1, path=path)
    assert second["lines"] == ["print raw line for A"]  # 无 call_id 行仍归属 A


def test_appended_lines_seen_by_next_poll(tmp_path):
    path = _log_file(tmp_path)
    page = ops_metrics.read_call_log(CALL_A, path=path)
    assert page["eof"] is True
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"STRUCT {CALL_A} late turn\n")
        fh.write("print raw late line\n")
    nxt = ops_metrics.read_call_log(CALL_A, after=page["next_offset"], path=path)
    assert nxt["lines"] == [f"STRUCT {CALL_A} late turn", "print raw late line"]
    assert nxt["eof"] is True


def test_missing_file_and_empty_call_id_are_safe(tmp_path):
    missing = tmp_path / "nope.log"
    out = ops_metrics.read_call_log(CALL_A, after=123, path=missing)
    assert out == {"lines": [], "next_offset": 123, "eof": True}
    path = _log_file(tmp_path)
    out = ops_metrics.read_call_log("", path=path)
    assert out["lines"] == [] and out["eof"] is False


def test_after_beyond_eof_clamped(tmp_path):
    path = _log_file(tmp_path)
    out = ops_metrics.read_call_log(CALL_A, after=10**9, path=path)
    assert out["lines"] == [] and out["eof"] is True
    assert out["next_offset"] == path.stat().st_size


# ---- 端点层：契约形状 / 权限 / 参数钳制 ----


def test_endpoint_shape_cursor_and_limit_clamp(monkeypatch, tmp_path):
    client, repo = _client_and_repo(monkeypatch)
    path = _log_file(tmp_path, monkeypatch=monkeypatch)
    _mk_call(repo, CALL_A)
    r = client.get(f"/api/calls/{CALL_A}/logs")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"lines", "next_offset", "eof"}
    assert body["lines"] == EXPECTED_A
    assert body["eof"] is True and body["next_offset"] == path.stat().st_size
    # after 缺省 0；limit 巨值钳 1000（不报错）；负 after 钳 0
    assert client.get(f"/api/calls/{CALL_A}/logs?limit=100000").status_code == 200
    assert client.get(f"/api/calls/{CALL_A}/logs?after=-5").json()["lines"] == EXPECTED_A
    # 游标续读：limit=2 分两页拼回全量
    p1 = client.get(f"/api/calls/{CALL_A}/logs?limit=2").json()
    assert p1["lines"] == EXPECTED_A[:2] and p1["eof"] is False
    p2 = client.get(f"/api/calls/{CALL_A}/logs?after={p1['next_offset']}&limit=2").json()
    assert p2["lines"] == EXPECTED_A[2:] and p2["eof"] is True


def test_endpoint_404_and_permission_matrix(monkeypatch, tmp_path):
    client, repo = _client_and_repo(monkeypatch)
    _log_file(tmp_path, monkeypatch=monkeypatch)
    _mk_call(repo, CALL_A)
    # 未知通话 404（不泄露存在性口径与既有 by-ID 端点一致）
    assert client.get("/api/calls/call-nope/logs").status_code == 404
    # auth-off：calls 页闸直通
    assert client.get(f"/api/calls/{CALL_A}/logs").status_code == 200

    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_CP_TOKEN", MACHINE_TOKEN)
    # 无凭据 → 401（identity 门）
    assert client.get(f"/api/calls/{CALL_A}/logs").status_code == 401
    # 机器通道直通（与 turns/whatsapp 上报同姿势）
    m = {"Authorization": f"Bearer {MACHINE_TOKEN}"}
    assert client.get(f"/api/calls/{CALL_A}/logs", headers=m).status_code == 200
    # 无 calls 页面键的 user → 403
    repo.create_user(username="peon", password_hash=hash_password(PW), role="user",
                     org_id="org-t", account_id="acc-001", permissions_json="[]")
    r = client.post("/api/auth/login", json={"username": "peon", "password": PW})
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get(f"/api/calls/{CALL_A}/logs", headers=h).status_code == 403
    # 默认页面键（含 calls）的 user → 200；跨账号 → 404
    repo.create_user(username="agent-ok", password_hash=hash_password(PW), role="user",
                     org_id="org-t", account_id="acc-001", permissions_json="")
    h_ok = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", json={"username": "agent-ok", "password": PW}
    ).json()["token"]}
    assert client.get(f"/api/calls/{CALL_A}/logs", headers=h_ok).status_code == 200
    repo.create_user(username="other-acct", password_hash=hash_password(PW), role="user",
                     org_id="org-t", account_id="acc-002", permissions_json="")
    h_other = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", json={"username": "other-acct", "password": PW}
    ).json()["token"]}
    assert client.get(f"/api/calls/{CALL_A}/logs", headers=h_other).status_code == 404


def test_endpoint_missing_log_file_returns_empty_page(monkeypatch, tmp_path):
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_AGENT_LOG", str(tmp_path / "absent.log"))
    _mk_call(repo, CALL_A)
    body = client.get(f"/api/calls/{CALL_A}/logs").json()
    assert body == {"lines": [], "next_offset": 0, "eof": True}
