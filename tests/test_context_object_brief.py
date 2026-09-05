"""P1-B 对象档案 + RAG 渲染门单测。

- set_object_brief:2 行 × 150 字有界、行边界=显式换行（行单元语义,绝不在
  句号处二次切分——多句背景+备注两行都必须存活）、硬截断、确定性。
- 【对象档案】进稳定指令前缀(话术总览之后、仅非空时),步骤推进不动前缀字节。
- render_context_tail 默认(rag_enabled=False)不出知识/联网节;置 True 后恢复。
"""

from __future__ import annotations

from agent_runtime.providers.livekit_plugins import ContextState


# ---- set_object_brief:行单元语义 + 有界 + 确定性 ----

def test_object_brief_single_line_with_periods_stays_one_line():
    """多句背景（只有句号、无换行）是【一个】行单元,绝不在句号处切碎。"""
    ctx = ContextState()
    ctx.set_object_brief("林先生。尊貴會員。要轉辦。第四句。第五句。")
    assert ctx._object_brief == "林先生。尊貴會員。要轉辦。第四句。第五句。"


def test_object_brief_capped_to_two_explicit_lines():
    """行边界=显式换行:取前 2 行,第 3 行丢弃。"""
    ctx = ContextState()
    ctx.set_object_brief("第一行背景。\n第二行备注。\n第三行唔要。")
    assert ctx._object_brief.count("\n") == 1  # 恰好 2 行
    assert ctx._object_brief.split("\n") == ["第一行背景。", "第二行备注。"]
    assert "第三行" not in ctx._object_brief


def test_object_brief_hard_truncates_long_lines():
    ctx = ContextState()
    long_line = "很" * 300  # 无换行单行 >150 字
    ctx.set_object_brief(long_line)
    assert len(ctx._object_brief) == 150  # 149 字 + 「…」
    assert ctx._object_brief.endswith("…")


def test_object_brief_each_line_truncated_independently():
    ctx = ContextState()
    ctx.set_object_brief("甲" * 200 + "\n" + "乙" * 200 + "\n第三行唔要")
    lines = ctx._object_brief.split("\n")
    assert len(lines) == 2
    assert all(len(line) == 150 for line in lines)
    assert lines[0].startswith("甲") and lines[1].startswith("乙")
    assert "第三行" not in ctx._object_brief


def test_object_brief_multisentence_background_and_notes_both_survive():
    """行单元契约的集成回归（P1 fix round 1）：多句背景（>150 字,含多个。）
    + 备注=显式两行 → 两行都存活,背景整行截断、备注原样保留。旧实现按句号
    二次切分,会把备注静默挤掉（只留背景前两句）——本测试钉死该洞。
    """
    ctx = ContextState()
    background = "客戶投訴運費計算錯誤。" + "集運件由廣州倉發出後滯留三日。" * 10  # 多句,>150 字
    notes = "備註:客戶偏好粵語溝通,之前承諾三日內回覆。"
    ctx.set_object_brief(background + "\n" + notes)  # _wire_object_brief 的产出形状
    lines = ctx._object_brief.split("\n")
    assert len(lines) == 2
    assert lines[0] == background[:149] + "…"  # 背景整行截断,唔係切头两句
    assert lines[1] == notes  # 备注存活,静默丢弃已绝
    assert "備註" in ctx.render_instruction_prefix()


def test_object_brief_deterministic():
    ctx1 = ContextState()
    ctx2 = ContextState()
    text = "客戶投訴運費。\n集運件滯留三日。後續轉辦。"
    ctx1.set_object_brief(text)
    ctx2.set_object_brief(text)
    assert ctx1._object_brief == ctx2._object_brief  # 同输入同字节
    # 重设同样内容:再确定性(无时序/累积效应)。
    ctx1.set_object_brief(text)
    assert ctx1._object_brief == ctx2._object_brief


