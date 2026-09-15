# Bok Voice 安全深度测试修复实施计划（2026-09-16）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 2026-09-16 六路 subagent 深度安全测试发现的全部 P1、P2 与可代码化 P3 问题（详见各任务对应发现编号）。

**Architecture:** 控制面加固为主（FastAPI 鉴权内核 auth.py + 路由面 main.py + 节点注册表 nodes_store.py + 仓储层 repository.py），辅以 agent 机器通道（control_plane.py / node_agent.py）、桌面壳配置（tauri.conf.json / capabilities）、前端诊断通道（weblog.ts / Streamdown）与 VCS/镜像卫生（.gitignore / .dockerignore）。每个修复带回归测试，全部探针可离线复跑（TestClient + 内存仓，不依赖 LLM/TTS/LiveKit）。

**Tech Stack:** Python 3.12（FastAPI/SQLAlchemy/PyJWT）、TypeScript/React（apps/web）、Tauri v2（Rust shell）、bash（install-node.sh）。

## Global Constraints

- 分支：从当前默认分支切 `security-remediation-20260916` 工作。
- Python：PEP 8、4 空格、`from __future__ import annotations`、签名带类型注解；每次 Python 改动后跑 `.venv312/bin/python -m compileall -q apps packages services tools scripts`。
- DB 迁移只准写在 `apps/control-plane/control_plane/deps.py` `build_engine()` 幂等段；SQL 必须方言可移植（`tests/test_db_portability.py` 门禁）。
- 术语门禁：不引入 `yue` 字面量（`tests/test_cantonese_terminology.py` 全仓扫描）。
- Conventional commits with scope（`fix(security):` / `test(security):` / `chore(ci):`）；一次提交一个逻辑变更；body 写根因 + 验证证据。
- 测试命令统一用 `.venv312/bin/python -m pytest`。
- E2E 铁律不适用本计划（无新 E2E）；回归以单元/组件测试 + 全量 pytest 为准。
- auth-on 部署标准姿势不变（`BOK_AUTH_REQUIRED=1 BOK_JWT_SECRET=… BOK_CP_TOKEN=…` 同设）；本计划新增约束：**两值必须异值、JWT secret ≥32 字节**。
- 前端验证：`cd apps/web && npx tsc --noEmit && npm run build`；桌面壳验证：`cd desktop/src-tauri && cargo test`。
- 合并门（全部任务完成后）：`pytest`、`npm test`（services/realtime-translation）、`cargo test`、`scripts/verify_bundle.sh --staging` 全绿。

## 发现 → 任务覆盖对照

| 发现（深测编号） | 任务 |
|---|---|
| P1-1 机器通道=JWT 密钥回落 | Task 2 |
| P1-2 license 配额 TOCTOU | Task 16 |
| P1-3 空指纹注册绕过机器绑定 | Task 16 |
| P1-4 settle 同步 LLM 阻塞事件循环 | Task 15 |
| P2-1 停用/降级账号旧 token 8h 有效 | Task 3 |
| P2-2 /api/token supervisor- 前缀绕过 | Task 4 |
| P2-3 object_id 归属三洞（战役电话外呼泄露 / create_call 跨账号档案链 / settle 落盘穿越） | Task 8、9 |
| P2-4 by-ID 绕过页面闸 + DELETE calls 无角色闸 | Task 10 |
| P2-5 webhook 无签名校验 | Task 5 |
| P2-6 X-User-ID 审计伪造 | Task 6 |
| P2-7 节点：指纹自愿绕过 / 开放期 token 残留 / 单请求踢真机不自愈 | Task 17、18 |
| P2-8 agent 端不带 BOK_CP_TOKEN | Task 19 |
| P2-9 Tauri csp:null + shell 死配置 | Task 20 |
| P2-10 knowledge/import 缺 scoped_account | Task 11 |
| P3 全部（登录时序、fillers hit、insights、setup、roster、web_logs、CORS、审计缺口、limit 钳制、campaign acc-001、指纹弱档、密钥面） | Task 1、7、8、12、13、14、17、18、21 |
| 密钥/CICD 手工项（PyPI 占位、action pin SHA、GHCR 扫描） | 见文末「手工跟进项」 |

---

## 任务组 A：仓库卫生（P3，先行止血）

### Task 1: VCS 与镜像构建上下文泄密面收口

**Files:**
- Modify: `.gitignore`
- Modify: `.dockerignore`

**Interfaces:**
- Consumes: 无
- Produces: `decks/`、`.mimosa/`、`.v2c/` 不再可被 `git add -A` 收入；`deploy/cloud/.env` 不再进入任何 `docker build .` 上下文。

- [ ] **Step 1: 修改 `.gitignore`**

在文件末尾（`# SDD plan workspace` 段之后）追加：

```gitignore

# 未跟踪本地资产（2026-09-16 深测 P3）：商业路演稿/扫描器会话/视频产物，
# 公开仓一次 git add -A 即泄——显式忽略。
decks/
.mimosa/
.v2c/
```

- [ ] **Step 2: 修改 `.dockerignore`**

`# macOS / 编辑器杂项` 段之前追加（`.env*` 无 `**/` 前缀只匹配根层，`deploy/cloud/.env` 实测进过 build context）：

```dockerignore

# 任意深度的 env 档不进 build context（.env* 只匹配根层，deploy/cloud/.env
# 曾整体送进 BuildKit——镜像层未 COPY 它，但上下文本身不该见密钥）。
**/.env
**/.env.*
!**/.env.example
```

- [ ] **Step 3: 验证**

Run: `git check-ignore -v decks .mimosa .v2c && git status --short | grep -c "^" `
Expected: check-ignore 三行命中新规则；`git status` 不再列出这三个目录。

Run: `printf 'x' | docker build --no-cache --progress=plain -f Dockerfile . 2>&1 | head -5 || true`（可选，有 docker 才跑）或直接 `docker build` dry 检查：`cat .dockerignore | grep "\*\*/.env"`
Expected: 规则存在。

- [ ] **Step 4: Commit**

```bash
git add .gitignore .dockerignore
git commit -m "chore(security): ignore local untracked assets and deep .env in build context

decks/(商业路演稿) .mimosa/ .v2c/ 未被 gitignore 覆盖，公开仓 git add -A 即泄；
.dockerignore 的 .env* 不带 **/ 只匹配根层，deploy/cloud/.env 整体进 build context
（镜像层干净、GHCR 不受影响）。2026-09-16 深度安全测试 P3 修复。"
```

---

## 任务组 B：控制面鉴权内核

### Task 2: JWT 密钥与机器通道强制分离（P1-1）

**Files:**
- Modify: `apps/control-plane/control_plane/auth.py:8-11`（模块 docstring）、`:68-69`（jwt_secret）、`:38-50`（豁免表）
- Modify: `apps/control-plane/control_plane/main.py:93`（FastAPI docs 条件）、`:200-204`（startup 校验）
- Test: `tests/test_security_hardening.py`（新建）

