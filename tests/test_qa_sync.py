"""QA 自动学习闭环(mine_qa --sync)单测:质量闸矩阵 + plan_sync + main 冒烟。

设计立场:保守自动+人工否决——错答案一旦罐头化就是复读机,闸必须严;
闸外条目打印给人看,人可用 DELETE /api/qa-entries/{id} 否决。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in ("packages/core", "packages/business-db"):
    _path = str(ROOT / _p)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from bok_voice_core.qa_text import (  # noqa: E402
    AUTO_MAX_Q_LEN,
    AUTO_MIN_CALLS,
    AUTO_MIN_Q_LEN,
    AUTO_MIN_VOTE_RATIO,
    auto_apply_verdict,
    normalize_question,
)


def _pair(question="你哋幾時可以送到", calls=6, votes=None, lang="cantonese", answer="一般三至五日到。"):
    """mine_qa_pairs 报告行形状(question 已归一,votes 缺省=全票)。"""
    return {
        "lang": lang,
        "question": question,
        "calls": calls,
        "answer": answer,
        "answer_votes": calls if votes is None else votes,
    }


# ---- 纯函数质量闸:全闸矩阵 ----

def test_gate_passes_clean_entry():
    ok, reason = auto_apply_verdict(_pair())
    assert ok is True
    assert reason == ""


def test_gate_rejects_low_calls():
    ok, reason = auto_apply_verdict(_pair(calls=AUTO_MIN_CALLS - 1))
    assert (ok, reason) == (False, "low_calls")


def test_gate_rejects_unstable_answer_and_boundary():
    ok, reason = auto_apply_verdict(_pair(calls=10, votes=7))  # 0.7 < 0.8
    assert (ok, reason) == (False, "unstable_answer")
    ok2, reason2 = auto_apply_verdict(_pair(calls=10, votes=8))  # 恰 0.8 过
    assert ok2 is True
    assert reason2 == ""


def test_gate_rejects_question_length_and_boundary():
    ok_short, r_short = auto_apply_verdict(_pair(question="幾時到"))  # 3 字
    assert (ok_short, r_short) == (False, "question_length")
    ok_long, r_long = auto_apply_verdict(_pair(question="這" * (AUTO_MAX_Q_LEN + 1)))
    assert (ok_long, r_long) == (False, "question_length")
    assert auto_apply_verdict(_pair(question="幾" * AUTO_MIN_Q_LEN))[0] is True
    assert auto_apply_verdict(_pair(question="這" * AUTO_MAX_Q_LEN))[0] is True


def test_gate_rejects_digit_runs():
    ok, reason = auto_apply_verdict(_pair(question="我的單號是1234到了沒"))
    assert (ok, reason) == (False, "digits")
    # 3 位数字不拦(长度与内容都干净)
    assert auto_apply_verdict(_pair(question="三天內到嗎123"))[0] is True


def test_gate_rejects_cjk_digit_runs():
    """中文数字与 ASCII 同口径:运行时旁路归一中文数字,「三七七八九零」类
    问句永远走不到快路,入库即死重(审查 #5)。"""
    ok, reason = auto_apply_verdict(_pair(question="我個單號係三七七八九零"))
    assert (ok, reason) == (False, "digits")
    # 少于 4 位的中文数字不拦
    assert auto_apply_verdict(_pair(question="三日前送到嗎"))[0] is True


def test_gate_rejects_duplicate_against_existing():
    existing = {normalize_question("你哋幾時可以送到!")}  # 带标点,归一后同键
    ok, reason = auto_apply_verdict(_pair(), existing)
    assert (ok, reason) == (False, "duplicate")
    # existing=None → 不查重,同条过闸
    ok2, _ = auto_apply_verdict(_pair(), None)
    assert ok2 is True


# ---- plan_sync(--sync 主循环的可测核心) ----

def _load_mine_qa():
    spec = importlib.util.spec_from_file_location("mine_qa_under_test", ROOT / "scripts" / "mine_qa.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_plan_sync_splits_auto_and_skipped_with_reasons():
    mod = _load_mine_qa()
    pairs = [
        _pair(question="你哋幾時可以送到"),
        _pair(question="幾時到", calls=6),  # 3 字
        _pair(question="我的單號是1234了沒", calls=6),  # 4 位数字
        _pair(question="運費幾多錢", calls=4),  # 不足 5 通
    ]
    existing = [{"question_text": "你哋幾時可以送到!", "lang": "cantonese"}]
    auto, skipped = mod.plan_sync(pairs, existing)
    assert auto == []
    reasons = {p["question"]: reason for p, reason in skipped}
    assert reasons == {
        "你哋幾時可以送到": "duplicate",
        "幾時到": "question_length",
        "我的單號是1234了沒": "digits",
        "運費幾多錢": "low_calls",
    }


def test_plan_sync_collects_auto_entries():
    mod = _load_mine_qa()
    good = _pair(question="可唔可以退換貨", calls=8)
    auto, skipped = mod.plan_sync([good, _pair(question="好的")], [])
    assert auto == [good]
    assert [reason for _p, reason in skipped] == ["question_length"]


# ---- main() 冒烟(fake CP,不落网) ----

def test_main_sync_dry_run_prints_lists_writes_nothing(monkeypatch, capsys):
    mod = _load_mine_qa()
    rows = [_pair(question="可唔可以退換貨", calls=8), _pair(question="幾時到", calls=9)]
    seen: list[tuple[str, str]] = []

    def fake_cp(base, path, token, *, method="GET", payload=None):
        seen.append((method, path))
        if path.startswith("/api/reports/qa-pairs"):
            assert "exclude_test=true" in path, "挖掘报告默认滤测试对象"
            return rows
        assert path.startswith("/api/qa-entries") and method == "GET"
        return [{"question_text": "已有問題", "lang": "zh"}]

    monkeypatch.setattr(mod, "_cp_request", fake_cp)
    rc = mod.main(["--sync", "--dry-run", "--cp", "http://cp.test"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "可唔可以退換貨" in out, "auto 列表要打印给人看"
    assert "question_length" in out, "skip 摘要带原因码"
    assert all(method != "POST" for method, _path in seen), "dry-run 不得写库"


def test_main_sync_posts_payload_and_runs_pregen(monkeypatch, capsys):
    mod = _load_mine_qa()
    rows = [_pair(question="可唔可以退換貨", calls=8), _pair(question="幾時到", calls=9)]
    posted: list[dict] = []
    spawned: list[list[str]] = []

    def fake_cp(base, path, token, *, method="GET", payload=None):
        if path.startswith("/api/reports/qa-pairs"):
            return rows
        if method == "POST":
            posted.append(payload)
            return {"id": "qa-new"}
        return []

    class FakeProc:
        returncode = 0

    monkeypatch.setattr(mod, "_cp_request", fake_cp)
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: spawned.append(cmd) or FakeProc())
    rc = mod.main(["--sync", "--cp", "http://cp.test", "--account", "acc-9"])
    out = capsys.readouterr().out
    assert rc == 0
    assert len(posted) == 1, "只有过闸条目入库"
    payload = posted[0]
    assert payload["source"] == "mined"
    assert payload["scope"] == "global"
    assert payload["enabled"] is True
    assert payload["account_id"] == "acc-9"
    assert payload["question_text"] == "可唔可以退換貨"
    assert payload["lang"] == "cantonese"
    assert "入库 1 条" in out
    assert len(spawned) == 1
    assert spawned[0][1].endswith("pregen_tts.py") and "--qa" in spawned[0], "入库后按语言物化 TTS"


def test_main_sync_pregen_failure_still_exits_zero(monkeypatch, capsys):
    """缺 MiniMax key 时 pregen 自报错(returncode 1)——入库成功不算失败。"""
    mod = _load_mine_qa()

    def fake_cp(base, path, token, *, method="GET", payload=None):
        if path.startswith("/api/reports/qa-pairs"):
            return [_pair(question="可唔可以退換貨", calls=8)]
        if method == "POST":
            return {"id": "qa-new"}
        return []

    class FakeProc:
        returncode = 1

    monkeypatch.setattr(mod, "_cp_request", fake_cp)
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: FakeProc())
    rc = mod.main(["--sync", "--cp", "http://cp.test"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "tts-pregen" in out, "打印物化指引"


def test_main_rejects_sync_and_apply_together():
    mod = _load_mine_qa()
    assert mod.main(["--sync", "--apply", "3", "--cp", "http://cp.test"]) == 2
