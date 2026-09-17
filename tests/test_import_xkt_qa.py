"""惜客通导入纯函数:&拆分/簇指针/语言启发/去重/停用保留(spec §6)。"""
from __future__ import annotations

import json
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


def test_inbatch_dedup_head_and_variant():
    # 同批重复归一化问法只建先到一条(终审 must-fix:qa_gate 0.90 字面匹配下
    # 同文双条双双满分,命中播哪条由遍历序决定)。主条目撞主条目 + 变体撞主条目两形态。
    creates, report = imp.plan_import(
        [
            _row("怎么退款", answer="A"),
            _row("怎么 退款？", answer="B"),  # 归一化后与首行同文
            _row("别的问法&怎么退款", answer="C"),  # 变体撞已建主条目
        ],
        existing=set(), lang_mode="zh",
    )
    assert len(creates) == 2
    assert creates[0]["question_text"] == "怎么退款"
    assert creates[0]["answer_text"] == "A"  # 先到先建
    assert creates[1]["question_text"] == "别的问法"
    assert report["skipped_inbatch"] == 2 and report["skipped_dup"] == 0


def test_apply_continues_after_failure_and_head_falls_back(monkeypatch, tmp_path, capsys):
    # apply 容错:单条 POST 失败打印问法+错误并继续;head 失败其变体回落独立条目。
    data = tmp_path / "xkt.json"
    data.write_text(json.dumps([
        _row("坏头&变体跟随", answer="A"),
        _row("好头", answer="B"),
    ]), encoding="utf-8")
    posted: list[dict] = []

    def fake_request(base, path, token, *, method="GET", payload=None):
        if method == "GET":  # existing 拉取:空库
            return []
        posted.append(dict(payload))
        if payload["question_text"] == "坏头":
            raise RuntimeError("boom")
        return {"id": f"id-{len(posted)}"}

    monkeypatch.setattr(imp, "_cp_request", fake_request)
    monkeypatch.setattr(sys, "argv", ["import_xkt_qa.py", "--input", str(data), "--apply"])
    assert imp.main() == 0
    out = capsys.readouterr().out
    variant = next(p for p in posted if p.get("question_text") == "变体跟随")
    assert variant["cluster_head_id"] == "" and "_head_question" not in variant
    assert any(p["question_text"] == "好头" for p in posted)  # 失败后整批继续
    assert "[失败] 坏头" in out and "失败 1 条" in out