**Interfaces:**
- Consumes: 无
- Produces: `auth.jwt_secret()` 只读 `BOK_JWT_SECRET`；startup 契约「auth-on ⇒ BOK_JWT_SECRET 必配、≥32 字节、与 BOK_CP_TOKEN 异值」。后续 Task 3/6 在同一文件继续改。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_security_hardening.py`：

```python
"""2026-09-16 深度安全测试修复回归：鉴权内核组。

覆盖：JWT 密钥分离（P1-1）、token 生命周期查库（P2-1）、/api/token supervisor
前缀闸（P2-2）、webhook 验签（P2-5）、审计 actor 不可伪造（P2-6）、登录时序（P3）。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # in-memory repo
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")

import pytest
from fastapi.testclient import TestClient

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import hash_password

PW = "Passw0rd!x"


def _make(monkeypatch, users=()):
    """TestClient(带 startup) + InMemory 仓；users=[{username, role, account}]。"""
    from control_plane import main as cp_main

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr(cp_main, "_repo", lambda: repo)
    for u in users:
        repo.create_user(
            username=u["username"], password_hash=hash_password(PW),
            role=u.get("role", "user"), org_id="org-t",
            account_id=u.get("account", "acc-001"),
        )
    return TestClient(cp_main.app), repo


def _login(client, username, password=PW):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_jwt_secret_never_falls_back_to_cp_token(monkeypatch):
    from control_plane import auth

    monkeypatch.delenv("BOK_JWT_SECRET", raising=False)
    monkeypatch.setenv("BOK_CP_TOKEN", "machine-token")
    assert auth.jwt_secret() == ""  # 机器通道凭据不再回落作签名密钥
    monkeypatch.setenv("BOK_JWT_SECRET", "real-secret-0123456789abcdef")
    assert auth.jwt_secret() == "real-secret-0123456789abcdef"


def test_startup_rejects_missing_or_shared_or_weak_secret(monkeypatch):
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.delenv("BOK_JWT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="BOK_JWT_SECRET"):
        from control_plane import main as cp_main

        cp_main._startup()
    monkeypatch.setenv("BOK_JWT_SECRET", "same-value-as-cp-token")
    monkeypatch.setenv("BOK_CP_TOKEN", "same-value-as-cp-token")
    with pytest.raises(RuntimeError, match="同值"):
        cp_main._startup()
    monkeypatch.setenv("BOK_JWT_SECRET", "short")
    with pytest.raises(RuntimeError, match="强度不足"):
        cp_main._startup()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_security_hardening.py -x -q`
Expected: FAIL（`jwt_secret()` 回落 CP token / `_startup` 不拒绝同值弱密钥）。

- [ ] **Step 3: 改 `auth.py`**

3a. 模块 docstring 第 8-11 行，`门禁开关：……拒绝用可伪造的 dev 密钥开认证。` 整段替换为：

```python
门禁开关：`BOK_AUTH_REQUIRED` 未设（默认）→ `identity_gate` 直通，单机/开发形态
零变化；置 1 后除豁免路径外全部要求有效用户 JWT 或机器 token。开认证时必须配置
`BOK_JWT_SECRET`（≥32 字节随机、与 `BOK_CP_TOKEN` 异值）——CP startup fail-closed，
机器通道凭据不再回落作签名密钥（2026-09-16 深测 P1：回落令 CP token 泄露=离线
伪造 8h root JWT）。
```

3b. `jwt_secret()`（68-69 行）替换：

```python
def jwt_secret() -> str:
    """JWT 签名密钥：只认 BOK_JWT_SECRET（2026-09-16 深测 P1 删除 CP token 回落）。

    BOK_CP_TOKEN 需拷进每台 agent worker，任何一份泄露即离线伪造 8h root JWT；
    两值必须独立配置（startup 校验同值/缺失/弱密钥拒绝开认证）。
    """
    return (os.environ.get("BOK_JWT_SECRET") or "").strip()
```

- [ ] **Step 4: 改 `main.py` startup（200-204 行）**

```python
    if auth_required():
        secret = (os.environ.get("BOK_JWT_SECRET") or "").strip()
        cp_token = (os.environ.get("BOK_CP_TOKEN") or "").strip()
        if not secret:
            # fail-closed：拒绝无签名密钥开认证（机器通道 CP token 不再回落）。
            raise RuntimeError(
                "BOK_AUTH_REQUIRED=1 但未配置 BOK_JWT_SECRET —— 拒绝开启认证"
                "（机器通道 CP token 不得兼作签名密钥）")
        if cp_token and cp_token == secret:
            # 2026-09-16 深测 P1：CP token 会拷进每台 agent worker，同值=泄露即
            # 离线伪造 root JWT。
            raise RuntimeError(
                "BOK_CP_TOKEN 与 BOK_JWT_SECRET 同值 —— 机器通道凭据不得兼作"
                " JWT 签名密钥，拒绝开启认证")
        if len(secret.encode("utf-8")) < 32:
            raise RuntimeError("BOK_JWT_SECRET 强度不足（<32 字节）—— 拒绝开启认证")
```

4b. 同步收紧 API 文档面（93 行 `app = FastAPI(...)` 替换）：

```python
app = FastAPI(
    title="Bok Voice Control Plane",
    version="0.1.0",
    # auth-on 生产关 API 文档（深测 P3：/openapi.json /docs /redoc 匿名可读=全
    # API 面暴露）。auth-on 必须在 CP 进程 env 先行设置（与下方 startup 校验同一
    # 要求）；auth-off 开发形态文档照常。
    docs_url=None if auth_required() else "/docs",
    redoc_url=None if auth_required() else "/redoc",
    openapi_url=None if auth_required() else "/openapi.json",
)
```

4c. `auth.py` `_EXEMPT_PATHS`（38-50 行）删除 `"/docs"`、`"/openapi.json"`、`"/redoc"` 三行（auth-on 下文档已不存在；auth-off 本就直通，表内残留无意义），注释改为：

```python
# 豁免路径：健康检查 / 登录本身 / 节点心跳与注册自鉴权 / LiveKit 服务端 webhook
#（webhook 由 LiveKit server 直调 CP，无用户也无机器 env，属基础设施通道；
# API 文档不再豁免——auth-on 生产由 FastAPI 条件参数直接关闭）。
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_security_hardening.py -x -q && .venv312/bin/python -m pytest tests/test_auth.py tests/test_scope.py -q`
Expected: 全 PASS（test_auth.py 已 setdefault BOK_JWT_SECRET，不受影响）。

- [ ] **Step 6: Commit**

```bash
git add apps/control-plane/control_plane/auth.py apps/control-plane/control_plane/main.py tests/test_security_hardening.py
git commit -m "fix(security): decouple machine token from JWT signing secret, fail closed on weak config

P1-1(深测): jwt_secret() 回落 BOK_CP_TOKEN——CP token 拷进每台 agent worker,
泄露任一份即可离线伪造 8h root JWT。现 startup 校验 auth-on 必须 BOK_JWT_SECRET
>=32 字节且与 BOK_CP_TOKEN 异值;同步关闭 auth-on 的 /docs /openapi.json /redoc。
验证: tests/test_security_hardening.py 3 项 + test_auth/test_scope 全绿。"
```

### Task 3: token 生命周期查库——禁用/降级/删号即时生效（P2-1）

**Files:**
- Modify: `apps/control-plane/control_plane/main.py`（startup 注册 lookup）、`auth.py:259-263`（identity_gate 解码后查库）
- Test: `tests/test_security_hardening.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 auth.py 现状。
- Produces: `app.state.user_lookup: Callable[[str], dict | None]`（startup 注入，identity_gate 可选消费；测试可手动设）。

- [ ] **Step 1: 追加失败测试**

```python
def test_disabled_or_demoted_token_dies_immediately(monkeypatch):
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    client, repo = _make(monkeypatch, users=[
        {"username": "peon", "role": "user"},
        {"username": "boss", "role": "admin"},
        {"username": "rooty", "role": "root", "account": "acc-002"},
    ])
    with client:  # 触发 startup：注入 app.state.user_lookup
        peon_token = _login(client, "peon")
        boss_token = _login(client, "boss")
        # 禁用 peon → 旧 token 立即 401
        rooty_headers = {"Authorization": "Bearer " + _login(client, "rooty")}
        assert client.post("/api/users", json={"username": "x1", "password": PW,
                                               "role": "user"}, headers=rooty_headers).status_code == 200
        # root 停用 peon（rooty 在 acc-002，跨账号管理被 deny——改由 admin 路径：
        # 直接用 repo 模拟主管禁用）
        repo.update_user(repo.get_user_by_username("peon")["id"], status="disabled")
        assert client.get("/api/objects", headers={"Authorization": f"Bearer {peon_token}"}).status_code == 401
        # 降级 boss：admin→user 后旧 token 打管理面立即 403
        repo.update_user(repo.get_user_by_username("boss")["id"], role="user")
        assert client.get("/api/settings", headers={"Authorization": f"Bearer {boss_token}"}).status_code == 403
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_security_hardening.py::test_disabled_or_demoted_token_dies_immediately -x -q`
Expected: FAIL（旧 token 200/200）。

- [ ] **Step 3: main.py startup 注入 lookup**

`_startup()` 内 `app.state.node_store = NodeStore(engine)` 行后加：

```python
    # token 生命周期（2026-09-16 深测 P2）：identity_gate 解码后按 sub 查库——
    # 禁用/删号立即 401、role 以库为准（降权即时生效），不再吃满 8h TTL。
    app.state.user_lookup = lambda user_id: _repo().get_user(user_id)
```

- [ ] **Step 4: auth.py identity_gate 查库**

`identity_gate` 中 `try: identity = decode_token(token) ... request.state.identity = identity` 段（decode 成功与赋值之间）插入：

```python
    lookup = getattr(getattr(request, "app", None), "state", None)
    lookup = getattr(lookup, "user_lookup", None) if lookup is not None else None
    if lookup is not None:
        row = lookup(identity.user_id) or {}
        if not row or str(row.get("status") or "") != "active":
            # 禁用/删号的旧 token 立即失效（此前最长 8h TTL 内照常全权调用）。
            return _unauthorized()
        # 角色以库为准：降权（admin→user）即时生效，require_role 不再信过期 claim。
        identity.role = str(row.get("role") or identity.role)
    request.state.identity = identity
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_security_hardening.py tests/test_auth.py tests/test_permissions.py -q`
Expected: 全 PASS（user_lookup 未注入时行为不变——auth-off/旧测试零破坏）。

- [ ] **Step 6: Commit**

```bash
git add apps/control-plane/control_plane/main.py apps/control-plane/control_plane/auth.py tests/test_security_hardening.py
git commit -m "fix(security): enforce account status/role from DB on every auth-on request

P2-1(深测): identity_gate 只验 JWT 签名不查库——停用/删号/降级后旧 token 仍全权
调用最长 8h。现解码后按 sub 查一次 users 行(主键查,开销可忽略):禁用/删号 401,
role 以库为准。app.state.user_lookup 由 startup 注入,未注入(旧测试)零变化。"
```

### Task 4: /api/token 的 supervisor- 前缀闸序修复（P2-2）

**Files:**
- Modify: `apps/control-plane/control_plane/main.py:851-861`（403 闸）、`:877-896`（角色解析）
- Test: `tests/test_security_hardening.py`（追加）

**Interfaces:**
- Consumes: TokenRequest（schemas.py:10，字段 role/purpose/participant_identity）。
- Produces: 无新接口。

- [ ] **Step 1: 追加失败测试**

```python
def test_token_supervisor_identity_prefix_blocked_for_user(monkeypatch):
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    client, repo = _make(monkeypatch, users=[
        {"username": "peon", "role": "user"},
        {"username": "op2", "role": "user", "account": "acc-002"},
    ])
    with client:
        peon = {"Authorization": "Bearer " + _login(client, "peon")}
        # 建一通本账号通话
        r = client.post("/api/calls", json={"account_id": "acc-001"}, headers=peon)
        call_id = r.json()["id"]
        # 直路：role/purpose 字段已被 B4 堵死
        assert client.post("/api/token", json={"call_id": call_id, "role": "supervisor"},
                           headers=peon).status_code == 403
        assert client.post("/api/token", json={"call_id": call_id, "purpose": "listen"},
                           headers=peon).status_code == 403
        # 第三条路（深测 P2）：participant_identity 前缀反推——旧版 201 漏签
        r = client.post("/api/token", json={"call_id": call_id,
                                            "participant_identity": f"supervisor-{call_id}"},
                        headers=peon)
        assert r.status_code == 403, r.text
        # operator 正常签发不受影响
        assert client.post("/api/token", json={"call_id": call_id},
                           headers=peon).status_code == 201
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_security_hardening.py::test_token_supervisor_identity_prefix_blocked_for_user -x -q`
Expected: FAIL（participant_identity 路径 201）。

- [ ] **Step 3: 改 main.py /api/token**

3a. 删除 855-861 行的既有 403 闸（`if _ident is not None and _ident.role not in ("root", "admin") and (...)` 整块），只保留 401 检查（853-854）。

3b. 在角色解析完成后（`if is_listen and not identity_input: role = "supervisor"` 之后、`if role == "me":` 之前）插入：

```python
    # supervisor 语义闸（2026-09-16 深测 P2）：**移到角色解析之后**——旧闸只看
    # req.role/req.purpose 字段，participant_identity="supervisor-<room>" 走前缀
    # 反推在闸后改写 role=supervisor，话务员可自签主管身份房 token（第三条绕过路）。
    # 显式字段与前缀两条路都由最终 role 统一把关；is_listen 单列（grants 语义不同）。
    if _ident is not None and _ident.role not in ("root", "admin") and (
        role == "supervisor" or is_listen
    ):
        raise HTTPException(status_code=403, detail="forbidden")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_security_hardening.py tests/test_supervisor_listen.py tests/test_auth.py -q`
Expected: 全 PASS（supervisor_listen 用 admin 签发，不受影响）。

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/control_plane/main.py tests/test_security_hardening.py
git commit -m "fix(security): gate supervisor room tokens after identity-prefix role resolution

P2-2(深测): /api/token 的 403 闸在 role 解析之前,user 传 participant_identity=
supervisor-<call_id> 可在闸后反推出 supervisor 角色——admin/root 专属契约被
打破。闸移到 role/is_listen 定型之后,显式字段与前缀两条路统一把关。"
```

### Task 5: LiveKit webhook 验签 + 重派锁上限（P2-5）

**Files:**
- Modify: `apps/control-plane/control_plane/main.py:2916-2961`（webhook handler 前段）
- Test: `tests/test_security_hardening.py`（追加）

**Interfaces:**
- Consumes: `LIVEKIT_API_SECRET`（env / app.state.lk_secret）；pyjwt（auth.py 已依赖）。
- Produces: `_verify_livekit_webhook(request: Request, body: bytes) -> bool`（模块级，供 handler 调用；secret 未配置时放行并打点——本地无 LiveKit 联调形态）。

- [ ] **Step 1: 追加失败测试**

```python
def _sign_webhook(body: bytes, secret: str) -> str:
    import hashlib
    import time

    import jwt as pyjwt

    now = int(time.time())
    return pyjwt.encode({
        "iss": "devkey", "sub": "devkey", "iat": now, "nbf": now - 5, "exp": now + 300,
        "video": {"webhook": True},
        "sha256": hashlib.sha256(body).hexdigest(),
    }, secret, algorithm="HS256")


def test_livekit_webhook_requires_valid_signature(monkeypatch):
    monkeypatch.setenv("LIVEKIT_API_SECRET", "whsec-test-0123456789abcdef")
    client, repo = _make(monkeypatch)
    payload = {"event": "participant_left", "room": {"name": "call-x"},
               "participant": {"identity": "bok-voice"}}
    # 无签名 → 401（旧版 200 handled:true）
    r = client.post("/api/webhook/livekit", json=payload)
    assert r.status_code == 401, r.text
    # 错误签名 → 401
    r = client.post("/api/webhook/livekit", json=payload,
                    headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401
    # 正确签名 → 200
    import json as _json

    raw = _json.dumps(payload).encode()
    r = client.post("/api/webhook/livekit", content=raw,
                    headers={"Authorization": "Bearer " + _sign_webhook(raw, "whsec-test-0123456789abcdef"),
                             "Content-Type": "application/json"})
    assert r.status_code == 200, r.text


def test_livekit_webhook_open_when_no_secret(monkeypatch):
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)
    client, repo = _make(monkeypatch)
    r = client.post("/api/webhook/livekit", json={"event": "participant_left",
                                                  "room": {"name": "call-x"},
                                                  "participant": {"identity": "bok-voice"}})
    assert r.status_code == 200  # 本地无 LiveKit 联调形态保持可用
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_security_hardening.py -k webhook -x -q`
Expected: FAIL（无签名 200）。

- [ ] **Step 3: 实现**

3a. `_redispatch_locks` 定义（2924 行）后加锁表上限守卫常量与函数不需要——直接在 handler 顶部做修剪（见下）。

3b. `livekit_webhook` handler 头部（`try: payload = await request.json()` 之前）替换为：

```python
    # 验签（2026-09-16 深测 P2）：本端点在中间件豁免表里=匿名可达。伪造
    # participant_left 会触发最多 3 轮 LiveKit 云 API 放大调用 + _redispatch_locks
    # 无界增长。官方姿势：LiveKit server 以 LIVEKIT_API_SECRET 签 JWT（HS256，
    # video.webhook grant）并在 claim 里带 sha256(body) 摘要——双验。
    # LIVEKIT_API_SECRET 未配置（本地无 LiveKit 联调）→ 放行并打点，生产必配。
    raw = await request.body()
    if not _verify_livekit_webhook(request, raw):
        raise HTTPException(status_code=401, detail="invalid webhook signature")
    if len(_redispatch_locks) > 512:
        # 攻击者可用任意 room 名撑大锁图（模块注释自认不做淘汰）——上限修剪。
        for _k in [k for k, v in _redispatch_locks.items() if not v.locked()][:256]:
            _redispatch_locks.pop(_k, None)
    try:
        payload = json.loads(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")
```

3c. handler 上方加模块级函数：

```python
def _verify_livekit_webhook(request: Request, body: bytes) -> bool:
    """LiveKit webhook 官方验签：Authorization Bearer JWT（HS256/LIVEKIT_API_SECRET）
    + video.webhook grant + sha256(body) 摘要（2026-09-16 深测 P2）。"""
    import hashlib

    import jwt as _pyjwt

    secret = getattr(app.state, "lk_secret", "") or os.environ.get("LIVEKIT_API_SECRET", "")
    if not secret:
        control_log.warning("webhook_unsigned_accepted", extra={"event": "webhook.unsigned"})
        return True
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not token:
        return False
    try:
        claims = _pyjwt.decode(token, secret, algorithms=["HS256"], options={"verify_aud": False})
    except _pyjwt.PyJWTError:
        return False
    if not (claims.get("video") or {}).get("webhook"):
        return False
    return claims.get("sha256") == hashlib.sha256(body).hexdigest()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_security_hardening.py -k webhook -q && .venv312/bin/python -m pytest tests/test_control_plane.py -q`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/control_plane/main.py tests/test_security_hardening.py
git commit -m "fix(security): verify LiveKit webhook JWT signature, bound redispatch lock map

P2-5(深测): /api/webhook/livekit 匿名可达且无任何校验,伪造 participant_left 可
放大打 LiveKit API 配额+撑大重派锁图。现按官方姿势验 Authorization JWT
(HS256/LIVEKIT_API_SECRET + video.webhook grant + sha256(body));锁图 >512 修剪。
secret 未配置(本地无 LiveKit)保持放行并打点。"
```

### Task 6: 审计 actor 不可伪造 + CP token 常量时间比较（P2-6 + P3）

**Files:**
- Modify: `apps/control-plane/control_plane/auth.py`（identity_gate 三处 + 新 helper）
- Modify: `apps/control-plane/control_plane/main.py:129`（optional_bearer_auth 比较）、imports 加 `hmac`
- Test: `tests/test_security_hardening.py`（追加）

**Interfaces:**
- Consumes: bok_voice_obs.context 的 get_correlation/set_correlation/Correlation（auth.py 已导入）。
- Produces: `auth._override_correlation_user(request: Request, user_id: str) -> None`。

- [ ] **Step 1: 追加失败测试**

```python
def test_machine_channel_actor_cannot_be_spoofed(monkeypatch):
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    monkeypatch.setenv("BOK_CP_TOKEN", "mach-token-xyz")
    client, repo = _make(monkeypatch)
    with client:  # startup 装审计 tap → 审计进 repo
        r = client.post("/api/calls", json={"account_id": "acc-001"},
                        headers={"Authorization": "Bearer mach-token-xyz",
                                 "X-User-ID": "spoofed-root"})
        assert r.status_code == 200
        import json as _json

        dumped = _json.dumps(repo.list_audit_events(limit=100)).lower()
        assert "spoofed" not in dumped
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_security_hardening.py::test_machine_channel_actor_cannot_be_spoofed -x -q`
Expected: FAIL（审计 actor=spoofed-root）。

- [ ] **Step 3: 实现 auth.py**

3a. `_unauthorized()` 定义后加：

```python
def _override_correlation_user(request: Request, user_id: str) -> None:
    """无验证身份的通道（机器/豁免/静态）覆写 correlation.user_id——审计 actor
    不可被客户端 X-User-ID 头伪造（2026-09-16 深测 P2：机器通道曾可自带
    X-User-ID: spoofed-root 落审计，破坏追溯完整性）。"""
    corr = get_correlation()
    set_correlation(
        Correlation(
            request_id=corr.request_id,
            call_id=corr.call_id,
            account_id=corr.account_id,
            object_id=corr.object_id,
            persona_id=corr.persona_id,
            user_id=user_id,
            span_id=corr.span_id,
        )
    )
```

3b. `identity_gate` 首个放行分支（247-248 行）改为：

```python
    if path in _EXEMPT_PATHS or static_get or not auth_required():
        _override_correlation_user(request, "anonymous")
        return await call_next(request)
```

3c. 机器通道分支（253-256 行）改为（顺带常量时间比较）：

```python
    cp_token = os.environ.get("BOK_CP_TOKEN", "").strip()
    if cp_token and token and hmac.compare_digest(token, cp_token):
        request.state.machine = True
        _override_correlation_user(request, "machine")
        return await call_next(request)
```

3d. main.py：imports 加 `import hmac`（asyncio 同列）；`optional_bearer_auth` 的比较（129 行）改为常量时间：

```python
        provided = request.headers.get("authorization", "")
        if not static_get and not (
            provided.startswith("Bearer ")
            and hmac.compare_digest(provided[7:].strip(), expected)
        ):
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_security_hardening.py tests/test_auth.py tests/test_turn_auditability.py -q`
Expected: 全 PASS（若 test_turn_auditability 断言 auth-off actor=X-User-ID 旧行为，按新契约 actor=anonymous 修正该断言——新契约是修复目标）。

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/control_plane/auth.py apps/control-plane/control_plane/main.py tests/test_security_hardening.py
git commit -m "fix(security): stop client X-User-ID header from forging audit actor, constant-time machine token compare

P2-6(深测): 机器通道/豁免路径不覆写 correlation,客户端 X-User-ID 直接落审计
actor;顺带把两处 CP token 比较换 hmac.compare_digest(与密码路径同标准)。"
```

### Task 7: 登录时序侧信道均衡（P3）

**Files:**
- Modify: `apps/control-plane/control_plane/main.py:701-720`（auth_login）
- Test: `tests/test_security_hardening.py`（追加功能断言）

**Interfaces:**
- Consumes: `hash_password` / `verify_password`（auth.py）。
- Produces: 模块常量 `_DUMMY_PASSWORD_HASH: str`。

- [ ] **Step 1: 追加测试**

```python
def test_login_unknown_user_still_401_with_uniform_message(monkeypatch):
    client, repo = _make(monkeypatch, users=[{"username": "alice", "role": "user"}])
    assert client.post("/api/auth/login",
                       json={"username": "nobody", "password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login",
                       json={"username": "alice", "password": "wrong"}).status_code == 401
    # 两条路径错误文案一致（存在性只可经时序探测——实现里已均衡，此处锁文案）
```

- [ ] **Step 2: 实现**

`auth_login` 上方加模块常量（`_TERMINAL_CALL_STATUSES` 附近）：

```python
# 登录时序均衡（2026-09-16 深测 P3）：用户不存在时也跑一次同价位 scrypt 校验。
# 旧版短路令「存在且 active」可被 ~20× 响应差探测（1.1ms vs 25.5ms 实测）。
_DUMMY_PASSWORD_HASH = hash_password("bok-dummy-login-timing-equalizer")
```

`auth_login` 开头（703-710 行）替换为：

```python
    user = _repo().get_user_by_username(req.username.strip())
    ok = verify_password(
        req.password,
        str((user or {}).get("password_hash") or "") or _DUMMY_PASSWORD_HASH,
    )
    if not user or user.get("status") != "active" or not ok:
        _audit("auth.login_failed", subject_type="user", subject_id=req.username[:64], outcome="denied")
        raise HTTPException(401, "用户名或密码不正确")
```

- [ ] **Step 3: 跑测试**

Run: `.venv312/bin/python -m pytest tests/test_security_hardening.py tests/test_auth.py -q`
Expected: 全 PASS。

- [ ] **Step 4: Commit**

```bash
git add apps/control-plane/control_plane/main.py tests/test_security_hardening.py
git commit -m "fix(security): equalize login timing for unknown users (dummy scrypt verify)

P3(深测): 用户不存在短路跳过 scrypt,存在性可被响应时序探测(实测差 >20x)。
统一走完整 verify_password(缺失时校验 dummy hash)。"
```

---

## 任务组 C：对象归属与授权面

### Task 8: object_id 跨账号归属闸（P2-3 前两洞）+ campaign 循环全账号（P3）

**Files:**
- Modify: `apps/control-plane/control_plane/main.py:1041-1053`（create_call）、`:1654-1692`（create_campaign）
- Modify: `packages/business-db/bok_voice_business_db/repository.py:923-929`（SQL list_campaigns）、`:1740-1745`（InMemory list_campaigns）
- Modify: `apps/control-plane/control_plane/campaign.py:145`
- Test: `tests/test_object_attribution.py`（新建）

**Interfaces:**
- Consumes: `deny_cross_account` 语义（404 不泄存在性）。
- Produces: `repo.list_campaigns(account_id, status)` 的 `account_id=""` = 跨账号全部（与 `list_calls("")` 同语义）。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_object_attribution.py`：

```python
"""2026-09-16 深测修复回归：object_id 跨账号归属（P2-3）与 campaign 全账号循环。"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")

