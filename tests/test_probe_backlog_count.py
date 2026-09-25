"""probe_interp_backlog.py count_drops 扩列测试（fix round 1，评审 I-2）。

背景：M-31 摘译路径日志是 `INTERP_BACKLOG source-skip xN`（无 `drop=` 字样），
旧 count_drops 只数 `drop=1` 行 → 摘译不进探针判据，「T5 低门槛腿 drop≥1 可复现」
的报告断言对现探针不成立。扩列后：drops（译文弃句）与 skipped（源句摘译）分开
计数，require_drop 判据取并集。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import probe_interp_backlog as probe  # noqa: E402


def _write_log(tmp_path, lines: list[str]) -> Path:
    log = tmp_path / "interp-fwd.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log


def test_count_drops_counts_say_drop_and_source_skip_separately(tmp_path, monkeypatch):
    log = _write_log(tmp_path, [
        "[interp] room=call-x listen=me-call-x",           # 锚（首次出现 call_id）
        "[interp] INTERP_BACKLOG depth=4 est_ms=5200 drop=2 total_dropped=2 src_pending_s=0 src_skips=0",
        "[interp] INTERP_BACKLOG source-skip x1 (12 chars, backlog gate 摘译保文)",
        "[interp] INTERP_BACKLOG depth=3 est_ms=4400 drop=1 total_dropped=3 src_pending_s=1600 src_skips=1",
        "noise line without markers",
    ])
    monkeypatch.setattr(probe, "LOG_PATH", log)
    events, drops, skips = probe.count_drops("call-x")
    # 行数口径（与旧 count_drops 一致）：drop=1 行 1 条、source-skip 行 1 条
    assert (events, drops, skips) == (3, 1, 1)


def test_count_drops_no_markers_zero(tmp_path, monkeypatch):
    log = _write_log(tmp_path, [
        "[interp] room=call-y listen=me-call-y",
        "[interp] mt empty for 8 chars, skipped",
        "[interp] INTERP_BACKLOG depth=2 est_ms=1600 drop=0 total_dropped=0 src_pending_s=0 src_skips=0",
    ])
    monkeypatch.setattr(probe, "LOG_PATH", log)
    assert probe.count_drops("call-y") == (1, 0, 0)  # drop=0 行不算弃句


def test_count_drops_missing_call_zero(tmp_path, monkeypatch):
    log = _write_log(tmp_path, ["[interp] INTERP_BACKLOG depth=4 drop=1 total_dropped=1"])
    monkeypatch.setattr(probe, "LOG_PATH", log)
    assert probe.count_drops("absent-call") == (0, 0, 0)
