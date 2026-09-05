"""P1 编排层：抢跑 churn 杀 / cached tokens 可视 / MiniMax 默认音色 / RAG 门控翻转 / 对象档案接线。

防回归点：
1. PREEMPTIVE_MAX_RETRIES 默认 3（A/B 两线 entrypoint 选项构造同源 helper）——
   旧 8 会把误判轮的 prefill 白烧放大（churn）。
2. 抢跑「标记轮暂停」机制钉死框架契约：1.7.1 每次 speculative trigger 现场
   重读 session.options.preemptive_generation（property live 读），且 PausableAgent
   只传 instructions → agent 级 _turn_handling 无 preemptive_generation 键、
   session 级原地 mutation 唔会被盖。官方若改成构造时快照，本测试红 → 要求重审。
   机制默认关（PREEMPTIVE_DISABLE_ON_MARKER，时序分析见 _marker_pause_enabled）。
3. LLM_TTFT_MS 行带 cached=prompt_cached_tokens/prompt_tokens（KV-cache 命中可视）。
4. MiniMax 空 voice map 兜底 Cantonese_crisp_news_anchor_vv2（zh+cantonese 双键
   同值，_resolve_voice 对缺键回落 zh，任何语言态都解析得到 → 不会 NO_VOICE）。
5. RAG 默认全关（CONTEXT_RAG=1 才开）；对象档案走 set_object_brief 一次装配。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

import agent_runtime.interpret as interp_mod  # noqa: E402
from agent_runtime.agent import (  # noqa: E402
    _MINIMAX_DEFAULT_VOICE,
    _assemble_minimax_voice_map,
    _build_object_brief,
    _context_rag_enabled,
    _format_llm_metrics,
    _marker_pause_enabled,
    _preemptive_generation_opts,
    _wire_object_brief,
)
from agent_runtime.providers.livekit_plugins import ContextState  # noqa: E402


# ---- (a) 抢跑预算默认 3（两线 entrypoint 选项构造）----


def test_preemptive_max_retries_default_three(monkeypatch):
    monkeypatch.delenv("PREEMPTIVE_MAX_RETRIES", raising=False)
    assert _preemptive_generation_opts()["max_retries"] == 3
    assert interp_mod._preemptive_generation_opts()["max_retries"] == 3


def test_preemptive_max_retries_env_override(monkeypatch):
    monkeypatch.setenv("PREEMPTIVE_MAX_RETRIES", "5")
    assert _preemptive_generation_opts()["max_retries"] == 5
    assert interp_mod._preemptive_generation_opts()["max_retries"] == 5


def test_preemptive_enabled_and_tts_defaults(monkeypatch):
    monkeypatch.delenv("PREEMPTIVE_GENERATION", raising=False)
    monkeypatch.delenv("PREEMPTIVE_TTS", raising=False)
    opts = _preemptive_generation_opts()
    assert opts["enabled"] is True
    assert opts["preemptive_tts"] is False


def test_marker_pause_default_off(monkeypatch):
    """P1 fix round 1:标记轮暂停默认关——时序分析见 _marker_pause_enabled
    (下一轮 speculation 快照一致本会命中,压掉=纯损失);机制留作实验档。"""
    monkeypatch.delenv("PREEMPTIVE_DISABLE_ON_MARKER", raising=False)
    assert _marker_pause_enabled() is False


def test_marker_pause_env_opt_in(monkeypatch):
    monkeypatch.setenv("PREEMPTIVE_DISABLE_ON_MARKER", "1")
    assert _marker_pause_enabled() is True


def test_livekit_preemptive_opts_are_read_live():
    """契约钉死：标记轮暂停（max_retries=0 原地 mutation）依赖两个 1.7.1 事实。

    ① AgentSessionOptions.preemptive_generation 是 property，直读存储的
       turn_handling dict（唔系构造时快照）；
    ② PausableAgent 只传 instructions → Agent.__init__ 走 _migrate_turn_handling()
       → agent 级 _turn_handling 无 preemptive_generation 键（agent 级 merge 会
       盖 session 级，空 = 不盖）。
    官方行为变了这里显式炸出来要求重审 marker 机制。
    """
    from livekit.agents import Agent
    from livekit.agents.voice.agent_session import AgentSessionOptions

    src = inspect_getsource_property(AgentSessionOptions, "preemptive_generation")
    assert "turn_handling" in src, "preemptive_generation 不再 live 直读 turn_handling"
    agent = Agent(instructions="x")
    assert "preemptive_generation" not in agent._turn_handling


def inspect_getsource_property(cls, name: str) -> str:
    import inspect

    return inspect.getsource(getattr(cls, name).fget)


# ---- (b) LLM metrics 行带 cached= ----


class _FakeLLMMetrics:
    """官方 LLMMetrics 同名字段鸭型（metrics/base.py）。"""

    type = "llm_metrics"
    ttft = 0.35
    prompt_tokens = 542
    prompt_cached_tokens = 418
    completion_tokens = 30
    tokens_per_second = 21.0


def test_llm_metrics_log_includes_cached_tokens():
    line = _format_llm_metrics(_FakeLLMMetrics())
    assert line.startswith("LLM_TTFT_MS 350 (official)")
    assert "cached=418/542" in line
    assert "prompt=542" in line
    assert "gen=30" in line


def test_llm_metrics_tolerates_missing_cache_fields():
    class _Sparse:
        type = "llm_metrics"
        ttft = 0.2
        completion_tokens = 5
        tokens_per_second = 10.0

    line = _format_llm_metrics(_Sparse())
    assert "cached=0/0" in line  # 缺字段按 0 兜底，绝不抛


# ---- (c) MiniMax 空 voice map 默认音色 ----


def test_minimax_empty_voice_map_gets_default():
    for mode in ("single", "per_language"):
        vmap = _assemble_minimax_voice_map(
            persona=None, tts_cfg={}, greet_lang="cantonese", voice_mode=mode
        )
        assert vmap.get("cantonese") == _MINIMAX_DEFAULT_VOICE, mode
        # zh 回落键必须有：MiniMaxTTS._resolve_voice 对缺键回落 zh，
        # 普通话/英语轮唔会 MINIMAX_TTS_NO_VOICE（beep）。
        assert vmap.get("zh") == _MINIMAX_DEFAULT_VOICE, mode


def test_minimax_local_only_voices_filtered_then_defaulted():
    # 人设只绑本地 Qwen3 克隆 → 全被过滤 → 空 map → 默认兜底
    vmap = _assemble_minimax_voice_map(
        persona={"reference_audio": "serena", "language": "cantonese"},
        tts_cfg={},
        greet_lang="cantonese",
        voice_mode="single",
    )
    assert vmap == {"zh": _MINIMAX_DEFAULT_VOICE, "cantonese": _MINIMAX_DEFAULT_VOICE}


def test_minimax_configured_voice_not_overridden():
    vmap = _assemble_minimax_voice_map(
        persona=None,
        tts_cfg={"speaker_cantonese": "Cantonese_GentleLady"},
        greet_lang="cantonese",
        voice_mode="per_language",
    )
    assert vmap == {"cantonese": "Cantonese_GentleLady"}


# ---- (d) RAG 门控默认翻转 ----


def test_context_rag_default_off_for_all_flows(monkeypatch):
    monkeypatch.delenv("CONTEXT_RAG", raising=False)
    assert _context_rag_enabled() is False


def test_context_rag_env_forces_on(monkeypatch):
    monkeypatch.setenv("CONTEXT_RAG", "1")
    assert _context_rag_enabled() is True
    monkeypatch.setenv("CONTEXT_RAG", "0")
    assert _context_rag_enabled() is False


# ---- (e) 对象档案接线：set_object_brief 一次、有界文本 ----


class _RecordingCtx:
    def __init__(self):
        self.calls: list[str] = []

    def set_object_brief(self, text: str) -> None:
        self.calls.append(text)


class _RecordingContextState(ContextState):
    """真 ContextState + 调用记录:一次性与存储语义都走真实实现(防 fake-green)。"""

    def __init__(self):
        super().__init__()
        self.calls: list[str] = []

    def set_object_brief(self, text: str) -> None:
        self.calls.append(text)
        super().set_object_brief(text)


class _FakeCp:
    def __init__(self, snippets=None):
        self._snippets = snippets or []
        self.queries: list[tuple] = []

    async def search_knowledge(self, query, account_id, limit):
        self.queries.append((query, account_id, limit))
        return self._snippets


def _run(coro):
    return asyncio.run(coro)


def test_object_brief_from_card_fields_bounded(monkeypatch):
    """接线走【真】ContextState（P1 fix round 1 钉死 fake-green 洞）：多句背景
    整行截断、备注行存活——旧 set_object_brief 按句号二次切分会把备注静默挤掉。"""
    monkeypatch.delenv("CONTEXT_RAG", raising=False)
    background = "客戶投訴運費計算錯誤。" + "集運件由廣州倉發出後滯留三日。" * 20  # 多句,>150 字
    notes = "備註:客戶偏好粵語溝通。"
    card = {"background": background, "notes": notes}
    ctx = _RecordingContextState()
    brief = _run(_wire_object_brief(_FakeCp(), object_card=card, account_id="acc-1", context_state=ctx))
    assert len(ctx.calls) == 1  # 至多一次
    assert ctx._object_brief == brief  # wire 产出=真实存储字节
    lines = brief.split("\n")
    assert len(lines) == 2  # background + notes,最多 2 段
    assert lines[0] == background[:149] + "…"  # 多句背景整行截断(唔係切头两句)
    assert lines[1] == notes  # 备注存活
    assert "備註" in ctx.render_instruction_prefix()  # 真前缀渲染


def test_object_brief_no_search_when_card_has_background(monkeypatch):
    monkeypatch.setenv("CONTEXT_RAG", "1")  # 即使逃生口开,卡上有背景也唔检索
    cp = _FakeCp(snippets=[{"text": "不该被检索到的知识"}])
    ctx = _RecordingCtx()
    _run(_wire_object_brief(cp, object_card={"background": "湾仔集运自提点。"}, account_id="acc-1", context_state=ctx))
    assert cp.queries == []
    assert ctx.calls == ["湾仔集运自提点。"]


def test_object_brief_empty_card_rag_off_no_call(monkeypatch):
    monkeypatch.delenv("CONTEXT_RAG", raising=False)
    cp = _FakeCp()
    ctx = _RecordingCtx()
    _run(_wire_object_brief(cp, object_card={}, account_id="acc-1", context_state=ctx))
    assert cp.queries == []  # RAG 关:连检索都唔发
    assert ctx.calls == []  # 空档案唔 set(唔占前缀)


def test_object_brief_empty_card_rag_on_searches_and_compresses(monkeypatch):
    monkeypatch.setenv("CONTEXT_RAG", "1")
    cp = _FakeCp(
        snippets=[
            {"text": "很长的检索条目。" * 100},  # 压到 150
            {"text": "第二条检索。"},
            {"text": "第三条被丢弃。"},
        ]
    )
    ctx = _RecordingCtx()
    brief = _run(_wire_object_brief(cp, object_card={}, account_id="acc-1", context_state=ctx))
    assert cp.queries == [("产品介绍", "acc-1", 2)]  # 空背景回落默认 query,取 top-2
    lines = brief.split("\n")
    assert len(lines) == 2
    assert all(len(ln) <= 150 for ln in lines)
    assert lines[1] == "第二条检索。"


def test_build_object_brief_pure_helper():
    assert _build_object_brief(None) == ""
    assert _build_object_brief({}) == ""
    assert _build_object_brief({"background": "  a\n b  ", "notes": ""}) == "a b"