from fastapi.testclient import TestClient

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import hash_password

PW = "Passw0rd!x"


def _make(monkeypatch, users=()):
    from control_plane import main as cp_main

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr(cp_main, "_repo", lambda: repo)
    for u in users:
        repo.create_user(username=u["username"], password_hash=hash_password(PW),
                         role=u.get("role", "user"), org_id="org-t",
                         account_id=u.get("account", "acc-001"))
    return TestClient(cp_main.app), repo


def _object(repo, account, phone="13800001111"):
    return repo.create_object(account, {"display_name": f"obj-{account}", "phone": phone})


def test_campaign_rejects_cross_account_objects(monkeypatch):
    client, repo = _make(monkeypatch, users=[{"username": "peon", "role": "user"}])
    foreign = _object(repo, "acc-002")
    r = client.post("/api/campaigns", json={
        "account_id": "acc-001", "name": "exfil", "object_ids": [foreign["id"]],
    })
    assert r.status_code == 404, r.text  # 旧版 201，items[].phone 回显他账号手机号


def test_create_call_rejects_cross_account_object(monkeypatch):
    client, repo = _make(monkeypatch, users=[{"username": "peon", "role": "user"}])
    foreign = _object(repo, "acc-002")
    r = client.post("/api/calls", json={"account_id": "acc-001", "object_id": foreign["id"]})
    assert r.status_code == 404  # 旧版 200 且 template_id 泄露他账号话术绑定
    # 本账号对象照常
    mine = _object(repo, "acc-001")
    assert client.post("/api/calls",
                       json={"account_id": "acc-001", "object_id": mine["id"}]).status_code == 200


def test_campaign_loop_covers_all_accounts(monkeypatch):
    repo = InMemoryBusinessRepository()
    camp_a = repo.create_campaign("acc-001", name="a", template_id="", persona_id="",
                                  language="zh", gap_seconds=5, object_ids=[])
    camp_b = repo.create_campaign("acc-002", name="b", template_id="", persona_id="",
                                  language="zh", gap_seconds=5, object_ids=[])
    repo.update_campaign(camp_a["id"], status="running")
    repo.update_campaign(camp_b["id"], status="running")
    running = repo.list_campaigns("", status="running")
    assert {c["id"] for c in running} == {camp_a["id"], camp_b["id"]}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_object_attribution.py -x -q`
Expected: FAIL（战役 201 / create_call 200 / `list_campaigns("")` 返回空）。

- [ ] **Step 3: 实现 main.py**

3a. `create_call`（1045-1053 行）在 `created_by = identity.user_id` 后追加：

```python
        # 对象归属（2026-09-16 深测 P2）：跨账号对象 404——旧版照常建单，对象卡
        # 话术快照外泄他账号话术 id，agent 装配线（机器通道）更会把他账号客户
        # 档案读进 prompt。root 例外；对象不存在沿用旧行为（无模板快照）。
        if req.object_id:
            obj = _repo().get_object(req.object_id)
            if obj is not None and str(obj.get("account_id") or "") != req.account_id:
                raise HTTPException(status_code=404, detail="not found")
```

3b. `create_campaign`（1666-1668 行的账号钉定块后、`if not req.object_ids` 前）追加：

```python
    # 名单归属（2026-09-16 深测 P2）：任一对象属他账号 → 整单 404（fail-closed）。
    # 旧版 items[].phone 直接回显他账号客户手机号，且战役循环会真实外呼该号码。
    if identity is not None and identity.role != "root":
        for oid in req.object_ids:
            obj = _repo().get_object(oid)
            if obj is not None and str(obj.get("account_id") or "") != req.account_id:
                raise HTTPException(status_code=404, detail="not found")
```

- [ ] **Step 4: repository 双后端 list_campaigns 支持空账号**

SQL 版（923 行）替换为：

```python
    def list_campaigns(self, account_id: str = "acc-001", status: str = "") -> list[dict]:
        q = self.session.query(models.Campaign)
        if account_id:  # 空=跨账号全部（campaign 循环巡检用，与 list_calls("") 同语义）
            q = q.filter(models.Campaign.account_id == account_id)
        if status:
            q = q.filter(models.Campaign.status == status)
        rows = q.order_by(models.Campaign.created_at.desc()).all()
        return [self._campaign_to_dict(r) for r in rows]
```

InMemory 版（1740 行）过滤行替换为：

```python
        rows = [self._campaign_public(r) for r in self.campaigns.values()
                if (not account_id or r["account_id"] == account_id)
                and (not status or r["status"] == status)]
```

`campaign.py:145` 改为：

```python
    for campaign in repo.list_campaigns("", status="running"):
