"""probe_qa_hit --rotation-duel 的纯函数判定(Phase 3.2):折组+轮换+kill 三断言。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))


def test_rotation_duel_three_verdicts(monkeypatch):
    monkeypatch.setenv("BOK_DUEL_SENTINEL", "1")  # 证明 monkeypatch 生效(防环境假绿)
    monkeypatch.delenv("BOK_QA_ROTATION", raising=False)
    from probe_qa_hit import rotation_duel

    v = rotation_duel()
    assert v["fold"] == "head"    # ① 变体问法命中 → 折组到 head 代表出场
    assert v["zero"] == "head"    # ① 零账本 pick → 插入序首=head
    assert v["ledger"] == "v1"    # ② 账本 [head] → 本通最少播放者 v1
    assert v["kill"] == "v1"      # ③ BOK_QA_ROTATION=0 → 变体自己赢(旧档裸竞争者)
    # duel 自管 env,跑完不外泄(sentinel 仍在,变量被恢复为未设)
    assert os.environ.get("BOK_DUEL_SENTINEL") == "1"
    assert "BOK_QA_ROTATION" not in os.environ


def test_rotation_duel_restores_preexisting_env(monkeypatch):
    """duel 的 save/restore 双向:调用前已设的值(非缺省)跑完原样仍在。"""
    monkeypatch.setenv("BOK_QA_ROTATION", "1")
    from probe_qa_hit import rotation_duel

    assert rotation_duel()["kill"] == "v1"  # kill 腿确实压过 0
    assert os.environ.get("BOK_QA_ROTATION") == "1"  # 恢复而非清除
