"""QA 快路每通汇总打点单测(task-9):format_qa_summary 纯函数 + QA_COUNTERS 计数语义。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))
sys.path.insert(0, str(ROOT / "packages" / "core"))

import agent_runtime.agent as ag  # noqa: E402
from agent_runtime.agent import QA_COUNTERS, format_qa_summary  # noqa: E402

_ZERO_LINE = (
    "QA_FASTPATH_SUMMARY hit=0 hit_audio=0 no_audio=0 match0=0 "
    "bypass digits=0 refuse=0 wa=0 advanced=0 verdict=0 disabled=0"
)


@pytest.fixture(autouse=True)
def _reset_qa_counters():
    QA_COUNTERS.clear()
    yield
    QA_COUNTERS.clear()


# ---- format_qa_summary 纯函数 ----

def test_format_empty_dict_all_zero_columns():
    """空 dict:所有列打零值,列固定(此断言同时钉死列顺序)。"""
    assert format_qa_summary({}) == _ZERO_LINE


def test_format_all_zero_dict_same_as_empty():
    full = {k: 0 for k in (
        "hit", "hit_audio", "no_audio", "match0",
        "bypass_digits", "bypass_refuse", "bypass_wa",
        "bypass_advanced", "bypass_verdict", "disabled",
    )}
    assert format_qa_summary(full) == _ZERO_LINE


def test_format_mixed_values_and_hit_audio_delta():
    counters = {
        "hit": 3, "no_audio": 1, "match0": 7,
        "bypass_digits": 2, "bypass_refuse": 1, "bypass_wa": 4,
        "bypass_advanced": 1, "bypass_verdict": 2, "disabled": 0,
    }
    # hit_audio = hit - no_audio 在格式化时求差(命中条目轮 3,其中 1 轮无音频)
    assert format_qa_summary(counters) == (
        "QA_FASTPATH_SUMMARY hit=3 hit_audio=2 no_audio=1 match0=7 "
        "bypass digits=2 refuse=1 wa=4 advanced=1 verdict=2 disabled=0"
    )


def test_format_sparse_dict_missing_keys_default_zero():
    assert format_qa_summary({"hit": 2}) == (
        "QA_FASTPATH_SUMMARY hit=2 hit_audio=2 no_audio=0 match0=0 "
        "bypass digits=0 refuse=0 wa=0 advanced=0 verdict=0 disabled=0"
    )


def test_format_ignores_unknown_keys_and_dirty_values():
    counters = {"hit": "x", "unknown_key": 5, "no_audio": None}
    out = format_qa_summary(counters)
    assert out == _ZERO_LINE


def test_format_column_order_is_fixed():
    """无论值如何,token 顺序恒定:hit/hit_audio/no_audio/match0/bypass(...)/disabled。"""
    counters = {k: 9 for k in (
        "hit", "no_audio", "match0", "bypass_digits", "bypass_refuse",
        "bypass_wa", "bypass_advanced", "bypass_verdict", "disabled",
    )}
    out = format_qa_summary(counters)
    assert out.startswith("QA_FASTPATH_SUMMARY hit=")
    order = re.findall(r"(hit|hit_audio|no_audio|match0|digits|refuse|wa|advanced|verdict|disabled)=", out)
    assert order == [
        "hit", "hit_audio", "no_audio", "match0",
        "digits", "refuse", "wa", "advanced", "verdict", "disabled",
    ]


def test_format_does_not_mutate_counters():
    counters = {"hit": 2, "no_audio": 1, "match0": 3, "bypass_wa": 1}
    snapshot = dict(counters)
    format_qa_summary(counters)
    assert counters == snapshot


def test_format_does_not_clear_counters():
    """零不清零在函数外管:format 后计数原样保留。"""
    QA_COUNTERS["hit"] = 1
    format_qa_summary(QA_COUNTERS)
    assert QA_COUNTERS == {"hit": 1}


# ---- QA_COUNTERS 计数语义(模块级 helper) ----

def test_qa_counters_is_module_level_dict():
    assert isinstance(QA_COUNTERS, dict)


def test_qa_bump_increments_and_defaults():
    ag._qa_bump("hit")
    ag._qa_bump("hit")
    ag._qa_bump("no_audio")
    assert QA_COUNTERS == {"hit": 2, "no_audio": 1}


def test_qa_bump_swallows_poisoned_dict():
    """打点绝不毒化通话:dict 被换成坏值也只吞异常。"""
    ag._qa_bump("hit")
    QA_COUNTERS["hit"] = None  # 脏值
    ag._qa_bump("hit")  # 不得抛
    assert "hit" in QA_COUNTERS


def test_bypass_reason_mapping():
    assert ag._QA_BYPASS_KEY == {
        "digits": "bypass_digits",
        "refuse": "bypass_refuse",
        "wa_signal": "bypass_wa",
        "wa_step_locked": "bypass_wa",
        "advanced": "bypass_advanced",
        "verdict": "bypass_verdict",
    }


def test_qa_bump_bypass_counts_by_reason():
    for reason in ("digits", "refuse", "wa_signal", "wa_step_locked", "advanced", "verdict"):
        ag._qa_bump_bypass(reason)
    assert QA_COUNTERS == {
        "bypass_digits": 1,
        "bypass_refuse": 1,
        "bypass_wa": 2,  # wa_signal + wa_step_locked 并入一格
        "bypass_advanced": 1,
        "bypass_verdict": 1,
    }


def test_qa_bump_bypass_unknown_reason_not_counted():
    """closing 等映射外 reason 不计任何列(汇总列固定,防列语义漂移)。"""
    ag._qa_bump_bypass("closing")
    ag._qa_bump_bypass("")
    ag._qa_bump_bypass(None)
    assert QA_COUNTERS == {}


def test_disabled_semantics_single_bump_per_call():
    """disabled 每通至多一次(装配点判定,非轮级)。"""
    ag._qa_bump("disabled")
    assert format_qa_summary(QA_COUNTERS).endswith("disabled=1")


# ---- 读点归一:agent 走 qa_gate.qa_fastpath_enabled() ----

def test_agent_uses_qa_gate_enabled_reader(monkeypatch):
    assert ag.qa_fastpath_enabled is ag.__dict__["qa_fastpath_enabled"]
    monkeypatch.setenv("BOK_QA_FASTPATH", "0")
    assert ag.qa_fastpath_enabled() is False
    monkeypatch.setenv("BOK_QA_FASTPATH", "1")
    assert ag.qa_fastpath_enabled() is True
