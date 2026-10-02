"""G7 would-hit 覆盖信号（2026-09-25）单测：漏网组标注 + 顶层 summary。

面：annotate_would_hit（复用 find_existing_qa_entry 幂等判据——同账号同 lang
归一同问法，不另造匹配器）+ build_llm_gap_report 的 gap 行 would_hit/
existing_qa_id 与 summary.would_hit_covered/gaps_total + 词条面读失败降级。
造数姿势照抄 tests/test_gap_mining.py（内存仓 + TurnEvent 单调时间戳）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-gap-wouldhit-012345678")

ROOT = Path(__file__).resolve().parents[1]
for _p in ("packages/core", "packages/business-db"):
    _path = str(ROOT / _p)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from bok_voice_business_db.repository import InMemoryBusinessRepository  # noqa: E402
from control_plane import gap_mining as gm  # noqa: E402

_SEQ = iter(range(200_000, 300_000))


def _ts(n: int) -> str:
    return f"2026-09-25T11:00:00.{n:06d}"


def _turn(cid, role, text, *, speaker="", gen="", line="a", lang="zh", created_at=""):
    from bok_voice_core.types import TurnEvent

    n = next(_SEQ)
    return TurnEvent(
        trace_id=cid,
        call_id=cid,
        turn_id=f"t{n}",
        role=role,
        transcript=text,
        provider="",
        language=lang,
        created_at=created_at or _ts(n),
        line=line,
        speaker=speaker,
        gen=gen,
        template_step=3,
    )


def _seed_call(repo, *, obj_name="张三", template_id="tpl-1", account_id="acc-001"):
    from bok_voice_core.types import CallMode, SessionManifest

    cid = f"call-wh-{next(_SEQ)}"
    obj_id = repo.create_object(account_id, {"display_name": obj_name, "phone": "+85200000001"})["id"]
    repo.create_call(
        SessionManifest(
            session_id=cid,
            account_id=account_id,
            object_id=obj_id,
            persona_id="",
            mode=CallMode.LIVE,
            direction="outbound",
            language="zh",
            providers={},
        )
    )
    if template_id:
        repo.update_call(cid, template_id=template_id)
    return cid


def _exchange(repo, cid, question, answer, *, lang="zh"):
    n = next(_SEQ)
    repo.create_turn(_turn(cid, "user", question, speaker="customer", lang=lang, created_at=_ts(n)))
    repo.create_turn(
        _turn(cid, "assistant", answer, speaker="agent_ai", gen="llm", lang=lang,
              created_at=_ts(n + 1))
    )


def _qa(repo, question, *, lang="zh", account_id="acc-001", answer="答案是。"):
    return repo.create_qa_entry(
        {"account_id": account_id, "question_text": question, "answer_text": answer, "lang": lang}
    )


def test_gap_rows_marked_would_hit_with_existing_id():
    """已有同语言词条的组 would_hit=True 且带词条 id；无词条组 False。"""
    repo = InMemoryBusinessRepository()
    hit = _qa(repo, "你哋幾時送到")  # 词条问法带标点差也行（归一同键）
    for _ in range(2):
        cid = _seed_call(repo)
        _exchange(repo, cid, "你哋幾時送到？", "兩到三日到。")
        _exchange(repo, cid, "可以退貨嗎", "七日內可以退。")
    out = gm.build_llm_gap_report(repo, account_id="acc-001", min_calls=2)
    by_text = {g["customer_text"]: g for g in out["gaps"]}
    assert set(by_text) == {"你哋幾時送到？", "可以退貨嗎"}
    covered = by_text["你哋幾時送到？"]
    assert covered["would_hit"] is True
    assert covered["existing_qa_id"] == hit["id"]
    missing = by_text["可以退貨嗎"]
    assert missing["would_hit"] is False
    assert missing["existing_qa_id"] == ""
    # 顶层 summary 计数
    assert out["summary"] == {"would_hit_covered": 1, "gaps_total": 2}


def test_would_hit_lang_scoped():
    """同问法不同语言词条不算覆盖——运行时快路按 lang 过滤，信号口径一致。"""
    repo = InMemoryBusinessRepository()
    _qa(repo, "幾時送到", lang="cantonese")  # 只有粤语词条
    for _ in range(2):
        cid = _seed_call(repo)
        _exchange(repo, cid, "幾時送到", "兩到三日。")  # 通话语言 zh
    out = gm.build_llm_gap_report(repo, account_id="acc-001", min_calls=2)
    assert out["gaps"][0]["would_hit"] is False
    assert out["summary"] == {"would_hit_covered": 0, "gaps_total": 1}
    # 补上 zh 词条 → 变 True
    _qa(repo, "幾時送到！", lang="zh")  # 标点差照归一命中
    out2 = gm.build_llm_gap_report(repo, account_id="acc-001", min_calls=2)
    assert out2["gaps"][0]["would_hit"] is True
    assert out2["summary"] == {"would_hit_covered": 1, "gaps_total": 1}


def test_would_hit_account_scoped():
    """词条归属别的账号不算覆盖（幂等判据按账号归属，管理口径同源）。"""
    repo = InMemoryBusinessRepository()
    _qa(repo, "幾時送到", account_id="acc-999")
    for _ in range(2):
        cid = _seed_call(repo)
        _exchange(repo, cid, "幾時送到", "兩到三日。")
    out = gm.build_llm_gap_report(repo, account_id="acc-001", min_calls=2)
    assert out["gaps"][0]["would_hit"] is False


def test_would_hit_degrades_when_qa_read_fails():
    """词条面读失败 → 整体降级全 False，报告照出（信号缺失不炸驾驶舱）。"""
    repo = InMemoryBusinessRepository()
    _qa(repo, "幾時送到")
    for _ in range(2):
        cid = _seed_call(repo)
        _exchange(repo, cid, "幾時送到", "兩到三日。")

    class BoomRepo:
        """转发 turns/calls/objects 读，词条面抛异常的替身。"""

        def __getattr__(self, name):
            if name == "list_qa_entries":
                raise RuntimeError("qa face down")

            def _fn(*a, **kw):
                return getattr(repo, name)(*a, **kw)

            return _fn

    rows = [
        {"customer_text": "幾時送到", "lang": "zh", "count": 2, "calls": 2,
         "template_id": "tpl-1", "step": 3, "sample_answer": "兩到三日。",
         "sample_call_id": "c1"},
    ]
    gm.annotate_would_hit(BoomRepo(), "acc-001", rows)
    assert rows[0]["would_hit"] is False and rows[0]["existing_qa_id"] == ""


def test_would_hit_endpoint_summary_shape(monkeypatch):
    """端点整包透传（路由零改动的回归钉）：summary 与行内字段都在。"""
    from fastapi.testclient import TestClient

    from control_plane import main as cp_main

    repo = InMemoryBusinessRepository()
    _qa(repo, "你哋幾時送到？")
    cid = _seed_call(repo)
    _exchange(repo, cid, "你哋幾時送到？", "兩到三日到。")
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    client = TestClient(cp_main.app)
    r = client.get("/api/stats/llm-gaps?account_id=acc-001&min_calls=1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"] == {"would_hit_covered": 1, "gaps_total": 1}
    assert body["gaps"][0]["would_hit"] is True
    assert body["gaps"][0]["existing_qa_id"]
