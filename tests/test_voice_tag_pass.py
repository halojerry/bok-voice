"""authoring-time 语气标记 pass(2026-10-07)——CP 模块+端点+白名单 parity pin。

三面:
1. **parity pin(源级)**:CP 不 import agent 包——测试直接读
   apps/agent/agent_runtime/voice_style.py 源,ast 抽 VOICE_TAG_WHITELIST 字面,
   断言 VOICE_TAG_ALLOWED ⊆ 白名单且全是 ASCII 小写规范形(镜像
   test_branch_syntax_parity 的读源 pin 模式);并钉 CP 模块源零 agent_runtime
   import(防将来顺手 import 破坏分层)。
2. **纯函数**:prompt 构建(白名单枚举/输出契约/user 透传)、回包解析(围栏/
   裸数组/坏包)、守卫(白名单归一/未知括号句首剥/≤2 枚/停顿清理/禁改写拒用)、
   draft 组装(steps 分支行逐字节保留/其余键保留/changed 计数)。
3. **端点全链(fake LLM,零网络)**:draft 回包形状/零 DB 写(九键不落库+零
   revision 行)/审计行/权限闸(共享 user 403、跨账号与不存在 404、本人
   200)/LLM 失败 502 带 stage 绝不 500。

假 LLM=monkeypatch voice_tag_pass._llm_chat/_resolve_model(与
test_qa_cluster_cp 同款姿势)。
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-voice-tag-pass-000")

import re  # noqa: E402
import sys  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
for _p in ("packages/core", "packages/business-db"):
    _path = str(ROOT / _p)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from control_plane import voice_tag_pass as vt  # noqa: E402

PW = "Passw0rd!vt"


# ---------------------------------------------------------------------------
# 1. parity pin(源级,CP 不 import agent 包)
# ---------------------------------------------------------------------------


def _voice_style_whitelist() -> frozenset:
    """读 agent_runtime/voice_style.py 源,ast 抽 VOICE_TAG_WHITELIST 字面值。

    不 import agent 包(分层铁律);赋值形=frozenset({…字面…}),literal_eval 只
    吃集合字面(对 Call 节点会炸),取 Call 的首参集合字面求值。改白名单必须过
    本 parity 门——改成非常量表达式即测试红。"""
    src = (ROOT / "apps" / "agent" / "agent_runtime" / "voice_style.py").read_text(
        encoding="utf-8"
    )

    def _eval_value(node):
        if isinstance(node, ast.Call) and node.args:
            return ast.literal_eval(node.args[0])  # frozenset({...}) → {...}
        return ast.literal_eval(node)

    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "VOICE_TAG_WHITELIST":
                    return frozenset(_eval_value(node.value))
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "VOICE_TAG_WHITELIST":
                return frozenset(_eval_value(node.value))
    raise AssertionError("voice_style.py 未找到 VOICE_TAG_WHITELIST 字面赋值")


def test_allowed_is_subset_of_agent_whitelist():
    whitelist = _voice_style_whitelist()
    assert whitelist, "白名单抽取失败(空集=读源姿势坏了)"
    assert vt.VOICE_TAG_ALLOWED, "VOICE_TAG_ALLOWED 不应为空"
    assert vt.VOICE_TAG_ALLOWED <= whitelist, (
        f"CP 允许标记超出 agent 白名单: {sorted(vt.VOICE_TAG_ALLOWED - whitelist)}"
    )


def test_allowed_tags_are_canonical_ascii_lowercase():
    """规范形纪律:ASCII 小写括号形(letters+连字符),无全角/大写/空白混入。"""
    for tag in vt.VOICE_TAG_ALLOWED:
        assert tag == tag.strip() and " " not in tag, tag
        assert re.fullmatch(r"[a-z][a-z-]*", tag) is not None, tag


def test_cp_module_does_not_import_agent_package():
    """分层铁律:CP 模块源不得 import agent 包(白名单只准读源 pin;docstring 提及
    agent_runtime 路径不犯法,import 语句才是分层破坏)。"""
    src = (ROOT / "apps" / "control-plane" / "control_plane" / "voice_tag_pass.py").read_text(
        encoding="utf-8"
    )
    assert "from agent_runtime" not in src
    assert "import agent_runtime" not in src
    assert "from apps" not in src


# ---------------------------------------------------------------------------
# 2. 纯函数
# ---------------------------------------------------------------------------

_REF_WITH_BRANCH = (
    "您好，请问是{姓名}吗？我们是{物流公司}。\n"
    "如果客户说不方便 → 问几时方便再跟进\n"
    "注意:一次只问一件事"
)
_TEMPLATE = {
    "id": "tpl-1",
    "account_id": "acc-001",
    "name": "三步话术",
    "language": "zh",
    "opening": "你好，请问是{姓名}吗？",
    "closing": "好的，感谢您的时间，再见。",
    "steps_json": json.dumps(
        [
            {"goal": "确认身份", "ref": _REF_WITH_BRANCH},
            {"goal": "办理", "ref": "理赔通过微信专员办理。", "say": 1, "emotion": "calm"},
            {"goal": "空步", "ref": ""},
        ],
        ensure_ascii=False,
    ),
}


def test_collect_lines_shapes_and_ids():
    lines = vt.collect_lines(_TEMPLATE)
    ids = [it["id"] for it in lines]
    assert ids == ["opening", "closing", "s1", "s2"], ids
    s1 = next(it for it in lines if it["id"] == "s1")
    # 正稿头只到首个分支行之前;分支/注意行不进可标记面
    assert s1["text"] == "您好，请问是{姓名}吗？我们是{物流公司}。"
    assert s1["kind"] == "step" and s1["step_index"] == 0
    # 空正稿步不出条
    assert "s3" not in ids


def test_collect_lines_tolerates_bad_steps_json():
    assert vt.collect_lines({**_TEMPLATE, "steps_json": "not-json"}) != []
    assert [it for it in vt.collect_lines({**_TEMPLATE, "steps_json": "not-json"}) if it["kind"] == "step"] == []


def test_prompt_contains_allowlist_and_contract():
    system, user = vt.build_voice_tag_prompt('[{"id":"s1","text":"你好"}]')
    for tag in vt.VOICE_TAG_ALLOWED:
        assert f"({tag})" in system, tag
    assert '"lines"' in system and "id" in system
    assert "不改写" in system and "至多 2 个" in system
    assert user == '[{"id":"s1","text":"你好"}]'


def test_parse_marked_lines_tolerant_shapes():
    good = '{"lines": [{"id": "s1", "text": "你好(breath)"}]}'
    assert vt.parse_marked_lines(good) == {"s1": "你好(breath)"}
    fenced = "```json\n" + good + "\n```"
    assert vt.parse_marked_lines(fenced) == {"s1": "你好(breath)"}
    bare = '[{"id": "s1", "text": "你好(breath)"}]'
    assert vt.parse_marked_lines(bare) == {"s1": "你好(breath)"}
    assert vt.parse_marked_lines("模型胡言乱语") == {}
    assert vt.parse_marked_lines("") == {}
    assert vt.parse_marked_lines('{"lines": "nope"}') == {}


def test_guard_inserts_canonical_marker():
    out = vt._guard_line("您好，请问是{姓名}吗？", "您好，(breath)请问是{姓名}吗？")
    assert out == "您好，(breath)请问是{姓名}吗？"


def test_guard_normalizes_fullwidth_and_case():
    out = vt._guard_line("您好。", "您好。（Breath）")
    # 全角括号/大写归一成 ASCII 小写规范形,绝不全角原样过(会被当文本念)
    assert out == "您好。(breath)"
    assert "（" not in out and "B" not in out


def test_guard_rejects_rewritten_sentence():
    """禁改写铁律:LLM 动了原句任何文字/标点 → 整行拒用回原文。"""
    original = "您好，请问是{姓名}吗？我们是{物流公司}。"
    rewritten = "您好，请问是{name}吗？(breath)我们是{物流公司}。"  # 换了变量名
    assert vt._guard_line(original, rewritten) == original
    dropped = "您好，请问是{姓名}吗？(breath)"  # 尾句被吞
    assert vt._guard_line(original, dropped) == original
    repunct = vt._guard_line("您好，请问是{姓名}吗？", "您好。(breath)请问是{姓名}吗？")  # 逗号改句号
    assert repunct == "您好，请问是{姓名}吗？"


def test_guard_clamps_tag_budget():
    original = "第一句讲完了。第二句也讲完了。第三句还在讲。第四句收尾。"
    returned = "第一句讲完了。(breath)第二句也讲完了。(emm)第三句还在讲。(sighs)第四句收尾。(chuckle)"
    out = vt._guard_line(original, returned)
    assert out.count("(breath)") == 1 and out.count("(emm)") == 1
    assert "(sighs)" not in out and "(chuckle)" not in out  # 超预算剥除,留前 2


def test_guard_unknown_paren_policy():
    """未知括号词:句首/句读后剥(舞台指示位),句中当内容保留——镜像 sanitize。"""
    out = vt._guard_line("您好。", "（停顿两秒）您好。")
    assert out == "您好。"
    content = vt._guard_line("讲粤语（广东话）的客户。", "讲粤语（广东话）(breath)的客户。")
    assert "（广东话）" in content and "(breath)" in content


def test_guard_pause_tokens():
    original = "您稍等，我马上帮您看。"
    out = vt._guard_line(original, "您稍等，<#abc#>我马上帮您看。<#0.3#>")
    assert "<#abc#>" not in out  # 坏格式剥
    assert "<#0.3#>" not in out  # 行尾停顿剥(必须夹在可念文本之间)
    assert out == original
    out2 = vt._guard_line(original, "您稍等，<#0.3#><#0.5#>我马上帮您看。")
    assert out2 == "您稍等，<#0.3#>我马上帮您看。"  # 相邻停顿合一


def test_apply_draft_preserves_branch_tail_and_step_keys():
    marked = {
        "opening": "你好，(breath)请问是{姓名}吗？",
        "s1": "您好，请问是{姓名}吗？(emm)我们是{物流公司}。",
        "closing": "好的，感谢您的时间，再见。(sighs)",
    }
    draft = vt.apply_voice_tag_draft(_TEMPLATE, marked)
    assert draft["draft_only"] is True and draft["template_id"] == "tpl-1"
    assert draft["changed"] == 3 and draft["lines"] == 4
    steps = json.loads(draft["steps_json"])
    # 步 1:正稿头加了标记,分支/注意行逐字节保留
    assert steps[0]["ref"].splitlines()[0] == "您好，请问是{姓名}吗？(emm)我们是{物流公司}。"
    assert steps[0]["ref"] == (
        "您好，请问是{姓名}吗？(emm)我们是{物流公司}。\n"
        "如果客户说不方便 → 问几时方便再跟进\n"
        "注意:一次只问一件事"
    )
    # 步 1 goal 键保留;步 2 没送标记 → 原样(say/emotion 键零变化)
    assert steps[0]["goal"] == "确认身份"
    assert steps[1] == {"goal": "办理", "ref": "理赔通过微信专员办理。", "say": 1, "emotion": "calm"}
    assert draft["opening"] == "你好，(breath)请问是{姓名}吗？"
    assert draft["closing"].endswith("(sighs)")


def test_apply_draft_untouched_lines_not_counted():
    draft = vt.apply_voice_tag_draft(_TEMPLATE, {"s2": "理赔通过微信专员办理。"})
    assert draft["changed"] == 0
    assert json.loads(draft["steps_json"]) == json.loads(_TEMPLATE["steps_json"])


# ---------------------------------------------------------------------------
# 3. 端点全链(fake LLM)
# ---------------------------------------------------------------------------


def _client_and_repo(monkeypatch):
    """auth-off 形态(镜像 test_template_publish 夹具)。"""
    from bok_voice_business_db.repository import InMemoryBusinessRepository
    from control_plane import main as cp_main
    from fastapi.testclient import TestClient

    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    repo = InMemoryBusinessRepository()
    monkeypatch.setattr(cp_main, "_repo", lambda: repo)
    client = TestClient(cp_main.app).__enter__()
    return client, repo, cp_main


def _make_template(client, **overrides):
    body = {
        "account_id": "acc-001",
        "name": "三步话术",
        "language": "zh",
        "steps_json": _TEMPLATE["steps_json"],
        "opening": _TEMPLATE["opening"],
        "closing": _TEMPLATE["closing"],
        **overrides,
    }
    r = client.post("/api/templates", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _fake_llm(monkeypatch, response: str | Exception = ""):
    """假 LLM:截获 prompt 做断言,回固定 JSON;零网络(_resolve_model 也截)。"""
    calls = {"chat": 0, "system": "", "user": "", "discover": 0}

    def fake_chat(base_url, model, system, user, **kw):
        calls["chat"] += 1
        calls["system"] = system
        calls["user"] = user
        calls["kw"] = kw
        if isinstance(response, Exception):
            raise response
        return response

    def fake_discover(base_url):
        calls["discover"] += 1
        return "/models/fake-Qwen3-4B"

    monkeypatch.setattr(vt, "_llm_chat", fake_chat)
    monkeypatch.setattr(vt, "_resolve_model", fake_discover)
    return calls


_OK_RESPONSE = json.dumps(
    {
        "lines": [
            {"id": "opening", "text": "你好，(breath)请问是{姓名}吗？"},
            {"id": "s1", "text": "您好，请问是{姓名}吗？(emm)我们是{物流公司}。"},
        ]
    },
    ensure_ascii=False,
)


def test_endpoint_draft_shape_and_no_db_write(monkeypatch):
    client, repo, cp_main = _client_and_repo(monkeypatch)
    tpl = _make_template(client)
    calls = _fake_llm(monkeypatch, _OK_RESPONSE)
    events: list[tuple] = []
    monkeypatch.setattr(cp_main, "_audit", lambda action, **kw: events.append((action, kw)) or {})

    r = client.post(f"/api/templates/{tpl['id']}/voice-tags")
    assert r.status_code == 200, r.text
    body = r.json()
    # draft 回包形状
    assert body["draft_only"] is True and body["template_id"] == tpl["id"]
    assert body["changed"] == 2 and body["lines"] == 4
    steps = json.loads(body["steps_json"])
    assert steps[0]["ref"].splitlines()[0].endswith("(emm)我们是{物流公司}。")
    assert body["opening"].startswith("你好，(breath)")
    # LLM 走了 mining 车道形状(system 含白名单、user 是 JSON 行)
    assert calls["chat"] == 1 and calls["discover"] == 1
    assert "(breath)" in calls["system"]
    lines_payload = json.loads(calls["user"])
    assert [it["id"] for it in lines_payload] == ["opening", "closing", "s1", "s2"]
    # 零 DB 写:九键原样、published_json 空、零 revision 行
    row = repo.get_template(tpl["id"])
    assert row["steps_json"] == tpl["steps_json"]
    assert row["opening"] == tpl["opening"] and row["closing"] == tpl["closing"]
    assert row["published_json"] == ""
    assert repo.list_template_revisions(tpl["id"]) == []
    # 审计行
    actions = [a for a, _kw in events]
    assert actions == ["template.voice_tag_pass"]
    detail = events[0][1]["detail"]
    assert detail["draft_only"] is True and detail["changed"] == 2
    assert events[0][1]["subject_id"] == tpl["id"]


def test_endpoint_empty_template_short_circuits_no_llm(monkeypatch):
    client, repo, _cp = _client_and_repo(monkeypatch)
    tpl = _make_template(client, opening="", closing="", steps_json="")
    calls = _fake_llm(monkeypatch, _OK_RESPONSE)
    r = client.post(f"/api/templates/{tpl['id']}/voice-tags")
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == 0
    assert calls["chat"] == 0, "无可标记面=零 LLM 请求"


def test_endpoint_llm_failure_502_with_stage(monkeypatch):
    client, repo, _cp = _client_and_repo(monkeypatch)
    tpl = _make_template(client)
    _fake_llm(monkeypatch, httpx.ConnectError("connection refused"))
    r = client.post(f"/api/templates/{tpl['id']}/voice-tags")
    assert r.status_code == 502, r.text
    body = r.json()
    assert body["stage"] == "template.voice_tag_pass"
    assert "语气标记生成失败" in body["detail"]
    # 零 DB 写(失败也不动库)
    assert repo.get_template(tpl["id"])["steps_json"] == tpl["steps_json"]


def test_endpoint_bad_llm_payload_is_noop_draft(monkeypatch):
    """LLM 回包解析不了 → 空改动 draft 200(不炸、不落库)——改写权在人。"""
    client, repo, _cp = _client_and_repo(monkeypatch)
    tpl = _make_template(client)
    _fake_llm(monkeypatch, "模型胡言乱语不是 JSON")
    r = client.post(f"/api/templates/{tpl['id']}/voice-tags")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] == 0
    assert json.loads(body["steps_json"]) == json.loads(tpl["steps_json"])


def test_endpoint_missing_template_404(monkeypatch):
    client, _repo, _cp = _client_and_repo(monkeypatch)
    _fake_llm(monkeypatch, _OK_RESPONSE)
    assert client.post("/api/templates/nope-nope/voice-tags").status_code == 404


def _login(client, repo, username: str, role: str) -> dict:
    from control_plane.auth import hash_password

    repo.create_user(username=username, password_hash=hash_password(PW), role=role, account_id="acc-001")
    r = client.post("/api/auth/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_endpoint_gates_share_403_and_404(monkeypatch):
    """闸链与 PUT 同族:共享模板 user 403 / 跨账号 404 / 本人 200。"""
    client, repo, _cp = _client_and_repo(monkeypatch)
    admin = _login(client, repo, "boss", "admin")
    shared = client.post(
        "/api/templates",
        headers=admin,
        json={"account_id": "acc-001", "name": "共享话术", "steps_json": _TEMPLATE["steps_json"],
              "opening": _TEMPLATE["opening"]},
    ).json()
    other = _make_template(client, account_id="acc-002", name="别家话术")
    op1 = _login(client, repo, "op1", "user")
    mine = client.post(
        "/api/templates",
        headers=op1,
        json={"account_id": "acc-001", "name": "OP1私", "steps_json": _TEMPLATE["steps_json"],
              "opening": _TEMPLATE["opening"]},
    ).json()
    _fake_llm(monkeypatch, _OK_RESPONSE)

    # user 对共享基线 → 403(deny_foreign_owner(edit=True),与 PUT 同闸)
    assert client.post(f"/api/templates/{shared['id']}/voice-tags", headers=op1).status_code == 403
    # 跨账号 → 404(不泄露存在性)
    assert client.post(f"/api/templates/{other['id']}/voice-tags", headers=op1).status_code == 404
    # 本人个人话术可用;admin 可用共享
    assert client.post(f"/api/templates/{mine['id']}/voice-tags", headers=op1).status_code == 200
    assert client.post(f"/api/templates/{shared['id']}/voice-tags", headers=admin).status_code == 200


# ---------------------------------------------------------------------------
# 4. LLM 车道取数(env 链形状;路由表档在 test_model_routing 已覆盖同款 resolve_route)
# ---------------------------------------------------------------------------


def test_lane_env_chain_default(monkeypatch):
    """env 缺省链:MLX_LLM_BASE_URL 未设 → :1235 缺省(与 qa_cluster 同链零漂移)。"""
    monkeypatch.delenv("MLX_LLM_BASE_URL", raising=False)
    monkeypatch.setenv("BOK_MODEL_ROUTING", "0")  # kill-switch=强制 env 链
    base_url, model_override, api_key, thinking = vt._lane()
    assert base_url == "http://127.0.0.1:1235/v1"
    assert model_override == "" and api_key == "" and thinking is False


def test_lane_routing_openai_carries_key(monkeypatch):
    """路由表 openai 档:base_url/model/api_key 透传(云端档才带 key/thinking)。"""
    routing = json.dumps(
        {
            "lanes": {
                "mining": {
                    "provider": "openai",
                    "base_url": "https://api.deepseek.example/v1",
                    "model": "deepseek-flash",
                    "api_key": "sk-test",
                    "extra": {"enable_thinking": False},
                }
            }
        }
    )
    monkeypatch.setattr(vt, "read_model_routing_raw", lambda: routing)
    base_url, model_override, api_key, thinking = vt._lane()
    assert base_url == "https://api.deepseek.example/v1"
    assert model_override == "deepseek-flash"
    assert api_key == "sk-test" and thinking is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://api.deepseek.example/v1", "https://api.deepseek.example/v1"),
        ("http://127.0.0.1:1235/v1/", "http://127.0.0.1:1235/v1"),
    ],
)
def test_base_url_rstrip(monkeypatch, raw, expected):
    monkeypatch.setenv("MLX_LLM_BASE_URL", raw)
    monkeypatch.setenv("BOK_MODEL_ROUTING", "0")
    assert vt._base_url() == expected
