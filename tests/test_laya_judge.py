"""Laya 决策旁路客户端（2026-09-26，docs/LAYA-EVAL.md 第一落位 intent judge）。

覆盖面：
- enabled 闸（BOK_LAYA_JUDGE 默认 "0"=零调用）与健康缓存（TTL/fail-open/归因）；
- decide_choice 单问封装：超时/异常/坏形一律 None=回落（fail-open 铁律）、
  below_floor 透传、NONE 自动补进候选面、未知 choice 拒收；
- build_intent_state：客户原话置头（LAYA-EVAL 截尾坑）、轮数裁剪、预算纪律；
- pick_intent_laya：off/silent/hit/abstain/unavailable 五判定 + 宽口径候选
  （纯关键词意图也入 choice 面）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

import agent_runtime.laya_judge as laya_mod  # noqa: E402
from agent_runtime.laya_judge import (  # noqa: E402
    DEFAULT_CONFIDENCE_FLOOR,
    LayaJudgeClient,
    build_intent_instructions,
    build_intent_state,
    decide_choice,
    laya_base_url,
    laya_judge_enabled,
    pick_intent_laya,
    recent_turn_pairs,
    reset_health_cache,
)
from bok_voice_core.flow_graph import parse_flow_graph  # noqa: E402

_ID_A = "int_1a2b3c4d"
_ID_B = "int_2b3c4d5e"
_BND_A = "bnd_7e8f9a0b"

_DIALECT_MARKS = "嘅唔係咗喇喺啲乜嘢畀睇嚟啩"


@pytest.fixture(autouse=True)
def _clean_health_cache():
    reset_health_cache()
    yield
    reset_health_cache()


@pytest.fixture(autouse=True)
def _laya_gate_on(monkeypatch):
    """pick 段需要真实 env 闸开（decide_choice 内层再查一次闸——双层保险）；
    闸行为测试自己 setenv/delenv 覆盖本 fixture（autouse 先于测试内 monkeypatch）。"""
    monkeypatch.setenv("BOK_LAYA_JUDGE", "1")


# ---- 假传输（子类覆写传输缝，计数不打真 HTTP）----


class _FakeSidecar(LayaJudgeClient):
    def __init__(
        self,
        *,
        health=(200, {"ok": True}),
        decide=None,
        health_exc: Exception | None = None,
        decide_exc: Exception | None = None,
        base_url: str = "http://127.0.0.1:8791",
    ) -> None:
        super().__init__(base_url)
        self.health = health
        self.decide = decide
        self.health_exc = health_exc
        self.decide_exc = decide_exc
        self.health_calls = 0
        self.decide_payloads: list[dict] = []
        self.decide_timeouts: list[float] = []

    async def _get_health(self):
        self.health_calls += 1
        if self.health_exc is not None:
            raise self.health_exc
        return self.health

    async def _post_decide(self, payload: dict, timeout_s: float):
        self.decide_payloads.append(payload)
        self.decide_timeouts.append(timeout_s)
        if self.decide_exc is not None:
            raise self.decide_exc
        return self.decide


def _decide_response(choice: str, conf: float, *, below: bool = False) -> dict:
    return {
        "answers": {
            "intent": {
                "choice": choice,
                "probabilities": {choice: conf, "NONE": round(1 - conf, 2)},
                "confidence": conf,
                "below_floor": below,
            }
        },
        "state_truncated": False,
        "latency_ms": 9.7,
    }


# ---- enabled 闸 / base_url ----


def test_gate_default_off_and_env_on(monkeypatch):
    monkeypatch.delenv("BOK_LAYA_JUDGE", raising=False)
    assert laya_judge_enabled() is False  # 默认 "0"=零变化的结构性保证
    monkeypatch.setenv("BOK_LAYA_JUDGE", "0")
    assert laya_judge_enabled() is False
    monkeypatch.setenv("BOK_LAYA_JUDGE", "1")
    assert laya_judge_enabled() is True


def test_gate_off_short_circuits_zero_transport(monkeypatch):
    """闸关=零调用（mock 传输断言 not called，fail-open 的最外层）。"""
    monkeypatch.delenv("BOK_LAYA_JUDGE", raising=False)
    fake = _FakeSidecar()
    assert asyncio.run(decide_choice("state", "instr", [_ID_A])) is None
    assert fake.health_calls == 0 and fake.decide_payloads == []


def test_base_url_default_and_env(monkeypatch):
    monkeypatch.delenv("BOK_LAYA_SIDECAR_URL", raising=False)
    assert laya_base_url() == "http://127.0.0.1:8791"
    monkeypatch.setenv("BOK_LAYA_SIDECAR_URL", "http://127.0.0.1:9999")
    assert laya_base_url() == "http://127.0.0.1:9999"


# ---- 健康缓存 fail-open ----


@pytest.mark.parametrize(
    ("health", "health_exc", "reason"),
    [
        ((200, {"ok": False}), None, "model_missing"),  # 模型缺位
        ((503, {"ok": False}), None, "http_status"),
        (None, ConnectionRefusedError("refused"), "connect"),
        (None, TimeoutError("slow"), "timeout"),
        (None, RuntimeError("boom"), "error"),
    ],
)
def test_health_failure_fails_open(health, health_exc, reason):
    """健康失败/ok=false → 本通禁用旁路：decide 恒 None（旧路逐字节）。"""
    fake = _FakeSidecar(health=health, health_exc=health_exc)
    ok, got = asyncio.run(fake.ensure_health())
    assert ok is False and got == reason
    assert asyncio.run(fake.decide_choice("s", "i", [_ID_A])) is None


def test_health_cache_ttl_and_shared_by_base_url(monkeypatch):
    """TTL 内零重探（60s 缓存）；按 base_url 分键共享；过期/清理后重探。"""
    monkeypatch.setenv("BOK_LAYA_JUDGE", "1")
    fake = _FakeSidecar()
    assert asyncio.run(fake.ensure_health()) == (True, "")
    other = _FakeSidecar()  # 同 base_url 的第二个客户端（worker 并发多通共享）
    assert asyncio.run(other.ensure_health()) == (True, "")
    assert fake.health_calls == 1 and other.health_calls == 0
    # TTL 过期（回拨 checked_at）→ 重探
    with laya_mod._HEALTH_LOCK:
        laya_mod._HEALTH[fake.base_url]["checked_at"] -= 61.0
    assert asyncio.run(other.ensure_health()) == (True, "")
    assert other.health_calls == 1
    reset_health_cache()
    assert asyncio.run(other.ensure_health()) == (True, "")
    assert other.health_calls == 2


def test_health_failure_also_cached(monkeypatch):
    """失败也入缓存：挂掉的 sidecar 不值得每轮重探（60s 内零 HTTP）。"""
    monkeypatch.setenv("BOK_LAYA_JUDGE", "1")
    fake = _FakeSidecar(health_exc=ConnectionRefusedError("refused"))
    assert asyncio.run(fake.ensure_health()) == (False, "connect")
    assert asyncio.run(fake.ensure_health()) == (False, "connect")
    assert fake.health_calls == 1


# ---- decide_choice：fail-open / below_floor 透传 / 候选面 ----


def test_decide_timeout_and_exception_return_none(monkeypatch):
    """任何异常/超时 → None=回落（fail-open 铁律：旁路挂了逐字节走旧路）。"""
    monkeypatch.setenv("BOK_LAYA_JUDGE", "1")
    for exc in (TimeoutError("slow"), RuntimeError("boom")):
        fake = _FakeSidecar(decide_exc=exc)
        assert asyncio.run(fake.decide_choice("s", "i", [_ID_A])) is None
        assert len(fake.decide_payloads) == 1  # 健康过了、判定挂了


@pytest.mark.parametrize(
    "decide",
    [
        None,  # 整体缺
        {},  # 非 dict answers
        {"answers": {}},  # qid 缺
        {"answers": {"intent": {}}},  # choice 空
        {"answers": {"intent": {"choice": "int_deadbeef", "confidence": 0.9}}},  # 未知 choice=坏形
        {"answers": {"intent": {"choice": _ID_A}}, "x": 1},  # confidence 缺 → 0.0
    ],
)
def test_decide_malformed_response_returns_none(monkeypatch, decide):
    """坏形响应一律 None（宁可回落 9B，绝不乱命中）。"""
    monkeypatch.setenv("BOK_LAYA_JUDGE", "1")
    fake = _FakeSidecar(decide=decide)
    assert asyncio.run(fake.decide_choice("s", "i", [_ID_A])) is None


def test_decide_hit_and_payload_contract(monkeypatch):
    """高置信命中透传；契约形状对账 sidecar：questions[qid] 带 type/instructions/criteria。"""
    monkeypatch.setenv("BOK_LAYA_JUDGE", "1")
    fake = _FakeSidecar(decide=_decide_response(_ID_A, 0.93))
    ans = asyncio.run(fake.decide_choice("客户原话", "判定说明", [_ID_A, _ID_B]))
    assert ans == {
        "choice": _ID_A,
        "confidence": 0.93,
        "below_floor": False,
        "probabilities": {_ID_A: 0.93, "NONE": 0.07},
        "state_truncated": False,
    }
    payload = fake.decide_payloads[0]
    assert payload["state"] == "客户原话"
    assert payload["confidence_floor"] == pytest.approx(DEFAULT_CONFIDENCE_FLOOR)
    q = payload["questions"]["intent"]
    assert q["type"] == "choice" and q["instructions"] == "判定说明"
    assert q["criteria"] == [_ID_A, _ID_B, "NONE"]  # NONE 自动补进候选面
    assert fake.decide_timeouts == [0.25]  # 默认预算：实测 9.7ms，250ms=25 倍余量


def test_decide_none_appended_once_and_empty_face_rejected(monkeypatch):
    monkeypatch.setenv("BOK_LAYA_JUDGE", "1")
    fake = _FakeSidecar(decide=_decide_response("NONE", 0.8))
    ans = asyncio.run(fake.decide_choice("s", "i", [_ID_A, "NONE"]))
    assert ans is not None and ans["choice"] == "NONE"
    assert fake.decide_payloads[0]["questions"]["intent"]["criteria"] == [_ID_A, "NONE"]
    # 空候选面=无判定语义 → 直接 None，零 HTTP
    empty = _FakeSidecar(decide=_decide_response(_ID_A, 0.9))
    assert asyncio.run(empty.decide_choice("s", "i", [])) is None
    assert empty.decide_payloads == []


def test_below_floor_passthrough_and_local_fallback(monkeypatch):
    """below_floor 透传（sidecar 旗优先）；缺席时按 confidence_floor 本地补算。"""
    monkeypatch.setenv("BOK_LAYA_JUDGE", "1")
    # 旗显式 true：即便置信高也回落
    fake = _FakeSidecar(decide=_decide_response(_ID_A, 0.99, below=True))
    ans = asyncio.run(fake.decide_choice("s", "i", [_ID_A]))
    assert ans is not None and ans["below_floor"] is True
    # 旗缺席：conf ≥ floor → False
    low_key = _decide_response(_ID_A, 0.9)
    del low_key["answers"]["intent"]["below_floor"]
    fake2 = _FakeSidecar(decide=low_key)
    assert asyncio.run(fake2.decide_choice("s", "i", [_ID_A]))["below_floor"] is False
    # 旗缺席：conf < floor → True（本地补算）
    low = _decide_response(_ID_A, 0.3)
    del low["answers"]["intent"]["below_floor"]
    fake3 = _FakeSidecar(decide=low)
    assert asyncio.run(fake3.decide_choice("s", "i", [_ID_A]))["below_floor"] is True
    # state_truncated 透传（响应级字段）
    trunc = _decide_response(_ID_A, 0.9)
    trunc["state_truncated"] = True
    fake4 = _FakeSidecar(decide=trunc)
    assert asyncio.run(fake4.decide_choice("s", "i", [_ID_A]))["state_truncated"] is True


# ---- state 组装（纯函数：原话置头 / 轮数裁剪 / 预算）----


def test_state_puts_utterance_at_head():
    """LAYA-EVAL 截尾坑：原话放尾部会被静默截没——必须在头。"""
    state = build_intent_state(
        user_text="你哋係咪呃人嘅",
        history=[("agent", "A" * 100), ("customer", "C" * 100)],
        step_1based=2,
        goal="核实下单平台",
    )
    assert state.startswith("客户原话：你哋係咪呃人嘅")
    assert state.index("你哋係咪呃人嘅") < state.index("核实下单平台") < state.index("C" * 20)


def test_state_trims_history_to_recent_turns():
    history = [
        ("agent", "旧一"), ("customer", "旧二"), ("agent", "旧三"),
        ("customer", "新四"), ("agent", "新五"),
    ]
    state = build_intent_state(user_text="原话", history=history, recent_turns=2)
    assert "新四" in state and "新五" in state
    assert "旧一" not in state and "旧二" not in state and "旧三" not in state
    # 输出序=新→旧（截尾丢弃方向=最旧先丢）
    assert state.index("新五") < state.index("新四")


def test_state_budget_never_exceeded():
    utt = "长" * 400
    history = [("agent", "回" * 120), ("customer", "客" * 120)] * 5
    state = build_intent_state(
        user_text=utt, history=history, recent_turns=10, max_chars=600
    )
    assert len(state) <= 600
    assert state.startswith("客户原话：" + "长" * 300)  # 原话半预算帽，整句保真


def test_recent_turn_pairs_filters_and_caps():
    new_msg = SimpleNamespace(role="user", text_content="本轮原话")
    items = [
        SimpleNamespace(role="system", text_content="[流程状态] 忽略"),
        SimpleNamespace(role="assistant", text_content="AI 一"),
        SimpleNamespace(role="user", text_content="   "),  # 空文本跳过
        SimpleNamespace(role="user", text_content="客 一"),
        SimpleNamespace(role="assistant", text_content="AI 二"),
        new_msg,
    ]
    pairs = recent_turn_pairs(items, exclude=new_msg, recent_turns=2)
    assert pairs == [("customer", "客 一"), ("agent", "AI 二")]
    # 不剔除时最新条目=本轮原话本体（调用方负责 exclude，原话不进 history）；空 items 安全
    assert recent_turn_pairs(items, exclude=None, recent_turns=1) == [("customer", "本轮原话")]
    assert recent_turn_pairs(None) == []


# ---- instructions（prompt 语言纯度 + 判据/关键词素材）----


def test_instructions_carries_semantics_and_purity():
    class _It:
        pass

    a, b = _It(), _It()
    a.id, a.label, a.judge_prompt, a.keywords = _ID_A, "投诉", "客户要求投诉或转人工", []
    b.id, b.label, b.judge_prompt, b.keywords = _ID_B, "退款", "", ["退款", "退钱"]
    text = build_intent_instructions([a, b])
    for token in (_ID_A, _ID_B, "投诉", "客户要求投诉或转人工", "退款、退钱"):
        assert token in text, token
    assert "NONE" in text
    bad = sorted({ch for ch in _DIALECT_MARKS if ch in text})
    assert not bad, f"instructions 含粤语特征字（prompt 语言纯度铁律）: {bad}"


# ---- pick_intent_laya：单轮快判装配（off/silent/hit/abstain/unavailable）----


def _graph(
    *,
    judge: dict | None = None,
    keywords: list[str] | None = None,
    bindings: list[dict] | None = None,
    steps: list[int] | None = None,
) -> object:
    intent: dict = {
        "id": _ID_A,
        "label": "投诉",
        "keywords": keywords if keywords is not None else ["投诉"],
        "enabled": True,
    }
    if judge is not None:
        intent["judge"] = judge
    if steps is not None:
        intent["steps"] = steps
    return parse_flow_graph(
        str(
            __import__("json").dumps(
                {
                    "version": 1,
                    "intents": [intent],
                    "bindings": bindings
                    if bindings is not None
                    else [{"id": _BND_A, "intent": _ID_A, "action": "jump_step", "step": 4}],
                }
            )
        )
    )


def _pick(graph, fake, **over):
    kwargs = {
        "graph": graph,
        "step_1based": 1,
        "fired": set(),
        "user_text": "你们这样搞我真的受不了了",
        "history": [("customer", "旧话")],
        "goal": "核实下单平台",
        "client": fake,
        "enabled": True,
    }
    kwargs.update(over)
    return asyncio.run(pick_intent_laya(**kwargs))


def test_pick_gate_off_zero_calls():
    """闸关（默认档）→ verdict=off 且零传输调用。"""
    fake = _FakeSidecar()
    res = _pick(_graph(), fake, enabled=False)
    assert res.verdict == "off" and res.answer is None
    assert fake.health_calls == 0 and fake.decide_payloads == []


def test_pick_no_candidates_is_silent():
    """无可触发绑定（once 已烧）→ silent：热路径静默零调用。"""
    fake = _FakeSidecar()
    graph = _graph(
        bindings=[{"id": _BND_A, "intent": _ID_A, "action": "jump_step", "step": 4, "once": True}]
    )
    res = _pick(graph, fake, fired={_BND_A})
    assert res.verdict == "silent"
    assert fake.health_calls == 0 and fake.decide_payloads == []


def test_pick_wide_gate_includes_keyword_only_intent():
    """宽口径：纯关键词意图（无 judge 判据）也纳入 choice 面。"""
    fake = _FakeSidecar(decide=_decide_response(_ID_A, 0.95))
    res = _pick(_graph(judge=None), fake)  # judge=None=纯关键词
    assert res.verdict == "hit" and res.answer["choice"] == _ID_A
    face = fake.decide_payloads[0]["questions"]["intent"]["criteria"]
    assert face == [_ID_A, "NONE"]
    # state 组装进 payload：原话置头 + 步语境
    state = fake.decide_payloads[0]["state"]
    assert state.startswith("客户原话：你们这样搞我真的受不了了")
    assert "核实下单平台" in state


def test_pick_hit_high_confidence():
    fake = _FakeSidecar(decide=_decide_response(_ID_A, 0.93))
    res = _pick(_graph(judge={"prompt": "客户表达强烈不满"}), fake)
    assert res.verdict == "hit"
    assert res.answer is not None and res.answer["choice"] == _ID_A
    assert res.intents == 1 and res.ms >= 0


def test_pick_below_floor_abstains():
    """below_floor=true → abstain（置信门回落 9B 后台判，不当轮触发）。"""
    fake = _FakeSidecar(decide=_decide_response(_ID_A, 0.31, below=True))
    res = _pick(_graph(), fake)
    assert res.verdict == "abstain" and res.answer is not None


def test_pick_none_choice_abstains():
    fake = _FakeSidecar(decide=_decide_response("NONE", 0.9))
    res = _pick(_graph(), fake)
    assert res.verdict == "abstain"


def test_pick_unavailable_health_and_decide(monkeypatch):
    """健康失败 / 判定失败 → unavailable（reason 归因）→ 回落旧路。"""
    fake = _FakeSidecar(health_exc=ConnectionRefusedError("refused"))
    res = _pick(_graph(), fake)
    assert res.verdict == "unavailable" and res.reason == "connect"
    assert fake.decide_payloads == []
    # 健康失败也入缓存（同 base_url 共享）——换独立客户端前先清，隔离两段归因。
    reset_health_cache()
    fake2 = _FakeSidecar(decide_exc=TimeoutError("slow"))
    res2 = _pick(_graph(), fake2)
    assert res2.verdict == "unavailable" and res2.reason == "decide_failed"


def test_pick_play_only_intent_filtered_when_advanced():
    """play_allowed=False（规则推进轮）滤掉仅剩 play_qa 绑定的意图（命中也播不出）。"""
    fake = _FakeSidecar(decide=_decide_response(_ID_A, 0.95))
    graph = _graph(
        bindings=[{"id": _BND_A, "intent": _ID_A, "action": "play_qa", "qa_id": "qa-1"}]
    )
    assert _pick(graph, fake, play_allowed=False).verdict == "silent"
    assert _pick(graph, fake, play_allowed=True).verdict == "hit"


def test_pick_step_scope_enforced():
    fake = _FakeSidecar(decide=_decide_response(_ID_A, 0.95))
    graph = _graph(steps=[3])
    assert _pick(graph, fake, step_1based=1).verdict == "silent"
    assert _pick(graph, fake, step_1based=3).verdict == "hit"
