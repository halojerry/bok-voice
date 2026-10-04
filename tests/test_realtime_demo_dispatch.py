"""云端 Realtime S2S 演示档（2026-09-25 阶段 B）建单→派单→worker 管线测试。

覆盖面：
- CP 建单：mode=realtime_demo 透传落库（CallMode 枚举新值）；出境红线闸——
  仅 root / 机器通道 / auth-off 匿名可建，admin/user 403 带人话；建单审计
  detail 带 mode=realtime_demo；
- CP 派单：token 挂 agent_name="bok-realtime" dispatch，metadata 七键契约
  （call_id/object_id/object_name/account_id/model/voice/instructions）；
  A 线 bok-voice 分支对演示房零串台；
- webhook：演示房 agent 离房不自动补位（补位只会派 bok-voice=跨线串台）；
- worker 纯函数（apps/agent/agent_runtime/realtime_demo.py）：假对象前缀闸、
  派单元数据解析缺省、时长熔断档读 env、usage 钩子探测、话风硬规则置顶、
  session_usage_updated 打点纯函数与新事件源级 pin。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "apps" / "agent"))

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-realtime-demo")
PW = "Passw0rd!x"  # 测试夹具口令(与 test_token_dispatch 同源),非真实凭据

# ---- CP 侧 harness（与 test_token_dispatch 同款） ----

from _bokpatch import patch_bok  # noqa: E402
from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import hash_password


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app
    from control_plane.nodes_store import NodeStore

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
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
    import base64

    seg = participant_token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))


def _dispatch_agents(participant_token: str) -> list[dict]:
    return _claims(participant_token).get("roomConfig", {}).get("agents") or []


def _fresh_limiter(monkeypatch):
    import control_plane.main as cp_main

    monkeypatch.setattr(cp_main, "_token_issue_times", {})


def _create_demo_call(client, account_id="acc-001", object_id=""):
    return client.post(
        "/api/calls",
        json={"account_id": account_id, "mode": "realtime_demo", "object_id": object_id},
    )


# ---- 建单：mode 透传 + 出境红线闸 + 审计 ----


def test_realtime_demo_mode_passthrough_and_audit(monkeypatch):
    """mode=realtime_demo 落库为字符串、建单审计 detail 带同名 mode。"""
    client, repo = _client_and_repo(monkeypatch)
    audits = []
    monkeypatch.setattr(
        "control_plane.main._audit",
        lambda action, **kw: audits.append((action, kw)),
    )
    r = _create_demo_call(client)
    assert r.status_code in (200, 201), r.text
    call = r.json()
    assert repo.get_call(call["id"])["mode"] == "realtime_demo"
    creates = [kw for action, kw in audits if action == "call.create"]
    assert creates and creates[-1]["detail"]["mode"] == "realtime_demo"


def test_realtime_demo_admin_user_403_root_ok(monkeypatch):
    """出境红线闸：admin/user 403 带人话；root 直通；auth-off 匿名（开发形态）
    与 model-routing 同姿势放行。"""
    client, repo = _client_and_repo(monkeypatch)
    _mk_user(repo, "rd-admin", role="admin")
    _mk_user(repo, "rd-user", role="user")
    _mk_user(repo, "rd-root", role="root")

    for name in ("rd-admin", "rd-user"):
        tok = _login(client, name)
        r = client.post(
            "/api/calls",
            json={"account_id": "acc-001", "mode": "realtime_demo"},
            headers=_auth(tok),
        )
        assert r.status_code == 403, (name, r.text)
        assert "root" in r.json()["detail"]  # 人话文案点破红线归属

    tok = _login(client, "rd-root")
    r = client.post(
        "/api/calls",
        json={"account_id": "acc-001", "mode": "realtime_demo"},
        headers=_auth(tok),
    )
    assert r.status_code in (200, 201), r.text


def test_normal_mode_admin_still_allowed(monkeypatch):
    """红线闸只拦 realtime_demo：admin 建普通通话不受影响（闸不误伤）。"""
    client, repo = _client_and_repo(monkeypatch)
    _mk_user(repo, "rd-admin2", role="admin")
    tok = _login(client, "rd-admin2")
    r = client.post(
        "/api/calls",
        json={"account_id": "acc-001", "mode": "simulation"},
        headers=_auth(tok),
    )
    assert r.status_code in (200, 201), r.text
    assert repo.get_call(r.json()["id"])["mode"] == "simulation"


# ---- 派单：bok-realtime dispatch + metadata 契约 + A 线零串台 ----


def test_realtime_demo_dispatch_agent_and_metadata(monkeypatch):
    """token 对演示房挂 agent_name=bok-realtime，metadata 七键契约在场，
    object_name 来自对象档案 display_name。"""
    client, repo = _client_and_repo(monkeypatch)
    _fresh_limiter(monkeypatch)
    obj = client.post(
        "/api/objects?account_id=acc-001",
        json={"display_name": "demo-测试客户", "language": "zh"},
    ).json()
    call = _create_demo_call(client, object_id=obj["id"]).json()
    r = client.post(
        "/api/token", json={"account_id": "acc-001", "call_id": call["id"]}
    )
    assert r.status_code == 201, r.text
    agents = _dispatch_agents(r.json()["participantToken"])
    assert len(agents) == 1 and agents[0]["agentName"] == "bok-realtime"
    meta = json.loads(agents[0]["metadata"])
    # 字段名定死（联调契约）：七键
    assert set(meta) == {
        "call_id", "object_id", "object_name", "account_id", "model", "voice", "instructions",
    }
    assert meta["call_id"] == call["id"]
    assert meta["object_id"] == obj["id"]
    assert meta["object_name"] == "demo-测试客户"
    assert meta["account_id"] == "acc-001"
    # model/voice/instructions 本版恒空串（worker 端内建缺省，覆盖位留给后续）。
    assert meta["model"] == ""
    assert meta["voice"] == ""
    assert meta["instructions"] == ""


def test_realtime_demo_room_no_bok_voice_dispatch(monkeypatch):
    """A 线 dispatch 分支对演示房让位：房里不会同时出现 bok-voice（跨线串台防线）。"""
    client, repo = _client_and_repo(monkeypatch)
    _fresh_limiter(monkeypatch)
    call = _create_demo_call(client).json()
    r = client.post(
        "/api/token", json={"account_id": "acc-001", "call_id": call["id"]}
    )
    assert r.status_code == 201, r.text
    names = {a["agentName"] for a in _dispatch_agents(r.json()["participantToken"])}
    assert names == {"bok-realtime"}
    assert "bok-voice" not in names


def test_realtime_demo_webhook_no_redispatch(monkeypatch):
    """演示房 agent 离房不补位：本路径只会派 bok-voice，补进演示房=抢接云端通话。"""
    client, repo = _client_and_repo(monkeypatch)
    call = _create_demo_call(client).json()
    monkeypatch.setattr(
        "control_plane.main._verify_livekit_webhook", lambda request, raw: True
    )
    r = client.post(
        "/api/webhook/livekit",
        json={
            "event": "participant_left",
            "room": {"name": call["id"]},
            "participant": {"identity": "agent-JOBDEMO"},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"handled": False, "reason": "realtime demo room"}


# ---- worker 纯函数（realtime_demo.py，无 livekit 依赖） ----


def _demo_module():
    import agent_runtime.realtime_demo as rd

    return rd


def test_demo_object_name_gate():
    rd = _demo_module()
    # 命中测试前缀族（含 demo- 段）→ 准出境
    assert rd.is_demo_safe_object_name("demo-张三") is True
    assert rd.is_demo_safe_object_name("E2E-cantonese") is True
    assert rd.is_demo_safe_object_name("soak1-客户") is True
    assert rd.is_demo_safe_object_name("LOAD-01") is True
    assert rd.is_demo_safe_object_name("并发压测") is True
    assert rd.is_demo_safe_object_name("probe-fast") is True
    # 真实命名/空名/前缀不完全 → 拒绝（出境红线：演示对象恒假数据）
    assert rd.is_demo_safe_object_name("王小明") is False
    assert rd.is_demo_safe_object_name("") is False
    assert rd.is_demo_safe_object_name(None) is False
    assert rd.is_demo_safe_object_name("demo") is False  # 无连字符不算前缀命中


def test_parse_demo_metadata_defaults_and_overrides():
    rd = _demo_module()
    meta = rd.parse_demo_metadata(json.dumps({"call_id": "call-abc", "object_name": "demo-x"}))
    assert meta["call_id"] == "call-abc"
    assert meta["object_name"] == "demo-x"
    # 空缺省：model/voice/instructions 补内建缺省
    assert meta["model"] == rd.DEFAULT_REALTIME_MODEL
    assert meta["voice"] == rd.DEFAULT_REALTIME_VOICE
    assert meta["instructions"] == rd.DEMO_INSTRUCTIONS
    # 显式覆盖原样保留
    meta2 = rd.parse_demo_metadata(
        json.dumps({"model": "qwen3-omni-turbo-realtime", "voice": "Cherry", "instructions": "custom"})
    )
    assert meta2["model"] == "qwen3-omni-turbo-realtime"
    assert meta2["instructions"] == "custom"
    # 坏 JSON 宽容：全缺省不炸
    meta3 = rd.parse_demo_metadata("not-json")
    assert meta3["model"] == rd.DEFAULT_REALTIME_MODEL
    assert meta3["call_id"] == ""


def test_demo_max_seconds_reads_env(monkeypatch):
    rd = _demo_module()
    monkeypatch.delenv("BOK_REALTIME_DEMO_MAX_S", raising=False)
    assert rd.demo_max_seconds() == 300
    monkeypatch.setenv("BOK_REALTIME_DEMO_MAX_S", "60")
    assert rd.demo_max_seconds() == 60
    monkeypatch.setenv("BOK_REALTIME_DEMO_MAX_S", "abc")
    assert rd.demo_max_seconds() == 300
    # 护栏不许关死：<=0 回落缺省
    monkeypatch.setenv("BOK_REALTIME_DEMO_MAX_S", "0")
    assert rd.demo_max_seconds() == 300


def test_attach_usage_hook_probe():
    rd = _demo_module()
    seen = []

    class _WithHook:
        def set_usage_callback(self, cb):
            seen.append(cb)

    cb = lambda usage: None  # noqa: E731
    assert rd.attach_usage_hook(_WithHook(), cb) is True
    assert seen == [cb]

    class _NoHook:
        pass

    assert rd.attach_usage_hook(_NoHook(), cb) is False


# ---- 话风收紧 + usage 事件迁移（2026-09-25 阶段 B 收尾遗留项） ----


def test_demo_instructions_brevity_hard_rule():
    """话风收紧（真会话冒烟实证 omni-flash 每轮 7-10s/33 字+）：简洁约束写成
    硬规则并置顶；演示专员人设与「不确定就说明是演示」句保留。"""
    rd = _demo_module()
    text = rd.DEMO_INSTRUCTIONS
    # 硬规则关键词（稳定子串）
    assert "回答必须简短" in text
    assert "一句" in text
    assert "最多两句" in text
    assert "25" in text  # 每句不超过 25 字
    assert "不要复述客户的问题" in text
    assert "宁可短" in text
    # 置顶：简洁硬规则先于人设句出现
    assert text.index("回答必须简短") < text.index("演示专员")
    # 人设与演示口径保留
    assert "演示专员" in text
    assert "功能演示" in text


def test_format_session_usage_llm_bucket_line():
    """usage 打点纯函数：新 payload（SessionUsageUpdatedEvent.usage →
    AgentSessionUsage.model_usage）→ 稳定行格式；只取 llm_usage 桶（Realtime
    模型的 token 账由收集器折进 LLM 桶，无 total_tokens 字段=input+output），
    空账/无 llm 条目返回 None（调用方跳过打点）。"""
    rd = _demo_module()

    class _LLM:
        def __init__(self, inp, outp, dur):
            self.type = "llm_usage"
            self.input_tokens = inp
            self.output_tokens = outp
            self.session_duration = dur

    class _TTS:  # 非 LLM 桶不进 token 账
        type = "tts_usage"
        input_tokens = 999
        output_tokens = 999
        session_duration = 9.9

    class _Usage:
        model_usage = [_LLM(120, 33, 1.5), _LLM(50, 17, 0.25), _TTS()]

    line = rd.format_session_usage(_Usage())
    assert line == "total_tokens=220 input_tokens=170 output_tokens=50 duration=1.75"
    # 空/坏 payload 宽容：None 跳过，绝不炸监听回调
    assert rd.format_session_usage(None) is None
    assert rd.format_session_usage(object()) is None  # 无 model_usage 属性

    class _Empty:
        model_usage = []

    class _OnlyTTS:
        model_usage = [_TTS()]

    assert rd.format_session_usage(_Empty()) is None
    assert rd.format_session_usage(_OnlyTTS()) is None


def test_usage_event_pinned_to_session_usage_updated():
    """源级 pin：usage 监听挂新事件 session_usage_updated（livekit-agents 1.8.2，
    旧事件注册会触发官方 deprecation 告警），旧的 metrics_collected 全文不再出现。"""
    rd = _demo_module()
    src = Path(rd.__file__).read_text(encoding="utf-8")
    assert 'session.on("session_usage_updated"' in src
    assert "metrics_collected" not in src


# ---- bok.py 侧：worker spec/prod unit opt-in 门（BOK_QWEN_REALTIME） ----


def test_bok_realtime_worker_spec_opt_in(monkeypatch, tmp_path):
    """BOK_QWEN_REALTIME=1 才出 realtime-demo spec（:8084）；缺省三 worker 不变。"""
    import bok as bok_mod

    patch_bok(monkeypatch, "app_data_dir", lambda: tmp_path)
    patch_bok(monkeypatch, "repo_python", lambda: "py")
    monkeypatch.delenv("BOK_QWEN_REALTIME", raising=False)
    names = [s["name"] for s in bok_mod._worker_specs("py")]
    assert "realtime-demo" not in names
    monkeypatch.setenv("BOK_QWEN_REALTIME", "1")
    specs = {s["name"]: s for s in bok_mod._worker_specs("py")}
    assert specs["realtime-demo"]["port"] == 8084
    assert specs["realtime-demo"]["argv"][-1].endswith("agent_runtime.realtime_demo")
    assert specs["realtime-demo"]["env"]["BOK_SERVICE"] == "realtime-demo"


def test_bok_realtime_prod_unit_opt_in(monkeypatch, tmp_path):
    """_prod_units 同门：opt-in 才生成 bok-realtime 常驻单元（健康面 WORKER_PORTS
    不收 :8084——默认栈不跑演示档，prod status 不得对未启用部署恒 DEGRADED）。"""
    import bok as bok_mod

    patch_bok(monkeypatch, "app_data_dir", lambda: tmp_path)
    patch_bok(monkeypatch, "repo_python", lambda: "py")
    patch_bok(monkeypatch, "_embedded_livekit", lambda: None)
    patch_bok(monkeypatch, "_agent_prod_env", lambda: {})
    patch_bok(monkeypatch, "_interp_env", lambda env: {})
    patch_bok(monkeypatch, "_control_plane_env", lambda db: {})
    monkeypatch.delenv("BOK_QWEN_REALTIME", raising=False)
    assert "bok-realtime" not in {u[0] for u in bok_mod._prod_units()}
    monkeypatch.setenv("BOK_QWEN_REALTIME", "1")
    assert "bok-realtime" in {u[0] for u in bok_mod._prod_units()}
    assert ("realtime-demo", 8084) not in bok_mod.WORKER_PORTS
