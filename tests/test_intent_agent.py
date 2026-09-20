"""W4-T2 agent 意向事实账本+挂断评估+notify_human 分支(2026-09-19,plan §7)。

三块钉死:
- 纯函数矩阵:`_intent_facts_snapshot`(账本→INTENT_FACTS 12 键)与
  `evaluate_intent_disposition`(kill-switch/未命中原样/命中覆盖/只打码/坏行跳过)
  ——模块级可 import,离线直连;
- `_report_notify_once` 回滚语义(照 _report_whatsapp_once,CancelledError 是
  BaseException,except Exception 接不住——不回滚=绑定永久占用永不补报);
- 源码 pin(照 test_intent_judge_wiring 惯例):notify 分支不 StopResponse/provider
  标记/end 接线/账本埋点——文本切片钉结构,改接线形态即红。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from bok_voice_core.intent_rules import INTENT_FACTS  # noqa: E402

_SRC = (
    Path(__file__).resolve().parents[1] / "apps" / "agent" / "agent_runtime" / "agent.py"
).read_text(encoding="utf-8")

_INT_KEYS = {
    "repeat_count",
    "refuse_count",
    "objection_count",
    "confirm_count",
    "question_count",
    "duration_s",
    "nudge_fired",
    "watchdog_fired",
    "storm_rounds",
    "step_max",
    "graph_notifies",
    "wa_captured",
}


def _rule(rid: str = "r1", *, intent_code: str = "complaint", **over: object) -> dict:
    row: dict = {
        "id": rid,
        "account_id": "",
        "intent_code": intent_code,
        "disposition": "escalated",
        "priority": 10,
        "enabled": True,
        "conditions": [{"fact": "refuse_count", "op": "gte", "value": 2}],
    }
    row.update(over)
    return row


def _facts(refuse: int = 2) -> dict:
    base: dict = {k: 0 for k in _INT_KEYS - {"wa_captured"}}
    base["refuse_count"] = refuse
    base["duration_s"] = 120
    base["step_max"] = 3
    return base


# ---------------------------------------------------------------------------
# _intent_facts_snapshot:账本 → INTENT_FACTS 12 键
# ---------------------------------------------------------------------------


def test_snapshot_shape_matches_intent_facts_contract():
    from agent_runtime.agent import _intent_facts_snapshot

    ledger = {
        "nudge_fired": 2,
        "watchdog_fired": 1,
        "storm_rounds": 3,
        "graph_notifies": 1,
        "verdict_counts": {"refuse": 2, "question": 5, "unclear": 7},  # unclear 非白名单键
        "t_start_wall": 0.0,
    }
    snap = _intent_facts_snapshot(ledger, wa_captured=True, duration_s=65, step_max=4)
    assert set(snap) == set(INTENT_FACTS) == _INT_KEYS  # 12 键恰好对齐共享契约
    assert snap["refuse_count"] == 2 and snap["question_count"] == 5
    assert snap["nudge_fired"] == 2 and snap["watchdog_fired"] == 1
    assert snap["storm_rounds"] == 3 and snap["graph_notifies"] == 1
    assert snap["wa_captured"] is True and snap["duration_s"] == 65 and snap["step_max"] == 4


def test_snapshot_empty_ledger_is_conservative():
    from agent_runtime.agent import _intent_facts_snapshot

    snap = _intent_facts_snapshot({}, wa_captured=False, duration_s=0, step_max=1)
    assert snap["wa_captured"] is False
    assert snap["step_max"] == 1  # 入参直传,缺账本不覆盖
    assert all(snap[k] == 0 for k in _INT_KEYS - {"wa_captured", "step_max"})


# ---------------------------------------------------------------------------
# evaluate_intent_disposition 矩阵
# ---------------------------------------------------------------------------


def test_eval_no_hit_returns_default_verbatim(monkeypatch):
    from agent_runtime.agent import evaluate_intent_disposition

    monkeypatch.delenv("BOK_INTENT_RULES", raising=False)
    disp, code = evaluate_intent_disposition(_facts(refuse=0), [_rule()], "declined")
    assert (disp, code) == ("declined", "")  # 未命中:逐字节同旧行为


def test_eval_hit_overrides_disposition_and_carries_code(monkeypatch):
    from agent_runtime.agent import evaluate_intent_disposition

    monkeypatch.delenv("BOK_INTENT_RULES", raising=False)
    disp, code = evaluate_intent_disposition(_facts(refuse=3), [_rule()], "declined")
    assert (disp, code) == ("escalated", "complaint")


def test_eval_hit_without_disposition_keeps_default_but_codes(monkeypatch):
    from agent_runtime.agent import evaluate_intent_disposition

    monkeypatch.delenv("BOK_INTENT_RULES", raising=False)
    disp, code = evaluate_intent_disposition(
        _facts(refuse=3), [_rule(disposition="")], "polite_close"
    )
    assert (disp, code) == ("polite_close", "complaint")  # 只打码不覆盖


def test_eval_kill_switch_off_disables_rules(monkeypatch):
    from agent_runtime.agent import evaluate_intent_disposition

    monkeypatch.setenv("BOK_INTENT_RULES", "0")
    disp, code = evaluate_intent_disposition(_facts(refuse=9), [_rule()], "declined")
    assert (disp, code) == ("declined", "")


def test_eval_bad_rows_skipped_good_row_still_hits(monkeypatch):
    from agent_runtime.agent import evaluate_intent_disposition

    monkeypatch.delenv("BOK_INTENT_RULES", raising=False)
    rules = [
        {"id": "bad1", "conditions": "not-a-list"},  # 结构坏:永不命中
        {"id": "bad2", "intent_code": "", "conditions": []},  # 缺 code:eval 跳过
        {"id": "bad3", "intent_code": "x", "enabled": False,
         "conditions": [{"fact": "refuse_count", "op": "gte", "value": 0}]},  # 禁用
        {"id": "bad4", "intent_code": "x",
         "conditions": [{"fact": "alien_fact", "op": "gte", "value": 1}]},  # 白名单外 fact
        _rule("good", priority=10),
    ]
    disp, code = evaluate_intent_disposition(_facts(refuse=3), rules, "declined")
    assert (disp, code) == ("escalated", "complaint")


def test_eval_priority_low_wins(monkeypatch):
    from agent_runtime.agent import evaluate_intent_disposition

    monkeypatch.delenv("BOK_INTENT_RULES", raising=False)
    rules = [
        _rule("late", intent_code="low-p", disposition="low",
              conditions=[{"fact": "refuse_count", "op": "gte", "value": 0}], priority=99),
        _rule("early", intent_code="high-p", disposition="high", priority=1),
    ]
    disp, code = evaluate_intent_disposition(_facts(refuse=3), rules, "declined")
    assert (disp, code) == ("high", "high-p")


# ---------------------------------------------------------------------------
# _report_notify_once:成功烧 once / 失败与取消回滚
# ---------------------------------------------------------------------------


class _OkCP:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def report_assist(self, call_id: str, status: str = "notified", source: str = "intent") -> None:
        self.calls.append({"call_id": call_id, "status": status, "source": source})


class _FailingCP:
    async def report_assist(self, call_id: str, status: str = "notified", source: str = "intent") -> None:
        raise RuntimeError("cp down")


class _SlowCP:
    async def report_assist(self, call_id: str, status: str = "notified", source: str = "intent") -> None:
        await asyncio.sleep(60)  # 模拟请求在途时被 teardown 掐杀


def test_notify_once_success_burns_once_and_counts():
    from agent_runtime.agent import _report_notify_once

    cp = _OkCP()
    fired: set = set()
    facts: dict = {"graph_notifies": 0}

    async def main():
        await _report_notify_once(cp, "call-x", "bnd_1a2b3c4d", fired, facts, where="r1")

    asyncio.run(main())
    assert fired == {"bnd_1a2b3c4d"}  # 入队成功才烧 once
    assert facts["graph_notifies"] == 1
    assert cp.calls == [{"call_id": "call-x", "status": "notified", "source": "intent"}]


def test_notify_once_failure_discards_once_and_logs(capsys):
    from agent_runtime.agent import _report_notify_once

    fired: set = {"bnd_1a2b3c4d"}  # 预置:失败必须回滚
    facts: dict = {"graph_notifies": 0}

    async def main():
        await _report_notify_once(_FailingCP(), "c1", "bnd_1a2b3c4d", fired, facts)

    asyncio.run(main())
    assert "bnd_1a2b3c4d" not in fired  # 不烧 once:下一轮信号补报
    assert facts["graph_notifies"] == 0  # 失败不记账
    assert "[flow-graph] notify report failed" in capsys.readouterr().out


def test_notify_once_cancelled_discards_and_propagates():
    from agent_runtime.agent import _report_notify_once

    fired: set = {"bnd_1a2b3c4d"}
    facts: dict = {"graph_notifies": 0}

    async def main():
        task = asyncio.create_task(
            _report_notify_once(_SlowCP(), "call-x", "bnd_1a2b3c4d", fired, facts)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert "bnd_1a2b3c4d" not in fired  # CancelledError 也回滚(AGENTS.md ⑦)
    assert facts["graph_notifies"] == 0


# ---------------------------------------------------------------------------
# client:end_call intent_code 仅非空带 / list_intent_rules / report_assist
# ---------------------------------------------------------------------------


def _client_capturing():
    from agent_runtime.control_plane import ControlPlaneClient

    outer = ControlPlaneClient("http://cp.test", call_id="call-x")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = {k: v for k, v in request.url.params.items()}
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json={})

    outer._client = httpx.AsyncClient(
        base_url="http://cp.test", transport=httpx.MockTransport(handler)
    )
    return outer, captured


def test_end_call_intent_code_only_when_nonempty():
    outer, captured = _client_capturing()

    async def main():
        await outer.end_call("call-x", disposition="declined")
        assert captured["path"] == "/api/supervisor/call-x/end"
        assert captured["params"] == {"disposition": "declined"}  # 空 code:请求同旧
        await outer.end_call("call-x", disposition="no_response", intent_code="complaint")
        assert captured["params"]["disposition"] == "no_response"
        assert captured["params"]["intent_code"] == "complaint"

    asyncio.run(main())


def test_client_assist_and_intent_rules_shapes():
    outer, captured = _client_capturing()

    async def main():
        await outer.report_assist("call-x")
        assert captured["path"] == "/api/calls/call-x/assist"
        body = json.loads(captured["body"])
        assert body == {"status": "notified", "source": "intent"}
        rows = await outer.list_intent_rules(account_id="acc-009")
        assert rows == []
        assert captured["path"] == "/api/intent-rules"
        assert captured["params"] == {"account_id": "acc-009"}

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 源码 pin:接线结构(照 test_intent_judge_wiring 惯例)
# ---------------------------------------------------------------------------


def test_graph_notify_arm_pinned_between_jump_and_play():
    jump = _SRC.index('if _gbinding.action == "jump_step":')
    notify = _SRC.index("elif _gbinding.action == ACTION_NOTIFY_HUMAN:")
    play = _SRC.index("else:  # play_qa:", notify)
    assert jump < notify < play  # 第三臂落在 jump 与 play 之间


def test_graph_notify_arm_does_not_stop_response():
    start = _SRC.index("elif _gbinding.action == ACTION_NOTIFY_HUMAN:")
    end = _SRC.index("else:  # play_qa:", start)
    seg = _SRC[start:end]
    # 唯一允许的出现形态=注释「不 raise StopResponse」;真实调用形态(带括号)禁现。
    assert "raise StopResponse()" not in seg  # 打铃不抢话:落回 LLM 生成
    assert "不 raise StopResponse" in seg  # 意图注释在场(删注释或改语义即红)
    assert '_turn_origin["provider"] = "graph-notify"' in seg
    assert "_spawn_report(" in seg and "_report_notify_once(" in seg
    assert "flow_ctrl.graph_fired" in seg
    assert "FLOW_GRAPH notify binding=" in seg
    assert "_invalidate_stale_preemptive(" in seg  # 照 jump 先例


def test_flow_graph_actions_contain_notify_human():
    from bok_voice_core.flow_graph import (
        ACTION_NOTIFY_HUMAN,
        ACTIONS,
        parse_flow_graph,
        validate_flow_graph,
    )

    assert ACTION_NOTIFY_HUMAN == "notify_human"
    assert ACTION_NOTIFY_HUMAN in ACTIONS
    doc_json = json.dumps(
        {
            "version": 1,
            "intents": [
                {"id": "int_1a2b3c4d", "label": "求助", "keywords": ["人工"], "enabled": True}
            ],
            "bindings": [
                # notify 无负载:不要求 qa_id/step,合法
                {"id": "bnd_7e8f9a0b", "intent": "int_1a2b3c4d", "action": "notify_human",
                 "once": True, "enabled": True}
            ],
        }
    )
    assert validate_flow_graph(doc_json) == []
    doc = parse_flow_graph(doc_json)
    assert doc.bindings and doc.bindings[0].action == "notify_human"


def test_end_wiring_pinned_to_schedule_call_end():
    fire = _SRC.index("async def _fire_end_call(")
    sched = _SRC.index("def _schedule_call_end(")
    fire_seg = _SRC[fire:sched]
    assert "intent_code=str(_end_scheduled.get(\"intent_code\") or \"\")" in fire_seg
    sched_seg = _SRC[sched : _SRC.index("async def _end_on_shutdown(")]
    assert "_intent_facts_snapshot(" in sched_seg
    assert "evaluate_intent_disposition(" in sched_seg
    assert '_end_scheduled["intent_code"]' in sched_seg
    assert "wa_captured=bool(_wa_captured[\"on\"])" in sched_seg  # wa 直接读账本
    # 五个经 _schedule_call_end 的收线点仍在(REFUSE/FAREWELL/心跳/漏斗 v2 stall
    # 收线/分支动作【收线】);时长 fuse 直调 cp.end_call,新参 intent_code 缺省空
    # =逐字节同旧。新增的第 6 处=路线 A-② 分支动作收线臂(早段派发,与 REFUSE
    # 车道同源调用)——运营在话术分支写「如果客户打错电话→【收线】…」时的出口。
    assert _SRC.count("_schedule_call_end(") == 6  # 定义+REFUSE+FAREWELL+心跳+stall收线+分支收线
    assert 'await cp.end_call(call_id, disposition="completed")' in _SRC  # fuse 零变化


def test_facts_ledger_and_hooks_pinned():
    assert '"nudge_fired": 0' in _SRC and '"watchdog_fired": 0' in _SRC
    assert '"storm_rounds": 0' in _SRC and '"graph_notifies": 0' in _SRC
    assert '"verdict_counts": {}' in _SRC and '"t_start_wall": time.time()' in _SRC
    assert '_facts["nudge_fired"] += 1' in _SRC
    assert '_facts["watchdog_fired"] += 1' in _SRC
    assert '_facts["storm_rounds"] += 1' in _SRC
    assert 'facts["graph_notifies"] = int(facts.get("graph_notifies") or 0) + 1' in _SRC
    assert '_facts["verdict_counts"][verdict] = int(_facts["verdict_counts"].get(verdict, 0)) + 1' in _SRC


def test_rules_fetch_pinned():
    assert 'os.environ.get("BOK_INTENT_RULES", "1") == "1"' in _SRC
    assert "cp.list_intent_rules(account_id=_ctx_account)" in _SRC
    assert "from bok_voice_core.intent_rules import eval_intent_rules" in _SRC
