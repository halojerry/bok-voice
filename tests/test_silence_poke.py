"""沉默戳话指标(W4-③,2026-09-24)单测。

面:silence_poke 纯函数(词族命中/不命中/大小写/一轮一次/语言分桶/零值形状/
merge_poke_stats)+ dashboard 接线(GET /api/stats/dashboard 的 silence_pokes 段
命中计数/涉及通话数/语言分桶/空账号零值)。

造数姿势与 test_qa_drift.py 同款:DATABASE_URL="" 强制内存仓 +
monkeypatch control_plane.main._repo;turns 直接 create_turn(TurnEvent),
created_at 用固定宽度单调计数器(字典序=时间序,turns 无 seq 列同仓假设)。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
# 占位值刻意用重复串(熵低于 gitleaks 阈值),与 test_qa_drift.py 同款防密钥门禁误报。
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-silence-poke-test-secret-silence-poke")

ROOT = Path(__file__).resolve().parents[1]
for _p in ("packages/core", "packages/business-db"):
    _path = str(ROOT / _p)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pytest  # noqa: E402

from bok_voice_business_db.repository import InMemoryBusinessRepository  # noqa: E402
from control_plane import silence_poke as sp  # noqa: E402

_SEQ = iter(range(1, 100_000))


# ---- 纯函数:词族命中/不命中/大小写 ----


def test_family_hits_all_three_languages():
    assert sp.is_silence_poke("有冇人知道")  # 真实通话 ×479 的那一族
    assert sp.is_silence_poke("喂喂,仲喺度嗎?")
    assert sp.is_silence_poke("你哋仲喺度嗎")
    assert sp.is_silence_poke("喂,聽到嗎")
    assert sp.is_silence_poke("有人在吗")
    assert sp.is_silence_poke("在吗在吗")
    assert sp.is_silence_poke("你还在吗")
    assert sp.is_silence_poke("听得到吗")
    assert sp.is_silence_poke("喂？喂？")
    assert sp.is_silence_poke("anyone there")
    assert sp.is_silence_poke("有人知道嗎你的意思是送邊度")  # 子串匹配:长句含词族照样计


def test_casefold_and_substring_matching():
    assert sp.is_silence_poke("ARE YOU THERE?")  # 大写命中(casefold)
    assert sp.is_silence_poke("Can You Hear Me??")
    assert sp.is_silence_poke("Hello? Hello? 仲喺度嗎")  # 跨语言同轮双词族
    assert sp.is_silence_poke("唔好意思,有人知道嗎?")  # 子串命中(前后带话)


def test_family_misses():
    assert not sp.is_silence_poke("")  # 空转写
    assert not sp.is_silence_poke(None)  # None 兜底不炸
    assert not sp.is_silence_poke("你好,我係XX快遞嘅")  # 开场白不是戳话
    assert not sp.is_silence_poke("好的,明白了")
    assert not sp.is_silence_poke("幾時送到")  # 普通问句
    assert not sp.is_silence_poke("喂")  # 单「喂」不算(词族钉「喂喂」/「喂？喂」,宁少算)


# ---- 纯函数:count_silence_pokes(角色过滤/一轮一次/语言分桶/零值) ----


def _turn(cid, role, text, *, speaker="", lang="zh", line="a", created_at=""):
    from bok_voice_core.types import TurnEvent

    n = next(_SEQ)
    return TurnEvent(
        trace_id=cid, call_id=cid, turn_id=f"t{n}", role=role, transcript=text,
        language=lang, created_at=created_at or f"2026-09-24T11:00:00.{n:06d}",
        line=line, speaker=speaker,
    )


def test_counts_customer_turns_only():
    """agent 轮就算念出同款词(如复述确认)也不计;旧行 speaker 空(分析账本前)不计。"""
    turns = [
        _turn("c1", "assistant", "有冇人知道", speaker="agent_ai"),  # AI 轮同词不计
        _turn("c1", "user", "喂喂", speaker=""),  # 旧行无 speaker 不计(保守)
        _turn("c1", "user", "在吗", speaker="customer", lang="zh"),
        _turn("c1", "user", "在吗", speaker="customer", lang="zh"),  # 两轮计两
    ]
    out = sp.count_silence_pokes(turns)
    assert out == {"pokes": 2, "by_lang": {"zh": 2}}


def test_one_turn_with_multiple_phrases_counts_once():
    turns = [_turn("c1", "user", "有冇人知道,仲喺度嗎?", speaker="customer", lang="cantonese")]
    assert sp.count_silence_pokes(turns) == {"pokes": 1, "by_lang": {"cantonese": 1}}


def test_lang_bucket_follows_turn_language_not_phrase_family():
    """语言桶=轮 language 列:粤语通话被转写漂移抄成普通话词族,按通话语言归桶。"""
    turns = [
        _turn("c1", "user", "有人在吗", speaker="customer", lang="cantonese"),
        _turn("c1", "user", "are you there", speaker="customer", lang="en"),
        _turn("c1", "user", "喂？喂", speaker="customer", lang="zh"),
    ]
    assert sp.count_silence_pokes(turns)["by_lang"] == {"cantonese": 1, "en": 1, "zh": 1}


def test_lang_bucket_falls_back_to_phrase_family_when_turn_lang_empty():
    """旧行 language 空 → 按命中短语词族语言归桶(不丢进未知桶)。"""
    turns = [
        _turn("c1", "user", "有人在吗", speaker="customer", lang=""),
        _turn("c1", "user", "聽到嗎", speaker="customer", lang=""),
    ]
    assert sp.count_silence_pokes(turns)["by_lang"] == {"zh": 1, "cantonese": 1}


def test_empty_turns_zero_shape():
    assert sp.count_silence_pokes([]) == {"pokes": 0, "by_lang": {}}
    assert sp.count_silence_pokes(None) == {"pokes": 0, "by_lang": {}}
    assert sp.count_silence_pokes([_turn("c1", "user", "今天天气不错", speaker="customer")]) == {
        "pokes": 0, "by_lang": {},
    }


def test_b_line_turns_not_counted():
    """B 线轮 speaker=me/other,天然被客户轮过滤排除。"""
    turns = [_turn("c1", "user", "喂喂", speaker="other", line="b")]
    assert sp.count_silence_pokes(turns) == {"pokes": 0, "by_lang": {}}


# ---- 纯函数:merge_poke_stats(dashboard 段形状) ----


def test_merge_poke_stats_shape():
    merged = sp.merge_poke_stats([
        {"pokes": 2, "by_lang": {"cantonese": 2}},
        {"pokes": 0, "by_lang": {}},  # 零命中通不计入 calls
        {"pokes": 1, "by_lang": {"zh": 1}},
        None,  # 容错
    ])
    assert merged == {"pokes": 3, "calls": 2, "by_lang": {"cantonese": 2, "zh": 1}}


def test_merge_poke_stats_empty():
    assert sp.merge_poke_stats([]) == {"pokes": 0, "calls": 0, "by_lang": {}}
    assert sp.merge_poke_stats(None) == {"pokes": 0, "calls": 0, "by_lang": {}}


# ---- dashboard 接线(GET /api/stats/dashboard silence_pokes 段) ----


def _seed_call(repo: InMemoryBusinessRepository, *, account_id="acc-001") -> str:
    from bok_voice_core.types import CallMode, SessionManifest

    cid = f"call-poke-{next(_SEQ)}"
    repo.create_call(SessionManifest(
        session_id=cid, account_id=account_id, object_id="", persona_id="",
        mode=CallMode.LIVE, direction="outbound", language="zh", providers={},
    ))
    return cid


@pytest.fixture()
def client_with_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    client = TestClient(app)
    return SimpleNamespace(client=client, repo=repo)


def test_dashboard_silence_pokes_section(client_with_repo):
    repo = client_with_repo.repo
    c1 = _seed_call(repo)
    c2 = _seed_call(repo)
    c3 = _seed_call(repo)
    for t in (
        _turn(c1, "user", "有冇人知道", speaker="customer", lang="cantonese"),
        _turn(c1, "assistant", "喺度喺度", speaker="agent_ai", lang="cantonese"),
        _turn(c1, "user", "仲喺度嗎", speaker="customer", lang="cantonese"),
        _turn(c2, "user", "Are you there?", speaker="customer", lang="en"),
        _turn(c3, "user", "今日天氣幾好", speaker="customer", lang="cantonese"),
    ):
        repo.create_turn(t)
    data = client_with_repo.client.get("/api/stats/dashboard").json()
    assert data["silence_pokes"] == {
        "pokes": 3, "calls": 2, "by_lang": {"cantonese": 2, "en": 1},
    }
    # 既有段形状不回归(新段是纯增量)。
    assert set(data) >= {"concurrency", "calls", "duration_buckets", "agents", "tags",
                         "todo", "silence_pokes"}


def test_dashboard_silence_pokes_empty_account_zero_shape(client_with_repo):
    data = client_with_repo.client.get("/api/stats/dashboard").json()
    assert data["silence_pokes"] == {"pokes": 0, "calls": 0, "by_lang": {}}
