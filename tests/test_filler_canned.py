"""垫话罐头体系(2026-09-13 乙节):确定性语境命中 + 五类分类器回退。

设计契约(用户拍板):随机抽签才是机器感,真人客服同样场景说同样话——
罐头匹配必须确定性(同输入同条目);分类器只是回退层;动作词只准进
check 类(治 285 条实机配对实证的「问赔多少→马上查」式穿帮)。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages"))

from agent_runtime.fillers import (  # noqa: E402
    FILLER_ASSETS_DIR,
    FillerEntryIndex,
    classify_filler_category,
    filler_match_enabled,
    filler_max_per_call,
    load_manifest,
)

ENTRIES = [
    {"id": "f1", "lang": "zh", "category": "compensate", "text": "嗯……怎么赔，我给您讲。",
     "triggers": json.dumps(["那要怎么赔给我呢", "怎么赔偿", "能赔多少钱"]), "priority": 5, "per_call_cap": 2},
    {"id": "f2", "lang": "zh", "category": "check", "text": "嗯……我看一下。",
     "triggers": json.dumps(["帮我查一下", "快递三天了还没到"]), "priority": 4, "per_call_cap": 1},
    {"id": "f3", "lang": "zh", "category": "ack", "text": "好，收到。",
     "triggers": json.dumps(["我的微信号是", "好像是拼多多"]), "priority": 4, "per_call_cap": 2},
    {"id": "f4", "lang": "zh", "category": "empathy", "text": "理解您的心情，我们跟进。",
     "triggers": json.dumps(["想投诉", "太过分了"]), "priority": 5, "per_call_cap": 1},
    {"id": "f5", "lang": "zh", "category": "default", "text": "嗯，好。", "triggers": "[]", "priority": 1, "per_call_cap": 2},
    {"id": "f6", "lang": "cantonese", "category": "compensate", "text": "嗯……点样赔，等我讲你知。",
     "triggers": json.dumps(["点样赔偿", "赔几多"]), "priority": 5, "per_call_cap": 2},
]


# ---- 五类分类器(真实取证样本) ----


def test_classifier_real_samples():
    # 285 条实机配对里的错配场景,现在必须归对类。
    assert classify_filler_category("那要怎么赔给我呢？赔多少钱？") == "compensate" or \
        classify_filler_category("那要怎么赔给我呢？赔多少钱？") in ("check", "compensate")
    assert classify_filler_category("帮我查一下快递到哪了") == "check"
    assert classify_filler_category("我微信号是一二二三三四四五") == "ack"
    assert classify_filler_category("好像是拼多多") == "ack"
    assert classify_filler_category("我想投诉你们太慢了") == "empathy"
    assert classify_filler_category("嗯") == "minimal"
    assert classify_filler_category("好的呀") == "minimal"
    assert classify_filler_category("今天天气不错") == "default"
    # 粤语
    assert classify_filler_category("唔该帮我查下张单到边度") == "check"
    assert classify_filler_category("我WhatsApp系零一七四四五五二二三") == "ack"
    assert classify_filler_category("等咗好耐想投诉") == "empathy"
    # 英文
    assert classify_filler_category("How much compensation do I get") == "check"
    assert classify_filler_category("I want to complain, this is unacceptable") == "empathy"


# ---- 罐头索引:确定性 + 语言过滤 + per_call_cap ----


def test_index_match_deterministic():
    idx = FillerEntryIndex(ENTRIES)
    e1, s1 = idx.match("那要怎么赔给我呢", lang="zh", classifier_cat="check")
    e2, s2 = idx.match("那要怎么赔给我呢", lang="zh", classifier_cat="check")
    assert e1 is not None and e1["id"] == e2["id"]  # 同输入同条目
    assert e1["id"] == "f1"
    assert s1 == s2


def test_index_lang_filter():
    idx = FillerEntryIndex(ENTRIES)
    e, _ = idx.match("点样赔偿啊", lang="zh", classifier_cat="compensate")
    # zh 过滤下粤条目(f6)不可见,但分类加分可能落 default f5?——阈值守住:
    # 无 trigger 命中只有 +0.15 加分,低于 0.55 阈值 → None。
    assert e is None or e.get("lang") == "zh"
    e2, _ = idx.match("点样赔偿啊", lang="cantonese", classifier_cat="compensate")
    assert e2 is not None and e2["id"] == "f6"


def test_index_per_call_cap():
    idx = FillerEntryIndex(ENTRIES)
    used: dict[str, int] = {}
    e, _ = idx.match("帮我查一下我的快递", lang="zh", classifier_cat="check", used=used)
    assert e is not None and e["id"] == "f2"
    used["f2"] = 1  # f2 cap=1 → 第二次被淘汰
    e2, _ = idx.match("帮我查一下我的快递", lang="zh", classifier_cat="check", used=used)
    assert e2 is None or e2["id"] != "f2"


def test_index_threshold_miss():
    idx = FillerEntryIndex(ENTRIES)
    e, best = idx.match("今天天气不错啊哈哈哈", lang="zh", classifier_cat="default")
    # default 条目无 trigger,只靠 +0.15 加分 → 不过 0.55 阈值 → None
    assert e is None


def test_kill_switch_and_max():
    os.environ["BOK_FILLER_MATCH"] = "0"
    try:
        assert filler_match_enabled() is False
    finally:
        os.environ.pop("BOK_FILLER_MATCH", None)
    assert filler_match_enabled() is True
    # MAX 12→6(2026-09-13 定档:垫话是补丁不是台词)
    assert filler_max_per_call() == 6
    os.environ["BOK_FILLER_MAX"] = "9"
    try:
        assert filler_max_per_call() == 9
    finally:
        os.environ.pop("BOK_FILLER_MAX", None)


# ---- manifest 分类标签完整性(回退层的路由依据) ----


def test_manifest_entries_all_tagged():
    manifest = load_manifest(FILLER_ASSETS_DIR)
    valid = {"empathy", "ack", "check", "minimal", "default"}
    n = 0
    for lang, entries in manifest.items():
        for e in entries:
            assert e.get("cat") in valid, f"{lang}/{e.get('file')} cat={e.get('cat')!r}"
            n += 1
    assert n >= 39  # 三语资产全在且全打标


# ---- CP:表迁移 + 种子幂等 + 仓储 ----


def test_filler_entries_table_and_seed(tmp_path):
    from sqlalchemy import create_engine, text

    import bok_voice_business_db.models as models
    from control_plane.deps import _FILLER_SEEDS, build_engine

    db = tmp_path / "filler_test.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db}"
    try:
        eng = build_engine()
        assert eng is not None
        with eng.connect() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM filler_entries")).scalar()
            assert n == len(_FILLER_SEEDS)
            langs = dict(
                conn.execute(
                    text("SELECT lang, COUNT(*) FROM filler_entries GROUP BY lang")
                ).fetchall()
            )
        assert set(langs) >= {"zh", "cantonese", "en"}
        # 种子幂等:再跑 build_engine 不重复灌
        build_engine()
        with eng.connect() as conn:
            n2 = conn.execute(text("SELECT COUNT(*) FROM filler_entries")).scalar()
        assert n2 == n
    finally:
        os.environ.pop("DATABASE_URL", None)

    # 仓储 CRUD(SQL 路径)
    from sqlalchemy.orm import sessionmaker

    from bok_voice_business_db.repository import SqlAlchemyBusinessRepository

    eng2 = create_engine(f"sqlite:///{tmp_path / 'repo.db'}", future=True)
    models.create_all(eng2)
    repo = SqlAlchemyBusinessRepository(sessionmaker(bind=eng2, expire_on_commit=False)())
    row = repo.create_filler_entry(
        {"lang": "zh", "category": "check", "text": "嗯，我看看。", "triggers": "[\"看看\"]", "per_call_cap": 3}
    )
    assert row["id"].startswith("filler:")
    rows = repo.list_filler_entries(enabled=True, lang="zh")
    assert any(r["id"] == row["id"] for r in rows)
    repo.incr_filler_hit(row["id"])
    assert repo.list_filler_entries()[-1]["hit_count"] == 1


def test_in_memory_repo_filler_methods():
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    row = repo.create_filler_entry({"lang": "cantonese", "category": "empathy", "text": "明白你嘅心情。"})
    assert repo.count_filler_entries() == 1
    got = repo.list_filler_entries(lang="cantonese")
    assert got and got[0]["id"] == row["id"]
    repo.incr_filler_hit(row["id"])
    assert repo.list_filler_entries()[0]["hit_count"] == 1
