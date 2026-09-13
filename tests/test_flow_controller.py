"""对话流程控制器:分步话术推进 + 对象变量渲染 + 每轮"只走一步"约束。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.flow import (  # noqa: E402
    FlowController,
    _digit_runs_in,
    decide_advance,
    facts_line,
    object_vars,
    parse_steps,
    render_template_text,
)


OBJ = {
    "display_name": "林先生",
    "tracking_no": "SF1234567890",
    "courier": "顺丰",
    "address": "香港湾仔活道 1 号",
    "phone": "13800000000",
}


def test_render_template_vars():
    text = "你好{姓名},你的{快递单号}已到,尾号{快递尾号},由{物流公司}派送。"
    out = render_template_text(text, {"姓名": "林先生", "快递单号": "SF1234567890", "快递尾号": "7890", "物流公司": "顺丰"})
    assert "林先生" in out and "SF1234567890" in out and "7890" in out and "顺丰" in out
    assert "{" not in out


def test_missing_var_keeps_placeholder():
    # 缺失变量保留占位 → LLM 向客户询问而非编造
    out = render_template_text("你好{姓名}", {"姓名": ""})
    assert "{姓名}" in out


def test_facts_line_marks_unknown():
    line = facts_line(OBJ)
    assert "林先生" in line and "顺丰" in line
    # 单号数字逐位转成粤语汉字(SF1234567890 → SF一二三四五六七八九零),TTS 才能按粤语念。
    assert "一二三四五六七八九零" in line
    assert "香港湾仔活道" in line  # 收货地址也在已知事实里
    line2 = facts_line({"display_name": "", "tracking_no": "", "courier": "", "address": ""})
    assert "待确认" in line2


def test_digits_to_cantonese():
    from agent_runtime.flow import digits_to_cantonese
    # 逐位读:0 读零、7890 → 七八九零(单号/尾号/电话按位读,不按数值读)。
    assert digits_to_cantonese("7890") == "七八九零"
    assert digits_to_cantonese("13800000000") == "一三八零零零零零零零零"
    # 字母混排只转数字部分。
    assert digits_to_cantonese("SF1234567890") == "SF一二三四五六七八九零"
    assert digits_to_cantonese("尾号7890") == "尾号七八九零"
    # 空/无数字原样返回。
    assert digits_to_cantonese("") == ""
    assert digits_to_cantonese("顺丰") == "顺丰"


def test_object_vars_converts_digits():
    from agent_runtime.flow import object_vars
    v = object_vars(OBJ)
    assert v["快递单号"] == "SF一二三四五六七八九零"
    assert v["快递尾号"] == "七八九零"  # 取后4位再转
    assert v["电话"] == "一三八零零零零零零零零"
    assert v["姓名"] == "林先生"


def test_address_var():
    # {收货地址}/{地址} 变量替换
    out = render_template_text("你嘅包裹会寄去{收货地址},确认係{地址}吗?",
                               {"收货地址": "香港湾仔活道 1 号", "地址": "香港湾仔活道 1 号"})
    assert "香港湾仔活道" in out and "{" not in out


def test_parse_steps_and_controller():
    steps_json = '[{"goal":"确认包裹是否本人的","ref":"你好{姓名}，{快递单号}是你的吗？"},{"goal":"说明理赔方案","ref":"以一赔二赔付"},{"goal":"引导办理理赔","ref":"加专员QQ办理"}]'
    tpl = {"steps_json": steps_json}
    fc = FlowController.from_template(tpl, OBJ)
    assert fc.has_steps and len(fc.steps) == 3
    assert fc.current == 0
    cur = fc.current_step_text()
    assert "第 1/3 步" in cur
    assert "SF一二三四五六七八九零" in cur  # 变量已渲染且数字已转粤语汉字


def test_advance_only_on_confirm():
    fc = FlowController.from_template({"steps_json": '[{"goal":"g1","ref":"r1"},{"goal":"g2","ref":"r2"}]'}, OBJ)
    fc.on_user_turn("是我的，请问怎么处理？")  # 确认(是我的) → 推进
    assert fc.current == 1
    assert "第 2/2 步" in fc.current_step_text()
    # 有提问(怎么处理)但确认在前——确认信号已推进;回到第2步后再遇提问不推进
    fc.on_user_turn("为什么要赔这么多？")  # 提问 → 停留
    assert fc.current == 1
    fc.on_user_turn("好，可以")  # 确认第2步 → 完成
    assert fc.done
    assert fc.current_step_text().startswith("话术流程已走完")  # 完成后注入答疑指引,唔再主动收线


def test_deny_objection_stays():
    fc = FlowController.from_template({"steps_json": '[{"goal":"g1","ref":"r1"},{"goal":"g2","ref":"r2"}]'}, OBJ)
    fc.on_user_turn("不是我，我没买过这个快递")  # 否认 → 停留
    assert fc.current == 0


def test_decide_advance_cases():
    assert decide_advance("是我的") == "confirm"
    assert decide_advance("不是我") == "objection"
    assert decide_advance("怎么赔？") == "question"
    assert decide_advance("你们是骗子吧") == "objection"
    assert decide_advance("随便") == "unclear"


# ---- 客户「答啱资料」都算确认(唔净靠社交词) ----
OBJ_FACTS = {"姓名": "林先生", "快递尾号": "七八九零", "快递单号": "SF一二三四五六七八九零"}


def test_decide_advance_fact_confirmation():
    # 覆述啱尾号 / 报名 → confirm(唔需要「係/好」)。
    assert decide_advance("七八九零啊。", facts=OBJ_FACTS) == "confirm"
    assert decide_advance("係，我係林先生。", facts=OBJ_FACTS) == "confirm"
    assert decide_advance("我個單號係 SF 一二三四五六七八九零", facts=OBJ_FACTS) == "confirm"


def test_decide_advance_fact_echo_question_not_confirm():
    # 纯提问/echo(「係咪你講嗰個七八九零?」)唔当确认;「我唔记得」唔当确认。
    assert decide_advance("係咪你講嗰個七八九零？", facts=OBJ_FACTS) == "question"
    assert decide_advance("點解你話個尾號七八九零？", facts=OBJ_FACTS) == "question"
    assert decide_advance("咩公司啊？", facts=OBJ_FACTS) == "question"
    assert decide_advance("我唔记得咗。", facts=OBJ_FACTS) == "unclear"
    # 否认优先于事实覆述。
    assert decide_advance("唔係我，不過個尾號又啱", facts=OBJ_FACTS) == "objection"


def test_fact_confirm_advances_flow_step():
    # 客户净系覆述啱尾号,流程都要由第 1 步推到第 2 步(之前卡死喺度)。
    fc = FlowController.from_template({"steps_json": '[{"goal":"确认包裹是否本人的","ref":"你好{姓名}"},'
                                                     '{"goal":"说明一赔二","ref":"会一赔二赔付"}]'}, OBJ)
    assert fc.current == 0
    fc.on_user_turn("七八九零啊。")  # 覆述啱尾号 → 推进
    assert fc.current == 1
    cur = fc.current_step_text()
    assert "第 2/2 步" in cur
    # 推进后注入「新一步」提示,提醒 LLM 换步(唔好延续旧承诺);
    # 2026-09-09 加复读禁令(推进轮逐字复读上轮=call-feaf914c 实证缺陷)——
    # 措辞必须收窄为「未经客户要求」:尾部账本冻结重放整通,一刀切禁令会
    # fight 客户明话要求的复述(「听唔清」→ REPEAT verdict 照讲)。
    assert "【新一步】" in cur and "不要延续上一步" in cur and "不要复读" in cur
    assert "未经客户要求" in cur and "要求重复时除外" in cur


def test_not_confirm_stays_with_recall_guidance():
    # 客户答唔到:唔推进,但当前步注入「核對/引導資料」後備——用訂單/截圖引導提供資料,
    # 唔係「轉專人」,亦唔好自己亂加「幫你查完再覆你」。
    fc = FlowController.from_template({"steps_json": '[{"goal":"确认包裹是不是{姓名}本人的","ref":"你好{姓名}"},'
                                                     '{"goal":"说明一赔二","ref":"会一赔二赔付"}]'}, OBJ)
    fc.on_user_turn("我唔记得咗。")
    assert fc.current == 0
    cur = fc.current_step_text()
    assert "核對/引導資料" in cur or "核对/引导资料" in cur
    assert "订单" in cur and "截图" in cur  # 引导提供资料，不是临时承诺
    assert "我帮你查完再答复你" in cur and "全程你自己与客户沟通" in cur


def test_non_verify_step_no_recall_guidance():
    # 纯说明步骤唔注入核对后备(唔会喺一赔二步无端转专员)。
    fc = FlowController.from_template({"steps_json": '[{"goal":"说明一赔二","ref":"会一赔二赔付"}]'}, OBJ)
    cur = fc.current_step_text()
    assert "轉俾專人" not in cur


# ---- LLM 推进判定器(build/parse/apply) ----
def test_build_judge_messages_has_roadmap_and_next():
    from agent_runtime.flow import build_judge_messages
    fc = FlowController.from_template(
        {"steps_json": '[{"goal":"問記唔記得買咩","ref":"r1"},{"goal":"引導核實","ref":"r2"},{"goal":"說明賠償","ref":"r3"}]'},
        OBJ,
    )
    msgs = build_judge_messages(
        current_index=fc.current + 1,
        total=len(fc.steps),
        overview_lines=fc.overview_goal_lines(),
        goal=fc.steps[0].goal,
        ref=fc.steps[0].ref,
        next_goal=fc.next_goal(),
        user_text="我唔記得喇",
        facts=fc.vars_map,
    )
    assert msgs[0]["role"] == "system" and msgs[1]["role"] == "user"
    sys = msgs[0]["content"]
    # 有全流程地圖 + 當前步 + 下一步(判定「唔記得」通去「引導核實」)。
    assert "共3步" in sys and "問記唔記得買咩" in sys and "引導核實" in sys
    assert "advance / stay / objection" in sys
    assert "我唔記得" in msgs[1]["content"]


def test_parse_judge_output():
    from agent_runtime.flow import parse_judge_output, CONFIRM, OBJECTION, UNCLEAR
    assert parse_judge_output("advance") == CONFIRM
    assert parse_judge_output("Advance<|im_end|>") == CONFIRM  # 容忍 EOS 尾巴
    assert parse_judge_output("objection") == OBJECTION
    assert parse_judge_output("stay") == UNCLEAR
    assert parse_judge_output("") == UNCLEAR
    assert parse_judge_output("我唔知你講咩") == UNCLEAR  # 亂答當不清,唔推進


def test_apply_judge_verdict_advances_or_stays():
    # LLM 判定 advance → 推進;stay/objection → 停留。
    fc = FlowController.from_template(
        {"steps_json": '[{"goal":"開場","ref":"r1"},{"goal":"引導核實","ref":"r2"}]'},
        OBJ,
    )
    fc.apply_judge_verdict("stay")
    assert fc.current == 0
    fc.apply_judge_verdict("confirm")  # advance 落地用 CONFIRM
    assert fc.current == 1
    assert "【新一步】" in fc.current_step_text()


# ---- WhatsApp 对接触发侦测 ----
WA_GOAL = "引導辦理:問客戶有冇用開WhatsApp、加專員"
WA_REF = "你加工作人員嘅WhatsApp帳號…你直接俾你個WhatsApp號碼我"


def test_should_auto_advance_opening_step():
    # 身份確認步(2026-09-12 開場白三段拆分):任何非拒絕回應都推——純提問
    # 「你哋邊間公司」都推,由下一步(來電通知,自報家門)承接;拒絕/異議/要求
    # 重複唔推(頂部攔走)。舊「純提問唔推」係長開場年代語義,已廢。
    from agent_runtime.flow import should_auto_advance
    g = "身份確認:問對方係咪{姓名}"
    r = "你好,請問係{姓名}嗎?"
    assert should_auto_advance(current=0, goal=g, ref=r, user_text="我唔记得咗啊！", verdict="unclear") is True
    assert should_auto_advance(current=0, goal=g, ref=r, user_text="好呀，係我", verdict="confirm") is True
    assert should_auto_advance(current=0, goal=g, ref=r, user_text="你哋係邊間公司㗎？", verdict="question") is True
    assert should_auto_advance(current=0, goal=g, ref=r, user_text="唔好再打嚟！", verdict="objection") is False
    assert should_auto_advance(current=0, goal=g, ref=r, user_text="你讲咩啊？", verdict="repeat") is False


def test_should_auto_advance_say_step():
    # 通知直念步(say=1):客户对「记唔记得买咩货品」的实质回应即推去平台步;
    # 纯提问(问公司/点解遗失)唔推,原地答(分支+QA 罐头)。未标 say 的步
    # (平台/赔偿)语义不变——unclear 唔推、答到平台先推。
    from agent_runtime.flow import should_auto_advance
    g = "來電通知:自報家門,問客户記唔記得買嘅貨品"
    r = "我哋係集運中轉倉…想問下你仲記唔記得當時買嘅係咩貨品呢?"
    assert should_auto_advance(current=1, goal=g, ref=r, user_text="我唔记得咗", verdict="unclear", say_step=True) is True
    assert should_auto_advance(current=1, goal=g, ref=r, user_text="我买咗件衫", verdict="confirm", say_step=True) is True
    assert should_auto_advance(current=1, goal=g, ref=r, user_text="你哋係邊間公司？", verdict="question", say_step=True) is False
    # 平台步(无 say):unclear 唔推
    assert should_auto_advance(current=2, goal="引导核实", ref="你係喺邊個平台買?", user_text="随便", verdict="unclear", say_step=False) is False


def test_parse_steps_say_flag():
    from agent_runtime.flow import parse_steps
    import json as _json
    steps = parse_steps(_json.dumps([
        {"goal": "身份確認", "ref": "你好，請問係{姓名}嗎？"},
        {"goal": "來電通知", "ref": "我哋係集運中轉倉…", "say": 1},
    ]))
    assert steps[0].say is False
    assert steps[1].say is True


def test_say_step_pending_and_ledger():
    import json as _json
    fc = FlowController.from_template({"steps_json": _json.dumps([
        {"goal": "身份確認:問對方係咪{姓名}", "ref": "你好，請問係{姓名}嗎？"},
        {"goal": "來電通知", "ref": "我哋係集運中轉倉，今次致電係想通知你。\n如果客户唔记得 → 去下一步问平台", "say": 1},
        {"goal": "引导核实", "ref": "邊個平台買？"},
    ])}, OBJ)
    # 身份步(0)不是直念步 → 无待念文本(开场白走 opening 机制)
    assert fc.pending_say_text() == ""
    fc.advance()  # 客户确认身份 → 通知步
    assert fc.pending_say_text() == "我哋係集運中轉倉，今次致電係想通知你。"
    # 念完记账:不再待念,当前步注入【通知已念】防 LLM 重复整段
    fc.note_step_said()
    assert fc.pending_say_text() == ""
    cur = fc.current_step_text()
    assert "【通知已念】" in cur
    # 收尾态/完成后不直念
    fc.enter_closing()
    assert fc.pending_say_text() == ""


def test_say_step_missing_var_returns_empty():
    import json as _json
    fc = FlowController.from_template({"steps_json": _json.dumps([
        {"goal": "身份", "ref": "你好"},
        {"goal": "通知", "ref": "我哋係{不存在的变量}中轉倉", "say": 1},
    ])}, OBJ)
    fc.advance()
    assert fc.pending_say_text() == ""  # 变量缺失宁可退 LLM,不念占位符


def test_should_auto_advance_platform_answer():
    # 純核對平台步(冇WhatsApp要求):客答到平台名 → 即過;未答到/純問 → 唔過。
    from agent_runtime.flow import should_auto_advance
    g = "引導核實:問客戶喺邊個平台買(拼多多/淘寶/京東等)"
    r = "你係喺拼多多、淘寶定京東買㗎?"
    assert should_auto_advance(current=1, goal=g, ref=r, user_text="哎呀，拼多多。", verdict="unclear") is True
    assert should_auto_advance(current=1, goal=g, ref=r, user_text="我喺淘寶買嘅", verdict="confirm") is True
    assert should_auto_advance(current=1, goal=g, ref=r, user_text="我冇訂單，唔記得喺邊買", verdict="unclear") is False
    assert should_auto_advance(current=1, goal=g, ref=r, user_text="點解要截圖呀？", verdict="question") is False
    # 非核實步(賠償標準)客講平台名 → 唔會誤推
    assert should_auto_advance(current=2, goal="說明賠償標準", ref="一賠二", user_text="我淘寶買嘅", verdict="unclear") is False


def test_should_auto_advance_whatsapp_gate():
    # 兼要攞WhatsApp/截圖嘅核實步:客戶淨係答到平台(未俾WA) → 停留;
    # 客戶俾咗號碼(captured) → 先推;offered(應承加,未俾號碼) → 停留等號碼。
    from agent_runtime.flow import should_auto_advance
    g = "核對貨品+攞WhatsApp+叫客戶傳訂單截圖"
    r = "你係喺拼多多、淘寶定京東買㗎?你俾你個WhatsApp號碼我,訂單截圖喺WhatsApp傳過嚟"
    # 答到平台但未俾WhatsApp → 停留(唔好跳去講賠償)
    assert should_auto_advance(current=1, goal=g, ref=r, user_text="我喺拼多多買嘅", verdict="unclear", wa=None) is False
    assert should_auto_advance(current=1, goal=g, ref=r, user_text="我喺拼多多買嘅", verdict="confirm", wa=None) is False
    # 客戶話冇WhatsApp / 唔想加 → 停留
    assert should_auto_advance(current=1, goal=g, ref=r, user_text="我冇WhatsApp喎", verdict="unclear", wa=None) is False
    # 俾咗號碼(captured) → 推
    assert should_auto_advance(current=1, goal=g, ref=r, user_text="我WhatsApp號碼係 6868123456", verdict="unclear", wa="captured") is True
    # 應承加但未俾號碼(offered) → 停留等號碼
    assert should_auto_advance(current=1, goal=g, ref=r, user_text="好呀,你加我啦", verdict="confirm", wa="offered") is False


def test_detect_whatsapp_captured_number():
    from agent_runtime.flow import detect_whatsapp_signal
    # 客戶喺引導辦理步讀出自己 WhatsApp 號碼(漢字/阿拉伯/空格)→ captured。
    assert detect_whatsapp_signal("我個WhatsApp係 六八六八一二三四五六", step_goal=WA_GOAL, step_ref=WA_REF) == ("captured", "6868123456")
    assert detect_whatsapp_signal("我WhatsApp號碼係 9852 6633", step_goal=WA_GOAL, step_ref=WA_REF) == ("captured", "98526633")


def test_detect_whatsapp_grouped_digits_collapsed():
    """分组报号(call-5f8bef6b 实证):客户按组报号「751 ⏸ 220」,ASR 在组间落
    逗号/顿号/连字符 → 归一后 run 被劈成 3+3,4 位连续下限全部打回 → captured
    永不触发、WA 步锁死。组间分隔符(两侧皆数字)折叠成一条 run;句号/小数点
    是句界与小数语义,不折叠。"""
    from agent_runtime.flow import detect_whatsapp_signal
    # 英文分组报号(本通实证形态)
    assert detect_whatsapp_signal("Seven five one, two two zero.", step_goal=WA_GOAL, step_ref=WA_REF) == ("captured", "751220")
    # 中文分组:逗号/顿号/连字符
    assert detect_whatsapp_signal("我嘅號碼係 9852，6633", step_goal=WA_GOAL, step_ref=WA_REF) == ("captured", "98526633")
    assert detect_whatsapp_signal("六四三二、五四三二", step_goal=WA_GOAL, step_ref=WA_REF) == ("captured", "64325432")
    assert detect_whatsapp_signal("我WhatsApp係 9852-6633", step_goal=WA_GOAL, step_ref=WA_REF) == ("captured", "98526633")
    # 修正句「唔係5，係3」:中间是汉字,唔折叠,两段各 <4 位照舊唔收
    assert detect_whatsapp_signal("唔係5，係3", step_goal=WA_GOAL, step_ref=WA_REF) is None
    # 小数点/句号不折叠:小数句号两侧虽是数字,折叠会把 1.5 变 15、把「赔300。号码…」两句焊成一条
    assert detect_whatsapp_signal("賠償1.5倍呀", step_goal=WA_GOAL, step_ref=WA_REF) is None
    assert detect_whatsapp_signal(
        "賠償300。我號碼係98526633", step_goal=WA_GOAL, step_ref=WA_REF
    ) == ("captured", "98526633")
    # review 加固:范围/金额语境不折——「300～500」是赔偿区间、「300-500块」带量词、
    # 「¥300,500」两笔金额;折了会捏出 300500 假号码假捕获。
    assert detect_whatsapp_signal("賠償300～500蚊", step_goal=WA_GOAL, step_ref=WA_REF) is None
    assert detect_whatsapp_signal("賠償300-500塊", step_goal=WA_GOAL, step_ref=WA_REF) is None
    assert detect_whatsapp_signal("¥300,500的賠償", step_goal=WA_GOAL, step_ref=WA_REF) is None


def test_wa_accum_merge_redecode_prefix_replaced():
    """累积合并(「唔结合上下文」根因):第二段 FINAL 常是全窗重解(自带前文头),
    盲拼 stash+新段 → 头重复 → 「oneSeven」粘连吃数字。归一前缀命中 → 用新段
    整句替换;真续段 → 带分隔符拼接(唔可以裸拼,「five one」「two zero」会粘词)。"""
    from agent_runtime.agent import _wa_accum_merge

    # 全窗重解(新段含暂存头)→ 替换,唔可以 doubling
    assert _wa_accum_merge("seven five one", "Seven five one, two two zero.") == "Seven five one, two two zero."
    # 大小写/词形差异由归一吃掉
    assert _wa_accum_merge("我的WhatsApp係", "我的WhatsApp係64325432") == "我的WhatsApp係64325432"
    # 真续段 → 带分隔符拼接
    assert _wa_accum_merge("我的WhatsApp係", "六四三二五四三二") == "我的WhatsApp係，六四三二五四三二"
    # 空暂存直通
    assert _wa_accum_merge("", "abc") == "abc"


def test_detect_whatsapp_known_number_excluded():
    from agent_runtime.flow import detect_whatsapp_signal
    F = {"姓名": "林先生", "快递单号": "SF1234567890", "快递尾号": "7890", "电话": "13800000000"}
    # 覆述已知 尾號/單號/電話 → 唔當新 WhatsApp。
    assert detect_whatsapp_signal("我個單號係 7890", step_goal=WA_GOAL, step_ref=WA_REF, facts=F) is None
    assert detect_whatsapp_signal("SF一二三四五六七八九零係我嗰件", step_goal=WA_GOAL, step_ref=WA_REF, facts=F) is None
    assert detect_whatsapp_signal("我個電話係 一三八零零零零零零零零", step_goal=WA_GOAL, step_ref=WA_REF, facts=F) is None


def test_detect_whatsapp_offered_ack():
    from agent_runtime.flow import detect_whatsapp_signal
    # 引導辦理步:客戶應承加(明確叫加 / 純短應承)→ offered。
    assert detect_whatsapp_signal("好呀，你加我啦", step_goal=WA_GOAL, step_ref=WA_REF) == ("offered", "")
    assert detect_whatsapp_signal("可以", step_goal=WA_GOAL, step_ref=WA_REF) == ("offered", "")
    assert detect_whatsapp_signal("嗯,好呀", step_goal=WA_GOAL, step_ref=WA_REF) == ("offered", "")


def test_detect_whatsapp_already_captured_no_more_offered():
    from agent_runtime.flow import detect_whatsapp_signal
    # 已捕获过号码后:純短應承/叫加係對當前步嘅確認,唔再判 offered
    # (offered 會鎖死確認輪唔推進,4B 就把自己上一句原樣再講一次——
    # 2026-09-06 call-e6e5f18e 逐字重複實證)。
    assert detect_whatsapp_signal("嗯", step_goal=WA_GOAL, step_ref=WA_REF, already_captured=True) is None
    assert detect_whatsapp_signal("好呀，你加我啦", step_goal=WA_GOAL, step_ref=WA_REF, already_captured=True) is None
    assert detect_whatsapp_signal("可以", step_goal=WA_GOAL, step_ref=WA_REF, already_captured=True) is None
    # 新號碼照捕(客戶可以改口俾另一個號)。
    assert detect_whatsapp_signal(
        "唔好意思，啱先報錯咗，我個WhatsApp係 9852 6633",
        step_goal=WA_GOAL, step_ref=WA_REF, already_captured=True,
    ) == ("captured", "98526633")


def test_detect_whatsapp_not_triggered():
    from agent_runtime.flow import detect_whatsapp_signal
    # 冇 WhatsApp / 提其他話題 / 問點加 → 唔觸發。
    assert detect_whatsapp_signal("我冇WhatsApp喎", step_goal=WA_GOAL, step_ref=WA_REF) is None
    assert detect_whatsapp_signal("我係林先生呀", step_goal=WA_GOAL, step_ref=WA_REF) is None
    assert detect_whatsapp_signal("好呀，咁點樣加呀？", step_goal=WA_GOAL, step_ref=WA_REF) is None
    # 非引導辦理步(冇 WhatsApp hint),淨係俾號碼 → 唔當(避免喺核實/賠償步亂觸發)。
    assert detect_whatsapp_signal("我電話 13800000000", step_goal="說明賠償標準", step_ref="一賠二") is None


def test_detect_whatsapp_caller_bound_implicit():
    """客戶話 WhatsApp 綁定「呢個來電/號碼」(號喺系統)→ captured_implicit。

    防死鎖:號碼俾 ASR 聽亂、或句度只有已知單號,客戶用「綁定來電」俾號 → 唔該卡死喺
    重複要號。真實 call-17e81d23: 14:58「我 WhatsApp 就是绑定这个来电的手机号」。
    """
    from agent_runtime.flow import detect_whatsapp_signal
    F = {"姓名": "陈先生", "快递单号": "sf一二三四五六七八九零", "快递尾号": "七八九零", "电话": "六四三二五四三"}
    assert detect_whatsapp_signal("我 WhatsApp 就是绑定这个来电的手机号", step_goal=WA_GOAL, step_ref=WA_REF, facts=F) == ("captured_implicit", "")
    assert detect_whatsapp_signal("WhatsApp 就係我而家呢個來電號碼", step_goal=WA_GOAL, step_ref=WA_REF, facts=F) == ("captured_implicit", "")
    assert detect_whatsapp_signal("whatsapp 就係我而家呢個電話", step_goal=WA_GOAL, step_ref=WA_REF, facts=F) == ("captured_implicit", "")
    # 複述單號 + 綁定來電(同一句)→ 綁定優先
    assert detect_whatsapp_signal("单号 SF 一二三四五六七八九零 我记下来了。我 WhatsApp 就是绑定这个来电的手机号", step_goal=WA_GOAL, step_ref=WA_REF, facts=F) == ("captured_implicit", "")
    # 非 WhatsApp 步(賠償步)講綁定來電 → 唔當(唔喺要號語境)
    assert detect_whatsapp_signal("WhatsApp 就係我而家呢個電話", step_goal="說明賠償標準", step_ref="一賠二") is None


def test_detect_whatsapp_short_number_and_announce():
    """真實走漏案例回歸(2026-09-03 call):客戶講 7 位號 + 撞對象電話 → 雙重走漏,
    唔爆閃、唔推進、AI 重複追問。

    - 「我WhatsApp是六四三二五四三」:7 位(舊 8-13 規則走漏)且撞對象電話(舊 known-number
      過濾走漏)→ 明確自報句式要照捕。
    - WhatsApp 步內 7 位新號碼 → captured。
    """
    from agent_runtime.flow import detect_whatsapp_signal
    F = {"姓名": "陈先生", "快递单号": "sf一二三四五六七八九零", "快递尾号": "七八九零", "电话": "六四三二五四三"}
    # 明確自報(7位+撞已知電話)→ captured
    assert detect_whatsapp_signal("我WhatsApp是六四三二五四三。", step_goal=WA_GOAL, step_ref=WA_REF, facts=F) == ("captured", "6432543")
    assert detect_whatsapp_signal("我 WhatsApp 就是 68681234", step_goal=WA_GOAL, step_ref=WA_REF, facts=F) == ("captured", "68681234")
    # WhatsApp 步內 7 位新號碼(無自報句式)→ captured(7位容錯)
    assert detect_whatsapp_signal("我俾你,六四三二五八八", step_goal=WA_GOAL, step_ref=WA_REF, facts=F) == ("captured", "6432588")
    # 非 WA 語境 7 位覆述已知電話 → 唔當
    assert detect_whatsapp_signal("我個電話 6432543", step_goal="說明賠償標準", step_ref="一賠二", facts=F) is None


def test_detect_whatsapp_captured_prefers_context_not_single_number():
    """WhatsApp 語境下客戶俾號,號碼就算撞已知單號都當 WhatsApp 號(先走 captured)。

    修正:「俾號 + 明講 whatsapp/加我」唔應該俾單號過濾誤吞 → 卡死。
    純覆述單號(冇 whatsapp 語境)先唔當。
    """
    from agent_runtime.flow import detect_whatsapp_signal
    F = {"姓名": "陈先生", "快递单号": "sf一二三四五六七八九零", "快递尾号": "七八九零", "电话": "六四三二五四三"}
    # 撞完整單號 + 明講 whatsapp → 當佢俾 WhatsApp 號(走 captured)
    assert detect_whatsapp_signal("你加我 whatsapp,我號碼係 一二三四五六七八九零", step_goal=WA_GOAL, step_ref=WA_REF, facts=F) == ("captured", "1234567890")
    # 純覆述單號(冇 whatsapp)→ 唔當
    assert detect_whatsapp_signal("我個單號係 一二三四五六七八九零", step_goal=WA_GOAL, step_ref=WA_REF, facts=F) is None


def test_detect_whatsapp_give_number_cue():
    """「俾個號你」都係俾號語境——唔淨係靠 whatsapp/微信/號碼字眼。

    防回歸:「俾個號」得個「號」字(冇「碼」)、又唔喺要 WhatsApp 步度(例如賠償步
    客主動俾號)→ 舊 code `"俾.*號" in t` 係字面子串永唔中 → 俾號都當冇俾 → 漏 captured。
    """
    from agent_runtime.flow import detect_whatsapp_signal
    # 非 WhatsApp 步(賠償步)客主動「俾個號你」+ 報號 → 當 WhatsApp 號(captured)
    assert detect_whatsapp_signal(
        "咁我俾個號你啦, 68681234", step_goal="說明賠償標準", step_ref="一賠二"
    ) == ("captured", "68681234")
    assert detect_whatsapp_signal(
        "你加我啦,我俾你個號 98526633", step_goal="說明賠償標準", step_ref="一賠二"
    ) == ("captured", "98526633")
    # 冇俾號語境淨係報號 → 唔當(保持舊行為)
    assert detect_whatsapp_signal("我電話 13800000000", step_goal="說明賠償標準", step_ref="一賠二") is None


def test_should_auto_advance_whatsapp_captured_implicit():
    """captured_implicit(綁定來電)同 captured 一樣放行推進;offered/None 停留。"""
    from agent_runtime.flow import should_auto_advance
    g = "核對貨品+攞WhatsApp+叫客戶傳訂單截圖"
    r = "你俾你個WhatsApp號碼我,訂單截圖喺WhatsApp傳過嚟"
    assert should_auto_advance(current=1, goal=g, ref=r, user_text="WhatsApp就係呢個來電", verdict="confirm", wa="captured_implicit") is True
    assert should_auto_advance(current=1, goal=g, ref=r, user_text="綁定來電", verdict="unclear", wa="captured_implicit") is True
    assert should_auto_advance(current=1, goal=g, ref=r, user_text="俾咗號", verdict="unclear", wa="captured") is True
    assert should_auto_advance(current=1, goal=g, ref=r, user_text="好呀加我", verdict="confirm", wa="offered") is False


def test_no_steps_no_flow():
    fc = FlowController.from_template({}, OBJ)
    assert not fc.has_steps
    assert fc.current_step_text().startswith("话术流程已走完")  # 完成后注入答疑指引
    assert fc.flow_overview() == ""


# ---- 旧式四段模板自动转分步(不一口气念完) ----
LEGACY_TPL = {
    "name": "粤语客服·产品咨询",
    "opening": "你好请问你{姓名}咩？我哋呢边系{快递公司}快递…",
    "core": "你嘅包裹运输途中丢失,我哋有买运费保险,会一赔二补俾你;你可以重新下单",
    "objection": "如果你担心真假,可以加线上专员核实",
    "closing": "好,唔该晒你,有咩问题随时搵我。拜拜!",
}


def test_legacy_four_sections_become_steps():
    # 旧式四段模板无 steps_json:应转成 4 个分步,逐轮推进,而不是整段塞给 LLM 念。
    from agent_runtime.flow import FlowController, template_to_steps

    steps = template_to_steps(LEGACY_TPL)
    assert len(steps) == 4
    assert steps[0].goal.startswith("开场")
    assert steps[-1].goal.startswith("收尾")

    fc = FlowController.from_template(LEGACY_TPL, OBJ)
    assert fc.has_steps
    txt = fc.current_step_text()
    assert "第 1/4 步" in txt
    # 开场步的 ref 是 opening 全文(渐进披露:每步首轮渲染注入底稿,二次调用
    # 已转分支模式——单次捕获断言)
    assert "你好请问" in txt


def test_legacy_steps_advance_one_by_one():
    # 四段转分步后:客户确认才推进,不会一口气念完。
    from agent_runtime.flow import FlowController

    fc = FlowController.from_template(LEGACY_TPL, OBJ)
    # 开场后:客户确认 → 才进第2步(core)
    fc.on_user_turn("係呀,係我嘅")
    assert fc.current == 1
    assert "第 2/4 步" in fc.current_step_text()
    # 第2步(core)客户再确认 → 进第3步(objection/异议)
    fc.on_user_turn("好嘅,可以")
    assert fc.current == 2
    # 未确认不会跳:流程到第3步就停,不会自己讲完收尾
    assert fc.current == 2
    assert "收尾" not in fc.current_step_text()


def test_current_step_explicit_no_leak_instruction():
    # 当前步注入须明确区分"内部底稿"与"对客户说的话",禁止复述分支指示;
    # 渐进披露后分支不再以「如果客户X→就Y」原文形态进 prompt(只改写成
    # 【应对客户当前回应】单条),命中分支的应对内容本身照给。
    from agent_runtime.flow import FlowController

    fc = FlowController.from_template(
        {"steps_json": '[{"goal":"确认包裹","ref":"你好{姓名}。\\n如果客户唔记得 → 提佢地址帮佢回忆"}]'},
        OBJ,
    )
    txt = fc.current_step_text()
    assert "内部资料" in txt  # 底稿明确标记为内部
    assert "绝不逐字念" in txt  # 禁止逐字念出
    assert "如果客户" not in txt  # 分支指示原文不进(首轮无分支)
    fc.last_verdict = "unclear"
    fc.last_user_text = "我唔记得了"
    txt2 = fc.current_step_text()
    assert "提佢地址帮佢回忆" in txt2  # 命中分支的应对照注入
    assert "如果客户" not in txt2  # 但分支指示原文形态不进


# ---- 明确拒绝 → REFUSE(一句礼貌收尾 + 主动结束通话) ----
def test_decide_advance_refuse_family():
    # 高频拒绝说法(旧版漏成 unclear/objection 然后无限重问):现在直接 REFUSE。
    from agent_runtime.flow import REFUSE, decide_advance
    assert decide_advance("唔需要喇，唔该") == REFUSE
    assert decide_advance("唔办啦。") == REFUSE
    assert decide_advance("我唔要。") == REFUSE
    assert decide_advance("唔要啦。") == REFUSE
    assert decide_advance("我拒绝你哋。") == REFUSE
    assert decide_advance("唔好再打嚟！") == REFUSE
    assert decide_advance("不用了谢谢") == REFUSE
    assert decide_advance("别再打来了") == REFUSE
    assert decide_advance("再见") == REFUSE
    assert decide_advance("拜拜") == REFUSE


def test_refuse_social_phrase_not_refuse():
    # 「唔使担心/唔使客气」係社交关心/客套,唔係拒绝(防误收线)。
    from agent_runtime.flow import REFUSE, decide_advance
    assert decide_advance("唔使担心，我明白嘅。") != REFUSE
    assert decide_advance("你哋唔使客气。") != REFUSE


def test_refuse_enters_closing_and_stays():
    # REFUSE → 收尾态:current_step_text 注入收尾话术;收尾后唔再推进/唔再按步走。
    from agent_runtime.flow import FlowController, REFUSE
    fc = FlowController.from_template(
        {"steps_json": '[{"goal":"开场","ref":"r1"},{"goal":"引导办理","ref":"r2"}]'}, OBJ
    )
    assert fc.rule_verdict("我唔需要，唔好再打嚟。") == REFUSE
    fc.enter_closing()
    cur = fc.current_step_text()
    assert "收尾" in cur and "再见" in cur  # 收尾例句已改中性书面中文(语言纯度,2026-09-06)
    # 收尾态即使客户改口应承,都唔翻流程(真改口由人工/新通话处理)。
    fc.on_user_turn("係我,可以㗎。")
    assert fc.current == 0 and fc.closing
    assert "收尾" in fc.current_step_text()


def test_should_auto_advance_never_on_refuse():
    # 拒绝轮任何一步都唔推进(开场步漏网拒绝尤其会误推,防回归)。
    from agent_runtime.flow import REFUSE, should_auto_advance
    assert should_auto_advance(current=0, goal="开场", ref="r", user_text="唔需要", verdict=REFUSE) is False
    assert should_auto_advance(current=1, goal="引导办理", ref="r", user_text="唔需要", verdict=REFUSE) is False


def test_opening_text_first_line_with_vars():
    # 开场白=第 1 步 ref 首行(变量已替换);「如果客户…」分支指引係畀 LLM 睇,唔会念出声。
    steps_json = (
        '[{"goal":"确认身份","ref":"您好，请问是{姓名}吗？我们是{物流公司}，'
        '有个包裹单号尾号{快递尾号}运输途中丢失了，想跟您核对一下。\\n'
        '如果客户不记得 → 提他下单时填的地址帮他回忆"},{"goal":"说明方案","ref":"以一赔二"}]'
    )
    fc = FlowController.from_template({"steps_json": steps_json}, OBJ)
    opening = fc.opening_text()
    assert opening.startswith("您好，请问是林先生吗？")
    assert "顺丰" in opening and "七八九零" in opening
    assert "如果客户" not in opening and "\n" not in opening


def test_opening_text_missing_var_returns_empty():
    # 变量缺失(渲染后仍剩 {占位}) → 空串:上层退通用开场白,唔会念出「{姓名}」。
    fc = FlowController.from_template(
        {"steps_json": '[{"goal":"g","ref":"您好，请问是{姓名}吗？"}]'}, {}
    )
    assert fc.opening_text() == ""


def test_opening_text_no_steps_empty():
    assert FlowController.from_template(None, OBJ).opening_text() == ""


def test_current_step_opening_played_note():
    # 开场直念后:第 1 步注入「勿重复开场」提示;推进到第 2 步后提示消失;
    # paused 起动(冇开场白,flag=False)就唔注入,LLM 自己补第 1 步。
    fc = FlowController.from_template(
        {"steps_json": '[{"goal":"开场","ref":"r1"},{"goal":"方案","ref":"r2"}]'}, OBJ
    )
    assert "开场已念" not in fc.current_step_text()
    fc.opening_played = True
    assert "开场已念" in fc.current_step_text()
    fc.advance()
    assert "开场已念" not in fc.current_step_text()


def test_object_vars_en_aliases():
    # EN 模板占位 {name}/{courier}/{tracking_tail}:尾号保留阿拉伯数字(英文 TTS 直读)。
    v = object_vars(OBJ)
    assert v["name"] == "林先生"
    assert v["courier"] == "顺丰"
    assert v["tracking_tail"] == "7890"


def test_current_step_verdict_guidance():
    """verdict 感知指引:question/unclear/objection 各有应答指引(治复读当前步),
    confirm 冇额外指引(由【新一步】接管)。"""
    fc = FlowController.from_template({"steps_json": '[{"goal":"g1","ref":"r1"},{"goal":"g2","ref":"r2"}]'}, OBJ)
    fc.last_verdict = "question"
    cur = fc.current_step_text()
    assert "客户在提问" in cur and "绝不重复你上一句" in cur
    fc.last_verdict = "unclear"
    assert "回应不明确" in fc.current_step_text()
    fc.last_verdict = "objection"
    assert "客户有疑虑" in fc.current_step_text()
    fc.last_verdict = "confirm"
    assert "客户在提问" not in fc.current_step_text()
    # 空 verdict(会话首轮)无指引
    fc2 = FlowController.from_template({"steps_json": '[{"goal":"g1","ref":"r1"}]'}, OBJ)
    assert "客户在提问" not in fc2.current_step_text()


def test_done_confirm_no_reask():
    """话术走完 + 客户刚确认 → 注入「毋需再问已答过的事」,治 WhatsApp 号码
    确认后逐字再问一遍(0f4df710 实证)。"""
    fc = FlowController.from_template({"steps_json": '[{"goal":"g1","ref":"r1"}]'}, OBJ)
    fc.advance()
    assert fc.done
    fc.last_verdict = "confirm"
    cur = fc.current_step_text()
    assert "毋需再问" in cur
    fc.last_verdict = "question"
    assert "毋需再问" not in fc.current_step_text()


# ---- WA 捕获:4 位下限/微信/wechat/英文数字词/接缝标点(2026-09-06 拆句调查配套) ----

from agent_runtime.flow import (  # noqa: E402
    _digit_normalize,
    detect_whatsapp_signal,
    should_auto_advance,
)

_WA_STEP = {
    "goal": "引导办理:请客户提供自己嘅{聯絡方式}号码,安排银联理赔专员对接",
    "ref": "唔该你留個{聯絡方式}號碼俾我哋，我哋安排專員加你。\n客户报出号码 → 复述确认。",
}


def _wa(text: str):
    return detect_whatsapp_signal(text, step_goal=_WA_STEP["goal"], step_ref=_WA_STEP["ref"])


def test_digit_floor4_captures_short_announce_not_fragments():
    # 5 位测试短号(「一二二三三」旧 6 位门槛全数走漏→唔爆闪)照捕;3 位碎片照拒。
    assert _wa("我的WhatsApp係一二二三三") == ("captured", "12233")
    assert _wa("我的微信号係12233") == ("captured", "12233")
    assert _wa("係333") is None
    assert _wa("唔係5，係3") is None


def test_wechat_and_en_context_capture():
    assert _wa("my wechat is 12233") == ("captured", "12233")
    assert _wa("my WhatsApp number is 12233") == ("captured", "12233")
    # 拒绝语(普/英)唔触发 offered
    assert _wa("我没有微信，不用了") is None
    assert _wa("I don't use WhatsApp") is None


def test_en_digit_words_normalize():
    assert _digit_normalize("one three two zero one") == "13201"
    assert _digit_normalize("Zero was three") == "0was3"
    # 英文号码词喺 WA 步直接捕获(归一后 run ≥4)
    assert _wa("One three two zero one") == ("captured", "13201")


def test_announce_seam_punctuation_captures_joined_text():
    # 跨段拼接后的接缝:「係。一七二…」——系词与数字之间夹句号都照捕
    assert _wa("我的WhatsApp是。一七二二三三四。") == ("captured", "1722334")
    assert _wa("my whatsapp, is 1234") == ("captured", "1234")


def test_known_number_facts_not_recaptured():
    facts = {"快递单号": "sf33468899", "快递尾号": "8899", "电话": "1336758"}
    assert detect_whatsapp_signal("尾号係8899", step_goal=_WA_STEP["goal"], step_ref=_WA_STEP["ref"], facts=facts) is None
    assert detect_whatsapp_signal("我电话係1336758", step_goal=_WA_STEP["goal"], step_ref=_WA_STEP["ref"], facts=facts) is None


def test_wa_step_blocks_advance_without_capture():
    # 收号码步:未 captured 一律唔推( CONFIRM/平台名都唔放行)——假确认推进由
    # agent 侧护栏挡(agent.py CONFIRM 分支),引擎侧 should_auto_advance 同向。
    assert should_auto_advance(current=3, goal=_WA_STEP["goal"], ref=_WA_STEP["ref"],
                               user_text="我的WhatsApp是。", verdict="CONFIRM", wa=None) is False
    assert should_auto_advance(current=3, goal=_WA_STEP["goal"], ref=_WA_STEP["ref"],
                               user_text="得，而家有時間", verdict="CONFIRM", wa=None) is False
    assert should_auto_advance(current=3, goal=_WA_STEP["goal"], ref=_WA_STEP["ref"],
                               user_text="6432543", verdict="ANSWER", wa="captured") is True


def test_en_platform_answer_advances():
    goal = "Verify: ask which platform the customer bought from"
    ref = "May I ask where you bought it — Amazon, Temu, or eBay?"
    assert should_auto_advance(current=1, goal=goal, ref=ref, user_text="I bought it on Temu", verdict="ANSWER") is True


def test_contact_channel_vars_by_language_and_override():
    base = {"display_name": "林总", "tracking_no": "sf33468899"}
    assert object_vars({**base, "language": "cantonese"})["聯絡方式"] == "WhatsApp"
    assert object_vars({**base, "language": "zh"})["聯絡方式"] == "微信"
    assert object_vars({**base, "language": "en"})["contact"] == "WhatsApp"
    assert object_vars({**base, "language": "zh", "contact_channel": "WhatsApp"})["联系方式"] == "WhatsApp"


def test_wa_numberish_and_announce_head():
    from agent_runtime.agent import _WA_ANNOUNCE_HEAD_RE, _wa_numberish

    assert _wa_numberish("一七二二三三四") is True
    assert _wa_numberish("Zero was three") is True
    assert _wa_numberish("我的微信号係64325432") is True
    assert _wa_numberish("我喺淘寶買嘢") is False
    assert _wa_numberish("Okay.") is False
    # 渠道英文词要先剥掉:「whatsapp」8 字母本身已超 ≤6 上限,唔剥英文通道永远进唔了累积
    assert _wa_numberish("我的WhatsApp係64325432") is True
    assert _wa_numberish("我的WhatsApp係六四三二") is True
    assert _wa_numberish("WhatsApp 6432") is True
    # 自报头:「我的WhatsApp是。」(零数字,号码喺后半句)
    assert _WA_ANNOUNCE_HEAD_RE.search("我的WhatsApp是。") is not None
    assert _WA_ANNOUNCE_HEAD_RE.search("你係咪加我WhatsApp呀") is None


def test_wa_confirm_guard_shared_rule_and_judge():
    from agent_runtime.flow import wa_confirm_advance_allowed

    # 收号码步未捕获:CONFIRM 唔放行(rule 与 judge 同一铁律)
    assert wa_confirm_advance_allowed(goal=_WA_STEP["goal"], ref=_WA_STEP["ref"], captured=False) is False
    # 捕获后放行
    assert wa_confirm_advance_allowed(goal=_WA_STEP["goal"], ref=_WA_STEP["ref"], captured=True) is True
    # 非 WA 步照常放行
    assert wa_confirm_advance_allowed(goal="核实平台", ref="你喺边个平台落单?", captured=False) is True


def test_judge_path_has_wa_guard():
    """源码级:背景 judge 的 CONFIRM 分支必须过同一护栏(nested closure 冇法直接
    单测,用 test_echo_guard 的源码断言姿势;泄漏点=c4f6e4f1 judge=confirm step=4)。"""
    import agent_runtime.agent as ag

    src = Path(ag.__file__).read_text(encoding="utf-8")
    guard_pos = src.index("wa_confirm_advance_allowed(goal=_gj")
    assert guard_pos < src.index("judge(bg)=confirm step=")
    assert src.count("wa_confirm_advance_allowed(") >= 2  # rule 与 judge 两路共用


# ---- WS1 总览带各步事实(2026-09-07) ----


def test_flow_overview_includes_step_fact_lines():
    fc = FlowController.from_template(
        {
            "steps_json": (
                '[{"goal":"开场核对","ref":"您好，请问是{courier}的件吗？\\n如果客户否认→记录"},'
                '{"goal":"理赔说明","ref":"我们有保险，会一赔二。"}]'
            )
        },
        OBJ,
    )
    ov = fc.flow_overview()
    # 每步带 ref 首行事实——客户问细节时模型手头有话术事实可引用
    assert "我们有保险，会一赔二" in ov
    # 分支指引(第二行)唔进总览
    assert "如果客户否认" not in ov
    # 确定性:两次渲染字节一致(KV 前缀安全)
    assert ov == fc.flow_overview()


# ---- WS3 单字确认收窄(2026-09-07):≤2 字纯应承只喺问话步先算确认 ----


def test_short_ack_confirms_only_on_question_step():
    # 疑问步(ref 含 ？)收到单字应承 → 答话,CONFIRM 推进
    fc = FlowController.from_template(
        {"steps_json": '[{"goal":"g1","ref":"您看这样处理，合不合适？"},{"goal":"g2","ref":"r2"}]'}, OBJ
    )
    assert fc.rule_verdict("对。") == "confirm"
    fc.last_verdict = "confirm"
    fc.on_user_turn("对。")
    assert fc.current == 1
    # 陈述步(ref 冇 ？)收到单字应承 → 寒暄,UNCLEAR 停留唔推流程
    fc2 = FlowController.from_template(
        {"steps_json": '[{"goal":"g1","ref":"我们会一赔二赔付到您的账户"},{"goal":"g2","ref":"r2"}]'}, OBJ
    )
    v = fc2.rule_verdict("嗯。")
    fc2.last_verdict = v
    assert v == "unclear"
    assert fc2.current == 0


def test_multi_char_ack_still_confirms_any_step():
    fc = FlowController.from_template(
        {"steps_json": '[{"goal":"g1","ref":"我们会一赔二赔付到您的账户"},{"goal":"g2","ref":"r2"}]'}, OBJ
    )
    fc.on_user_turn("好啊，好啊，好。")
    assert fc.current == 1


def test_short_ack_on_statement_step_renders_reask_guidance():
    fc = FlowController.from_template(
        {"steps_json": '[{"goal":"g1","ref":"这一步是说明赔付方案"},{"goal":"g2","ref":"r2"}]'}, OBJ
    )
    v = fc.rule_verdict("嗯。")
    fc.last_verdict = v
    assert v == "unclear"
    assert "换个说法简短再引导" in fc.current_step_text()


# ---- WS4 数字读回核对(2026-09-07) ----


def test_digit_runs_in_extracts_normalized_runs():
    assert _digit_runs_in("我的单号是一二三四") == ["1234"]
    assert _digit_runs_in("尾号7890，电话98765432") == ["7890", "98765432"]
    assert _digit_runs_in("没有数字") == []


def test_last_digits_render_readback_guidance():
    fc = FlowController.from_template(
        {"steps_json": '[{"goal":"g1","ref":"r1"},{"goal":"g2","ref":"r2"}]'}, OBJ
    )
    fc.last_digits = ["1234"]
    cur = fc.current_step_text()
    assert "逐位复述核对" in cur and "1234" in cur
    fc.last_digits = []
    assert "逐位复述核对" not in fc.current_step_text()


# ---- REPEAT verdict:没听清/要求重复 → 照讲复述(2026-09-09 校准,防复读禁令一刀切)----

from agent_runtime.flow import QUESTION, REFUSE, REPEAT, decide_advance  # noqa: E402


def test_repeat_verdict_detection():
    # 短句没听清/要求重复 → REPEAT
    for t in ("听唔清", "听不清", "冇听清", "你说什么？", "乜嘢话？", "再说一次", "再讲一遍啦",
              "大声啲", "what?", "pardon", "come again"):
        assert decide_advance(t) == REPEAT, t
    # 长句里出现「乜嘢」=内容提问,唔係 REPEAT(短句门 12 字)
    assert decide_advance("我想问下乜嘢时候可以赔到我") != REPEAT
    # 拒绝优先于 REPEAT:「听唔清,唔好再打」主体係收线
    assert decide_advance("听唔清，唔好再打") == REFUSE
    # 普通提问照 QUESTION
    assert decide_advance("赔偿要点样算？") == QUESTION


def test_repeat_never_advances():
    # 开场步与中段步:REPEAT 都停留
    assert should_auto_advance(current=0, goal="开场", ref="请问係咪你?", user_text="听唔清", verdict=REPEAT) is False
    assert should_auto_advance(current=2, goal="办理", ref="帮你办理", user_text="再说一次", verdict=REPEAT) is False


def test_repeat_guidance_renders():
    fc2 = FlowController(
        steps=parse_steps('[{"goal":"确认包裹是否本人的","ref":"你好{姓名}"},{"goal":"说明一赔二","ref":"会一赔二赔付"}]'),
    )
    fc2.vars_map = {"姓名": "陈生"}
    fc2.advance()
    fc2.last_verdict = REPEAT
    cur = fc2.current_step_text()
    assert "【客户没听清，要求重复】" in cur
    assert "照讲" in cur
    # REPEAT 唔触发【新一步】(冇 advance 发生);QUESTION 指引照旧禁复读(非重复语境)
    fc2.last_verdict = QUESTION
    assert "绝不重复" in fc2.current_step_text()


def test_say_step_loose_semantics_scoped_to_notification_step():
    # 2026-09-12 call-8fa17d2b:赔偿直念步(say=1,承诺型问句「可以接受吗」)吃到
    # 通知步的宽松推进语义——UNCLEAR 语气点评「呃，自然多了」假推进 step4→5。
    # 修复:say_step 由调用方只对通知型步(goal 含「通知」)传 True;此测试钉死
    # 流程层契约:赔偿步传 say_step=False 时 UNCLEAR 唔推。
    from agent_runtime.flow import should_auto_advance

    g4 = "赔偿标准:核实订单金额后按三档标准讲赔偿,问客户是否接受"
    r4 = "首先，我们会先核实您这件货品的订单金额…您看这个方案可以接受吗？"
    assert should_auto_advance(current=3, goal=g4, ref=r4, user_text="呃，自然多了", verdict="unclear", say_step=False) is False
    # 真确认经 agent 的 rule=confirm 分支推进(wa_confirm_advance_allowed 放行)
    from agent_runtime.flow import wa_confirm_advance_allowed

    assert wa_confirm_advance_allowed(goal=g4, ref=r4, captured=False) is True
    # 通知步(调用方会传 say_step=True)宽松语义照旧
    g2 = "来电通知:自报家门,通知货件遗失,问客户记不记得货品"
    assert should_auto_advance(current=1, goal=g2, ref="我哋係…記唔記得?", user_text="啊，不记得了。", verdict="unclear", say_step=True) is True


def test_judge_confirm_gate_blocks_ackless_long_utt():
    # call-8fa17d2b:长 UNCLEAR 轮被 judge 误判 confirm——问句步(赔偿接受吗)
    # 无应承特征的长句拦下;短句与真应承放行;非问句步(通知)保持宽松。
    from agent_runtime.flow import judge_confirm_advance_allowed

    g4 = "赔偿标准:核实订单金额后按三档标准讲赔偿,问客户是否接受"
    r4 = "首先，我们会先核实您这件货品的订单金额…您看这个方案可以接受吗？"
    assert judge_confirm_advance_allowed(goal=g4, ref=r4, user_text="他这个就过来了。") is False
    assert judge_confirm_advance_allowed(goal=g4, ref=r4, user_text="可以，就这样赔偿吧") is True
    assert judge_confirm_advance_allowed(goal=g4, ref=r4, user_text="行") is True  # 短句
    g2 = "来电通知:自报家门,通知货件遗失"  # 非问句步(无？)
    assert judge_confirm_advance_allowed(goal=g2, ref="我哋係集運中轉倉…", user_text="随便讲点什么都很长的一段话") is True
