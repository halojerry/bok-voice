"""probe_qa_hit --priority-duel 的纯函数判定(Phase 3.1):三档胜者对照。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))


def _duel():
    from probe_qa_hit import duel_verdicts

    return duel_verdicts()


def test_duel_three_verdicts(monkeypatch):
    monkeypatch.delenv("BOK_QA_PRIORITY", raising=False)
    v = _duel()
    # legacy 档(等价全默认):插入序老条目胜——与线上现状一致
    assert v["legacy"] == "old"
    # priority 档:pinned(1) 压过 old(10)
    assert v["priority"] == "pinned"
    # kill 档:回纯分数/插入序
    assert v["kill"] == "old"


def test_duel_missing_env_defaults_on(monkeypatch):
    monkeypatch.delenv("BOK_QA_PRIORITY", raising=False)
    from probe_qa_hit import duel_verdicts

    assert duel_verdicts()["priority"] == "pinned"
