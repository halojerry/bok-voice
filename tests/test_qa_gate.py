"""Q→A 快路单测:资格闸/匹配器/挖掘/仓储/CP 端点。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))
sys.path.insert(0, str(ROOT / "packages" / "core"))

from agent_runtime.qa_gate import QaIndex, qa_exclude_reason  # noqa: E402
from bok_voice_core.qa_text import mine_qa_pairs, normalize_question  # noqa: E402
from bok_voice_core.types import TurnEvent  # noqa: E402


# ---- 资格闸(四道闸) ----

def test_exclude_digits_and_wa_signal_and_refuse():
    assert qa_exclude_reason("我單號係三七七八九零") == "digits"
    assert qa_exclude_reason("好的", wa_signal="captured") == "wa_signal"
    assert qa_exclude_reason("我唔要", verdict="") == "refuse"
    assert qa_exclude_reason("好的", advanced=True) == "advanced"
    assert qa_exclude_reason("好的", closing=True) == "closing"
    assert qa_exclude_reason("好的", flow_done=True) == "closing"
    assert qa_exclude_reason("隨便啦", verdict="objection") == "verdict"
    assert qa_exclude_reason("好的", verdict="confirm", wa_step_locked=True, wa_captured=False) == "wa_step_locked"
    # 干净轮:确认/含糊/提问/无判定都放行
    for v in ("", "confirm", "unclear", "question"):
        assert qa_exclude_reason("你哋幾時送到", verdict=v) == ""


# ---- 匹配器 ----

def _entries():
    return [
        {"id": "qa1", "question_text": "你們幾時送到", "answer_text": "一般三至五日到。", "lang": "zh", "scope": "global"},
        {"id": "qa2", "question_text": "運費幾多錢", "answer_text": "首重八蚊。", "lang": "cantonese", "scope": "global"},
        {"id": "qa3", "question_text": "可唔可以退換", "answer_text": "七日內可以退換。", "lang": "cantonese", "scope": "step", "step_index": 2},
    ]


def test_match_exact_and_threshold():
    idx = QaIndex(_entries())
    entry, score = idx.match("你們幾時送到", lang="zh")
    assert entry is not None and entry["id"] == "qa1"
    assert score >= 0.90
    # 归一化后同 key:全半角标点差异不影响命中
    entry2, _ = idx.match("你們幾時送到?", lang="zh")
    assert entry2 is not None and entry2["id"] == "qa1"


def test_match_unrelated_and_lang_mismatch():
    idx = QaIndex(_entries())
    entry, _score = idx.match("今日天氣點呀", lang="cantonese")
    assert entry is None
    # 语言不符 → 该条不参评
    entry2, _ = idx.match("運費幾多錢", lang="zh")
    assert entry2 is None


def test_match_step_scope():
    idx = QaIndex(_entries())
    hit, _ = idx.match("可唔可以退換", lang="cantonese", step_index=2)
    assert hit is not None and hit["id"] == "qa3"
    miss, _ = idx.match("可唔可以退換", lang="cantonese", step_index=0)
    assert miss is None
    miss2, _ = idx.match("可唔可以退換", lang="cantonese", step_index=None)
    assert miss2 is None


# ---- 挖掘 ----

def test_mine_qa_pairs_counts_calls_not_turns():
    convos = []
    for _ in range(6):
        convos.append([
            {"role": "user", "text": "幫我查下快遞", "lang": "cantonese"},
            {"role": "assistant", "text": "好，我幫您查一下。", "lang": "cantonese"},
            {"role": "user", "text": "幫我查下快遞", "lang": "cantonese"},  # 同通复读,唔叠加
            {"role": "assistant", "text": "好，我幫您查一下。", "lang": "cantonese"},
        ])
    convos.append([
        {"role": "user", "text": "今日天氣點", "lang": "cantonese"},
        {"role": "assistant", "text": "幾好。", "lang": "cantonese"},
    ])
    rows = mine_qa_pairs(convos, min_calls=5)
    assert len(rows) == 1
    assert rows[0]["question"] == normalize_question("幫我查下快遞")
    assert rows[0]["calls"] == 6, "按通话数计,同通复读去重"
    assert "查一下" in rows[0]["answer"]


# ---- 仓储(InMemory) ----

def test_inmemory_repo_qa_crud_and_conversations():
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    row = repo.create_qa_entry({"question_text": "幾時到", "answer_text": "三至五日", "lang": "cantonese"})
    assert row["id"] and row["enabled"] is True
    assert len(repo.list_qa_entries("acc-001")) == 1
    assert len(repo.list_qa_entries("acc-001", enabled=True)) == 1
    repo.update_qa_entry(row["id"], {"enabled": False})
    assert repo.list_qa_entries("acc-001", enabled=True) == []
    repo.incr_qa_hit(row["id"], 2)
    assert repo.list_qa_entries()[0]["hit_count"] == 2
    assert repo.delete_qa_entry(row["id"]) is True
    assert repo.list_qa_entries() == []

    # iter_call_conversations:按 role/text/lang 展开轮次
    repo.turns["c1"] = [
        TurnEvent(trace_id="c1", call_id="c1", turn_id="t0", role="user", transcript="你好", language="zh"),
        TurnEvent(trace_id="c1", call_id="c1", turn_id="t1", role="assistant", transcript="您好", language="zh"),
    ]
    convos = repo.iter_call_conversations()
    assert len(convos) == 1 and convos[0][0]["role"] == "user" and convos[0][1]["role"] == "assistant"


def test_inmemory_repo_exclude_test_objects():
    """挖掘过滤(InMemory):测试对象+无对象通话滤掉;默认参数(不过滤)全保留。

    无对象=A 线真实通话按对象发起,E2E 免 object_id 路径的合成残留。
    """
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    seed = {
        "c-test": ("obj-test", "E2E-甲", "幫我查下測試單號"),
        "c-real": ("obj-real", "陳大文", "你哋幾時送到"),
        "c-orphan": ("obj-gone", "", "單唔見咗"),  # 对象缺失(挂了不存在的 id)
        "c-noobj": ("", "", "冇對象通話"),  # 无对象通话(E2E 免 object_id 路径)
    }
    for cid, (oid, _name, text) in seed.items():
        repo.calls[cid] = {"id": cid, "account_id": "acc-001", "object_id": oid}
        if _name:
            repo.objects[oid] = {"id": oid, "display_name": _name}
        repo.turns[cid] = [
            TurnEvent(trace_id=cid, call_id=cid, turn_id="t0", role="user", transcript=text, language="cantonese")
        ]

    assert {c[0]["text"] for c in repo.iter_call_conversations("acc-001")} == {
        "幫我查下測試單號",
        "你哋幾時送到",
        "單唔見咗",
        "冇對象通話",
    }
    assert [c[0]["text"] for c in repo.iter_call_conversations("acc-001", exclude_test_objects=True)] == [
        "你哋幾時送到"
    ]


# ---- CP 端点(sqlite 路径,验证建表+CRUD+报告) ----

def test_cp_qa_endpoints(monkeypatch):
    os.environ.setdefault("DATABASE_URL", "")
    os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
    os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
    os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
    monkeypatch.setenv("DATABASE_URL", "")

    from fastapi.testclient import TestClient

    from control_plane.main import app

    with TestClient(app) as client:
        r = client.post(
            "/api/qa-entries",
            json={"question_text": "運費幾多錢", "answer_text": "首重八蚊。", "lang": "cantonese"},
        )
        assert r.status_code in (200, 201), r.text
        eid = r.json()["id"]
        assert client.get("/api/qa-entries").json()
        assert client.patch(f"/api/qa-entries/{eid}", json={"enabled": False}).status_code == 200
        assert client.post(f"/api/qa-entries/{eid}/hit").status_code == 200
        assert client.get("/api/reports/qa-pairs?min_calls=5").json() == []
        assert client.delete(f"/api/qa-entries/{eid}").json()["deleted"] is True


def test_cp_qa_pairs_exclude_test_objects(tmp_path, monkeypatch):
    """CP 挖掘链(sqlite):默认滤测试对象+无对象通话,exclude_test=false 看全量。

    同账号三通(测试对象 E2E-甲 / 真实对象 陳大文 / 无对象)各含一个
    user→assistant 对。无对象=E2E /api/start 免 object_id 路径的残留
    (「湾仔活道」197 通 fixture 实证)。
    """
    os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
    os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
    os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'qa_pairs.db'}")

    from fastapi.testclient import TestClient

    from control_plane.main import app

    with TestClient(app) as client:
        test_obj = client.post(
            "/api/objects", params={"account_id": "acc-001"}, json={"display_name": "E2E-甲", "language": "cantonese"}
        ).json()
        real_obj = client.post(
            "/api/objects", params={"account_id": "acc-001"}, json={"display_name": "陳大文", "language": "cantonese"}
        ).json()
        test_call = client.post(
            "/api/calls", json={"account_id": "acc-001", "object_id": test_obj["id"], "language": "cantonese"}
        ).json()
        real_call = client.post(
            "/api/calls", json={"account_id": "acc-001", "object_id": real_obj["id"], "language": "cantonese"}
        ).json()
        noobj_call = client.post(
            "/api/calls", json={"account_id": "acc-001", "language": "cantonese"}  # object_id 缺省=""
        ).json()
        convos = {
            test_call["id"]: ("幫我查下測試單號", "測試回覆。"),
            real_call["id"]: ("你哋幾時送到", "一般三至五日。"),
            noobj_call["id"]: ("冇對象嘅單點查", "無對象回覆。"),
        }
        for call_id, (question, answer) in convos.items():
            r = client.post(f"/api/calls/{call_id}/turns", params={"role": "user", "transcript": question, "language": "cantonese"})
            assert r.status_code == 200, r.text
            r = client.post(f"/api/calls/{call_id}/turns", params={"role": "assistant", "transcript": answer, "language": "cantonese"})
            assert r.status_code == 200, r.text

        # 默认(exclude_test=true):报告只含真实对象通话的问答
        rows = client.get("/api/reports/qa-pairs", params={"min_calls": 1}).json()
        assert [r["question"] for r in rows] == [normalize_question("你哋幾時送到")]
        # 显式 false:三通都在(含无对象通话)
        rows_all = client.get("/api/reports/qa-pairs", params={"min_calls": 1, "exclude_test": "false"}).json()
        assert {r["question"] for r in rows_all} == {
            normalize_question("你哋幾時送到"),
            normalize_question("幫我查下測試單號"),
            normalize_question("冇對象嘅單點查"),
        }
