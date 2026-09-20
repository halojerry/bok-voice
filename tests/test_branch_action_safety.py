"""F4 破坏性动作双护栏单测（2026-09-20 验收实证 call-23516077）。

现象：客户说「这个事情嘛你等等先我还想想」，ASR 把弱尾音节抄成热词表里的
「打错电话」（模板 hotwords 偏置）→ 家族+bigram 模糊命中「说打错电话了」
分支 →【收线】真把客户挂了（hold 腿因此 FAIL：verify 轮无人应答）。

两道护栏（只作用于 refuse 动作；handoff/jump/hold/无动作分支维持现有模糊
匹配语义）：
① 确定性命中（BOK_BRANCH_REFUSE_CONFIRM）：条件核心词（拆「/」「、」「,」
   「或」多选、剥引导动词/尾语气词）必须【字面】出现在客户原话里；纯
   家族/bigram 模糊命中不足以收线。
② 热词幻觉（BOK_BRANCH_REFUSE_HOTWORD_GUARD）：整轮（或末子句）剥词表词后
   剩余过短/为空 → 判 ASR 抄词表，不收线。词表=模板 hotwords+行业词+对象
   字段（asr_hotword_context 组装、_parse_vocab_terms 反解），取不到退化为
   长度近似（核心词命中且整轮净长 ≤ 核心词长+2）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in ("apps/agent", "packages/core", "tools"):
    sp = str(ROOT / p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

import bok  # noqa: E402

from agent_runtime.agent import branch_hit_plan  # noqa: E402
from agent_runtime.flow import (  # noqa: E402
    BRANCH_ACTION_HANDOFF,
    BRANCH_ACTION_REFUSE,
    OBJECTION,
    REFUSE,
    hotword_only_by_length,
    hotword_only_utterance,
    refuse_condition_confirmed,
    refuse_condition_options,
)
from agent_runtime.providers import livekit_plugins as lp  # noqa: E402

# probe_branch_action.py 同款探针模板第 2 步 ref（真实收线分支在场）
_REF_STEP2 = (
    "我这边系统显示这个包裹送达到的时候家里没有人签收，想跟您核实一下。\n"
    "如果客户说打错电话了→【收线】不好意思打扰了，我们再核对一下资料，再见\n"
    "如果客户要找真人客服→【转人工】好的，我帮您转接人工同事\n"
    "如果客户说包裹几时送到→【跳第5步】\n"
    "如果客户说等等先→【留本步】好的，您慢慢看\n"
    "如果客户查运单号是多少→这个包裹编号我帮您查过了"
)
# probe_branch_action.py 同款模板热词（「打错电话」在词表里=抄词来源）
_HOTWORDS = "打错电话,真人客服,运单号,包裹,几时送到,签收,理赔"
# 生产组装形态：asr_hotword_context → 「Vocabulary: …」 → _parse_vocab_terms 反解
_VOCAB_TERMS = lp._parse_vocab_terms(f"Vocabulary: {_HOTWORDS}")
# F4 实弹轮（call-23516077 hold 腿 turns 表原话）
_INCIDENT_TEXT = "你等等先，我还想。打错电话。"
# probe refuse 腿真实触发语（正常收线轮，护栏不能误伤）
_REAL_REFUSE_TEXT = "你是不是打错电话了我不是这个人"


def _plan(user_text: str, ref: str = _REF_STEP2, verdict: str = OBJECTION, **kw):
    return branch_hit_plan(
        action_enabled=True,
        canned_enabled=True,
        step_index=1,
        closing=False,
        paused=False,
        done=False,
        user_text=user_text,
        goal="说明来意核实签收",
        ref=ref,
        wa_captured=False,
        verdict=verdict,
        vars_map={},
        hotword_terms=_VOCAB_TERMS,
        **kw,
    )


# ---- 纯函数：确定性命中（护栏①）---------------------------------------------


def test_condition_options_split_and_strip():
    """多选拆分 + 引导动词/尾语气词剥离。"""
    assert refuse_condition_options("说打错电话了") == ("打错电话",)
    assert refuse_condition_options("打错电话/不是本人") == ("打错电话", "不是本人")
    assert refuse_condition_options("打错电话、不是本人") == ("打错电话", "不是本人")
    assert refuse_condition_options("打错电话，不是本人") == ("打错电话", "不是本人")
    assert refuse_condition_options("打错电话或不是本人") == ("打错电话", "不是本人")
    assert refuse_condition_options("查运单号是多少") == ("运单号是多少",)
    assert refuse_condition_options("") == ()


def test_condition_confirmed_positive_and_negative():
    """正例：字面命中（原串或去标点归一串）；反例：模糊形态不命中。"""
    assert refuse_condition_confirmed("说打错电话了", _REAL_REFUSE_TEXT) is True
    # 转写夹标点不断开字面命中
    assert refuse_condition_confirmed("说打错电话了", "你。打错、电话了吧") is True
    # 家族/bigram 形态（「打错了电话」隔字）不算字面命中
    assert refuse_condition_confirmed("说打错电话了", "我怀疑你打错了电话") is False
    assert refuse_condition_confirmed("说打错电话了", "你们搞错了号码吧") is False


# ---- 纯函数：热词幻觉（护栏②）-----------------------------------------------


def test_hotword_only_whole_turn():
    """整轮就是词表词 → True。"""
    assert hotword_only_utterance("打错电话。", _VOCAB_TERMS) is True
    assert hotword_only_utterance("打错电话", _VOCAB_TERMS) is True


def test_hotword_only_tail_clause_incident_shape():
    """实弹形态（call-23516077）：末子句整体是词表词 → True。"""
    assert hotword_only_utterance(_INCIDENT_TEXT, _VOCAB_TERMS) is True


def test_hotword_only_real_speech_not_flagged():
    """真实话轮：剥词表后剩余充实 → False（正常收线轮不被误杀）。"""
    assert hotword_only_utterance(_REAL_REFUSE_TEXT, _VOCAB_TERMS) is False
    assert hotword_only_utterance("你们是不是打错电话了，我不需要", _VOCAB_TERMS) is False


def test_hotword_only_short_rest():
    """剥词表后剩余 ≤2 字 → True（「打错电话啊」）。"""
    assert hotword_only_utterance("打错电话啊", _VOCAB_TERMS) is True


def test_hotword_only_empty_vocab_is_false():
    """词表空 → False（退化近似由 hotword_only_by_length 承担）。"""
    assert hotword_only_utterance("打错电话", ()) is False


def test_hotword_only_by_length_degraded():
    """退化近似：核心词命中且整轮净长 ≤ 核心词长+2。"""
    assert hotword_only_by_length("打错电话啊", "说打错电话了") is True
    assert hotword_only_by_length("我还想想打错电话", "说打错电话了") is False
    # 核心词不字面命中 → False（不与护栏①重复）
    assert hotword_only_by_length("你们搞错了号码", "说打错电话了") is False


# ---- branch_hit_plan 集成 ----------------------------------------------------


def test_incident_turn_refuse_skipped_hotword_only(capsys):
    """F4 实弹回归：实弹轮唔再派发【收线】，落回 LLM/内置 REFUSE 车道。"""
    plan = _plan(_INCIDENT_TEXT)
    assert plan is None
    out = capsys.readouterr().out
    assert "BRANCH_ACTION refuse_skipped reason=hotword_only" in out


def test_incident_turn_blocked_even_with_refuse_verdict(capsys):
    """护栏②係最终否决：即使 verdict=REFUSE 过了护栏①，实弹形态仍被拦。"""
    plan = _plan(_INCIDENT_TEXT, verdict=REFUSE)
    assert plan is None
    out = capsys.readouterr().out
    assert "BRANCH_ACTION refuse_skipped reason=hotword_only" in out


def test_refuse_verdict_dispatches_without_literal_hit():
    """call-179c7608 二修回归：verdict=REFUSE（内置明确拒绝）时,条件词不字面
    在场也派发——允许用运营为这条分支写的收线台词（比通用收尾稿贴语境）。

    语料「你打错了电话」：与条件「说打错电话了」共享 bigram（打错/电话）被
    选branch、但不含字面「打错电话」（中间隔「了」）——正走护栏① 的 OR 半边。
    注：真栈片段2「我不是这个人。」的 rule_verdict 实为 objection（_DENY_RE），
    且它与任何收线条件都无 bigram——那种轮在 match_step_branch 层就返回 None
    （上游于本护栏），唔属本护栏的管辖面。"""
    plan = _plan("你打错了电话", verdict=REFUSE)
    assert plan is not None
    assert plan["action"] == BRANCH_ACTION_REFUSE
    assert "不好意思打扰了" in plan["text"]


def test_normal_refuse_still_dispatched(capsys):
    """正常拒绝轮（字面+词表剥离后剩余充实）照常收线。"""
    plan = _plan(_REAL_REFUSE_TEXT)
    assert plan is not None
    assert plan["action"] == BRANCH_ACTION_REFUSE
    assert "不好意思打扰了" in plan["text"]
    assert "refuse_skipped" not in capsys.readouterr().out


def test_fuzzy_family_hit_without_literal_skipped(capsys):
    """家族+bigram 命中但条件词不字面在场、verdict 又非 REFUSE → 不收线
    （not_literal）——仅模糊相似、无内置拒绝依据不足以让 AI 挂客户电话。"""
    plan = _plan("我怀疑你打错了电话")
    assert plan is None
    out = capsys.readouterr().out
    assert "BRANCH_ACTION refuse_skipped reason=not_literal" in out


def test_multi_option_condition_any_hit_dispatches():
    """条件多选（/）：任一项字面命中即可收线。"""
    ref = (
        "核实来意。\n"
        "如果客户打错电话/不是本人→【收线】不好意思打扰了，再见\n"
        "如果客户嫌麻烦→【留本步】好嘅慢慢嚟"
    )
    plan = _plan("我看你们是不是搞错了，我不是本人", ref=ref)
    assert plan is not None
    assert plan["action"] == BRANCH_ACTION_REFUSE


def test_handoff_branch_unaffected_by_guards(capsys):
    """护栏只作用于 refuse：handoff 模糊命中照常派发（语义零改动）。"""
    ref = "請問係咪張小姐？\n如果客户要投诉→【转人工】我帮您转接同事"
    # 「我要投诉」按词表剥完剩「我要」（≤2 字）——若护栏误作用于此会被跳过
    plan = _plan("我要投诉", ref=ref, verdict=OBJECTION)
    assert plan is not None
    assert plan["action"] == BRANCH_ACTION_HANDOFF
    assert "refuse_skipped" not in capsys.readouterr().out


# ---- kill-switch（各自可回退）------------------------------------------------


def test_kill_refuse_confirm_restores_fuzzy_dispatch(monkeypatch, capsys):
    """BOK_BRANCH_REFUSE_CONFIRM=0 → 模糊命中照旧派发（护栏①回退口）。"""
    monkeypatch.setenv("BOK_BRANCH_REFUSE_CONFIRM", "0")
    plan = _plan("我怀疑你打错了电话")
    assert plan is not None
    assert plan["action"] == BRANCH_ACTION_REFUSE
    capsys.readouterr()  # 清缓冲，隔离断言面


def test_kill_hotword_guard_restores_dispatch(monkeypatch, capsys):
    """BOK_BRANCH_REFUSE_HOTWORD_GUARD=0 → 实弹轮照旧派发（护栏②回退口）。"""
    monkeypatch.setenv("BOK_BRANCH_REFUSE_HOTWORD_GUARD", "0")
    plan = _plan(_INCIDENT_TEXT)
    assert plan is not None
    assert plan["action"] == BRANCH_ACTION_REFUSE
    capsys.readouterr()


def test_no_vocab_terms_falls_back_to_length_approx(monkeypatch, capsys):
    """词表取不到（BOK_ASR_HOTWORDS=0 等场景）→ 长度近似仍拦整轮≈核心词的轮。"""
    plan = branch_hit_plan(
        action_enabled=True,
        canned_enabled=True,
        step_index=1,
        closing=False,
        paused=False,
        done=False,
        user_text="打错电话啊",
        goal="说明来意核实签收",
        ref=_REF_STEP2,
        wa_captured=False,
        verdict=OBJECTION,
        vars_map={},
        hotword_terms=(),  # 词表缺位
    )
    assert plan is None
    assert "refuse_skipped reason=hotword_only" in capsys.readouterr().out


# ---- 源级 pin ----------------------------------------------------------------


def test_call_sites_pass_hotword_terms():
    """源级 pin：两个 branch_hit_plan 调用点都喂词表（罐头腿同享护栏）。"""
    src = (ROOT / "apps/agent/agent_runtime/agent.py").read_text(encoding="utf-8")
    assert src.count("hotword_terms=_parse_vocab_terms(_hotword_ctx)") == 2
    assert "reason=hotword_only" in src
    assert "reason=not_literal" in src


def test_forward_env_registered():
    for key in ("BOK_BRANCH_REFUSE_CONFIRM", "BOK_BRANCH_REFUSE_HOTWORD_GUARD"):
        assert key in bok._FORWARD_ENV, f"{key} 未登记 _FORWARD_ENV"
