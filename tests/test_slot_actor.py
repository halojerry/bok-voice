"""D1 槽位化 actor 纯函数层（slot_actor.build_slot_system/build_slot_task_block）。

规格（docs/superpowers/plans/2026-10-01-first-principles-rework.md §三 D1）：
system=角色卡（目标 250-450c、硬帽 600c：人设压缩+facts_line+语言块+口吻/长度
规则+2 条压缩回应范例）；任务块（目标 100-250c：当前步/首轮底稿首行/命中分支
应答 + 事实槽三源 + 8 字锚），**绝不包含**总览/共享规则/步骤纪律/verdict 指引/
状态标记/记忆摘要。语言纯度：共享区标准书面中文，粤语块仅 cantonese 通话渲染。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.slot_actor import (  # noqa: E402
    SLOT_SYSTEM_MAX_CHARS,
    build_slot_system,
    build_slot_task_block,
    compose_slot_user_message,
    slot_actor_enabled,
)

# 与 tests/test_agent_language_follow._DIALECT_MARKS 同款粤语特征字（共享区禁令）。
_DIALECT_MARKS = "唔係嘅咗喺嚟嘢冇啲嗰㗎乜睇攞"

_FACTS = "已知客户信息:姓名:陳大文 快递单号:七八九零 物流公司:順豐 收货地址:香港觀塘。信息缺失时向客户确认,不要编造。"
_PERSONA = {"name": "小嵐", "company": "集運中轉倉", "language": "cantonese", "tone": "親切"}


def test_slot_gate_default_off(monkeypatch):
    """闸缺省 "0"=旧路径；只有显式 "1" 开（读法与 BOK_TAIL_SLIM 同款字面量）。"""
    monkeypatch.delenv("BOK_SLOT_ACTOR", raising=False)
    assert slot_actor_enabled() is False
    for v in ("0", "", "true", "yes"):
        monkeypatch.setenv("BOK_SLOT_ACTOR", v)
        assert slot_actor_enabled() is False, v
    monkeypatch.setenv("BOK_SLOT_ACTOR", "1")
    assert slot_actor_enabled() is True


def test_slot_system_card_size_and_required_blocks():
    """角色卡四要件在场且体积在目标带（实测 zh 250c 级 / cantonese 344c）。"""
    card_zh = build_slot_system(
        persona={"name": "客服小藍", "company": "集運中轉倉", "language": "zh"},
        template=None,
        facts=_FACTS,
        language="zh",
    )
    card_canto = build_slot_system(
        persona=_PERSONA,
        template={"tone_override": "港式親切"},
        facts=_FACTS,
        language="cantonese",
    )
    for tag, card in (("zh", card_zh), ("cantonese", card_canto)):
        assert 250 <= len(card) <= 450, (tag, len(card))
        assert len(card) <= SLOT_SYSTEM_MAX_CHARS
        assert "【语言】" in card and "【口吻与长度】" in card and "【回应范例】" in card
        assert _FACTS in card, "facts_line 必须进角色卡"
        assert card.count("→") >= 2, "2 条压缩回应范例（答所问一句+带回一句）"
    # 人设压缩：name/company 在场；tone_override 优先于 persona.tone。
    assert "客服小藍" in card_zh and "集運中轉倉" in card_zh
    assert "港式親切" in card_canto and "親切。" in card_canto


def test_slot_system_language_blocks_and_purity():
    """zh 卡零粤语特征字；粤语块（含港式词表压缩版）仅 cantonese 通话渲染。"""
    zh = build_slot_system(persona=None, template=None, facts="", language="zh")
    canto = build_slot_system(persona=None, template=None, facts="", language="cantonese")
    assert not [ch for ch in _DIALECT_MARKS if ch in zh]
    assert "港式粵語" not in zh and "速遞" not in zh
    assert "速遞" in canto and "唔該晒" in canto
    assert "港式粵語" in canto and "简体" not in canto
    en = build_slot_system(persona=None, template=None, facts="", language="en")
    assert "【Language】" in en and "English" in en
    # 未知/空语言 fail-safe 回 zh 块（不冒粵语风格进未知通话）。
    assert build_slot_system(language="klingon") == zh


def test_slot_system_hard_cap_deterministic():
    """超长 facts 触发确定性降级：先弃范例、facts 按剩余预算截断「…」；
    语言/口吻块保底；同输入恒同字节。"""
    huge = "已知客户信息:" + ("某" * 2000)
    card = build_slot_system(persona=_PERSONA, template=None, facts=huge, language="cantonese")
    assert len(card) <= SLOT_SYSTEM_MAX_CHARS
    assert "…" in card
    assert card == build_slot_system(persona=_PERSONA, template=None, facts=huge, language="cantonese")
    # 降级①先弃范例（语言/口吻块仍在——每轮都需要，绝不被超长 facts 挤掉）。
    assert "【语言】" in card and "【口吻与长度】" in card
    assert "【回应范例】" not in card


def test_slot_task_block_shape_and_only_if_present():
    """任务块：当前步/底稿/应对/事实槽/锚按固定序；缺数据行不出（有才带）。"""
    view = {
        "state": "steps",
        "step_no": 3,
        "total": 8,
        "goal": "核實購買平台",
        "script": "咁你係喺邊個平台買㗎？",
        "branch": "逐档讲：金额不足 100 蚊，申请 300 到 600 蚊赔偿。",
        "overview": "话术流程总览（不得渲染）",
        "rules": "共享规则（不得渲染）",
    }
    block = build_slot_task_block(
        view=view,
        object_brief="拼多多買嘅衫\n想快啲搞掂",
        call_facts=["平台 拼多多", "WhatsApp 六五一二三四五六"],
        whatsapp_note="65123456",
        anchor="【你上一句】「收到，拼多」",
    )
    assert block.startswith("【当前步】第3/8步：核實購買平台")
    assert "【本步底稿】咁你係喺邊個平台買㗎？" in block
    assert "【本步应对】逐档讲" in block
    assert "【客户资料】拼多多買嘅衫；想快啲搞掂" in block
    assert "【通话中客户已讲】平台 拼多多；WhatsApp 六五一二三四五六" in block
    assert "【已记录客户 WhatsApp】65123456" in block
    assert block.endswith("【你上一句】「收到，拼多」")
    assert "总览" not in block and "共享规则" not in block, "未知键绝不渲染（协议零泄漏）"
    # 目标带：实测典型 100-250c（事实槽满时允许更长，测试用典型档）。
    assert 100 <= len(block) <= 260, len(block)


def test_slot_task_block_minimal_and_terminal_states():
    """缺数据行不出；closing/done 给状态行（与 legacy closing_text/done_text 同语义）。"""
    minimal = build_slot_task_block(view={"state": "steps", "step_no": 1, "total": 2, "goal": "確認身份"})
    assert minimal == "【当前步】第1/2步：確認身份"
    closing = build_slot_task_block(view={"state": "closing"}, anchor="【你上一句】「好嘅」")
    assert "礼貌收尾" in closing and "不推销" in closing and closing.endswith("【你上一句】「好嘅」")
    done = build_slot_task_block(view={"state": "done"})
    assert "流程已走完" in done and "不要主动讲再见" in done
    assert build_slot_task_block(view=None) == ""


def test_compose_slot_user_message_order():
    """任务块在前、客户话在后（D1 规格形状；chat 冻结点与投机预热共用单点）。"""
    assert compose_slot_user_message("【当前步】第1/2步", "你好") == "【当前步】第1/2步\n\n你好"
    assert compose_slot_user_message("", "你好") == "你好"
    assert compose_slot_user_message("【当前步】", "") == "【当前步】"
    assert compose_slot_user_message("", "") == ""