```

（原 `DEFAULT_ACCOUNT_ID` 硬编码令非 acc-001 账号的战役永不起拨——多账号功能性静默失效。）

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_object_attribution.py tests/test_campaign_api.py tests/test_campaign_loop.py tests/test_campaign_repo.py tests/test_scope.py -q`
Expected: 全 PASS（campaign 既有测试跑在 acc-001，`""` 超集不影响断言）。

- [ ] **Step 6: Commit**

```bash
git add apps/control-plane/control_plane/main.py packages/business-db/bok_voice_business_db/repository.py apps/control-plane/control_plane/campaign.py tests/test_object_attribution.py
git commit -m "fix(security): enforce object ownership on call/campaign creation, loop campaigns across accounts

P2-3(深测): 建战役/建单引用跨账号 object_id 不校验——items[].phone 回显他账号
客户手机号+真实外呼,create_call 快照他账号话术并把客户档案喂进 agent prompt。
现非 root 引用他账号对象一律 404。顺带修 campaign 循环硬编码 acc-001(非该账号
战役永不起拨),repo.list_campaigns 支持空账号=全账号(与 list_calls 同语义)。"
```

### Task 9: settle 落盘路径段白名单（P2-3 第三洞）

**Files:**
- Modify: `apps/control-plane/control_plane/main.py:2161-2236`（_write_settlement_docs / _write_distill_knowledge）+ imports 加 `re`
- Test: `tests/test_object_attribution.py`（追加）

**Interfaces:**
- Consumes: 无
- Produces: `_safe_segment(value: str, fallback: str) -> str`。

- [ ] **Step 1: 追加失败测试**

```python
def test_settle_docs_path_sanitizes_object_id(monkeypatch, tmp_path):
    from control_plane import main as cp_main

    assert cp_main._safe_segment("../../acc-002/knowledge/evil", "unknown") == ".._.._acc-002_knowledge_evil"
    assert cp_main._safe_segment("", "unknown") == "unknown"
    assert cp_main._safe_segment("obj-abc123", "unknown") == "obj-abc123"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_object_attribution.py::test_settle_docs_path_sanitizes_object_id -x -q`
Expected: FAIL（`_safe_segment` 不存在）。

- [ ] **Step 3: 实现**

main.py imports 加 `import re`。`_write_settlement_docs` 上方加：

```python
def _safe_segment(value: str, fallback: str) -> str:
    """vault 相对路径段白名单（2026-09-16 深测 P2）：object_id/call_id 拼路径前收敛
    成 [A-Za-z0-9_-]。旧版 create_call 收任意 object_id，settle 落盘
    accounts/{account}/objects/{object}/… 可用 ../../ 把转写/蒸馏写进他账号
    knowledge/ 并被重启索引（跨租户知识投毒）。合法 id（obj-*/call-*/acc-*）逐字节不变。"""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", str(value or "").strip())
    return cleaned or fallback
```

`_write_settlement_docs` 内（2165-2168 行）：

```python
    account_id = _safe_segment(call.get("account_id") or "", "acc-001")
    object_id = _safe_segment(call.get("object_id") or "", "unknown")
    call_id = _safe_segment(call.get("id") or "", "unknown")
```

`_write_distill_knowledge` 内（2206-2208 行）同样三行替换。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_object_attribution.py tests/test_control_plane.py tests/test_qa_summary.py -q`
Expected: 全 PASS（落盘审计断言 path 仍含合法 id，逐字节不变）。

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/control_plane/main.py tests/test_object_attribution.py
git commit -m "fix(security): whitelist path segments before writing settlement/distill docs to vault

P2-3(深测): settle 落盘直接拼 object_id,任意串可 ../ 穿进他账号 knowledge/ 并被
重启索引(跨租户知识投毒)。_safe_segment 收敛 [A-Za-z0-9_-],合法 id 零变化。"
```

### Task 10: by-ID 端点页面闸 + DELETE calls 角色闸（P2-4）

**Files:**
- Modify: `apps/control-plane/control_plane/main.py`（下列函数各自首行）
- Test: `tests/test_access_gates.py`（新建）

