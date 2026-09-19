"""probe_qa_hit --priority-duel 的纯函数判定(Phase 3.1):三档胜者对照。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))


def test_duel_three_verdicts(monkeypatch):
    monkeypatch.setenv("BOK_DUEL_SENTINEL", "1")  # 证明 monkeypatch 生效(防环境假绿)
    monkeypatch.delenv("BOK_QA_PRIORITY", raising=False)
    from probe_qa_hit import duel_verdicts

    v = duel_verdicts()
    assert v["legacy"] == "old"       # 无 priority 键=纯分数,高分胜(线上现状)
    assert v["priority"] == "pinned"  # prio 1 压过分数差(异问法双过关)
    assert v["kill"] == "old"         # BOK_QA_PRIORITY=0 回纯分数档
    # duel 自管 env,跑完不外泄(sentinel 仍在,变量被恢复)
    assert os.environ.get("BOK_DUEL_SENTINEL") == "1"
