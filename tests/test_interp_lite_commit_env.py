"""薄线句档提交缺省（W6×interp-lite 合流刀1）三态钉死——2026-10-09。

优先序=运营显式 env > 薄线句档（999/20/2.0）> B 档碎片缺省（6/8/1.0）。
语义依据：docs/superpowers/plans/2026-10-09-bline-fluency.md（病=碎片化串行）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import bok  # noqa: E402

servers = bok.servers
paths = bok.paths

_KEYS = (
    "QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS",
    "QWEN3_ASR_CLAUSE_LEN_CHARS",
    "QWEN3_ASR_COMMIT_MIN_INTERVAL_S",
)
_FRAGMENT_BASE = {"QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS": "6",
                  "QWEN3_ASR_CLAUSE_LEN_CHARS": "8",
                  "QWEN3_ASR_COMMIT_MIN_INTERVAL_S": "1.0"}


def _clean(monkeypatch):
    for k in (*_KEYS, "BOK_INTERP_LITE"):
        monkeypatch.delenv(k, raising=False)


def test_pure_overrides_fragment_defaults(monkeypatch):
    """句档硬覆盖 B 档碎片缺省（_interp_env 已 setdefault 6/8/1.0 进 base——
    setdefault 在这里是死路，必须硬覆盖）；入参 dict 不被 mutate。"""
    _clean(monkeypatch)
    base = dict(_FRAGMENT_BASE)
    out = servers.interp_lite_commit_env(base)
    assert out["QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS"] == "999"  # 逗号档结构性关闭
    assert out["QWEN3_ASR_CLAUSE_LEN_CHARS"] == "30"  # 无标点保险丝（实弹定档 2026-10-09）
    assert out["QWEN3_ASR_COMMIT_MIN_INTERVAL_S"] == "2.0"  # 句档限速
    assert base == _FRAGMENT_BASE  # 入参零 mutate


def test_pure_operator_explicit_wins(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("QWEN3_ASR_CLAUSE_LEN_CHARS", "12")
    out = servers.interp_lite_commit_env(dict(_FRAGMENT_BASE))
    assert out["QWEN3_ASR_CLAUSE_LEN_CHARS"] == "12"  # 运营显式最高
    assert out["QWEN3_ASR_COMMIT_MIN_INTERVAL_S"] == "2.0"  # 未显式的仍句档


def test_worker_specs_lite_wiring(monkeypatch):
    """BOK_INTERP_LITE=1：interp spec 换薄线入口 + env 吃到句档三键。"""
    _clean(monkeypatch)
    monkeypatch.setenv("BOK_INTERP_LITE", "1")
    specs = servers._worker_specs(paths.repo_python())
    interp = [s for s in specs if s["name"] == "interp-fwd"]
    assert interp, "interp spec 缺席"
    argv = interp[0]["argv"]
    assert "agent_runtime.interp_lite.worker" in argv
    e = interp[0]["env"]
    assert (e["QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS"],
            e["QWEN3_ASR_CLAUSE_LEN_CHARS"],
            e["QWEN3_ASR_COMMIT_MIN_INTERVAL_S"]) == ("999", "30", "2.0")


def test_worker_specs_old_line_untouched(monkeypatch):
    """开关关（缺省）：旧线入口逐字节 + B 档碎片缺省原样（A/旧线零变化）。"""
    _clean(monkeypatch)
    specs = servers._worker_specs(paths.repo_python())
    interp = [s for s in specs if s["name"] == "interp-fwd"]
    assert "agent_runtime.interpret" in interp[0]["argv"]
    e = interp[0]["env"]
    assert (e["QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS"],
            e["QWEN3_ASR_CLAUSE_LEN_CHARS"],
            e["QWEN3_ASR_COMMIT_MIN_INTERVAL_S"]) == ("6", "8", "1.0")