**Interfaces:**
- Consumes: `_gate_page` / `require_role`（main.py 既有）。
- Produces: 无新接口。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_access_gates.py`：

```python
"""2026-09-16 深测修复回归：by-ID 页面闸与 DELETE 角色闸（P2-4）+ 杂项闸（P3）。"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")

import pytest
from fastapi.testclient import TestClient

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import hash_password

PW = "Passw0rd!x"


def _make(monkeypatch, permissions=None):
    from control_plane import main as cp_main

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr(cp_main, "_repo", lambda: repo)
    repo.create_user(username="peon", password_hash=hash_password(PW), role="user",
                     org_id="org-t", account_id="acc-001",
                     permissions_json="[]" if permissions == "none" else "")
    return TestClient(cp_main.app), repo


def _peon(client):
    r = client.post("/api/auth/login", json={"username": "peon", "password": PW})
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_permissionless_user_cannot_read_or_delete_calls_by_id(monkeypatch):
    from bok_voice_core.types import TurnEvent

    client, repo = _make(monkeypatch, permissions="none")
    repo.create_call({"id": "call-g1", "account_id": "acc-001", "object_id": "",
                      "mode": "simulation"})
    repo.update_call("call-g1", status="ended")
    repo.create_turn(TurnEvent(trace_id="call-g1", call_id="call-g1", turn_id="t1",
                               role="user", transcript="hi"))
    h = _peon(client)
    assert client.get("/api/calls", headers=h).status_code == 403      # 列表闸既有
    assert client.get("/api/calls/call-g1", headers=h).status_code == 403
    assert client.get("/api/calls/call-g1/turns", headers=h).status_code == 403
    assert client.delete("/api/calls/call-g1", headers=h).status_code == 403
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_access_gates.py -x -q`
Expected: FAIL（by-ID 200 / DELETE 200）。

- [ ] **Step 3: 实现（逐端点首行插入闸）**

3a. 以下**读面**函数体首行插入 `_gate_page(request, "calls")`（与 `GET /api/calls` 列表同口径；机器通道/无身份直通，agent 上报零破坏）：

`get_call`、`get_settlement`、`get_turns`、`get_call_metrics`、`hangup`、`add_turn`、`report_whatsapp`、`report_dial_result`、`mark_whatsapp_handled`、`settle`、`ingest_session_report`。

示例（get_call，其余同款一行）：

```python
@app.get("/api/calls/{call_id}")
def get_call(call_id: str, request: Request) -> dict:
    _gate_page(request, "calls")
    call = deny_cross_account(request, _repo().get_call(call_id))
    ...
```

3b. **删改面**加角色闸 + 页面闸（通话转写=客户敏感记录，user 不应有删权）：

```python
@app.delete("/api/calls/{call_id}")
def delete_call(call_id: str, request: Request) -> dict:
    require_role(request, "admin", "root")
    _gate_page(request, "calls")
    deny_cross_account(request, _repo().get_call(call_id))
    ...

@app.delete("/api/calls")
def clear_ended_calls(request: Request, account_id: str = "acc-001") -> dict:
    require_role(request, "admin", "root")
    """清空该账号下已结束(ended)的通话历史。活跃/进行中的通话不删。"""
    account_id = scoped_account(request, account_id)
    ...
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_access_gates.py tests/test_control_plane.py tests/test_turns_ledger.py tests/test_roster_api.py -q`
Expected: 全 PASS。若既有测试以「user 删通话」为预期，按新契约改断言（user 删除=403 是修复目标）。

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/control_plane/main.py tests/test_access_gates.py
git commit -m "fix(security): gate call by-ID endpoints under calls page key, admin-only delete

P2-4(深测): 页面闸只设在列表端点,permissions=[] 的 user 仍可按 call_id 读全部
转写(PII)、hangup/settle;DELETE /api/calls* 更是无任何角色闸(审计灭失)。
by-ID 全族补 _gate_page(calls),删除面收归 admin/root(机器通道直通不受影响)。"
```

### Task 11: knowledge/import 收窄 + 路径段校验（P2-10）

**Files:**
- Modify: `apps/control-plane/control_plane/main.py:2548-2553`（import_knowledge）
- Test: `tests/test_object_attribution.py`（追加）

**Interfaces:**
- Consumes: `scoped_account`（auth.py）。
- Produces: 无新接口。

- [ ] **Step 1: 追加失败测试**

```python
def test_knowledge_import_scoped_and_no_dotdot(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
    client, repo = _make(monkeypatch)  # _make 已用 hash_password 建号（Task 8）
    repo.create_user(username="adm", password_hash=hash_password(PW),
                     role="admin", org_id="org-t", account_id="acc-001")
    with client:  # startup：装配 app.state.knowledge（VAULT_ROOT 此刻已 patch）
        r = client.post("/api/auth/login", json={"username": "adm", "password": PW})
        h = {"Authorization": "Bearer " + r.json()["token"]}
        # ① body account_id 指他账号 → 被压回本账号（旧版 200 写进他账号命名空间）
        r = client.post("/api/knowledge/import",
                        json={"account_id": "acc-002", "path": "notes.md", "content": "hi"},
                        headers=h)
        assert r.status_code == 200
        assert not (tmp_path / "accounts" / "acc-002").exists()
        assert (tmp_path / "accounts" / "acc-001" / "knowledge" / "notes.md").exists()
        # ② path 含 .. → 400
        r = client.post("/api/knowledge/import",
                        json={"account_id": "acc-001", "path": "../acc-002/knowledge/x.md",
                              "content": "pwned"},
                        headers=h)
        assert r.status_code == 400
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_object_attribution.py -k knowledge -x -q`
Expected: FAIL（写入 acc-002 / `..` 200）。

- [ ] **Step 3: 实现**

`import_knowledge` 替换为：

```python
@app.post("/api/knowledge/import")
async def import_knowledge(req: ImportRequest, request: Request) -> dict:
    require_role(request, "admin", "root")
    # 账号收窄（2026-09-16 深测 P2）：读侧 search/list/delete 都过 scoped_account，
    # 写侧曾原样收 body account_id——admin 可写穿他账号知识命名空间（读写口径分裂）。
    account_id = scoped_account(request, req.account_id)
    # 路径段校验：vault 内相对路径不允许 ..（跨目录挪位；与 LocalMarkdownSource
    # 的 root 逃逸守卫互补——那层只防逃出 vault，不防 vault 内跨账号目录）。
    parts = [p for p in req.path.replace("\\", "/").split("/") if p]
    if any(p == ".." for p in parts):
        raise HTTPException(status_code=400, detail="path must not contain '..'")
    result = await app.state.knowledge.import_document(account_id, req.path, req.content)
    _audit("knowledge.import", subject_type="knowledge", subject_id=req.path or "",
           account_id=account_id, detail={"content_len": len(req.content)})
    return result
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_object_attribution.py tests/test_control_plane.py -q`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/control_plane/main.py tests/test_object_attribution.py
git commit -m "fix(security): scope knowledge import to caller account, reject dotdot paths

P2-10(深测): import 端点原样收 body account_id,admin 可写他账号知识命名空间
(读侧同 admin 被压回——读写口径分裂);path 的 .. 可在 vault 内跨账号挪位,
重启后被正式索引。现 scoped_account 收窄 + '..' 段 400。"
```

### Task 12: personas 跨账号闸（P3）

**Files:**
- Modify: `apps/control-plane/control_plane/main.py:2664-2730`（personas 五端点）
- Test: `tests/test_access_gates.py`（追加）

**Interfaces:**
- Consumes: `deny_cross_account` / `current_identity`。
- Produces: 无新接口。

- [ ] **Step 1: 追加失败测试**

```python
def test_personas_account_scoped_for_admin(monkeypatch):
    client, repo = _make(monkeypatch)
    foreign = repo.create_persona({"account_id": "acc-002", "name": "p2"})
    repo.create_user(username="adm", password_hash=hash_password(PW), role="admin",
                     org_id="org-t", account_id="acc-001")
    h = _peon(client)  # peon 仍 403（管理面不变）
    assert client.get(f"/api/personas/{foreign['id']}", headers=h).status_code == 403
    # admin 登录后跨账号读 → 404（旧版 200）；删除 → 404
    r = client.post("/api/auth/login", json={"username": "adm", "password": PW})
    ah = {"Authorization": "Bearer " + r.json()["token"]}
    assert client.get(f"/api/personas/{foreign['id']}", headers=ah).status_code == 404
    assert client.delete(f"/api/personas/{foreign['id']}", headers=ah).status_code == 404
    # 建人设强制本账号（旧版可建进任意账号）
    r = client.post("/api/personas", json={"account_id": "acc-002", "name": "evil"}, headers=ah)
    assert r.status_code == 200 and r.json()["account_id"] == "acc-001"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_access_gates.py::test_personas_account_scoped_for_admin -x -q`
Expected: FAIL（admin 跨账号 200/200、建进 acc-002）。

- [ ] **Step 3: 实现**

`get_persona` 替换为：

```python
@app.get("/api/personas/{persona_id}")
def get_persona(persona_id: str, request: Request) -> dict:
    require_role(request, "admin", "root")
    persona = deny_cross_account(request, _repo().get_persona(persona_id))
    if not persona:
        raise HTTPException(404, "persona not found")
    return persona
```

`create_persona` / `upsert_persona` 在 `require_role` 后加：

```python
    identity = current_identity(request)
    if identity is not None and identity.role != "root":
        # admin 建人设强制本账号（深测：曾可建进/挪进任意账号）。
        req = req.model_copy(update={"account_id": identity.account_id})
```

`update_persona` 的 `existing = dict(...)` 后加：

```python
    if existing:
        deny_cross_account(request, existing)
    identity = current_identity(request)
    if existing and identity is not None and identity.role != "root":
        # 冻结归属：非 root 不得经 UpdatePersonaRequest.account_id 挪账号。
        req = req.model_copy(update={"account_id": str(existing.get("account_id") or "")})
```

`delete_persona` 的 `existing = _repo().get_persona(persona_id)` 替换为 `existing = deny_cross_account(request, _repo().get_persona(persona_id))`。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_access_gates.py tests/test_persona_auto_pregen.py tests/test_pregen_personas.py -q`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/control_plane/main.py tests/test_access_gates.py
git commit -m "fix(security): scope personas to admin's account, freeze account_id on update

P3(深测): personas 全族 require_role 后无跨账号闸——admin 可读/改/删/建任意
账号人设(update 白名单还收 account_id 可挪档)。by-ID 补 deny_cross_account
(404 口径),建/改对非 root 强制/冻结本账号。"
```

### Task 13: 杂项授权闸批量（P3：fillers hit / insights / setup / roster 认领保护）

**Files:**
- Modify: `apps/control-plane/control_plane/main.py:2636-2640`（hit_filler_entry）、`:2859-2863`（list_insights）、`:3169-3202`（setup 两端点）、`:1547-1583`（roster claim/unclaim）
- Test: `tests/test_access_gates.py`（追加）

**Interfaces:**
- Consumes: 既有闸 helper。
- Produces: 无新接口。

- [ ] **Step 1: 追加失败测试**

```python
def test_misc_gates(monkeypatch):
    client, repo = _make(monkeypatch, permissions="none")
    h = _peon(client)
    # fillers hit 曾完全无闸（匿名/任意身份可刷他账号计数）
    entry = repo.create_filler_entry({"account_id": "acc-001", "lang": "zh", "text": "稍等"})
    r = client.post(f"/api/fillers/{entry['id']}/hit", headers=h)
    assert r.status_code == 403  # permissions=[] → calls/qa 均无,垫话计数归 qa 键
    # insights 曾仅 reports 页面键且无账号维度 → 管理面 only
    assert client.get("/api/insights", headers=h).status_code == 403
    # setup 曾无角色闸 → admin/root only
    assert client.get("/api/setup", headers=h).status_code == 403
    assert client.post("/api/setup/download", headers=h).status_code == 403
    # roster 认领保护：u2 不能释放/抢走 u1 的认领
    repo.create_user(username="op1", password_hash=hash_password(PW), role="user",
                     org_id="org-t", account_id="acc-001")
    entry2 = repo.upsert_roster_entry(account_id="acc-001", call_id="call-r1",
                                      object_id="", channel="whatsapp", number="138",
                                      display_name="", summary="")
    repo.update_roster_entry(entry2["id"], status="claimed", claimed_by="op1")
    r = client.post(f"/api/roster/{entry2['id']}/unclaim", headers=h)
    assert r.status_code == 403  # peon ≠ 认领人 op1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_access_gates.py::test_misc_gates -x -q`
Expected: FAIL（多处 200）。

- [ ] **Step 3: 实现**

3a. `hit_filler_entry`：

```python
@app.post("/api/fillers/{entry_id}/hit")
def hit_filler_entry(entry_id: str, request: Request) -> dict:
    """agent 垫话罐头命中计数(fire-and-forget,幂等无副作用)。

    2026-09-16 深测 P3：旧版完全无闸（对照 qa hit 有 deny_cross_account）——
    经列表全量反查目标条目后走同一 404 口径；qa 键页面闸兜匿名刷计数。
    """
    _gate_page(request, "qa")
    entry = next((e for e in _repo().list_filler_entries("")
                  if str(e.get("id") or "") == entry_id), None)
    deny_cross_account(request, entry)
    _repo().incr_filler_hit(entry_id)
    return {"id": entry_id}
```

3b. `list_insights`（GlobalInsight 无 account 列=全平台蒸馏面，user 即便授 reports 也不该看）：

```python
@app.get("/api/insights")
def list_insights(request: Request) -> list[dict]:
    """全局洞察（结算蒸馏产出）。跨账号内容（GlobalInsight 无 account 维度，
    深测 P2）→ 收管理面；账号维度化留待 schema 加列（见计划尾部延后项）。"""
    require_role(request, "admin", "root")
    return _repo().list_global_insights(kind="insight")
```

3c. setup 两端点加 `request: Request` 参数与 `require_role(request, "admin", "root")` 首行；`setup_download` 成功分支前加：

```python
    _audit("setup.download", subject_type="global_settings", subject_id="models")
```

3d. `roster_claim`：在 `claimed_by = ident.username if ident else req.claimed_by` 行**之前**插入占用校验（该函数已有 `ident = current_identity(request)`）：

```python
    holder = str(entry.get("claimed_by") or "")
    if holder and ident is not None and ident.role == "user" and holder != ident.username:
        # 认领保护（深测 P3）：话务员只能认领无人/自己持有的条目；403 而非 404
        # ——认领人本就在同账号名册池可见，无存在性泄露。主管/机器通道不受限。
        raise HTTPException(403, "claimed by another operator")
```

（`roster_claim` 现有 `deny_cross_account` 行需顺手把返回值接住：`entry = deny_cross_account(request, _repo().get_roster_entry(entry_id))`，供上面取 `claimed_by`。）

3e. `roster_unclaim`：`deny_cross_account` 行改为接住返回值，并在 update 前插同款校验（该函数需新加 `ident = current_identity(request)`）：

```python
    _gate_page(request, "roster")
    entry = deny_cross_account(request, _repo().get_roster_entry(entry_id))
    if not entry:
        raise HTTPException(404, "roster entry not found")
    ident = current_identity(request)
    holder = str(entry.get("claimed_by") or "")
    if holder and ident is not None and ident.role == "user" and holder != ident.username:
        raise HTTPException(403, "claimed by another operator")
    entry = _repo().update_roster_entry(entry_id, status="unclaimed", claimed_by="", claimed_at="")
    if not entry:
        raise HTTPException(404, "roster entry not found")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_access_gates.py tests/test_roster_api.py tests/test_roster_channel.py tests/test_permissions.py -q`
Expected: 全 PASS（roster 既有测试无人抢认领场景，零破坏）。

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/control_plane/main.py tests/test_access_gates.py
git commit -m "fix(security): gate filler hits/insights/setup, protect roster claims

P3(深测): fillers hit 全无闸可刷他账号计数;insights 无账号维度且 reports 键即
可达(user 可读全平台蒸馏);setup 端点任意 user 可反复拉子进程;roster unclaim/
claim 无占用保护(last-write-wins 抢认领)。逐项收闸。"
```

### Task 14: 资源钳制、审计补点、web_logs 对齐、CORS env（P3 批量）

**Files:**
- Modify: `apps/control-plane/control_plane/main.py`（list_audit / import_objects / supervisor_join / upsert_persona / web_logs / CORS）
- Modify: `apps/web/lib/weblog.ts`
- Test: `tests/test_access_gates.py`（追加）

**Interfaces:**
- Consumes: `collections.deque`（main.py imports 扩充）、web `authHeaders()`（apps/web/lib/api.ts:33 既有）。
- Produces: 无新接口。

- [ ] **Step 1: 追加测试**

```python
def test_audit_limit_clamped(monkeypatch):
    client, repo = _make(monkeypatch)
    repo.create_user(username="rooty", password_hash=hash_password(PW), role="root",
                     org_id="", account_id="")
    r = client.post("/api/auth/login", json={"username": "rooty", "password": PW})
    h = {"Authorization": "Bearer " + r.json()["token"]}
    # 巨值 limit 不再透传（SQL LIMIT 巨值=全表进内存）；钳制后正常返回
    assert client.get("/api/audit", params={"limit": 999999999}, headers=h).status_code == 200
```

- [ ] **Step 2: 实现 main.py**

2a. imports：`from collections import defaultdict, deque`。

2b. `list_audit`（3159 行）在 `require_role` 后加：

```python
    limit = max(1, min(int(limit or 200), 1000))  # 巨值 limit 曾直透 SQL（全表进内存）
```

2c. `import_objects` 在 `require_role` 后加行数上限 + 审计补点（批量 PII 入库曾零审计）：

```python
    if len(rows) > 500:
        raise HTTPException(400, "单次导入上限 500 行")
```

成功 return 前加：

```python
    _audit("object.import", subject_type="object", account_id=account_id,
           detail={"rows": len(created)})
```

2d. `supervisor_join` 在 return 前加审计（对照 listen 有 start/stop 双审计，join 曾无痕）：

```python
    _audit("supervisor.join", subject_type="call", subject_id=call_id,
           account_id=str(call.get("account_id") or ""), call_id=call_id)
```

2e. `upsert_persona` 在 create 后加审计（对照 POST /api/personas 有 persona.create）：

```python
    _audit("persona.upsert", subject_type="persona", subject_id=persona.get("id", ""),
           account_id=req.account_id, detail={"name": req.name})
```

2f. `web_logs` 限速：模块级 `_weblog_times: deque = deque(maxlen=600)`（`_redispatch_locks` 旁），handler `event` 校验前加：

```python
    import time as _t

    now = _t.time()
    if len(_weblog_times) >= 600 and now - _weblog_times[0] < 60:
        return {"ok": False, "reason": "rate_limited"}  # 600 行/分钟上限,防刷盘
    _weblog_times.append(now)
```

2g. CORS（98-103 行）：

```python
_cors_origins = [o.strip() for o in os.environ.get("BOK_CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],  # 云端部署设 BOK_CORS_ORIGINS 收敛到管理台 origin
    allow_methods=["*"],
    allow_headers=["*"],
)
```

- [ ] **Step 3: 实现 weblog.ts**

import 行改为 `import { apiBase, authHeaders } from "@/lib/api";`，fetch headers 改为：

```ts
      headers: { "Content-Type": "application/json", ...authHeaders() },
```

（auth-on 云端下 web 诊断通道曾全量 401 静默丢弃——端点本身在 /api/* 后已被 identity_gate 保护，缺的是客户端头。）

- [ ] **Step 4: 验证**

Run: `.venv312/bin/python -m pytest tests/test_access_gates.py -q && .venv312/bin/python -m compileall -q apps packages services tools scripts && cd apps/web && npx tsc --noEmit && npm run build`
Expected: 全绿。

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/control_plane/main.py apps/web/lib/weblog.ts tests/test_access_gates.py
git commit -m "fix(security): clamp resource params, backfill audit events, align weblog auth, CORS via env

P3(深测): audit limit 巨值直透 SQL;objects/import 批量 PII 入库零审计;
supervisor/join 与 personas upsert 无痕;web_logs 无限速;weblog.ts 不带鉴权头
(auth-on 下诊断通道全 401);CORS 恒 * 改 BOK_CORS_ORIGINS 可收敛。"
```

---

## 任务组 D：settle 异步化

### Task 15: Summarizer 移出事件循环（P1-4）

**Files:**
- Modify: `apps/control-plane/control_plane/summarize.py:29`（timeout 15）
- Modify: `apps/control-plane/control_plane/main.py:2349-2358`（settle 内调用改 to_thread、去二次重试）
- Test: 既有 `tests/test_summarize.py` 回归 + 新增行为测试

**Interfaces:**
- Consumes: `Summarizer().build(turns, call, settings) -> dict`（签名不变）。
- Produces: 无新接口（调用方式变为 `await asyncio.to_thread(...)`）。

- [ ] **Step 1: 追加失败测试（tests/test_summarize.py 末尾追加）**

```python
def test_settle_uses_thread_offload_not_inline_llm_call(monkeypatch):
    """P1-4：settle 不得在事件循环里同步 httpx 调用——黑洞 LLM 曾致 /health
    59.4s 全局停摆。静态锁死调用形态：settle 源码必须经 asyncio.to_thread。"""
    import inspect

    from control_plane import main as cp_main

    src = inspect.getsource(cp_main.settle)
    assert "asyncio.to_thread" in src
    assert src.count("Summarizer().build") == 1  # 二次同步重试一并移除（重试=双倍停摆）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_summarize.py::test_settle_uses_thread_offload_not_inline_llm_call -x -q`
Expected: FAIL。

- [ ] **Step 3: 实现**

3a. summarize.py `__init__` 默认 timeout `60.0` → `15.0`。

3b. main.py settle 内（2349-2358 行）替换为：

```python
    try:
        from .summarize import Summarizer

        settings = _repo().get_settings()
        # P1-4（2026-09-16 深测）：Summarizer.build 是同步 httpx 调用（原 timeout
        # 60s×2 次重试），直接跑在 async 路由=事件循环整体冻结——黑洞 LLM 实测
        # /health 59.4s 停摆（turns 上报/心跳/token 全部停摆）。挪工作线程+单次
        # 尝试；蒸馏失败由审计 settle.distill_empty 可观测。
        summ = await asyncio.to_thread(Summarizer().build, turns, call, settings)
        if not (summ.get("summary") or "").strip() and turns:
            _audit("settle.distill_empty", subject_type="call", subject_id=call_id,
                   detail={"turns": len(turns)})
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_summarize.py tests/test_control_plane.py tests/test_qa_summary.py -q && .venv312/bin/python -m compileall -q apps`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add apps/control-plane/control_plane/summarize.py apps/control-plane/control_plane/main.py tests/test_summarize.py
git commit -m "fix(security): offload sync LLM summarizer to worker thread in settle

P1-4(深测): settle 在 async 路由内同步 httpx 调 LLM(timeout 60s x2),黑洞配置
实测冻结整个事件循环 59.4s——settle 是每通挂断必经链路。改 asyncio.to_thread
+单次尝试+timeout 15s,LLM 抖动不再全局停摆。"
```

---

## 任务组 E：节点鉴权

### Task 16: 注册指纹必填 + 配额 TOCTOU 收口（P1-2 / P1-3）

**Files:**
- Modify: `apps/control-plane/control_plane/nodes_store.py`（__init__ 锁、register_licensed、register IntegrityError 转换）
- Modify: `apps/control-plane/control_plane/main.py:2015-2047`（register_node）、`deps.py:196-198` 之后（部分唯一索引）
- Test: `tests/test_nodes_hardening.py`（新建）

**Interfaces:**
- Consumes: `LicenseError(status_code, reason)`（nodes_store 既有）。
- Produces: `NodeStore.register_licensed(*, license_key: str, fingerprint: str, name: str = "", platform: str = "", org_id: str = "", version: str = "") -> tuple[dict, str, str]`（返回 license 行、node_id、明文 token；失败抛 LicenseError）。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_nodes_hardening.py`：

```python
"""2026-09-16 深测修复回归：节点 license 配额 TOCTOU（P1-2）与指纹强制（P1-3）。"""
from __future__ import annotations

import os
import threading

os.environ.setdefault("DATABASE_URL", "")

import pytest

from control_plane.nodes_store import LicenseError, NodeStore


def _store() -> NodeStore:
    s = NodeStore(None)  # 内存双模
    return s


def test_register_licensed_basic_and_quota():
    s = _store()
    lic = s.create_license(max_nodes=1, note="t")
    row, node_id, token = s.register_licensed(
        license_key=lic["license_key"], fingerprint="f" * 64, name="n1")
    assert node_id and token
    # 配额满：不同指纹 → 403
    with pytest.raises(LicenseError) as ei:
        s.register_licensed(license_key=lic["license_key"], fingerprint="a" * 64)
    assert ei.value.status_code == 403
    # 同指纹幂等复用（换 token）不吃配额
    row2, node_id2, token2 = s.register_licensed(
        license_key=lic["license_key"], fingerprint="f" * 64)
    assert node_id2 == node_id and token2 != token


def test_quota_race_closed_by_lock():
    """P1-2：max_nodes=1 并发 12 注册旧版 10 个全过闸；同锁收口后只中 1。"""
    s = _store()
    lic = s.create_license(max_nodes=1, note="race")
    ok: list[str] = []
    barrier = threading.Barrier(12)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            _, _, _ = s.register_licensed(
                license_key=lic["license_key"], fingerprint=f"{i:064x}")
            ok.append(str(i))
        except LicenseError:
            pass

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == 1, ok
    assert s._count_nodes(lic["license_id"]) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_nodes_hardening.py -x -q`
Expected: FAIL（`register_licensed` 不存在）。

- [ ] **Step 3: 实现 nodes_store.py**

3a. imports 加 `import threading`；`__init__` 末尾加 `self._register_lock = threading.Lock()`。

3b. `validate_license_for_register` 后加：

```python
    def register_licensed(self, *, license_key: str, fingerprint: str, name: str = "",
                          platform: str = "", org_id: str = "", version: str = ""
                          ) -> tuple[dict, str, str]:
        """加固模式注册（2026-09-16 深测 P1）：license 三查 + 建行同锁收口配额
        TOCTOU——旧版 validate 与 register 分属两事务，max_nodes=1 并发 12 实测
        10 个全过闸落库。进程内由 _register_lock 串行；多实例部署由 deps 幂等
        段的 (license_id, fingerprint) 部分唯一索引兜底（撞索 → 403）。
        返回 (license 行, node_id, 明文 token)；失败抛 LicenseError。"""
        with self._register_lock:
            lic = self.find_license(license_key)
            if lic is None:
                raise LicenseError(401, "unknown or missing license key")
            if lic["status"] != "active":
                raise LicenseError(401, "license revoked")
            reuse_id = self.find_node_by_fingerprint(lic["license_id"], fingerprint)
            if reuse_id is None and lic["nodes_used"] >= lic["max_nodes"]:
                raise LicenseError(
                    403, f"license quota exhausted ({lic['nodes_used']}/{lic['max_nodes']})")
            try:
                node_id, token = self.register(
                    name=name, platform=platform, org_id=org_id, version=version,
                    license_id=lic["license_id"], fingerprint=fingerprint)
            except Exception as exc:
                # 多实例并发撞 (license_id, fingerprint) 唯一索引 → 按配额语义 403。
                if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                    raise LicenseError(403, "duplicate node registration") from exc
                raise
            return lic, node_id, token
```

- [ ] **Step 4: 实现 main.py register_node 加固分支**

`register_node` 的加固段（2026-2033 行区域）替换为：

```python
    store = _node_store()
    license_id = ""
    if node_license_required():
        # 指纹必填（2026-09-16 深测 P1）：空指纹曾以 " " 兜底查询=永不命中复用，
        # 任何机器心跳全过、克隆检测结构性永不触发——1 配额=无限台机器。
        fingerprint = (req.fingerprint or "").strip()
        if not fingerprint:
            raise HTTPException(400, "fingerprint is required in hardened mode")
        try:
            lic, node_id, token = store.register_licensed(
                license_key=(req.license_key or "").strip(), fingerprint=fingerprint,
                name=req.name, platform=req.platform, org_id=req.org_id,
                version=req.version)
        except LicenseError as exc:
            raise HTTPException(exc.status_code, exc.reason) from exc
        _audit("node.registered", subject_type="node", subject_id=node_id,
               account_id="", detail={
                   "name": req.name, "platform": req.platform, "version": req.version,
                   "license_id": lic["license_id"],
                   "fingerprint_prefix": fingerprint[:12]})
        return {"node_id": node_id, "node_token": token,
                "heartbeat_interval_s": HEARTBEAT_INTERVAL_S}
    elif (req.license_key or "").strip():
        # 非加固模式也尊重显式 license（登记归属，不强制）。
        lic = store.find_license((req.license_key or "").strip())
        license_id = lic["license_id"] if lic else ""
    node_id, token = store.register(
        name=req.name, platform=req.platform, org_id=req.org_id, version=req.version,
        license_id=license_id, fingerprint=(req.fingerprint or "").strip(),
    )
    _audit("node.registered", subject_type="node", subject_id=node_id,
           account_id="", detail={
               "name": req.name, "platform": req.platform, "version": req.version,
               "license_id": license_id,
               "fingerprint_prefix": (req.fingerprint or "")[:12]})
    return {"node_id": node_id, "node_token": token, "heartbeat_interval_s": HEARTBEAT_INTERVAL_S}
```

- [ ] **Step 5: deps.py 幂等段加部分唯一索引（多实例兜底）**

`deps.py` 幂等段（`_ensure_column(conn, "nodes", "fingerprint", ...)` 行后）追加：

```python
                # 节点鉴权(P1,深测): (license_id, fingerprint) 部分唯一索引——多实例
                # 部署下配额竞态的库级兜底(进程内由 NodeStore.register_licensed 的
                # 锁收口)。只约束 license 绑定行:开放模式存量空值行不受影响。
                # 部分索引 WHERE 语法 SQLite/Postgres 双支持。
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_nodes_license_fingerprint "
                    "ON nodes (license_id, fingerprint) WHERE license_id <> ''"
                ))
```

- [ ] **Step 6: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_nodes_hardening.py tests/test_nodes_registry.py -q && .venv312/bin/python -m pytest tests/test_db_portability.py -q && .venv312/bin/python scripts/dump_postgres_ddl.py >/dev/null && git diff --exit-code scripts/.p0_supabase_schema.sql || echo "SCHEMA-DIFF: 重新生成引导件后一并提交"`
Expected: 测试全绿；若引导件 diff 出现新索引 DDL，`git add scripts/.p0_supabase_schema.sql` 一并提交（AGENTS 规约：改表后重跑 dump）。

- [ ] **Step 7: Commit**

```bash
git add apps/control-plane/control_plane/nodes_store.py apps/control-plane/control_plane/main.py apps/control-plane/control_plane/deps.py tests/test_nodes_hardening.py scripts/.p0_supabase_schema.sql
git commit -m "fix(security): close license quota TOCTOU, require fingerprint in hardened mode

P1-2(深测): validate 与 register 分属两事务,max_nodes=1 并发 12 实测 10 个全过闸。
register_licensed 同锁收口+deps 部分唯一索引兜底多实例。
P1-3(深测): 空指纹注册曾以 ' ' 兜底=克隆检测结构性永不触发,现加固模式 400。"
```

### Task 17: 心跳指纹协议强制 + 开放期 token 拒收 + 节点级吊销（P2-7）

**Files:**
- Modify: `apps/control-plane/control_plane/nodes_store.py:301-354`（heartbeat）、`_revoke_node` 公有化
- Modify: `apps/control-plane/control_plane/main.py:2050-2066`（node_heartbeat）、新增 revoke 端点
- Test: `tests/test_nodes_hardening.py`（追加）

**Interfaces:**
- Consumes: Task 16 的 NodeStore。
- Produces: `heartbeat(token, metrics=None, fingerprint="", require_license=False) -> tuple[bool, str]`（新 kwarg，默认 False 零破坏）；`NodeStore.revoke_node(node_id: str) -> bool`；`POST /api/nodes/{node_id}/revoke`（root）。

- [ ] **Step 1: 追加失败测试**

```python
def test_heartbeat_requires_fingerprint_when_bound():
    s = _store()
    lic = s.create_license(max_nodes=2, note="fp")
    _, node_id, token = s.register_licensed(
        license_key=lic["license_key"], fingerprint="f" * 64)
    # 绑定了指纹的节点，心跳缺指纹 = 不合作客户端 → 按指纹不符自动吊销
    ok, reason = s.heartbeat(token, {}, fingerprint="")
    assert not ok and reason == "fingerprint_mismatch"
    rows = s.list_nodes()
    assert all(r["status"] == "revoked" for r in rows if r["node_id"] == node_id)


def test_heartbeat_rejects_open_mode_token_in_hardened_mode(monkeypatch):
    s = _store()
    _, _, token = s.register(name="legacy", platform="t", license_id="", fingerprint="")
    ok, reason = s.heartbeat(token, {}, require_license=True)
    assert not ok and reason == "license_required"
    # 未加固模式照常
    ok, reason = s.heartbeat(token, {})
    assert ok and reason == ""


def test_node_revoke_endpoint_semantics():
    s = _store()
    lic = s.create_license(max_nodes=1, note="rv")
    _, node_id, token = s.register_licensed(
        license_key=lic["license_key"], fingerprint="f" * 64)
    assert s.revoke_node(node_id) is True
    assert s.revoke_node(node_id) is False  # 已吊销
    ok, reason = s.heartbeat(token, {})
    assert not ok
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_nodes_hardening.py -k "heartbeat or revoke" -x -q`
Expected: FAIL。

- [ ] **Step 3: 实现 nodes_store.py**

3a. `_revoke_node` 重命名为公有 `revoke_node`（返回 bool），heartbeat 内两处调用同步改名：

```python
    def revoke_node(self, node_id: str) -> bool:
        """吊销单节点；行存在且未吊销返回 True（root 面 /api/nodes/{id}/revoke 用）。"""
        if self._session_factory is None:
            row = self._rows.get(node_id)
            if row is None or row["revoked"]:
                return False
            row["revoked"] = True
            return True
        from sqlalchemy import update

        from bok_voice_business_db import models

        with self._session_factory() as session:
            result = session.execute(
                update(models.Node).where(models.Node.id == node_id,
                                           models.Node.status != "revoked")
                .values(status="revoked"))
            session.commit()
            return result.rowcount > 0
```

3b. `heartbeat` 签名与校验段改为：

```python
    def heartbeat(self, token: str, metrics: dict | None = None,
                  fingerprint: str = "", require_license: bool = False) -> tuple[bool, str]:
        """心跳四验：token / license active /（加固模式）license 绑定 / 指纹。

        require_license=True（CP 加固模式）：开放期注册的 license_id="" 存量 token
        是不可吊销的长命凭证（吊销端点够不着它）——拒绝但不自动吊销，留现场供
        root 处置/迁移（2026-09-16 深测 P2）。注册绑定了指纹的节点心跳缺指纹=
        不合作客户端绕过克隆检测——按指纹不符自动吊销（协议强制）。
        """
        token_hash = _hash(token)
        node = self._get_node_for_token(token_hash)
        if node is None:
            return False, "unknown_token"
        if node.get("license_id"):
            lic = None
            if self._session_factory is None:
                lic = self._licenses.get(node["license_id"])
            else:
                from bok_voice_business_db import models

                with self._session_factory() as session:
                    row = session.get(models.NodeLicense, node["license_id"])
                    lic = {"status": row.status} if row else None
            if lic is None or lic["status"] != "active":
                self.revoke_node(node["node_id"])
                return False, "license_revoked"
        if node["revoked"] or node.get("status") == "revoked":
            return False, "revoked"
        if require_license and not node.get("license_id"):
            return False, "license_required"
        if node.get("fingerprint") and not fingerprint:
            # 协议强制（深测 P2）：指纹检测是「客户端自愿」时对不合作实现无效。
            self.revoke_node(node["node_id"])
            return False, "fingerprint_mismatch"
        if (node.get("fingerprint") and fingerprint
                and fingerprint != node["fingerprint"]):
            self.revoke_node(node["node_id"])
            return False, "fingerprint_mismatch"
        # ↓ 此处原样保留现有实现的末段（last_seen_at/metrics_json 更新，双模两分支），
        #   仅把其中的 _revoke_node 调用改为 revoke_node；函数最终 return True, "" 不变。
```

（`heartbeat` 内原有两处 `self._revoke_node(...)` 全部改调 `self.revoke_node(...)`；函数签名行同步替换为上面带 `require_license` 的版本。）

- [ ] **Step 4: 实现 main.py**

4a. `node_heartbeat` 传加固标志并扩 detail 表：

```python
    ok, reason = (False, "unknown_token")
    if token:
        ok, reason = _node_store().heartbeat(
            token, req.metrics, fingerprint=(req.fingerprint or "").strip(),
            require_license=node_license_required())
    if not ok:
        if reason in ("fingerprint_mismatch", "license_revoked"):
            _audit(f"node.denied.{reason}", subject_type="node", subject_id="",
                   account_id="", detail={"reason": reason})
        detail = {"fingerprint_mismatch": "fingerprint mismatch (clone/relocated?)",
                  "license_revoked": "license revoked", "revoked": "node revoked",
                  "license_required": "node not licensed (hardened mode)"}.get(reason)
        raise HTTPException(401, detail or "unknown node token")
```

4b. `revoke_node_license` 端点后加：

```python
@app.post("/api/nodes/{node_id}/revoke")
def revoke_node(node_id: str, request: Request) -> dict:
    """吊销单个节点（root 专属）：token 即刻失效。开放期存量 token（无 license）
    此前无任何吊销手段（2026-09-16 深测 P2）。"""
    require_role(request, "root")
    if not _node_store().revoke_node(node_id):
        raise HTTPException(404, "node not found")
    _audit("node.revoked", subject_type="node", subject_id=node_id)
    return {"node_id": node_id, "revoked": True}
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_nodes_hardening.py tests/test_nodes_registry.py -q && .venv312/bin/python scripts/node_handshake_smoke.py >/dev/null 2>&1 && echo SMOKE-OK || echo SMOKE-FAIL（需本地起 CP，见脚本头注释；CI node-handshake.yml 兜底）`
Expected: 单测全绿；smoke 由 CI 兜底（本地可选）。

- [ ] **Step 6: Commit**

```bash
git add apps/control-plane/control_plane/nodes_store.py apps/control-plane/control_plane/main.py tests/test_nodes_hardening.py
git commit -m "fix(security): enforce heartbeat fingerprint protocol, reject unlicensed tokens, add node revoke

P2-7(深测): 克隆检测是客户端自愿(心跳省略 fingerprint 即绕过);开放期注册的
license_id='' token 在加固模式永久有效且无吊销手段。心跳对已绑定指纹节点强制
带指纹(缺=按 mismatch 吊销);加固模式拒收无 license 心跳;新增 root 专属
POST /api/nodes/{id}/revoke。"
```

### Task 18: node-agent 自愈 + 凭据落盘原子 0600 + license 不走 argv（P2-7 + P3）

**Files:**
- Modify: `tools/node_agent.py`（heartbeat_once / ensure_token / heartbeat_tick+heartbeat_loop / main argparse）
- Modify: `scripts/install-node.sh:131-143`
- Test: `tests/test_node_agent_selfheal.py`（新建）

**Interfaces:**
- Consumes: CP 侧 register 幂等复用（Task 16）。
- Produces: `node_agent.heartbeat_tick(cfg, missed, *, license_key="", state_file=None) -> int`；`--license-key` 支持 `BOK_LICENSE_KEY` env 兜底。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_node_agent_selfheal.py`：

```python
"""2026-09-16 深测修复回归：node-agent token 失效自愈与状态文件权限。"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import node_agent


