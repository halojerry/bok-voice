"""分支动作(2026-09-20 路线 A-②)单测:动作前缀解析 + branch_hit_plan 规划闸门。

A-② 把「如果客户X→就Y」分支从纯提示词素材升为引擎一等出口:应答首部可携带
【收线】/【转人工】/【跳第N步】/【留本步】动作前缀,运行时命中后按动作派发
(收线=收尾态+定时挂断;转人工=打铃不抢话;跳步=镜像话术图 jump 副作用包;
留本步=本轮规则推进让位)。本文件覆盖纯函数面(parse_branch_action 全形态、
branch_hit_plan 闸门表)与漏斗接线源级断言;罐头腿消费语义在
tests/test_branch_canned.py。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in ("apps/agent", "packages/core"):
    sp = str(ROOT / p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

from agent_runtime.agent import branch_hit_plan  # noqa: E402
from agent_runtime.flow import (  # noqa: E402
    BRANCH_ACTION_HANDOFF,
    BRANCH_ACTION_HOLD,
    BRANCH_ACTION_JUMP,
    BRANCH_ACTION_REFUSE,
    FAREWELL,
    OBJECTION,
    QUESTION,
    REFUSE,
    UNCLEAR,
    parse_branch_action,
)

_REF_PLATFORM = (
    "咁您係喺邊個平台買？\n"
    "如果客户嫌赔偿少→我哋會按平台規則盡量幫您爭取。\n"
    "如果客户说不知道哪个平台→唔緊要，打開訂單看看就有平台名。"
)

# 带动作前缀的分支 ref:打错电话→收线(挂断线路由收线台词自己完成)
_REF_WRONG_NUMBER = (
    "請問您係咪張小姐？\n"
    "如果客户打错电话→【收线】唔好意思打搅咗，我哋再核对下资料，拜拜\n"
    "如果客户嫌麻烦→【留本步】好嘅，我哋慢慢嚟。"
)


# ---- parse_branch_action:全形态 ----


def test_parse_refuse_and_hangup_marker():
    assert parse_branch_action("【收线】唔好意思打搅咗，拜拜") == (
        BRANCH_ACTION_REFUSE, 0, "唔好意思打搅咗，拜拜",
    )
    # 【挂断】是【收线】同义标记,同落 refuse
    assert parse_branch_action("【挂断】拜拜") == (BRANCH_ACTION_REFUSE, 0, "拜拜")
    # 标记后直接换行(解析进来已被 strip)→ 空文本,仍消费标记
    assert parse_branch_action("【收线】") == (BRANCH_ACTION_REFUSE, 0, "")


def test_parse_handoff_and_hold_marker():
    assert parse_branch_action("【转人工】我帮您转接同事") == (
        BRANCH_ACTION_HANDOFF, 0, "我帮您转接同事",
    )
    assert parse_branch_action("【留本步】好嘅，我哋慢慢嚟") == (
        BRANCH_ACTION_HOLD, 0, "好嘅，我哋慢慢嚟",
    )


def test_parse_jump_marker_and_zero_step_fallback():
    assert parse_branch_action("【跳第3步】") == (BRANCH_ACTION_JUMP, 3, "")
    assert parse_branch_action("【跳第12步】先办这个") == (BRANCH_ACTION_JUMP, 12, "先办这个")
    # N < 1:标记已消费、退回默认语义——不跳步也不把「【跳第0步】」念出来
    assert parse_branch_action("【跳第0步】直接讲") == ("", 0, "直接讲")
    assert parse_branch_action("【跳第000步】x") == ("", 0, "x")


def test_parse_no_marker_is_byte_identical():
    # 无标记 → 原样逐字节返回(现状语义)
    assert parse_branch_action("安抚并报时效") == ("", 0, "安抚并报时效")
    assert parse_branch_action("") == ("", 0, "")
    assert parse_branch_action("  原样文本  ") == ("", 0, "  原样文本  ")
    # 【…】但不是动作词 → 不消费,整条原样(画布 round-trip 靠原文无损)
    assert parse_branch_action("【不是动作】尾巴") == ("", 0, "【不是动作】尾巴")


def test_parse_marker_whitespace_tolerance():
    # 标记内空白容错:【 收线 】/【跳第 3 步】都算命中
    assert parse_branch_action("【 收线 】 文本") == (BRANCH_ACTION_REFUSE, 0, "文本")
    assert parse_branch_action("【跳第 3 步】好的") == (BRANCH_ACTION_JUMP, 3, "好的")
    assert parse_branch_action("【 留本步 】慢慢嚟") == (BRANCH_ACTION_HOLD, 0, "慢慢嚟")


# ---- branch_hit_plan:闸门表 ----


def _plan(**kw):
    base = dict(
        action_enabled=True,
        canned_enabled=True,
        step_index=1,  # 0-based → 第 2 步
        closing=False,
        paused=False,
        done=False,
        user_text="为什么赔这么少",
        goal="核实购买平台",
        ref=_REF_PLATFORM,
        wa_captured=False,
        verdict=OBJECTION,
        vars_map={},
    )
    base.update(kw)
    return branch_hit_plan(**base)


def test_plan_hit_unmarked_branch_defaults():
    plan = _plan()
    assert plan is not None
    assert plan == {
        "cond": "嫌赔偿少",
        "action": "",
        "jump": 0,
        "text": "我哋會按平台規則盡量幫您爭取。",
        "hold": True,
    }


def test_plan_gate_total_switch_off():
    # 两腿全关 → None(零开销直落);单腿开照常命中
    assert _plan(action_enabled=False, canned_enabled=False) is None
    assert _plan(canned_enabled=False) is not None  # 仅动作腿
    assert _plan(action_enabled=False) is not None  # 仅罐头腿(A-① 回退档)


def test_plan_gate_basic_paused_done_empty():
    assert _plan(paused=True) is None
    assert _plan(done=True) is None
    assert _plan(user_text="   ") is None
    assert _plan(ref="") is None


def test_plan_gate_wa_step_not_captured():
    wa_ref = "請問您嘅WhatsApp號碼係幾多？\n如果客户问为什么→平台流程需要，纯记录用途。"
    assert (
        _plan(goal="加客戶WhatsApp", ref=wa_ref, user_text="为什么要加我", verdict=QUESTION)
        is None
    )
    out = _plan(
        goal="加客戶WhatsApp", ref=wa_ref, user_text="为什么要加我",
        verdict=QUESTION, wa_captured=True,
    )
    assert out is not None and out["text"] == "平台流程需要，纯记录用途。"


def test_plan_no_branch_step():
    assert _plan(ref="我哋係集運中轉倉，通知您件貨到咗。") is None
    assert _plan(user_text="随便讲句", verdict=UNCLEAR, ref="普通正稿没有分支行") is None


def test_plan_step0_only_refuse_handoff_pass():
    # 第 1 步(身份步)「任何非拒绝回应都推进」铁律:留本步/跳步/无动作分支都
    # 不许抢;收线与打铃不推进流程,放行
    assert _plan(step_index=0) is None  # 无动作
    assert _plan(step_index=0, ref=_REF_WRONG_NUMBER, user_text="你打错电话了") is not None
    assert (
        _plan(step_index=0, ref=_REF_WRONG_NUMBER, user_text="你打错电话了")["action"]
        == BRANCH_ACTION_REFUSE
    )
    handoff_ref = "請問係咪張小姐？\n如果客户要投诉→【转人工】我帮您转接同事"
    p = _plan(step_index=0, ref=handoff_ref, user_text="我要投诉你哋", verdict=OBJECTION)
    assert p is not None and p["action"] == BRANCH_ACTION_HANDOFF
    hold_ref = "請問係咪張小姐？\n如果客户嫌麻烦→【留本步】好嘅慢慢嚟"
    assert _plan(step_index=0, ref=hold_ref, user_text="好麻烦啊", verdict=OBJECTION) is None
    jump_ref = "請問係咪張小姐？\n如果客户嫌太慢→【跳第3步】"
    assert _plan(step_index=0, ref=jump_ref, user_text="你们太慢啦", verdict=OBJECTION) is None
    # 限制只对动作腿生效:纯罐头腿(A-① 回退档)第 1 步照旧可命中
    assert _plan(step_index=0, action_enabled=False) is not None


def test_plan_closing_only_refuse_pass():
    assert _plan(closing=True) is None  # 无动作分支不抢收线轮
    p = _plan(closing=True, ref=_REF_WRONG_NUMBER, user_text="你打错电话了")
    assert p is not None and p["action"] == BRANCH_ACTION_REFUSE


def test_plan_refuse_verdict_yields_only_unmarked():
    # 客户明确拒绝轮:无动作分支让位收线话术(A-① 原闸);【收线】动作本身
    # 是幂等收线,不受此闸
    assert _plan(verdict=REFUSE) is None
    assert _plan(verdict=FAREWELL) is None
    p = _plan(ref=_REF_WRONG_NUMBER, user_text="你打错电话了", verdict=REFUSE)
    assert p is not None and p["action"] == BRANCH_ACTION_REFUSE


def test_plan_placeholder_residual_blocks_answer_actions():
    ref = "正稿\n如果客户问运费→您的运费是{金额}元。"
    assert _plan(ref=ref, user_text="运费是多少", verdict=QUESTION, vars_map={}) is None
    # 【留本步】配占位残留同样不认——不许 hold 钉死流程
    hold_ref = "正稿\n如果客户问运费→【留本步】您的运费是{金额}元。"
    assert _plan(ref=hold_ref, user_text="运费是多少", verdict=QUESTION, vars_map={}) is None
    # 变量在场 → 正常渲染命中
    hit = _plan(ref=ref, user_text="运费是多少", verdict=QUESTION, vars_map={"金额": "三十"})
    assert hit is not None and hit["text"] == "您的运费是三十元。"


def test_plan_empty_render_blocks_hold():
    # 渲染后空文本 + hold → None(不许把流程钉死)
    ref = "正稿\n如果客户问运费→【留本步】   "
    assert _plan(ref=ref, user_text="运费是多少", verdict=QUESTION) is None


def test_plan_hold_mapping():
    # hold 语义:handoff(打铃不抢话)=False,其余动作(含无动作)=True
    assert _plan()["hold"] is True  # 无动作
    p_ref = _plan(ref=_REF_WRONG_NUMBER, user_text="你打错电话了")
    assert p_ref["hold"] is True  # refuse
    handoff_ref = "正稿\n如果客户要投诉→【转人工】我帮您转接同事"
    assert _plan(ref=handoff_ref, user_text="我要投诉你哋")["hold"] is False
    hold_ref = "正稿\n如果客户嫌麻烦→【留本步】好嘅，我哋慢慢嚟。"
    assert _plan(ref=hold_ref, user_text="好麻烦啊")["hold"] is True
    jump_ref = "正稿\n如果客户嫌太慢→【跳第3步】"
    assert _plan(ref=jump_ref, user_text="你们太慢啦")["hold"] is True


def test_plan_jump_carries_step_value():
    jump_ref = "正稿\n如果客户嫌太慢→【跳第3步】"
    p = _plan(ref=jump_ref, user_text="你们太慢啦")
    assert p is not None
    assert p["action"] == BRANCH_ACTION_JUMP and p["jump"] == 3
    assert p["text"] == ""  # 动作标记已消费,无剩余台词
    # N<1 退默认语义:当普通无动作分支处理
    p0 = _plan(ref="正稿\n如果客户嫌太慢→【跳第0步】先办这个", user_text="你们太慢啦")
    assert p0 is not None
    assert p0["action"] == "" and p0["jump"] == 0 and p0["text"] == "先办这个"


def test_plan_refuse_text_stripped_and_cond_kept():
    p = _plan(ref=_REF_WRONG_NUMBER, user_text="你打错电话了")
    assert p is not None
    assert p["cond"] == "打错电话"
    assert p["text"] == "唔好意思打搅咗，我哋再核对下资料，拜拜"


# ---- 漏斗接线(源级断言,与 test_flow_graph_runtime 同姿势) ----


def _agent_src() -> str:
    return (ROOT / "apps/agent/agent_runtime/agent.py").read_text(encoding="utf-8")


def test_funnel_early_block_wiring():
    src = _agent_src()
    # 旧函数不许留兼容 shim(禁止死代码)
    assert "def branch_canned_pick" not in src
    assert "def branch_hit_plan" in src
    # 早段评估块在 verdict 车道之前(say 待念锁计算之后)
    i_eval = src.index("# ---- 分支动作早段评估")
    i_refuse_lane = src.index("if verdict == REFUSE:")
    assert i_eval < i_refuse_lane
    assert 'os.environ.get("BOK_BRANCH_ACTION", "1") == "1"' in src  # 默认开
    # 三个动作派发臂的归因日志与 provider 标记
    assert "BRANCH_ACTION refuse step=" in src
    assert "BRANCH_ACTION handoff step=" in src
    assert "BRANCH_ACTION jump step=" in src
    assert "BRANCH_ACTION jump_noop step=" in src
    assert "BRANCH_ACTION hold step=" in src
    assert '_turn_origin["provider"] = "branch-refuse"' in src
    assert '_turn_origin["provider"] = "branch-notify"' in src
    assert '_turn_origin["provider"] = "branch-jump"' in src
    # 收线台词直念出口:raise 在流程 try 之外(StopResponse 是 Exception 子类)
    i_exit = src.index("if _branch_refuse_say:")
    i_try_end = src.index("# pragma: no cover - 流程推进失败不阻断回复")
    i_stall = src.index("# ---- stall 升级阶梯")
    assert i_try_end < i_exit < i_stall
    assert "raise StopResponse()" in src[i_exit:i_stall]
    # 规则推进让位:分支命中轮引擎不再自动推进
    assert "and not _branch_hold" in src
    # env 立法:BOK_BRANCH_ACTION 进 _FORWARD_ENV 白名单(漏登记 CI 红)
    bok_src = (ROOT / "tools/bok.py").read_text(encoding="utf-8")
    assert '"BOK_BRANCH_ACTION"' in bok_src
