"""3.4 意图引擎 runtime 面:判据判定 prompt / 输出解析 / 调度门 / `_llm_judge` 参数面。

契约(plan 2026-09-18 §1):`build_intent_judge_messages` 单次批量(全部候选进一份
请求)+ 单选输出契约(id 或 NONE);`parse_intent_judge_output` 剥空白/反引号后
精确匹配;`_llm_judge(..., *, max_tokens=8)` 具名缺省=既有调用点零变化;
`_intent_judge_candidates` 六闸调度门(无判据数据恒空=零任务)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime import agent as agent_mod  # noqa: E402
from agent_runtime.flow import (  # noqa: E402
    FlowController,
    build_intent_judge_messages,
    parse_intent_judge_output,
)

_ID_A = "int_1a2b3c4d"
_ID_B = "int_2b3c4d5e"

# 粤语独有特征字(标准书面中文禁令的自查集):共享指引文本混方言会把小模型中文
# 回复带偏(AGENTS.md Prompt 语言纯度铁律),故判据判定模板逐字禁入。
_DIALECT_MARKS = "嘅唔係咗喇喺啲乜嘢畀睇嚟啩"


# ---- build_intent_judge_messages ----


def test_build_includes_all_candidates_and_user_text():
    msgs = build_intent_judge_messages(
        intents=[
            {"id": _ID_A, "label": "投诉", "prompt": "客户明确要求投诉或转人工"},
            {"id": _ID_B, "label": "退款", "prompt": "客户要求退回款项，含不满货物质量"},
        ],
        user_text="你们这个件拖了半个月了",
        step_1based=2,
        goal="核实下单平台",
    )
    assert [m["role"] for m in msgs] == ["system", "user"]
    sys_text = msgs[0]["content"]
    for token in (_ID_A, _ID_B, "投诉", "退款", "客户明确要求投诉或转人工", "退回款项", "第2步", "核实下单平台"):
        assert token in sys_text, token
    assert "你们这个件拖了半个月了" in msgs[1]["content"]
    # 单选输出契约(一个 id 或 NONE),防模型自由发挥;review N9 后措辞钉「原样照抄
    # id」且明确禁序号——「编号」旧措辞会诱导 9B 回 1/2/3 序号(parse 只认 id 原文)。
    assert "NONE" in sys_text and "原样照抄" in sys_text and "不要输出序号" in sys_text
    assert "编号" not in sys_text
    assert "NONE" in msgs[1]["content"]


def test_build_handles_missing_label_and_goal():
    msgs = build_intent_judge_messages(
        intents=[{"id": _ID_A, "label": "", "prompt": "判据"}],
        user_text="喂",
        step_1based=1,
        goal="",
    )
    assert "(无)" in msgs[0]["content"]


def test_build_is_standard_written_chinese():
    """Prompt 语言纯度:模板唔准有粤语特征字(无条件进每通判据请求)。"""
    msgs = build_intent_judge_messages(
        intents=[{"id": _ID_A, "label": "标签", "prompt": "判据"}],
        user_text="客户原话",
        step_1based=1,
        goal="目标",
    )
    text = msgs[0]["content"] + msgs[1]["content"]
    bad = sorted({ch for ch in _DIALECT_MARKS if ch in text})
    assert not bad, f"判据判定模板含粤语特征字: {bad}"


# ---- parse_intent_judge_output ----


@pytest.mark.parametrize(
    "raw",
    [
        _ID_A,
        f"  {_ID_A}  ",
        f"```\n{_ID_A}\n```",
        f"```{_ID_A}```",
        f"'{_ID_A}'",
        f'"{_ID_A}"',
        f"\n\n{_ID_A}\n",
        # review N9:9B 收尾带中文标点(句号/感叹号)旧版唔剥=永久 miss
        f"{_ID_A}。",
        f"{_ID_A}！",
        f"「{_ID_A}」",
    ],
)
def test_parse_plain_and_fenced_variants(raw):
    assert parse_intent_judge_output(raw, [_ID_A, _ID_B]) == _ID_A


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "NONE",
        "none",
        "int_deadbeef",  # 未知 id
        _ID_A.upper(),  # 大小写敏感(契约)
        "意图是 int_deadbeef",  # 唔认自然语言(宁可无命中)
        "先说明一下\n" + _ID_A,  # 只取首个有内容行
        "```\n\n```",
    ],
)
def test_parse_unrecognized_is_empty(raw):
    assert parse_intent_judge_output(raw, [_ID_A]) == ""


def test_parse_accepts_second_line_when_first_is_fence():
    assert parse_intent_judge_output(f"```\n{_ID_B}\n```", [_ID_A, _ID_B]) == _ID_B


# ---- _llm_judge max_tokens 参数面 ----


class _FakeAsyncOpenAI:
    """最小 AsyncOpenAI 替身:只记 create 的 kwargs。"""

    calls: list[dict] = []

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        type(self).calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=_ID_A))]
        )


@pytest.fixture()
def fake_openai(monkeypatch):
    _FakeAsyncOpenAI.calls = []
    monkeypatch.setattr("openai.AsyncOpenAI", _FakeAsyncOpenAI)
    return _FakeAsyncOpenAI


def _run_llm_judge(**kwargs):
    import asyncio

    return asyncio.run(agent_mod._llm_judge("http://127.0.0.1:1235/v1", "m", [{"role": "user", "content": "x"}], **kwargs))


def test_llm_judge_default_max_tokens_is_8(fake_openai):
    """既有调用点(推进判定器)零变化:具名默认 8 原值。"""
    assert _run_llm_judge() == _ID_A
    assert fake_openai.calls[0]["max_tokens"] == 8


def test_llm_judge_max_tokens_plumbs(fake_openai):
    """意图判据判定显式传 32(id 长过 8 token)。"""
    assert _run_llm_judge(max_tokens=32) == _ID_A
    assert fake_openai.calls[0]["max_tokens"] == 32


def test_llm_judge_empty_endpoint_short_circuits(fake_openai):
    import asyncio

    assert asyncio.run(agent_mod._llm_judge("", "m", [])) == ""
    assert fake_openai.calls == []


# ---- _intent_judge_candidates:调度门 ----


def _template(*, judge: dict | None, bindings: list[dict] | None = None) -> dict:
    intent: dict = {
        "id": _ID_A,
        "label": "投诉",
        "keywords": ["投诉"],
        "steps": [],
        "enabled": True,
    }
    if judge is not None:
        intent["judge"] = judge
    return {
        "steps_json": json.dumps(
            [{"goal": f"第{i}步", "ref": f"第{i}步说法"} for i in range(1, 7)],
            ensure_ascii=False,
        ),
        "graph_json": json.dumps(
            {
                "version": 1,
                "intents": [intent],
                "bindings": bindings
                if bindings is not None
                else [{"id": "bnd_7e8f9a0b", "intent": _ID_A, "action": "jump_step", "step": 4}],
            },
            ensure_ascii=False,
        ),
    }


def _candidates(template: dict, **over):
    fc = FlowController.from_template(template, None)
    kwargs = {
        "step_1based": 1,
        "fired": fc.graph_fired,
        "closing": fc.closing,
        "inflight": False,
        "pending": False,
    }
    kwargs.update(over)
    return agent_mod._intent_judge_candidates(fc.graph, **kwargs)


def test_gate_zero_regression_without_judge_data(monkeypatch):
    """**零回归 pin**:纯关键词图 → 调度门恒不过(零任务零专线调用),与 3.3 同。"""
    monkeypatch.delenv("BOK_FLOW_GRAPH_JUDGE", raising=False)
    monkeypatch.delenv("BOK_FLOW_GRAPH", raising=False)
    assert _candidates(_template(judge=None)) == []
    # 意图带判据但无绑定(=判返都触发唔到)同档早退
    assert _candidates(_template(judge={"prompt": "判据"}, bindings=[])) == []


def test_gate_passes_with_judge_data(monkeypatch):
    monkeypatch.delenv("BOK_FLOW_GRAPH_JUDGE", raising=False)
    monkeypatch.delenv("BOK_FLOW_GRAPH", raising=False)
    assert [i.id for i in _candidates(_template(judge={"prompt": "判据"}))] == [_ID_A]


@pytest.mark.parametrize("key", ["BOK_FLOW_GRAPH_JUDGE", "BOK_FLOW_GRAPH"])
def test_gate_kill_switches(monkeypatch, key):
    monkeypatch.setenv(key, "0")
    assert _candidates(_template(judge={"prompt": "判据"})) == []


@pytest.mark.parametrize("flag", ["closing", "inflight", "pending"])
def test_gate_single_flight_and_lifecycle(monkeypatch, flag):
    monkeypatch.delenv("BOK_FLOW_GRAPH_JUDGE", raising=False)
    assert _candidates(_template(judge={"prompt": "判据"}), **{flag: True}) == []


def test_gate_empty_graph(monkeypatch):
    monkeypatch.delenv("BOK_FLOW_GRAPH_JUDGE", raising=False)
    empty = {"steps_json": "[]", "graph_json": ""}
    assert _candidates(empty) == []
