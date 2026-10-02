"""M-22③(2026-09-23 修复波#4):模板 steps_json 分支行内部指令保存校验。

与 graph_json 校验同族(validate_flow_graph→400):POST/PUT 显式携带 steps_json
时逐分支行过 canned_guard.is_internal_instruction(动作前缀【…】先消费)——
「教练文案进罐头」挡在写入口;命中 400 invalid_branch_text,拒绝不写版本行
(「拒绝对数据无副作用」,与 invalid_graph_json 同门)。存量违例数据由运营
清理(清单见 fix-wave-4 报告),校验只防新增。
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-canned")

_CLEAN_STEPS = json.dumps(
    [
        {
            "goal": "平台",
            "ref": "咁您係喺邊個平台買？\n"
            "如果客户嫌赔偿少→我哋會按平台規則盡量幫您爭取。",
        },
        {"goal": "办理", "ref": "好的，我哋而家安排专员加你。"},
    ],
    ensure_ascii=False,
)
# task-4 M1 实弹教练文案(zh/en 两族)
_COACH_STEPS = json.dumps(
    [
        {
            "goal": "异议",
            "ref": "正稿\n"
            "如果客户说是骗局→说要核对订单才能确认到，去下一步问平台。",
        },
    ],
    ensure_ascii=False,
)
_COACH_STEPS_EN = json.dumps(
    [
        {
            "goal": "objection",
            "ref": "script\n"
            "If the customer says it's a scam→admit the packing mistake, "
            "apologise sincerely.",
        },
    ],
    ensure_ascii=False,
)
# 动作前缀消费后仍係教练文案 → 照拒(余文受检,标记本身不是护身符)
_COACH_WITH_ACTION = json.dumps(
    [
        {
            "goal": "收线",
            "ref": "正稿\n如果客户打错电话→【收线】礼貌收线，交代安慰话术",
        },
    ],
    ensure_ascii=False,
)


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    client = TestClient(app).__enter__()
    return client, repo


def test_create_with_coach_branch_rejected(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    bad = client.post(
        "/api/templates", json={"name": "t", "steps_json": _COACH_STEPS}
    )
    assert bad.status_code == 400
    body = bad.json()["detail"]
    assert body["error"] == "invalid_branch_text"
    assert any("说" in str(d) or "step" in str(d) for d in body["detail"])
    # en 族照拒
    bad_en = client.post(
        "/api/templates", json={"name": "t2", "steps_json": _COACH_STEPS_EN}
    )
    assert bad_en.status_code == 400
    assert bad_en.json()["detail"]["error"] == "invalid_branch_text"


def test_create_with_coach_after_action_prefix_rejected(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    bad = client.post(
        "/api/templates", json={"name": "t", "steps_json": _COACH_WITH_ACTION}
    )
    assert bad.status_code == 400
    assert bad.json()["detail"]["error"] == "invalid_branch_text"


def test_create_clean_branches_pass(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    ok = client.post(
        "/api/templates", json={"name": "t", "steps_json": _CLEAN_STEPS}
    )
    assert ok.status_code == 200, ok.text
    assert repo.get_template(ok.json()["id"])["steps_json"] == _CLEAN_STEPS


def test_put_coach_rejected_without_side_effects(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    tpl = client.post(
        "/api/templates", json={"name": "t", "steps_json": _CLEAN_STEPS}
    ).json()
    before_rows = len(repo.list_template_revisions(tpl["id"]))
    bad = client.put(
        f"/api/templates/{tpl['id']}",
        json={"steps_json": _COACH_STEPS},
    )
    assert bad.status_code == 400
    assert bad.json()["detail"]["error"] == "invalid_branch_text"
    # 旧值保留+零版本行(校验先于 append_template_revision)
    assert repo.get_template(tpl["id"])["steps_json"] == _CLEAN_STEPS
    assert len(repo.list_template_revisions(tpl["id"])) == before_rows


def test_put_without_steps_json_untouched(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    tpl = client.post(
        "/api/templates", json={"name": "t", "steps_json": _CLEAN_STEPS}
    ).json()
    ok = client.put(f"/api/templates/{tpl['id']}", json={"name": "t2"})
    assert ok.status_code == 200
    assert repo.get_template(tpl["id"])["steps_json"] == _CLEAN_STEPS


def test_invalid_steps_json_shape_still_saves(monkeypatch):
    """本守卫只管分支行文本;steps_json 形状坏(非 json)走既有宽容路径不变——
    运行时 parse_steps 有自己的宽容面,这里不扩权。"""
    client, _repo = _client_and_repo(monkeypatch)
    ok = client.post(
        "/api/templates", json={"name": "t", "steps_json": "{not json"}
    )
    assert ok.status_code == 200


# ---- 存量豁免（2026-09-26 理赔模板实弹）-------------------------------------
# 校验器 docstring 原意「只防新增」,但整稿 PUT 全量校验把规则上线前保存的模板
# 整本锁死(任何编辑应用都 400)。PUT 按 legacy_raw=旧稿 steps_json 行级豁免:
# 存量行原样放行;新写/改动一行(哪怕一个字)照拦;create 无旧稿全量校验不变。


def test_put_legacy_coach_line_unchanged_grandfathered(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    tpl = client.post(
        "/api/templates", json={"name": "t", "steps_json": _CLEAN_STEPS}
    ).json()
    # 直写 repo 模拟「规则上线前的存量违例」(create 通道今天起就拒 coach 行,
    # 存量只能来自历史数据)
    repo.update_template(tpl["id"], {"steps_json": _COACH_STEPS})
    # 整稿原样重存(加个无关字段)→ 存量行豁免,200
    ok = client.put(
        f"/api/templates/{tpl['id']}",
        json={"steps_json": _COACH_STEPS, "tone_override": "专业"},
    )
    assert ok.status_code == 200, ok.text
    assert repo.get_template(tpl["id"])["steps_json"] == _COACH_STEPS


def test_put_new_coach_line_still_rejected_alongside_legacy(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    tpl = client.post(
        "/api/templates", json={"name": "t", "steps_json": _CLEAN_STEPS}
    ).json()
    repo.update_template(tpl["id"], {"steps_json": _COACH_STEPS})
    # 旧稿 + 一条新增 coach 行 → 新行照拦(豁免只保存量,不放宽新增)
    combined = json.dumps(
        json.loads(_COACH_STEPS) + json.loads(_COACH_WITH_ACTION),
        ensure_ascii=False,
    )
    bad = client.put(
        f"/api/templates/{tpl['id']}",
        json={"steps_json": combined},
    )
    assert bad.status_code == 400
    body = bad.json()["detail"]
    assert body["error"] == "invalid_branch_text"
    # 只报新行,不报豁免掉的存量行
    assert any("收线" in str(d) for d in body["detail"])
    assert not any("骗局" in str(d) for d in body["detail"])
    # 拒绝零副作用
    assert repo.get_template(tpl["id"])["steps_json"] == _COACH_STEPS


def test_put_legacy_coach_line_edited_forces_cleanup(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    tpl = client.post(
        "/api/templates", json={"name": "t", "steps_json": _CLEAN_STEPS}
    ).json()
    repo.update_template(tpl["id"], {"steps_json": _COACH_STEPS})
    # 存量违例行被编辑(哪怕只改一处措辞)→ 不再豁免,强制运营清理后才存得进
    edited = json.loads(_COACH_STEPS)
    edited[0]["ref"] = edited[0]["ref"].replace(
        "说要核对订单才能确认到", "说要核对订单后才能确认到"
    )
    bad = client.put(
        f"/api/templates/{tpl['id']}",
        json={"steps_json": json.dumps(edited, ensure_ascii=False)},
    )
    assert bad.status_code == 400
    assert bad.json()["detail"]["error"] == "invalid_branch_text"
