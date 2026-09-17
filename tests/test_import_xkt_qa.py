"""惜客通导入纯函数:&拆分/簇指针/语言启发/去重/停用保留(spec §6)。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import import_xkt_qa as imp  # noqa: E402


def _row(q: str, answer: str = "答", status: int = 1, **kw):
    base = {"Question": q, "Answer": answer, "Status": status, "Id": 1}
    base.update(kw)
    return base


def test_split_variants_and_cluster_pointer():
    creates, report = imp.plan_import(
        [_row("怎么查物流&物流咋查&点样查物流")], existing=set(), lang_mode="auto",
    )
    assert len(creates) == 3
    head = creates[0]
    assert head["cluster_head_id"] == "" and "_head_question" not in head
    assert creates[1]["question_text"] == "物流咋查"
    assert creates[1]["_head_question"] == "怎么查物流"  # apply 阶段解析为主条目真实 id
    assert creates[2]["_head_question"] == "怎么查物流"
    assert report["variants"] == 2 and report["created"] == 3


def test_lang_heuristic():
    creates, _ = imp.plan_import([_row("唔該問下佢&唔該")], existing=set(), lang_mode="auto")
    assert all(c["lang"] == "cantonese" for c in creates)
    creates2, _ = imp.plan_import([_row("你好&请问")], existing=set(), lang_mode="auto")
    assert all(c["lang"] == "zh" for c in creates2)


def test_lang_override_and_disabled_and_dedup():
    creates, report = imp.plan_import(
        [_row("你好", status=0)],
        existing={"你好"}, lang_mode="en",
    )
    assert creates == [] and report["skipped_dup"] == 1  # 去重优先
    creates2, report2 = imp.plan_import([_row("新问法", status=0)], existing=set(), lang_mode="en")
    assert creates2[0]["lang"] == "en" and creates2[0]["enabled"] is False
    assert report2["disabled"] == 1


def test_multi_answers_counted_not_imported():
    row = _row("问法一")
    row["Answer2"] = "备选答案"
    creates, report = imp.plan_import([row], existing=set(), lang_mode="zh")
    assert len(creates) == 1
    assert report["alt_answers_seen"] == 1