def _cfg(tmp_path):
    return node_agent.NodeConfig(cp_url="http://cp.test", node_token="dead-token",
                                 heartbeat_interval_s=60, fingerprint="f" * 64)


def test_heartbeat_tick_reregisters_on_unknown_token(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    calls = []

    def fake_once(c, metrics=None):
        calls.append(c.node_token)
        return (False, {"detail": "unknown node token"}) if c.node_token == "dead-token" \
            else (True, {})

    def fake_ensure(cp_url, license_key, fingerprint, state_file):
        cfg.node_token = "fresh-token"
        return "fresh-token"

    monkeypatch.setattr(node_agent, "heartbeat_once", fake_once)
    monkeypatch.setattr(node_agent, "ensure_token", fake_ensure)
    missed = node_agent.heartbeat_tick(cfg, 2, license_key="bokn_k",
                                       state_file=tmp_path / "state.json")
    assert missed == 0 and cfg.node_token == "fresh-token"
    assert calls == ["dead-token", "fresh-token"]


def test_heartbeat_tick_counts_missed_without_license_flow(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(node_agent, "heartbeat_once",
                        lambda c, metrics=None: (False, {"detail": "network"}))
    assert node_agent.heartbeat_tick(cfg, 1) == 2  # 无 license 流不自愈


def test_ensure_token_writes_state_file_0600_from_creation(monkeypatch, tmp_path):
    state = tmp_path / "sub" / "node-state.json"
    monkeypatch.setattr(node_agent, "register_once",
                        lambda *a, **k: ("node-1", "tok-1"))
    monkeypatch.setattr(node_agent, "_post_json",
                        lambda *a, **k: (401, {}))  # 缓存探测直接 401
    token = node_agent.ensure_token("http://cp.test", "bokn_k", "f" * 64, state)
    assert token == "tok-1"
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_node_agent_selfheal.py -x -q`
Expected: FAIL（heartbeat_tick 不存在 / state 文件 0644）。

- [ ] **Step 3: 实现 node_agent.py**

3a. `heartbeat_once` 改用 `_post_json`（返回 body 供自愈判定；install-node 探针打印行为不变）：

```python
def heartbeat_once(cfg: NodeConfig, metrics: dict | None = None) -> tuple[bool, dict]:
    code, body = _post_json(
        f"{cfg.cp_url.rstrip('/')}/api/nodes/heartbeat",
        {"metrics": metrics or {}, "fingerprint": cfg.fingerprint},
        headers={"Authorization": f"Bearer {cfg.node_token}"}, timeout=10)
    return code == 200, body
```

（异常吞掉语义保留：`_post_json` 只捕 HTTPError，网络异常仍会抛——外包一层：

```python
    try:
        code, body = _post_json(...)
    except Exception as exc:  # noqa: BLE001 - 失联不抛，计数交给调用方
        print(f"[node-agent] heartbeat failed: {exc!r}", flush=True)
        return False, {}
```

）

3b. `ensure_token` 状态文件写法（133-139 行）替换为原子 0600（消灭 0644 窗口）：

```python
    node_id, token = register_once(cp_url, license_key, fingerprint)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    # 先 0600 建档再写（write_text+chmod 有 0644 窗口）：token=本机凭据。
    import os as _os

    fd = _os.open(str(state_file), _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600)
    with _os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({"node_id": node_id, "node_token": token}, ensure_ascii=False))
```

3c. `heartbeat_loop` 拆 tick + 自愈（被顶掉/吊销后不再瘫痪到人工重启）：

```python
def heartbeat_tick(cfg: NodeConfig, missed: int, *, license_key: str = "",
                   state_file: Path | None = None) -> int:
    """单次心跳；token 失效且带 license 流 → 同 (license,fingerprint) 幂等重注册
    自愈（2026-09-16 深测 P2：单请求顶掉真机后旧版 REFUSE_JOBS 挂到人工重启）。
    被克隆顶掉的场景双方互踢，官方指纹靠 60s 周期最终抢回——每轮至多一次重注册，
    无风暴。返回新 missed 计数。"""
    ok, body = heartbeat_once(cfg, metrics={"missed": missed})
    if not ok and license_key and state_file is not None:
        detail = str((body or {}).get("detail") or "")
        if "unknown node token" in detail or "revoked" in detail or "license_required" in detail:
            try:
                cfg.node_token = ensure_token(cfg.cp_url, license_key, cfg.fingerprint, state_file)
                return 0
            except SystemExit as exc:
                print(f"[node-agent] re-register failed: {exc}", flush=True)
    return 0 if ok else missed + 1


def heartbeat_loop(cfg: NodeConfig, stop: threading.Event, *,
                   license_key: str = "", state_file: Path | None = None) -> None:
    missed = 0
    while not stop.wait(cfg.heartbeat_interval_s):
        missed = heartbeat_tick(cfg, missed, license_key=license_key, state_file=state_file)
        if should_refuse_jobs(missed, cfg.max_missed):
            print(f"[node-agent] missed={missed} >= {cfg.max_missed}: REFUSE_JOBS (L1)", flush=True)
```

3d. `main()`：argparse `--license-key` default 改 `os.environ.get("BOK_LICENSE_KEY", "")`（help 注明 env 兜底，键不再必须走 argv——`ps` 可见）；license 流调用改 `heartbeat_loop(cfg, stop, license_key=args.license_key, state_file=state_file)`（state_file 提升到 license 分支外定义：`state_file = None`，license 流内赋值）。

- [ ] **Step 4: 实现 install-node.sh（license 不进 argv）**

134-143 行替换为：

```bash
  local agent_log; agent_log="$(mktemp "${TMPDIR:-/tmp}/bok-node-agent.XXXXXX")"
  local agent_args=(--cp-url "$CP_URL" --livekit-url "$LIVEKIT_URL" \
    --ui-dir "$REPO_ROOT/apps/web/out" --heartbeat-only --interval 1)
  if [[ -n "$NODE_TOKEN" ]]; then
    "$PY" "$REPO_ROOT/tools/node_agent.py" "${agent_args[@]}" \
      --node-token "$NODE_TOKEN" >"$agent_log" 2>&1 &
  else
    # license 经 env 传递（2026-09-16 深测 P3）：argv 在 ps/shell history 可见。
    BOK_LICENSE_KEY="$LICENSE_KEY" "$PY" "$REPO_ROOT/tools/node_agent.py" \
      "${agent_args[@]}" >"$agent_log" 2>&1 &
  fi
  AGENT_PID=$!
```

- [ ] **Step 5: 验证**

Run: `.venv312/bin/python -m pytest tests/test_node_agent_selfheal.py -q && .venv312/bin/python -m compileall -q tools scripts && bash -n scripts/install-node.sh`
Expected: 全绿。

- [ ] **Step 6: Commit**

```bash
git add tools/node_agent.py scripts/install-node.sh tests/test_node_agent_selfheal.py
git commit -m "fix(security): node-agent token self-heal, atomic 0600 state file, license via env

P2-7(深测): license_key+指纹单请求顶掉真机后 node-agent 只累 missed 到
REFUSE_JOBS,不自动重注册——瘫痪到人工重启。heartbeat_tick 对 unknown_token/
revoked/license_required 幂等重注册自愈。状态文件先 0600 建档再写(消灭 0644
窗口);license key 改 env 传递(argv 在 ps/history 可见)。"
```

---

## 任务组 F：agent 机器通道

### Task 19: ControlPlaneClient 自动携带 BOK_CP_TOKEN（P2-8）

**Files:**
- Modify: `apps/agent/agent_runtime/control_plane.py:1-14`
- Test: `tests/test_agent_cp_token.py`（新建）

**Interfaces:**
- Consumes: env `BOK_CP_TOKEN`。
- Produces: 客户端全部请求默认带 `Authorization: Bearer <BOK_CP_TOKEN>`（未设 env 时零变化）。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_agent_cp_token.py`：

```python
"""2026-09-16 深测 P2-8：agent 端 ControlPlaneClient 从未携带 BOK_CP_TOKEN，
auth-on 全栈 turns/QA/设置上报全 401（文档契约 aspirational，代码 0 处引用）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))


def test_cp_client_carries_machine_token(monkeypatch):
    monkeypatch.setenv("BOK_CP_TOKEN", "mach-token-123")
    from agent_runtime.control_plane import ControlPlaneClient

    c = ControlPlaneClient("http://127.0.0.1:8000", call_id="call-x")
    assert c._client.headers.get("authorization") == "Bearer mach-token-123"


def test_cp_client_without_token_unchanged(monkeypatch):
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    from agent_runtime.control_plane import ControlPlaneClient

    c = ControlPlaneClient("http://127.0.0.1:8000")
    assert not c._client.headers.get("authorization")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_agent_cp_token.py -x -q`
Expected: FAIL（无 Authorization 头）。

- [ ] **Step 3: 实现**

`control_plane.py` imports 加 `import os`；`__init__` 替换为：

```python
    def __init__(self, base_url: str, call_id: str = ""):
        self.base_url = base_url.rstrip("/")
        # 会话级 correlation:全部请求带 X-Call-ID → CP 审计行的 call_id 列
        # 自动填充(此前恒空,web 按 callId 过滤审计查不到)。
        headers = {"X-Call-ID": call_id} if call_id else {}
        # 机器通道（2026-09-16 深测 P2-8）：CP 设 BOK_CP_TOKEN 时全部请求自动
        # 携带——auth-on 下 turns/QA/垫话/设置上报不再 401（此前 env 无任何代码
        # 读取，文档「agent env 必须带同值」是 aspirational）。未设=零变化。
        cp_token = (os.environ.get("BOK_CP_TOKEN") or "").strip()
        if cp_token:
            headers["Authorization"] = f"Bearer {cp_token}"
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=15, headers=headers)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv312/bin/python -m pytest tests/test_agent_cp_token.py -q && .venv312/bin/python -m compileall -q apps`
Expected: 全绿。

- [ ] **Step 5: Commit**

```bash
git add apps/agent/agent_runtime/control_plane.py tests/test_agent_cp_token.py
git commit -m "fix(security): agent ControlPlaneClient carries BOK_CP_TOKEN machine channel

P2-8(深测): apps/agent 全目录 0 处引用 BOK_CP_TOKEN,auth-on 全栈下 turns/QA/
设置上报全 401——把部署逼回 auth-off 的安全压力。现客户端默认注入
Authorization 头,未设 env 零变化。"
```

---

## 任务组 G：桌面壳与前端

### Task 20: Tauri 配置加固（P2-9）

**Files:**
- Modify: `desktop/src-tauri/tauri.conf.json`
- Modify: `desktop/src-tauri/capabilities/default.json`

**Interfaces:**
- Consumes: 前端只用 `window.__TAURI_INTERNALS__.invoke`（apps/web/lib/tauri.ts:26、desktop/src/bridge.ts:16——`withGlobalTauri` 的 `window.__TAURI__` 全仓 0 引用，已核实）。
- Produces: 非 null CSP；shell 插件能力清零。

- [ ] **Step 1: 改 tauri.conf.json**

`"withGlobalTauri": true` → `"withGlobalTauri": false`；`"security"` 段改为：

```json
    "security": {
      "csp": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob: data:; font-src 'self' data:; connect-src 'self' ipc: http://ipc.localhost http://127.0.0.1:* ws://127.0.0.1:* http://localhost:* ws://localhost:*"
    }
```

（Next 静态导出有内联 hydration 脚本 → script-src 需 `unsafe-inline` 起步；核心收益是 `connect-src` 白名单——XSS 若未来出现，token 外带目标被 CSP 拦住。`ipc:`/`http://ipc.localhost` 是 Tauri v2 IPC 必需。）

删除文件尾部整个 `"plugins": { "shell": { "open": true } }` 块。

- [ ] **Step 2: 改 capabilities/default.json**

`permissions` 数组删去 `"shell:allow-open"` 与 `"shell:allow-execute"`（死配置：`lib.rs` 未注册 tauri-plugin-shell，前端 0 调用——但任何人注册插件一行即激活，叠加 csp:null 是 XSS→进程执行的预埋升级链）：

```json
{
  "$schema": "../gen/schemas/desktop-schema.json",
  "identifier": "default",
  "description": "Default capability for the main window",
  "windows": ["main"],
  "permissions": [
    "core:default",
    "core:event:default"
  ]
}
```

- [ ] **Step 3: 验证**

Run: `cd desktop/src-tauri && cargo test && cd ../.. && cd apps/web && npx tsc --noEmit`
Expected: cargo/tsc 全绿。

手动冒烟（本机）：`cd desktop && npx tauri dev`，确认 CallStudio 连房、TTS 试听、`open_logs` 自定义命令可用、控制台无 CSP violation report。（此步为人工验证项，不进 CI。）

- [ ] **Step 4: Commit**

```bash
git add desktop/src-tauri/tauri.conf.json desktop/src-tauri/capabilities/default.json
git commit -m "fix(security): enable Tauri CSP, drop unused shell capabilities and global API

P2-9(深测): csp:null + withGlobalTauri + shell:allow-execute 死配置——渲染实时
客户转写的应用无任何 XSS 兜底,且注册插件一行即激活 execute 面。现设 CSP
(connect-src 白名单拦 token 外带),删 shell 能力与全局 API(前端只用
__TAURI_INTERNALS__,__TAURI__ 全仓 0 引用)。"
```

### Task 21: 转写区 Markdown 链接去活（P3）

**Files:**
- Modify: `apps/web/components/agents-ui/agent-chat-transcript.tsx:137`

**Interfaces:**
- Consumes: Streamdown（2.6.0，基于 react-markdown；已核实 sanitize 后无脚本执行，残余=可点击 `<a>` 钓鱼面）。
- Produces: 无新接口。

- [ ] **Step 1: 实现（首选）**

`<Streamdown>{message}</Streamdown>` 改为：

```tsx
<Streamdown disallowedElements={["a"]} unwrapDisallowed>{message}</Streamdown>
```

（`unwrapDisallowed` 摘掉链接壳保留文字——「点这个链接 https://…」文本照读，锚不可点。）

- [ ] **Step 2: 验证（含类型否决分支）**

Run: `cd apps/web && npx tsc --noEmit`
- 若 PASS：进入 Step 3。
- 若 tsc 报 `disallowedElements` 不是 Streamdown 合法 prop（其类型未透传 react-markdown props）：改用源文本去链（同文件，message 渲染前）：

```tsx
const plainMessage = useMemo(
  () => message.replace(/\[([^\]]*)\]\((?:[^)]*)\)/g, "$1"),
  [message],
);
// <Streamdown>{plainMessage}</Streamdown>
```

- [ ] **Step 3: 构建验证 + Commit**

Run: `cd apps/web && npm run build`
Expected: 构建成功。

```bash
git add apps/web/components/agents-ui/agent-chat-transcript.tsx
git commit -m "fix(security): disable clickable links in agent transcript markdown

P3(深测): Streamdown sanitize 后无脚本执行,但恶意房间参与者的 data-channel
文本可在话务员转写时间线渲染可点击 <a>(钓鱼面)。链接去壳留文字。"
```

---

## 任务组 H：终验与文档

### Task 22: 全量回归 + 契约文档更新

**Files:**
- Modify: `AGENTS.md`（三层 RBAC bullet、auth-on 标准姿势 bullet）
- Run: 全仓回归

**Interfaces:**
- Consumes: 全部前序任务。
- Produces: 与代码一致的部署契约文档。

- [ ] **Step 1: AGENTS.md 契约更新（两处小改）**

「三层 RBAC（B1/B2…）」bullet 中 `root 种子走 BOK_ROOT_USERNAME/PASSWORD。` 之后插入：

```text
**密钥分离铁律（2026-09-16 深测修复）**：`BOK_CP_TOKEN` 与 `BOK_JWT_SECRET` 必须异值、JWT secret ≥32 字节——机器通道凭据不得兼作签名密钥（CP startup fail-closed 校验；`jwt_secret()` 已删 CP token 回落）；agent worker 的 `ControlPlaneClient` 自动携带 `BOK_CP_TOKEN`（env 注入即可，无需手工拼头）。节点加固模式注册必须带指纹；心跳对已绑定指纹节点强制带指纹。
```

「auth-on 开发栈标准姿势」bullet 末尾追加一句：

```text
两把密钥必须异值（`jwt_secret()` 不再回落 CP token，同值/缺失/<32 字节 startup 拒启）。
```

- [ ] **Step 2: 全量回归**

Run:
```bash
.venv312/bin/python -m pytest -q
.venv312/bin/python -m compileall -q apps packages services tools scripts
cd apps/web && npx tsc --noEmit && npm run build && cd ../..
cd services/realtime-translation && npm ci && npm test && cd ../..
cd desktop/src-tauri && cargo test && cd ../..
```
Expected: 全绿。修复引入的既有断言冲突按「新契约优先」修断言（每处单独列进 commit body）。

- [ ] **Step 3: 合并门（项目规约）**

Run: `scripts/verify_bundle.sh --staging`（随后按需 `--app`、`--doctor` 逐模式）
Expected: 绿；`doctor --packaged` 报 `token endpoint: ok (real JWT)`。

- [ ] **Step 4: Commit**

```bash
git add AGENTS.md
git commit -m "docs(security): record key-separation iron law and node fingerprint contract

与 2026-09-16 安全修复集同步的部署契约:CP token != JWT secret、agent 自动带
机器通道头、节点指纹强制。"
```

---

## 明确延后项（本轮不做，附理由）

| 项 | 理由 |
|---|---|
| GlobalInsight 加 account_id 列（insights 账号维度化） | 需 schema 迁移+蒸馏链路改写；Task 13 已用 `require_role(admin,root)` 封住 user 泄露面，维度化留独立 spec |
| `PATCH /api/users/{id}` 403/404 存在性 oracle | user id 为 `user-<uuid4hex12>` 不可枚举，需带外获得候选 id——残余风险可接受；改序会破坏「admin 管理面 403」语义 |
| settings PUT 并发 last-write-wins（secret 保留逻辑 TOCTOU） | 双 admin 并发改设置窗口极小；修需 settings 级锁，收益低 |
| `_redispatch_locks` 完整 LRU | Task 5 上限修剪已封无界增长；LRU 属优化非安全 |
| PyPI 抢注 `bok-voice-*` 占位包 | 需用户 PyPI 账号操作（手工项，见下） |
| CI third-party action/镜像 pin 到 SHA/digest | 需联网解析 SHA；手工项（见下） |
| 弱指纹档 `sha256("weak:"+hostname)` 加 per-license pepper | 涉及已签发指纹迁移；Task 16/17 已把指纹变为强制因子，弱档仅存量节点 |
| 密码策略增强（复杂度/黑名单，现仅 ≥8 位） | scrypt(N=2^14) 已抬在线爆破成本；改策略牵连存量账号与既有测试口令，收益低——运维侧靠 BOK_ROOT_PASSWORD 强值约束 |

## 手工跟进项（非代码任务）

1. **PyPI 占位**：在 pypi.org 注册 `bok-voice-core` / `bok-voice-business-db` / `bok-voice-obs` / `bok-voice-knowledge`（现均 404，依赖混淆窗口）。
2. **CI pin**：`git ls-remote https://github.com/dtolnay/rust-toolchain.git refs/tags/stable` 取 SHA 后替换 release.yml:48 的 `@stable`；`docker pull zricethezav/gitleaks:8.30.1 && docker inspect --format '{{index .RepoDigests 0}}' zricethezav/gitleaks:8.30.1` 取 digest 替换 gitleaks.yml:45。
3. **GHCR 已发布镜像反层复核**：`docker history ghcr.io/<org>/bok-control-plane:latest` + trivy 扫一次（本轮已验证 Dockerfile 不 COPY 敏感文件）。
4. **Supabase 真库 RLS 复核**：新索引 `uq_nodes_license_fingerprint` 需在 Supabase 引导库应用 `scripts/.p0_supabase_schema.sql` 重生成版（Task 16 Step 6 产物）。
