"""B 线 interp session-report 409 竞态（2026-09-18，「全量 debug 收编」缓项收编）。

同传一通 call 有 fwd/rev 两个 worker（同 call_id=房间名），挂断后各自上报官方
SessionReport（真实逐模型 usage + 权威 chat_history 快照）。CP 幽灵覆盖闸
（C2 闸2）对「ended 且已有 report」一律 409 → 后到的兄弟 worker 报告（双语
对照的另一半 + 其逐模型 usage）被当失败丢弃，纪要静默缺半边。

修法=reporter 标记幂等合并：fwd/rev 报告各带 reporter 标记（agent 侧
`_mark_session_report`），不同标记的兄弟报告按 chat_history.items/usage 追加
合并进已存 blob，返回 200+merged（审计可观测，不再静默）；同标记重复（幽灵
重派同 worker 线）与无标记（A 线单 worker 语义）维持 409 幽灵防护——首个
报告永不覆盖，A 线既有结算语义零变化。
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "")  # in-memory repo
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

from fastapi.testclient import TestClient  # noqa: E402

from control_plane.main import app  # noqa: E402


def _mk_interp_call(client: TestClient) -> str:
    created = client.post(
        "/api/calls",
        json={"account_id": "acc-001", "object_id": "obj-1", "persona_id": "p-1",
              "mode": "simulation", "kind": "interpret", "target_lang": "en"},
    ).json()
    return str(created["id"])


def _report(reporter: str, item_text: str, input_tokens: int) -> dict:
    """官方 SessionReport.to_dict() 关键字段的极简替身（fwd/rev 各自快照不相交）。"""
    payload: dict = {
        "job_id": f"AJ_{reporter}",
        "room": "call-x",
        "chat_history": {"items": [{"type": "message", "role": "user", "content": [item_text]}]},
        "usage": [{"type": "llm_usage", "input_tokens": input_tokens, "output_tokens": 10}],
    }
    if reporter:
        payload["reporter"] = reporter
    return payload


def _stored_blob(client: TestClient, call_id: str) -> dict:
    row = client.get(f"/api/calls/{call_id}").json()
    return json.loads(row.get("session_report") or "{}")


# ---- 兄弟报告合并：纪要不丢 ----


def test_sibling_session_report_merged_not_rejected():
    """fwd 先存、rev 后到 → 200+merged 合并进已存 blob（修复前 409 丢弃）。"""
    with TestClient(app) as client:
        call_id = _mk_interp_call(client)
        client.post(f"/api/supervisor/{call_id}/end")
        # agent 收尾顺序：先 ended 后上报；fwd（我方→对方）先存
        first = client.post(
            f"/api/calls/{call_id}/session-report", json=_report("interp-me", "原文：我方一", 100)
        )
        assert first.status_code == 200 and first.json()["stored"] is True
        # rev（对方→我方）后到：不同 reporter 标记=兄弟 worker，幂等合并不丢
        second = client.post(
            f"/api/calls/{call_id}/session-report", json=_report("interp-other", "原文：对方一", 200)
        )
        assert second.status_code == 200
        assert second.json()["merged"] is True
        blob = _stored_blob(client, call_id)
        texts = [c for it in blob["chat_history"]["items"] for c in it["content"]]
        assert "原文：我方一" in texts and "原文：对方一" in texts
        assert {u["input_tokens"] for u in blob["usage"]} == {100, 200}
        assert blob["reporters"] == ["interp-me", "interp-other"]


def test_merge_preserves_first_writer_fields():
    """合并只追加 chat_history/usage/reporters，首个报告的 job_id 等字段不动。"""
    with TestClient(app) as client:
        call_id = _mk_interp_call(client)
        client.post(f"/api/supervisor/{call_id}/end")
        client.post(f"/api/calls/{call_id}/session-report", json=_report("interp-me", "a", 1))
        second = client.post(
            f"/api/calls/{call_id}/session-report", json=_report("interp-other", "b", 2)
        )
        assert second.status_code == 200
        blob = _stored_blob(client, call_id)
        assert blob["job_id"] == "AJ_interp-me"  # 首个报告者身份不被后者顶掉


def test_merged_report_is_observable_in_audit():
    """合并走显式审计事件（call.session_report_merged），不再静默。"""
    with TestClient(app) as client:
        call_id = _mk_interp_call(client)
        client.post(f"/api/supervisor/{call_id}/end")
        client.post(f"/api/calls/{call_id}/session-report", json=_report("interp-me", "a", 1))
        client.post(f"/api/calls/{call_id}/session-report", json=_report("interp-other", "b", 2))
        events = client.get("/api/audit", params={"call_id": call_id}).json()
        actions = {str(e.get("action") or "") for e in events}
        assert "call.session_report_merged" in actions


# ---- 幽灵防护不削弱 ----


def test_same_reporter_duplicate_still_rejected():
    """同 worker 线（同标记）的第二份=幽灵重派，维持 409，真数据不被覆盖。"""
    with TestClient(app) as client:
        call_id = _mk_interp_call(client)
        client.post(f"/api/supervisor/{call_id}/end")
        assert client.post(
            f"/api/calls/{call_id}/session-report", json=_report("interp-me", "真", 100)
        ).status_code == 200
        ghost = client.post(
            f"/api/calls/{call_id}/session-report", json=_report("interp-me", "幽灵", 999)
        )
        assert ghost.status_code == 409
        blob = _stored_blob(client, call_id)
        texts = [c for it in blob["chat_history"]["items"] for c in it["content"]]
        assert texts == ["真"] and {u["input_tokens"] for u in blob["usage"]} == {100}


def test_unmarked_second_report_still_rejected():
    """无 reporter 标记（A 线单 worker）的第二份照旧 409——既有语义零变化。"""
    with TestClient(app) as client:
        call_id = _mk_interp_call(client)
        client.post(f"/api/supervisor/{call_id}/end")
        first = client.post(f"/api/calls/{call_id}/session-report", json=_report("", "真", 1))
        assert first.status_code == 200
        ghost = client.post(f"/api/calls/{call_id}/session-report", json=_report("", "幽灵", 999))
        assert ghost.status_code == 409
        assert _stored_blob(client, call_id)["usage"][0]["input_tokens"] == 1


# ---- agent 侧：报告带 reporter 标记（纯函数） ----


def test_interpret_report_carries_reporter_marker():
    from agent_runtime.interpret import _mark_session_report

    official = {"job_id": "AJ_x", "usage": [{"type": "llm_usage"}]}
    fwd = _mark_session_report(official, "me")
    assert fwd["reporter"] == "interp-me"
    rev = _mark_session_report(official, "other")
    assert rev["reporter"] == "interp-other"
    # 官方 to_dict 产物不被原地改（fwd/rev 各自从同一份快照打标）
    assert "reporter" not in official
    # 空值兜底：标记恒非空（CP 以非空标记作为兄弟合并门槛）
    assert _mark_session_report({}, "")["reporter"] == "interp-unknown"
