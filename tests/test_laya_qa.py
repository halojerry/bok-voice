"""Laya QA 验证车道（2026-09-26，docs/LAYA-EVAL.md 第二落位：qa fastpath 复核）。

覆盖面：
- 车道闸（BOK_LAYA_QA 默认 "0"=零调用零变化；与意图闸 BOK_LAYA_JUDGE 独立立法）；
- decide_qa_match：原始面五判定（hit/none/abstain/off/unavailable）、payload 契约
  （qid=qa_match、候选截前 8+NONE 恰好一次、多选一形状）、fail-open 全谱
  （超时/连不上/坏形/健康失败一律 unavailable=落 QA_SEM）；
- env 默认与坏值回退（TIMEOUT_MS 300ms / P 0.85）；
- build_qa_state 短形（原话置头、无会话史）与 build_qa_instructions（prompt
  语言纯度 + question_text 截断）；
- agent.py 接线源级锚（同 test_intent_judge_wiring.py 姿势）：车道块在
  `_qa_index.match(` 之后、QA_SEM 判断之前；hit 落公共出场链；高置信 none 置
  拒绝旗跳过 QA_SEM；车道关=旗恒 False 条件同旧（字节等价）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.laya_judge import (  # noqa: E402
    DEFAULT_QA_HIT_P,
    DEFAULT_QA_TIMEOUT_S,
    QA_MAX_CANDIDATES,
    build_qa_instructions,
    build_qa_state,
    decide_qa_match,
    laya_qa_enabled,
    qa_hit_floor_p,
    qa_timeout_s,
)
from agent_runtime.laya_judge import (  # noqa: E402
    LayaJudgeClient,
    reset_health_cache,
)

_DIALECT_MARKS = "嘅唔係咗喇喺啲乜嘢畀睇嚟啩"

# ---- 假传输（子类覆写传输缝，计数不打真 HTTP；姿势照抄 test_laya_judge.py）----


class _FakeSidecar(LayaJudgeClient):
    def __init__(
        self,
        *,
        health=(200, {"ok": True}),
        decide=None,
        health_exc: Exception | None = None,
        decide_exc: Exception | None = None,
    ) -> None:
        super().__init__()
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
            "qa_match": {
                "choice": choice,
                "probabilities": {choice: conf, "NONE": round(1 - conf, 2)},
                "confidence": conf,
                "below_floor": below,
            }
        },
        "state_truncated": False,
    }


def _entries(n: int) -> list[dict]:
    return [
        {"id": f"qa_{i:02d}", "question_text": f"问题{i}要怎么处理"} for i in range(n)
    ]


@pytest.fixture(autouse=True)
def _clean_health_cache():
    reset_health_cache()
    yield
    reset_health_cache()


# ---- 车道闸：默认关=零调用零变化 ----


def test_gate_default_off(monkeypatch):
    monkeypatch.delenv("BOK_LAYA_QA", raising=False)
    assert laya_qa_enabled() is False  # 默认 "0"=零变化的结构性保证
    monkeypatch.setenv("BOK_LAYA_QA", "0")
    assert laya_qa_enabled() is False
    monkeypatch.setenv("BOK_LAYA_QA", "1")
    assert laya_qa_enabled() is True


def test_gate_off_zero_transport(monkeypatch):
    """闸关（默认档）→ off 且零传输调用（fail-open 最外层）。"""
    monkeypatch.delenv("BOK_LAYA_QA", raising=False)
    fake = _FakeSidecar()
    assert asyncio.run(decide_qa_match(["客户原话：x"], _entries(2), client=fake)) == {
        "verdict": "off",
        "choice": "",
        "p": 0.0,
        "conf": 0.0,
    }
    assert fake.health_calls == 0 and fake.decide_payloads == []


def test_lane_gate_independent_of_intent_gate(monkeypatch):
    """QA 车道闸与 BOK_LAYA_JUDGE 独立：意图闸显式关（env 清空）时 QA 车道照常打判定。"""
    monkeypatch.setenv("BOK_LAYA_QA", "1")
    monkeypatch.delenv("BOK_LAYA_JUDGE", raising=False)  # 意图车道显式清空（不再默认关）
    fake = _FakeSidecar(decide=_decide_response("qa_00", 0.93))
    res = asyncio.run(decide_qa_match(["客户原话：x"], _entries(2), client=fake))
    assert res["verdict"] == "hit"
    assert len(fake.decide_payloads) == 1


# ---- payload 契约：qid / 候选面 / 超时 / instructions ----


def test_payload_contract_qid_and_face(monkeypatch):
    """qid=qa_match；候选截前 8（+NONE=9 顶格、NONE 恰好一次）；超时走 env。"""
    monkeypatch.setenv("BOK_LAYA_QA", "1")
    monkeypatch.setenv("BOK_LAYA_QA_TIMEOUT_MS", "450")
    fake = _FakeSidecar(decide=_decide_response("qa_00", 0.95))
    cands = _entries(12)  # 超容量 → 截前 8
    res = asyncio.run(decide_qa_match(["客户原话：怎么退货"], cands, client=fake))
    assert res["verdict"] == "hit" and res["choice"] == "qa_00"
    payload = fake.decide_payloads[0]
    assert set(payload["questions"].keys()) == {"qa_match"}
    q = payload["questions"]["qa_match"]
    assert q["type"] == "choice"  # 多选一形状（实测二元面偏糊，禁用）
    assert q["criteria"] == [e["id"] for e in cands[:QA_MAX_CANDIDATES]] + ["NONE"]
    assert len(q["criteria"]) <= 9 and q["criteria"].count("NONE") == 1
    assert fake.decide_timeouts == [pytest.approx(0.45)]  # 毫秒转秒
    assert payload["state"].startswith("客户原话：怎么退货")
    # confidence_floor 与意图 judge 同档（0.5，客户端默认）
    assert payload["confidence_floor"] == pytest.approx(0.5)


def test_instructions_semantics_and_purity():
    """判定指引：词条问法进 instructions、criteria 只放 id；标准书面中文。"""
    entries = [
        {"id": "qa_01", "question_text": "怎么查快递单号"},
        {"id": "qa_02", "question_text": "  你们   是谁  "},
    ]
    text = build_qa_instructions(entries)
    assert "qa_01" in text and "怎么查快递单号" in text
    assert "你们 是谁" in text  # 内部空白折叠
    assert "NONE" in text
    bad = sorted({ch for ch in _DIALECT_MARKS if ch in text})
    assert not bad, f"instructions 含粤语特征字（prompt 语言纯度铁律）: {bad}"


def test_instructions_option_text_truncated():
    """选项描述=question_text 截 ≤40 字。"""
    long_entries = [{"id": "qa_big", "question_text": "长" * 100}]
    text = build_qa_instructions(long_entries)
    assert ("长" * 40) in text and ("长" * 41) not in text
    empty = build_qa_instructions([{"id": "qa_e", "question_text": "   "}])
    assert "问法 (无)" in empty


# ---- 判定面：hit / none / abstain / p ----


def test_verdict_hit(monkeypatch):
    monkeypatch.setenv("BOK_LAYA_QA", "1")
    fake = _FakeSidecar(decide=_decide_response("qa_01", 0.97))
    res = asyncio.run(decide_qa_match(["s"], _entries(3), client=fake))
    assert res == {"verdict": "hit", "choice": "qa_01", "p": 0.97, "conf": 0.97}


def test_verdict_none_high_confidence(monkeypatch):
    monkeypatch.setenv("BOK_LAYA_QA", "1")
    fake = _FakeSidecar(decide=_decide_response("NONE", 0.9))
    res = asyncio.run(decide_qa_match(["s"], _entries(3), client=fake))
    assert res["verdict"] == "none" and res["choice"] == "NONE"


def test_verdict_abstain_below_floor(monkeypatch):
    monkeypatch.setenv("BOK_LAYA_QA", "1")
    # sidecar 旗显式 true：即便置信高也不裁边界案
    fake = _FakeSidecar(decide=_decide_response("qa_02", 0.99, below=True))
    res = asyncio.run(decide_qa_match(["s"], _entries(3), client=fake))
    assert res["verdict"] == "abstain" and res["choice"] == "qa_02"
    # 旗缺席：conf < floor(0.5) → 本地补算 below_floor → abstain
    low = _decide_response("qa_02", 0.3)
    del low["answers"]["qa_match"]["below_floor"]
    fake2 = _FakeSidecar(decide=low)
    res2 = asyncio.run(decide_qa_match(["s"], _entries(3), client=fake2))
    assert res2["verdict"] == "abstain"


def test_p_from_probabilities_fallback_conf(monkeypatch):
    """p=胜者面概率（probabilities 缺席回退 confidence）。"""
    monkeypatch.setenv("BOK_LAYA_QA", "1")
    resp = _decide_response("qa_00", 0.5)
    resp["answers"]["qa_match"]["probabilities"] = {"qa_00": 0.88, "NONE": 0.12}
    fake = _FakeSidecar(decide=resp)
    res = asyncio.run(decide_qa_match(["s"], _entries(2), client=fake))
    assert res["p"] == pytest.approx(0.88) and res["conf"] == pytest.approx(0.5)
    resp2 = _decide_response("qa_00", 0.66)
    del resp2["answers"]["qa_match"]["probabilities"]
    fake2 = _FakeSidecar(decide=resp2)
    res2 = asyncio.run(decide_qa_match(["s"], _entries(2), client=fake2))
    assert res2["p"] == pytest.approx(0.66) and res2["conf"] == pytest.approx(0.66)


# ---- fail-open 全谱：超时/连不上/坏形/健康失败 → unavailable（落 QA_SEM）----


@pytest.mark.parametrize(
    "exc", [TimeoutError("slow"), ConnectionRefusedError("refused"), RuntimeError("boom")]
)
def test_fail_open_transport_errors(monkeypatch, exc):
    monkeypatch.setenv("BOK_LAYA_QA", "1")
    fake = _FakeSidecar(decide_exc=exc)
    res = asyncio.run(decide_qa_match(["s"], _entries(2), client=fake))
    assert res == {"verdict": "unavailable", "choice": "", "p": 0.0, "conf": 0.0}
    assert len(fake.decide_payloads) == 1  # 健康过了、判定挂了


@pytest.mark.parametrize(
    "decide",
    [
        None,  # 整体缺
        {},  # 非 dict answers
        {"answers": {}},  # qid 缺
        {"answers": {"qa_match": {}}},  # choice 空
        {"answers": {"qa_match": {"choice": "qa_99", "confidence": 0.9}}},  # 未知 id
        {"answers": {"qa_match": {"choice": "qa_00"}}},  # confidence 缺=坏形
    ],
)
def test_fail_open_malformed(monkeypatch, decide):
    monkeypatch.setenv("BOK_LAYA_QA", "1")
    fake = _FakeSidecar(decide=decide)
    res = asyncio.run(decide_qa_match(["s"], _entries(2), client=fake))
    assert res["verdict"] == "unavailable"


@pytest.mark.parametrize(
    ("health", "health_exc"),
    [
        ((200, {"ok": False}), None),  # 模型缺位
        ((503, None), None),  # http 状态
        (None, ConnectionRefusedError("refused")),
        (None, TimeoutError("slow")),
    ],
)
def test_fail_open_health(monkeypatch, health, health_exc):
    monkeypatch.setenv("BOK_LAYA_QA", "1")
    fake = _FakeSidecar(health=health, health_exc=health_exc)
    res = asyncio.run(decide_qa_match(["s"], _entries(2), client=fake))
    assert res["verdict"] == "unavailable"
    assert fake.decide_payloads == []  # 健康失败零 decide 调用


def test_empty_candidates_zero_calls(monkeypatch):
    """空候选/全空 id=无判定语义 → unavailable 且零 HTTP（健康都不探）。"""
    monkeypatch.setenv("BOK_LAYA_QA", "1")
    fake = _FakeSidecar()
    res = asyncio.run(decide_qa_match(["s"], [], client=fake))
    assert res["verdict"] == "unavailable"
    assert fake.health_calls == 0 and fake.decide_payloads == []
    fake2 = _FakeSidecar()
    res2 = asyncio.run(
        decide_qa_match(["s"], [{"id": "  ", "question_text": "x"}], client=fake2)
    )
    assert res2["verdict"] == "unavailable"
    assert fake2.health_calls == 0 and fake2.decide_payloads == []


# ---- env 默认与坏值回退 ----


def test_env_defaults(monkeypatch):
    for key in ("BOK_LAYA_QA", "BOK_LAYA_QA_TIMEOUT_MS", "BOK_LAYA_QA_P"):
        monkeypatch.delenv(key, raising=False)
    assert laya_qa_enabled() is False
    assert qa_timeout_s() == pytest.approx(DEFAULT_QA_TIMEOUT_S)
    assert DEFAULT_QA_TIMEOUT_S == pytest.approx(0.3)
    assert qa_hit_floor_p() == pytest.approx(DEFAULT_QA_HIT_P)
    assert DEFAULT_QA_HIT_P == pytest.approx(0.85)


@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        ("", 0.3),
        ("abc", 0.3),
        ("0", 0.3),
        ("-5", 0.3),
        ("300", 0.3),
        ("500", 0.5),
    ],
)
def test_timeout_env_parsing(monkeypatch, raw, expect):
    monkeypatch.setenv("BOK_LAYA_QA_TIMEOUT_MS", raw)
    assert qa_timeout_s() == pytest.approx(expect)


@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        ("", 0.85),
        ("abc", 0.85),
        ("0", 0.85),
        ("-1", 0.85),
        ("1.5", 0.85),
        ("0.7", 0.7),
        ("1", 1.0),
    ],
)
def test_p_env_parsing(monkeypatch, raw, expect):
    monkeypatch.setenv("BOK_LAYA_QA_P", raw)
    assert qa_hit_floor_p() == pytest.approx(expect)


# ---- build_qa_state：短形（原话置头、无会话史）----


def test_state_short_shape_utterance_head():
    lines = build_qa_state("你们的退货政策是怎样的", "核实退货诉求")
    assert isinstance(lines, list) and len(lines) == 2  # 原话+goal，无会话史
    assert lines[0].startswith("客户原话：你们的退货政策是怎样的")  # 原话置头
    assert lines[1] == "当前流程：核实退货诉求"
    text = "\n".join(lines)
    assert "客户原话" in text and text.index("你们的退货政策") < text.index("核实退货诉求")


def test_state_empty_goal_single_line():
    assert build_qa_state("hello", "") == ["客户原话：hello"]
    assert build_qa_state("hello", "   ") == ["客户原话：hello"]


def test_state_budget_caps():
    utt = "问" * 500
    lines = build_qa_state(utt, "g" * 200)
    assert lines[0] == "客户原话：" + "问" * 150  # 原话半预算帽
    assert lines[1] == "当前流程：" + "g" * 60  # goal 截断
    assert sum(len(line) + 1 for line in lines) <= 300


# ---- agent.py 接线源级锚（闭包离线起不了真栈：文本切片钉结构，改接线即红）----

_AGENT_SRC = (
    Path(__file__).resolve().parents[1]
    / "apps"
    / "agent"
    / "agent_runtime"
    / "agent.py"
).read_text(encoding="utf-8")

_MATCH_ANCHOR = "_qa_entry, _qa_score = _qa_index.match("
_LANE_GATE = "if _qa_entry is None and laya_qa_enabled():"
_SEM_COND_ANCHOR = "_qa_sem is not None"


def _lane_segment() -> str:
    gate = _AGENT_SRC.index(_LANE_GATE)
    sem = _AGENT_SRC.index(_SEM_COND_ANCHOR, gate)
    return _AGENT_SRC[gate:sem]


def test_lane_sits_after_literal_match_before_qa_sem():
    """插入点钉死：词面 0.90 未中之后、QA_SEM 之前（任务书的硬性落点）。"""
    m = _AGENT_SRC.index(_MATCH_ANCHOR)
    init = _AGENT_SRC.index("_qa_laya_reject = False")
    gate = _AGENT_SRC.index(_LANE_GATE)
    sem = _AGENT_SRC.index(_SEM_COND_ANCHOR, gate)
    assert m < init < gate < sem
    assert _AGENT_SRC.count(_LANE_GATE) == 1  # 唯一车道入口
    assert _AGENT_SRC.count(_SEM_COND_ANCHOR) == 1  # QA_SEM 门唯一


def test_lane_off_is_byte_equivalent_to_old_path():
    """车道关（默认）：闭车道块唯一无条件新增执行=旗初始化一行（恒 False），
    拒绝守卫恒真；语义补位内层条件与旧档逐字节同串（test_qa_semantic 源级锚
    钉死的同一字面量，不得改写）。"""
    assert _AGENT_SRC.count("_qa_laya_reject = False") == 1
    assert _AGENT_SRC.count("_qa_laya_reject = True") == 1
    lane_gate = _AGENT_SRC.index(_LANE_GATE)
    gate = _AGENT_SRC.index("if not _qa_laya_reject:")
    assert lane_gate < gate  # 拒绝守卫包在车道块之后的语义补位外层
    inner = _AGENT_SRC.index(
        "if _qa_entry is None and _qa_sem is not None:", gate
    )
    assert (
        _AGENT_SRC[inner : inner + len("if _qa_entry is None and _qa_sem is not None:")]
        == "if _qa_entry is None and _qa_sem is not None:"
    )
    # 内层守卫仍吃词面 None（词面命中轮结构性零语义调用，旧纪律不变）
    guard = _AGENT_SRC[inner : inner + 60]
    assert "_qa_entry is None" in guard


def test_lane_calls_module_entry_with_recall_and_short_state():
    seg = _lane_segment()
    # rank 契约未就位=静默跳过；召回异常=当池空（fail-open 落 QA_SEM）
    assert 'getattr(_qa_index, "rank", None)' in seg
    assert "except Exception:" in seg
    # 召回参数单点=qa_gate env helper(BOK_QA_RECALL_K/FLOOR,2026-09-25 审计
    # 废除 laya_judge 常量副本);lang/step 与词面 match 同源
    assert "k=qa_recall_k()" in seg and "floor=qa_recall_floor()" in seg
    # 双召回腿:词面 rank ∪ 语义 rank 并池(golden 标定实证词面喂不动改写/同音族)
    assert 'getattr(_qa_sem, "rank", None)' in seg
    assert "k=qa_recall_k()" in seg
    assert "flow_ctrl.has_steps" in seg
    # 判定装配只经模块入口（state/instructions 组装不在闭包里重写一份）
    assert "decide_qa_match(" in seg and "build_qa_state(" in seg
    assert "build_qa_instructions(" not in seg
    # 观测行（QA_SEM 行同风格）
    assert "QA_LAYA verdict=" in seg and "pool={len(_qa_pool)}" in seg


def test_lane_hit_none_gated_by_probability_door():
    """p 门钉死：hit 消费与 none 拒绝旗都嵌在 `p >= qa_hit_floor_p()` 之下。"""
    seg = _lane_segment()
    door = seg.index('if _qa_dec["p"] >= qa_hit_floor_p():')
    hit = seg.index('_qa_entry = _qa_folded or _qa_hit_entry')
    reject = seg.index('_qa_laya_reject = True')
    assert door < hit < reject  # hit 分支在前、none 拒绝在后，同门之下
    assert seg.count('if _qa_dec["verdict"] == "hit":') == 1
    assert seg.count('elif _qa_dec["verdict"] == "none":') == 1


def test_lane_hit_feeds_common_playout_chain():
    """hit 消费路径钉死：落 _qa_entry（折组+概率分）后与词面/QA_SEM 共用同一条
    出场链（轮换/PCM/canned_say，gen 仍 qa_fastpath）；汇总口径 hit/match0 不变。"""
    seg = _lane_segment()
    assert "_qa_index.team_head(" in seg  # 折组与 QA_SEM 命中同款
    assert "if qa_rotation_enabled()" in seg  # 轮换 kill-switch 配对
    sem = _AGENT_SRC.index(_SEM_COND_ANCHOR)
    tail = _AGENT_SRC[sem:]
    chain_start = tail.index("if _qa_entry is not None:")
    chain = tail[chain_start:]
    assert "_qa_rotation_plan(_qa_entry, _qa_members, flow_ctrl.qa_played)" in chain
    assert 'await _qa_canned_say(_qa_member, _qa_pcm, provider="qa-fastpath")' in chain
    # 汇总打点保持在出场链之前、口径不变（match0=零命中语义；hit 轮计 hit）
    bump = '_qa_bump("match0" if _qa_entry is None else "hit")'
    assert tail.index(bump) < chain_start
