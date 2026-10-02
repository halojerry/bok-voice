"""前提推翻更正 lane（D2，2026-09-30 多轮上下文计划 Phase 2）。

病灶：「唔係拼多多，係淘宝」旧路取 _PLATFORM_RE 首个位置匹配=沉淀**被否定
的平台**（主动下毒）；facts FIFO 纯 append 无槽位覆盖，矛盾事实并排每轮喂
模型。修法=确定性更正识别 + ContextState.supersede_call_fact 顶替（A-CC
2512.00332：结构化槽位权威+确定性写入，零 LLM 仲裁）。

契约：
- extract_fact_updates：非更正轮与 extract_call_facts 逐字节同产出；
  更正轮产出「客户更正…以X为准」+ 顶替 needle；纯否认轮不沉淀；
- supersede_call_fact：needle 命中的旧事实移除（新条目自身豁免）、新值
  append、revision 照 bump；**冻结尾部永不回溯重写**（严格前缀铁律）；
- kill-switch BOK_FACT_CORRECTION=0 回旧 append-only（含毒化行为，字节同旧）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.flow import extract_call_facts, extract_fact_updates  # noqa: E402
from agent_runtime.providers.livekit_plugins import ContextState  # noqa: E402

AGENT_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(
    encoding="utf-8"
)
BOK_SRC = (ROOT / "tools" / "bok.py").read_text(encoding="utf-8")


# ---- 纯函数面 ----

def test_correction_pair_cantonese():
    out = extract_fact_updates("唔係拼多多，係淘宝")
    assert out == [
        ("客户更正：在淘宝买（此前讲过拼多多，以淘宝为准）", "客户讲过在拼多多买")
    ]


def test_correction_pair_mandarin():
    out = extract_fact_updates("不是淘宝，是京东")
    assert out == [("客户更正：在京东买（此前讲过淘宝，以京东为准）", "客户讲过在淘宝买")]


def test_correction_deny_between_mentions():
    # 「拼多多唔係，係淘宝」——否认在两提及之间
    out = extract_fact_updates("拼多多唔係，係淘宝")
    assert out[0][1] == "客户讲过在拼多多买"
    assert "以淘宝为准" in out[0][0]


def test_correction_swap_verb_no_old():
    # 改换动词、旧值未知 → 顶替一切平台事实
    out = extract_fact_updates("改成淘宝")
    assert out == [("客户更正：在淘宝买（以此为准）", "客户讲过在")]


def test_correction_deny_then_affirm_single():
    out = extract_fact_updates("唔係咩，係淘宝")
    assert out == [("客户更正：在淘宝买（以此为准）", "客户讲过在")]


def test_pure_denial_sediments_nothing():
    """纯否认（「唔係淘宝」）=异议非更正——不沉淀（治旧路首位置匹配毒化）。"""
    assert extract_fact_updates("唔係淘宝") == []
    assert extract_fact_updates("不是京东") == []


def test_uncertainty_falls_back_to_normal_mention():
    """「可能係淘宝」=不确定语气 → 普通提及沉淀（旧路）。"""
    assert extract_fact_updates("可能係淘宝") == [("客户讲过在淘宝买", None)]


def test_normal_turns_byte_identical_with_legacy():
    for t in ("我喺拼多多買嘅", "我電話係 一三八零零零零零零零零", "你好啊"):
        assert extract_fact_updates(t) == [(f, None) for f in extract_call_facts(t)]


def test_correction_turn_still_sediments_numbers():
    out = extract_fact_updates("唔係拼多多，係淘宝，我電話九八五二六六三三")
    assert len(out) == 2
    assert out[0][1] == "客户讲过在拼多多买"
    assert out[1] == ("客户报过号码:九八五二六六三三", None)


def test_killswitch_returns_legacy_bytes():
    """BOK_FACT_CORRECTION=0 → 更正轮也走旧 append-only（含毒化行为，字节同旧）。"""
    out = extract_fact_updates("唔係拼多多，係淘宝", enabled=False)
    assert out == [("客户讲过在拼多多买", None)]


def test_correction_strings_pure_written_chinese():
    """沉淀串标准书面中文（zh 通话 prompt 纯度纪律，无粤语方言标记）。"""
    for t in ("唔係拼多多，係淘宝", "改成淘宝", "唔係咩，係京东"):
        for fact, _ in extract_fact_updates(t):
            assert not any(m in fact for m in ("嘅", "哋", "咩", "嚟", "唔"))


# ---- ContextState.supersede_call_fact ----

def test_supersede_replaces_old_and_bumps_revision():
    st = ContextState(account_id="t")
    st.add_call_fact("客户讲过在拼多多买")
    st.add_call_fact("客户报过号码:九八五二六六三三")
    rev = st.revision
    st.supersede_call_fact("客户讲过在拼多多买", "客户更正：在淘宝买（此前讲过拼多多，以淘宝为准）")
    assert st._call_facts == [
        "客户报过号码:九八五二六六三三",
        "客户更正：在淘宝买（此前讲过拼多多，以淘宝为准）",
    ]
    assert st.revision == rev + 1


def test_supersede_self_exempt_and_empty_needle_noop():
    st = ContextState(account_id="t")
    st.supersede_call_fact("", "新事实")  # 空 needle 不动
    assert st._call_facts == []
    st.add_call_fact("客户讲过在淘宝买")
    rev = st.revision
    st.supersede_call_fact("客户讲过在", "客户讲过在淘宝买")  # 新值==旧值自身豁免
    assert st._call_facts == ["客户讲过在淘宝买"]
    assert st.revision == rev


def test_supersede_respects_fifo_limit():
    st = ContextState(account_id="t")
    for i in range(4):
        st.add_call_fact(f"事实{i}")
    st.supersede_call_fact("事实0", "客户更正：在淘宝买（以此为准）")
    assert len(st._call_facts) == 4
    assert st._call_facts[-1] == "客户更正：在淘宝买（以此为准）"
    assert "事实0" not in st._call_facts


# ---- 严格前缀铁律（冻结尾部永不回溯重写） ----

class _CaptureInner:
    def __init__(self):
        self.captured: list[list[tuple[str, str]]] = []

    def on(self, *a, **k):
        pass

    async def chat(self, *, chat_ctx, **kw):
        self.captured.append(
            [(getattr(it, "role", ""), it.content if isinstance(it.content, str) else str(it.content)) for it in chat_ctx.items]
        )
        return "ok"


def test_correction_midcall_keeps_strict_prefix():
    """更正只影响其后各轮尾部增量;旧 user 冻结重放原样(旧事实仍在冻结尾,
    由新尾部「以X为准」显式压倒)——前缀铁律不破。"""
    from livekit.agents.llm import ChatContext

    from agent_runtime.providers.livekit_plugins import ContextAwareLLM

    inner = _CaptureInner()
    llm = ContextAwareLLM(inner=inner, context_state=ContextState(account_id="t"))
    llm._ctx.set_user_language("zh")

    def _run(history):
        cc = ChatContext()
        for role, text in history:
            cc.add_message(role=role, content=text)
        asyncio.run(llm.chat(chat_ctx=cc))
        return "\n".join(f"{ro}:{c}" for ro, c in inner.captured[-1])

    s1 = _run([("system", "人设base"), ("user", "你好")])
    llm._ctx.add_call_fact("客户讲过在拼多多买")
    s2 = _run([("system", "人设base"), ("user", "你好"), ("assistant", "您好"), ("user", "我喺拼多多買")])
    assert s2.startswith(s1 + "\n")
    # 更正轮(生产路径=agent 接线 supersede)
    for fact, needle in extract_fact_updates("唔係拼多多，係淘宝"):
        if needle:
            llm._ctx.supersede_call_fact(needle, fact)
        else:
            llm._ctx.add_call_fact(fact)
    s3 = _run(
        [
            ("system", "人设base"),
            ("user", "你好"),
            ("assistant", "您好"),
            ("user", "我喺拼多多買"),
            ("assistant", "好的"),
            ("user", "唔係拼多多，係淘宝"),
        ]
    )
    assert s3.startswith(s2 + "\n"), "更正不得回溯重写冻结尾部(严格前缀)"
    assert "以淘宝为准" in s3  # 新尾部携带更正
    assert "拼多多" in s3  # 旧 user 冻结重放原样保留(≤8 轮自然截断)


# ---- 接线 pin ----

def test_agent_wiring_source_pins():
    assert "extract_fact_updates(" in AGENT_SRC
    assert "context_state.supersede_call_fact(_needle, _fact)" in AGENT_SRC
    assert 'os.environ.get("BOK_FACT_CORRECTION", "1") == "1"' in AGENT_SRC


def test_forward_env_registered():
    assert '"BOK_FACT_CORRECTION"' in BOK_SRC
