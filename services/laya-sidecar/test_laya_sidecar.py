"""laya-sidecar 离线单测（零 laya-mlx 依赖：模型/tokenizer 全 mock）。

覆盖契约面：截断保头纪律（LYA-EVAL 实锤坑：上游 1024 硬顶静默截尾）、
below_floor 计算、choice-only 请求级 400、kill-switch 503、批级失败逐问隔离、
/health 形状。真栈延迟/概率分布验收归实弹（curl /v1/decide），不在本文件。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent))

import app as laya_app  # noqa: E402
from app import (  # noqa: E402
    LayaService,
    resolve_model_dir,
    truncate_state_head,
)


class FakeTok:
    """每字符一 token 的假 tokenizer（数数精确、decode 逐字还原）。"""

    def __call__(self, text: str, add_special_tokens: bool = False) -> dict:
        return {"input_ids": [ord(c) for c in text]}

    class _Backend:
        @staticmethod
        def decode(ids: list[int], skip_special_tokens: bool = True) -> str:
            return "".join(chr(i) for i in ids)

    backend = _Backend()


class FakeAgent:
    """predict 假体：batch_size>1 可配置炸批，逐问重试按 qid 黑名单炸。"""

    def __init__(self, batch_boom: bool = False, bad_qids: tuple[str, ...] = ()):
        self.batch_boom = batch_boom
        self.bad_qids = bad_qids
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def predict(self, state: str, questions: dict) -> dict:
        self.calls.append((state, tuple(questions)))
        if self.batch_boom and len(questions) > 1:
            raise RuntimeError("batch boom")
        answers = {}
        for qid, q in questions.items():
            if qid in self.bad_qids:
                raise ValueError(f"question {qid!r} has too many options for the token budget")
            labels = q["criteria"]
            answers[qid] = {
                "choice": labels[0],
                "probabilities": {lab: round(1.0 / len(labels), 4) for lab in labels},
                "confidence": 0.91,
            }
        return {"answers": answers}


def _svc(agent: FakeAgent) -> LayaService:
    svc = LayaService()
    svc._agent = agent
    svc._tok = FakeTok()
    svc._load_error = None
    return svc


def _decide(svc: LayaService, payload: dict) -> dict:
    return svc.decide(payload)


def _count(text: str) -> list[int]:
    return FakeTok()(text)["input_ids"]


def _decode(ids: list[int]) -> str:
    return FakeTok.backend.decode(ids)


# ---------------------------------------------------------------- 截断保头纪律

def test_truncate_over_budget_keeps_head_and_flags():
    state = "前缀很重要_" + "尾" * 50
    text, n, truncated = truncate_state_head(state, _count, _decode, 10)
    assert truncated is True
    assert n == len(state)          # 原文真实 token 数恒报
    assert len(text) == 10          # 实际喂模型 = 预算内保头
    assert text == state[:10]
    assert "前缀" in text           # 保头：头部的关键上下文必须活着


def test_truncate_within_budget_passthrough():
    state = "short"
    text, n, truncated = truncate_state_head(state, _count, _decode, 800)
    assert (text, n, truncated) == (state, 5, False)


def test_truncate_exact_boundary_not_flagged():
    state = "x" * 800
    _text, n, truncated = truncate_state_head(state, _count, _decode, 800)
    assert (n, truncated) == (800, False)


def test_decide_reports_truncation_and_feeds_head(monkeypatch):
    monkeypatch.delenv("BOK_LAYA_JUDGE", raising=False)
    agent = FakeAgent()
    svc = _svc(agent)
    long_state = "重要头。" + "冗" * 1200
    out = _decide(svc, {
        "state": long_state,
        "questions": {"q1": {"type": "choice", "instructions": "i", "criteria": ["A", "B"]}},
    })
    assert out["state_truncated"] is True
    assert out["state_tokens"] == len(long_state)
    fed, _qids = agent.calls[0]
    assert len(fed) == 800 and fed.startswith("重要头。")


# ---------------------------------------------------------------- below_floor

def test_below_floor_matches_floor():
    mapped = LayaService._map_answer({"choice": "A", "probabilities": {"A": 0.9}, "confidence": 0.71}, 0.5)
    assert mapped == {"choice": "A", "probabilities": {"A": 0.9}, "confidence": 0.71, "below_floor": False}
    low = LayaService._map_answer({"choice": "B", "probabilities": {"B": 0.4}, "confidence": 0.38}, 0.5)
    assert low["below_floor"] is True


def test_below_floor_boundary_is_strict_less_than():
    at = LayaService._map_answer({"choice": "A", "probabilities": {}, "confidence": 0.5}, 0.5)
    assert at["below_floor"] is False


def test_below_floor_missing_confidence_is_zero():
    mapped = LayaService._map_answer({"choice": "A"}, 0.5)
    assert mapped["confidence"] == 0.0 and mapped["below_floor"] is True


# ------------------------------------------------------------- choice-only 400

@pytest.mark.parametrize("bad_type", ["score", "noul", "", "CHOICE"])
def test_non_choice_type_is_request_level_400(bad_type, monkeypatch):
    monkeypatch.delenv("BOK_LAYA_JUDGE", raising=False)
    svc = _svc(FakeAgent())
    with pytest.raises(HTTPException) as ei:
        _decide(svc, {
            "state": "s",
            "questions": {"ok": {"type": "choice", "instructions": "i", "criteria": ["A"]},
                          "bad": {"type": bad_type, "instructions": "i", "criteria": ["1", "2", "3"]}},
        })
    assert ei.value.status_code == 400
    assert "choice" in ei.value.detail and "LAYA-EVAL" in ei.value.detail


def test_non_dict_question_is_400(monkeypatch):
    monkeypatch.delenv("BOK_LAYA_JUDGE", raising=False)
    svc = _svc(FakeAgent())
    with pytest.raises(HTTPException) as ei:
        _decide(svc, {"state": "s", "questions": {"bad": "choice"}})
    assert ei.value.status_code == 400


# ------------------------------------------------------------ 请求形状/kills

def test_state_and_questions_shape_400(monkeypatch):
    monkeypatch.delenv("BOK_LAYA_JUDGE", raising=False)
    svc = _svc(FakeAgent())
    with pytest.raises(HTTPException):
        _decide(svc, {"questions": {"q": {"type": "choice", "instructions": "i", "criteria": ["A"]}}})
    with pytest.raises(HTTPException):
        _decide(svc, {"state": "s"})
    with pytest.raises(HTTPException):
        _decide(svc, {"state": "s", "questions": {}})


def test_confidence_floor_non_number_400(monkeypatch):
    monkeypatch.delenv("BOK_LAYA_JUDGE", raising=False)
    svc = _svc(FakeAgent())
    with pytest.raises(HTTPException):
        _decide(svc, {"state": "s", "confidence_floor": "0.5",
                      "questions": {"q": {"type": "choice", "instructions": "i", "criteria": ["A"]}}})


def test_kill_switch_zero_is_503_but_health_stays(monkeypatch):
    monkeypatch.setenv("BOK_LAYA_JUDGE", "0")
    svc = _svc(FakeAgent())
    with pytest.raises(HTTPException) as ei:
        _decide(svc, {"state": "s", "questions": {"q": {"type": "choice", "instructions": "i", "criteria": ["A"]}}})
    assert ei.value.status_code == 503
    assert "BOK_LAYA_JUDGE" in ei.value.detail
    # 进程可起但不服务：health 照报（kill_switch 旗可见）。
    assert svc.health()["kill_switch"] is True
    monkeypatch.delenv("BOK_LAYA_JUDGE", raising=False)
    assert svc.health()["kill_switch"] is False


# ------------------------------------------------------------ 批与逐问隔离

def test_batch_answers_mapped_with_floor(monkeypatch):
    monkeypatch.delenv("BOK_LAYA_JUDGE", raising=False)
    agent = FakeAgent()
    svc = _svc(agent)
    out = _decide(svc, {
        "state": "对话原文",
        "confidence_floor": 0.5,
        "questions": {
            "a": {"type": "choice", "instructions": "i", "criteria": ["A", "B"]},
            "b": {"type": "choice", "instructions": "i", "criteria": ["X", "Y", "Z"]},
        },
    })
    assert set(out["answers"]) == {"a", "b"}
    assert all("error" not in ans for ans in out["answers"].values())
    assert out["answers"]["a"]["choice"] == "A" and out["answers"]["a"]["confidence"] == 0.91
    assert out["latency_ms"] >= 0
    # 多问一次 batch（LAYA-EVAL 实测 8 问 57ms 的前提）。
    assert len(agent.calls) == 1 and len(agent.calls[0][1]) == 2


def test_single_question_failure_does_not_sink_batch(monkeypatch):
    monkeypatch.delenv("BOK_LAYA_JUDGE", raising=False)
    agent = FakeAgent(bad_qids=("bad",))
    svc = _svc(agent)
    out = _decide(svc, {
        "state": "s",
        "questions": {
            "good": {"type": "choice", "instructions": "i", "criteria": ["A", "B"]},
            "bad": {"type": "choice", "instructions": "i", "criteria": ["A", "B"]},
        },
    })
    assert out["answers"]["good"]["choice"] == "A"
    assert "error" in out["answers"]["bad"]
    assert "too many options" in out["answers"]["bad"]["error"]


def test_batch_level_failure_falls_back_to_per_question(monkeypatch):
    monkeypatch.delenv("BOK_LAYA_JUDGE", raising=False)
    agent = FakeAgent(batch_boom=True)
    svc = _svc(agent)
    out = _decide(svc, {
        "state": "s",
        "questions": {
            "q1": {"type": "choice", "instructions": "i", "criteria": ["A", "B"]},
            "q2": {"type": "choice", "instructions": "i", "criteria": ["A", "B"]},
        },
    })
    assert out["answers"]["q1"]["choice"] == "A"
    assert out["answers"]["q2"]["choice"] == "A"
    assert len(agent.calls) == 3  # 1 次炸批 + 2 次逐问重试


def test_per_question_validation_error_keeps_batch(monkeypatch):
    monkeypatch.delenv("BOK_LAYA_JUDGE", raising=False)
    agent = FakeAgent()
    svc = _svc(agent)
    out = _decide(svc, {
        "state": "s",
        "questions": {
            "good": {"type": "choice", "instructions": "i", "criteria": ["A", "B"]},
            "noins": {"type": "choice", "criteria": ["A"]},
            "dup": {"type": "choice", "instructions": "i", "criteria": ["A", "A"]},
        },
    })
    assert out["answers"]["good"]["choice"] == "A"
    assert "instructions" in out["answers"]["noins"]["error"]
    assert "唯一" in out["answers"]["dup"]["error"]
    assert len(agent.calls) == 1  # 坏问没进批


# ---------------------------------------------------------------- /health 形状

def test_health_shape_keys():
    svc = LayaService()
    h = svc.health()
    assert {"ok", "model", "loaded_ms"} <= set(h)
    assert h["ok"] is False and h["loaded_ms"] is None  # 未加载诚实报


def test_health_not_ok_when_load_failed(monkeypatch):
    monkeypatch.setenv("LAYA_DISABLE_LOAD", "1")
    svc = LayaService()
    svc.load()
    h = svc.health()
    assert h["ok"] is False
    assert h["load_error"]  # 人话在场
    assert h["loaded_ms"] is None


def test_resolve_model_dir_prefers_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LAYA_MODEL_DIR", str(tmp_path / "ckpt"))
    assert resolve_model_dir() == tmp_path / "ckpt"
    monkeypatch.delenv("LAYA_MODEL_DIR", raising=False)
    d = resolve_model_dir()
    assert d.name == "aac6fef--laya-multilingual-mlx" and d.parent.name == "models"


def test_checkpoint_predicate_rejects_incomplete_dir(tmp_path):
    (tmp_path / "model.safetensors").write_text("x")
    assert laya_app._is_checkpoint(tmp_path) is False
    (tmp_path / "rl_agent_config.json").write_text("{}")
    (tmp_path / "encoder").mkdir()
    (tmp_path / "encoder" / "config.json").write_text("{}")
    (tmp_path / "tokenizer").mkdir()
    (tmp_path / "tokenizer" / "tokenizer.json").write_text("{}")
    assert laya_app._is_checkpoint(tmp_path) is True