def test_object_brief_empty_clears():
    ctx = ContextState()
    ctx.set_object_brief("有内容。")
    ctx.set_object_brief("")
    assert ctx._object_brief == ""
    ctx.set_object_brief(None)  # type: ignore[arg-type]
    assert ctx._object_brief == ""


# ---- 前缀集成:【对象档案】在话术总览之后,整场字节稳定 ----

def test_prefix_contains_brief_after_flow_overview():
    ctx = ContextState()
    ctx.set_user_language("cantonese")
    ctx.set_flow("第1步:确认\n第2步:引導", "流程第 1/2 步")
    ctx.set_object_brief("林先生,尊貴會員。")
    prefix = ctx.render_instruction_prefix()
    assert "【对象档案】" in prefix
    assert "林先生,尊貴會員。" in prefix
    # 位置:在话术总览之后、其余指令节之后(前缀最后一节)。
    assert prefix.index("【话术流程总览") < prefix.index("【对象档案】")
    assert prefix.index("【用户语言】") < prefix.index("【对象档案】")


def test_prefix_omits_brief_section_when_empty():
    ctx = ContextState()
    ctx.set_flow("总览", "第 1 步")
    assert "【对象档案】" not in ctx.render_instruction_prefix()


def test_prefix_bytes_stable_across_step_advances_with_brief():
    """步骤推进只改尾部;带对象档案的前缀必须跨步逐字节不变。"""
    ctx = ContextState()
    ctx.set_user_language("cantonese")
    ctx.set_flow("第1步:确认\n第2步:引導", "流程第 1/2 步\n要达成:核對平台")
    ctx.set_object_brief("陳小姐,集運件滯留。")
    p1 = ctx.render_instruction_prefix()
    ctx.set_flow_current("流程第 2/2 步\n要达成:引導辦理")  # 推进
    ctx.add_summary("user", "客戶報咗單號")
    p2 = ctx.render_instruction_prefix()
    assert p1 == p2, "带对象档案的前缀跨步必须字节不变(KV-cache 前缀断裂 → 整段重 prefill)"
    # 尾部确实承载了推进后的当前步。
    tail = ctx.render_context_tail()
    assert "引導辦理" in tail and "【现在这一步】" in tail


# ---- RAG 渲染门:默认无知识/联网节,置 True 恢复 ----

def test_tail_default_has_no_knowledge_or_web_sections():
    ctx = ContextState()
    ctx.set_flow("总览", "当前步文本")
    ctx.set_knowledge([{"text": "知识库条目"}, {"text": "另一条"}])
    ctx.set_web(["联网结果"])
    ctx.add_summary("user", "客户第一轮内容")
    tail = ctx.render_context_tail()
    # 数据照常收(set_knowledge/set_web 保持功能),只是默认不渲染。
    assert ctx._snippets and ctx._web
    assert "【现在这一步】" in tail and "当前步文本" in tail
    assert "【本通对话记忆】" in tail and "客户第一轮内容" in tail
    assert "【实时检索到的资料" not in tail
    assert "【联网检索到的资料" not in tail
    assert "知识库条目" not in tail and "联网结果" not in tail


def test_tail_renders_knowledge_and_web_when_rag_enabled():
    ctx = ContextState()
    ctx.set_knowledge([{"text": "知识库条目"}])
    ctx.set_web(["联网结果"])
    ctx.add_summary("user", "客户内容")
    ctx.rag_enabled = True
    tail = ctx.render_context_tail()
    assert "【实时检索到的资料" in tail and "知识库条目" in tail
    assert "【联网检索到的资料" in tail and "联网结果" in tail
    assert "【本通对话记忆】" in tail


def test_from_env_reads_context_rag(monkeypatch):
    monkeypatch.delenv("CONTEXT_RAG", raising=False)
    assert ContextState.from_env().rag_enabled is False
    monkeypatch.setenv("CONTEXT_RAG", "1")
    ctx = ContextState.from_env(account_id="acc-001")
    assert ctx.rag_enabled is True
    assert ctx.account_id == "acc-001"
