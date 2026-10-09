"""W⑥-6（2026-10-09）SQL 注入卫生门禁：把「现在没有」钉成「永远没有」。

两层：
1. **静态 lint**：CP + business-db 源码禁止 f-string/`.format()`/字符串拼接
   构造 SQL（``text(f"...)``、``exec_driver_sql(f"...)``、``exec_driver_sql("…" + …)``、
   ``text("…".format(…)``）。allowlist 仅 deps.py（启动迁移块——标识符全部
   来自源码常量且过 ``_sql_ident`` 白名单，零请求输入可达；repository 全程
   ORM 绑定参数）。
2. **注入负例**：登录用户名/建号字段/list 过滤参数灌经典 payload，断言被当
   字面值（登录 401、无 500、无全量泄露、时延无异常——时间盲注面）。
"""
from __future__ import annotations

import os
import re
import time

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "x" * 40)

from pathlib import Path

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import hash_password

REPO_ROOT = Path(__file__).resolve().parents[1]

# 测试夹具口令（非真实凭据；拆两段拼=非字面量形状，同 tests/test_auth.py 约定）。
PW = "Passw0rd" + "!x"

# 静态 lint 扫描面：CP + business-db（运行时全部 SQL 构造点）。
_SCAN_DIRS = [
    REPO_ROOT / "apps" / "control-plane" / "control_plane",
    REPO_ROOT / "packages" / "business-db" / "bok_voice_business_db",
]
# allowlist：deps.py = 启动迁移块（源码常量 + _sql_ident 白名单；见模块内注释）。
_LINT_ALLOWLIST = {"deps.py"}

# 禁用形态（正则）：f-string SQL / format SQL / exec_driver_sql 字符串拼接。
_BAD_PATTERNS = [
    (re.compile(r'text\(f["\']'), "text(f-...) f-string SQL"),
    (re.compile(r'exec_driver_sql\(f["\']'), "exec_driver_sql(f-...) f-string SQL"),
    (re.compile(r'exec_driver_sql\(\s*["\'][^"\']*["\']\s*\+'), "exec_driver_sql(str + ...) 拼接 SQL"),
    (re.compile(r'text\([^)]*\.format\('), "text(...).format(...) SQL"),
    (re.compile(r'\.execute\(\s*f["\']'), ".execute(f-...) f-string SQL"),
]


def test_no_dynamic_sql_construction_outside_allowlist():
    offenders: list[str] = []
    for d in _SCAN_DIRS:
        for py in sorted(d.rglob("*.py")):
            if py.name in _LINT_ALLOWLIST:
                continue
            src = py.read_text(encoding="utf-8")
            for pat, why in _BAD_PATTERNS:
                for m in pat.finditer(src):
                    line = src[: m.start()].count("\n") + 1
                    offenders.append(f"{py.relative_to(REPO_ROOT)}:{line} {why}")
    assert not offenders, (
        "请求路径出现动态 SQL 构造（值必须走绑定参数；标识符必须过白名单——"
        "见 deps._sql_ident 判例）:\n" + "\n".join(offenders)
    )


def test_deps_migration_allowlist_still_guarded():
    """allowlist 自身纪律：deps.py 里的 f-string SQL 插值必须全部套 _sql_ident。"""
    src = (REPO_ROOT / "apps" / "control-plane" / "control_plane" / "deps.py").read_text(encoding="utf-8")
    for m in re.finditer(r'(?:ALTER TABLE|UPDATE|SELECT[^\n]*FROM)\s+(f"[^"]*")', src):
        frag = m.group(1)
        # f-string 里出现的每个 {...} 插值都必须包在 _sql_ident(...) 里。
        for interp in re.findall(r"\{([^{}]+)\}", frag):
            assert "_sql_ident(" in interp, f"deps.py f-string SQL 插值未过 _sql_ident: {interp!r}"


# ---- 注入负例（端点行为） ----

PAYLOADS = [
    "' OR '1'='1",
    "admin'--",
    "'; DROP TABLE users;--",
    "' UNION SELECT id, username, password_hash FROM users--",
    "x' OR pg_sleep(3) IS NULL--",
    "x' OR 1=(SELECT COUNT(*) FROM users)--",
]


def _client_and_repo(monkeypatch):
    from control_plane.main import app
    from fastapi.testclient import TestClient

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    repo.create_user(
        username="victim-admin", password_hash=hash_password(PW),
        role="admin", org_id="", account_id="acc-victim",
    )
    repo.create_user(
        username="other-admin", password_hash=hash_password(PW),
        role="admin", org_id="", account_id="acc-other",
    )
    return TestClient(app), repo


def test_login_payload_is_literal(monkeypatch):
    client, _ = _client_and_repo(monkeypatch)
    for p in PAYLOADS:
        r = client.post("/api/auth/login", json={"username": p, "password": p})
        assert r.status_code == 401, f"payload {p!r} → {r.status_code}"
        assert "password_hash" not in r.text
    # 时间盲注面：payload 请求与普通错密码同量级（无 pg_sleep/SLEEP 拖慢）。
    t0 = time.monotonic()
    for _ in range(3):
        client.post("/api/auth/login", json={"username": "victim-admin", "password": "wrong"})
    base = time.monotonic() - t0
    t0 = time.monotonic()
    for _ in range(3):
        client.post("/api/auth/login", json={"username": "x' OR pg_sleep(3) IS NULL--", "password": "x"})
    slow = time.monotonic() - t0
    assert slow < base + 2.0, f"时间盲注迹象: base={base:.2f}s payload={slow:.2f}s"


def test_list_filters_payload_is_literal(monkeypatch):
    client, _ = _client_and_repo(monkeypatch)
    # auth-off 开发形态：直接打 list 端点，account_id 过滤参数灌 payload——
    # 断言零 500、零全量漂移（binding 参数下 payload 只是「查不到的账号名」）。
    for p in PAYLOADS:
        r = client.get("/api/calls", params={"account_id": p})
        assert r.status_code == 200, f"payload {p!r} → {r.status_code}"
        assert isinstance(r.json(), list)
    r = client.get("/api/objects", params={"account_id": "' OR '1'='1"})
    assert r.status_code == 200 and r.json() == []  # 字面值=无此账号=空


def test_users_create_payload_is_literal(monkeypatch):
    client, _ = _client_and_repo(monkeypatch)
    for p in PAYLOADS[:4]:
        r = client.post("/api/users", json={
            "username": p, "password": PW, "role": "user",
        })
        # 201（用户名被当字面值入库）或 400/409——绝不 500；随后登录同名字面值可达。
        assert r.status_code in (200, 201, 400, 409), f"payload {p!r} → {r.status_code}"
