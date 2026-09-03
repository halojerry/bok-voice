"""对话流程控制器:分步话术推进 + 对象变量渲染 + 每轮"只走一步"约束。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.flow import (  # noqa: E402
    FlowController,
    decide_advance,
    facts_line,
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
    assert fc.current_step_text() == ""  # 流程完成不再注入当前步


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
    # 推进后注入「新一步」提示,提醒 LLM 换步(唔好延续旧承诺)。
    assert "【新一步】" in cur and "唔好延续上一步" in cur


def test_not_confirm_stays_with_recall_guidance():
    # 客户答唔到:唔推进,但当前步注入「核對/引導資料」後備——用訂單/截圖引導提供資料,
    # 唔係「轉專人」,亦唔好自己亂加「幫你查完再覆你」。
    fc = FlowController.from_template({"steps_json": '[{"goal":"确认包裹是不是{姓名}本人的","ref":"你好{姓名}"},'
                                                     '{"goal":"说明一赔二","ref":"会一赔二赔付"}]'}, OBJ)
    fc.on_user_turn("我唔记得咗。")
    assert fc.current == 0
    cur = fc.current_step_text()
    assert "核對/引導資料" in cur or "核对/引导资料" in cur
    assert "訂單" in cur and "截圖" in cur  # 引導提供資料,唔係臨時承諾
    assert "我幫你查完再覆你" in cur and "全程你自己同客戶傾" in cur


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
    # 開場步:客俾咗實質回應(唔記得)→ 即過;純提問/拒絕 → 唔過。
    from agent_runtime.flow import should_auto_advance
    g = "開場說明:通知貨件遺失,問客戶記唔記得買咗咩"
    r = "你好,請問係{姓名}嗎?…件貨遺失…問你記唔記得買咗咩"
    assert should_auto_advance(current=0, goal=g, ref=r, user_text="我唔记得咗啊！", verdict="unclear") is True
    assert should_auto_advance(current=0, goal=g, ref=r, user_text="好呀，係我", verdict="confirm") is True
    assert should_auto_advance(current=0, goal=g, ref=r, user_text="你哋係邊間公司㗎？", verdict="question") is False
    assert should_auto_advance(current=0, goal=g, ref=r, user_text="唔好再打嚟！", verdict="objection") is False


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
    assert fc.current_step_text() == ""
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
    assert "第 1/4 步" in fc.current_step_text()
    # 开场步的 ref 是 opening 全文
    assert "你好请问" in fc.current_step_text()


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
    # 当前步注入须明确区分"参考要点(内部)"与"对客户说的话",禁止复述分支指示。
    from agent_runtime.flow import FlowController

    fc = FlowController.from_template(
        {"steps_json": '[{"goal":"确认包裹","ref":"你好{姓名}。\\n如果客户唔记得 → 提佢地址帮佢回忆"}]'},
        OBJ,
    )
    txt = fc.current_step_text()
    assert "勿念给客户" in txt
    assert "绝不把「如果" in txt
    assert "参考要点(内部指示" in txt
