#!/usr/bin/env python3
"""三语模板 6 步 → 8 步迁移(2026-09-13 甲.1 拆步终稿)。

背景:step4 赔偿三档 147 字/step6 收尾 160 字一次性直念=28-30s 长独白,
是 call-909744db「太长啦」饿死链的数据根源。拆 8 步,单次直念铁律 ≤50 字
(≈10s);三档细则移入「客户追问」分支(渐进披露);承诺单源化(銀聯理賠專員
全程/zh 语音通话补渠道/EN 补 Hong Kong/登记口令三语统一繁体「貨件遺失申請理賠」);
补高频质疑分支(骗子/点解有我电话/唔得闲约跟进);hotwords 补平台名。

用法:
    python scripts/migrate_templates_8step_0913.py           # dry-run(校验+打印)
    python scripts/migrate_templates_8step_0913.py --apply   # 落库(更新+revisions)
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))
from agent_runtime.flow import parse_step_ref, parse_steps  # noqa: E402

DB = Path.home() / "Library/Application Support/BokVoice/bok_voice.db"

SAY_LIMIT_ZH = 55  # 中文字(≈11s 音频)
SAY_LIMIT_EN_WORDS = 28  # 英文词(≈9-10s)

TEMPLATES: dict[str, dict] = {
    # ---- zh b0d50586a040 ----
    "b0d50586a040": {
        "hotwords": "拼多多,淘宝,京东,小红书,微信小店,银联,中转仓,货件遗失申请理赔",
        "steps": [
            {"goal": "身份确认", "say": False, "ref": (
                "您好，请问是{姓名}吗？\n"
                "如果客户听不清、要你再讲一次 → 放慢语速再问一次，不要自报家门。\n"
                "如果客户说打错电话/不是本人 → 礼貌道歉收线，不要继续说下去。\n"
                "如果客户问你是谁/为什么打来 → 简单说是集运中转仓的来电，然后继续确认身份。"
            )},
            {"goal": "通知：货件遗失致歉+问货品", "say": True, "ref": (
                "我们是集运中转仓。很抱歉，您有一件货件在打包期间遗失了，我们会负责赔偿。您还记得当时买了什么货品吗？\n"
                "如果客户说不记得 → 没关系，我们会帮您核对订单，然后去下一步问平台。\n"
                "如果客户说出了货品 → 认真听，简单回应一声，然后去下一步问平台。\n"
                "如果客户质疑为什么会遗失 → 承认是我们打包环节的失误，诚恳道歉，不推卸；答完再带回货品问题。\n"
                "如果客户很生气/在骂 → 先安抚：完全理解您的不满，我们会负责到底；等客户情绪平复些再继续问。\n"
                "如果客户问丢的是什么货 → 说要核对订单才能确认到，去下一步问平台。\n"
                "如果客户问你们是不是骗子/为什么知道我电话 → 说明是集运仓按收件登记信息来电核实，语气诚恳不争辩；答完带回货品问题。"
            )},
            {"goal": "核实购买平台", "say": False, "ref": (
                "那您是在拼多多、淘宝还是京东买的？您在您的订单里就可以看到。\n"
                "如果客户说不记得哪个平台 → 提示客户看手机里最近的购物订单。\n"
                "如果客户问为什么要核对 → 说要确认清楚是哪件遗失才赔得准。\n"
                "如果客户问怎么赔偿 → 简单说按香港速递条例标准赔、最低 300 元起，细节到赔偿方案那一步讲；答完继续问平台。\n"
                "注意:客户在其他平台买（小红书、微信小店等）也算答到，照常推进。"
            )},
            {"goal": "通知：赔偿方案确认", "say": True, "ref": (
                "我们会按香港速递条例给您赔偿，最低 300 元起。您看这个方案可以接受吗？\n"
                "如果客户问具体怎么赔/赔多少 → 逐档讲：金额不足 100 元，申请 300 到 600 元赔偿；大于 200 元，按货价 2 至 3 倍；超过 1000 元，按原价赔再加 300 元快递保险补偿。\n"
                "如果客户问为什么这样赔 → 简单说是按香港速递条例和快递保险标准。\n"
                "如果客户嫌少/不接受 → 不承诺额外金额，说会帮客户争取最高标准，记录意见。\n"
                "如果客户问什么时候收到 → 核实好订单金额之后就办理。\n"
                "注意:三档金额跟足这个标准讲，不要自创金额。\n"
                "注意:这步只讲赔偿方案，不用收号码，号码在下一步办理才收。"
            )},
            {"goal": "办理收号（传截图）", "say": False, "ref": (
                "办理理赔要用{联系方式}传订单截图核对。麻烦您提供一个{联系方式}号码，银联理赔专员会加您收截图。\n"
                "如果客户说有时间但没给号码 → 提醒传截图要用{联系方式}，请客户报号码。\n"
                "如果客户说没用/不方便/现在没空 → 问客户什么时间方便或者用什么方式，约好再跟进，不要硬要。\n"
                "如果客户问为什么要号码 → 说传截图和理赔核对都是经{联系方式}由专员跟进。\n"
                "如果客户报出号码(数字串) → 复述确认一次，说专员会加客户收截图，然后推进下一步交代办理要求。\n"
                "注意:方向是请客户报自己的号码、由专员加客户；不是叫客户加我们。"
            )},
            {"goal": "通知：办结确认", "say": True, "ref": (
                "好的，收到您的号码了。我们马上提交资料，稍后银联理赔专员会加您，您留意通知就可以。\n"
                "如果客户还有疑问 → 先答疑问，答完提醒留意专员通知。"
            )},
            {"goal": "通知：办理须知", "say": True, "ref": (
                "两点提前说清楚：专员加上您之后，把对应的商品订单发给他核对；发完之后打字回复「貨件遺失申請理賠」登记。\n"
                "如果客户问要等多久 → 说大约一分钟，专员收到资料会直接在{联系方式}打语音通话给您，您接听就可以。\n"
                "如果客户说记不住口令 → 放慢再说一遍「貨件遺失申請理賠」，提醒照原样打字回复。\n"
                "注意:口令字面三语统一用「貨件遺失申請理賠」，不要换成简体或英文。"
            )},
            {"goal": "收线道别", "say": False, "ref": (
                "好的，感谢您今天的时间，再见！\n"
                "如果客户还有其他问题 → 先答完再道别收线。\n"
                "如果客户说不想办/拒绝 → 说声没关系，礼貌收线。\n"
                "注意:道别后不再开启新话题，等客户挂线或按收线流程结束。"
            )},
        ],
    },
    # ---- cantonese febeeeebac97 ----
    "febeeeebac97": {
        "hotwords": "拼多多,淘寶,京東,小紅書,微信小店,銀聯,中轉倉,貨件遺失申請理賠",
        "steps": [
            {"goal": "身份確認", "say": False, "ref": (
                "你好，請問係{姓名}嗎？\n"
                "如果客户听唔清、要你再讲一次 → 放慢语速再问一次，唔好自报家门。\n"
                "如果客户话打错电话/唔係本人 → 礼貌道歉收线，唔好继续讲落去。\n"
                "如果客户问你係边个/点解打嚟 → 简单话係集运中转仓来电，然后继续确认身份。"
            )},
            {"goal": "通知：貨件遺失致歉+問貨品", "say": True, "ref": (
                "我哋係集運中轉倉。好抱歉，你有一件貨件喺打包期間遺失咗，我哋會負責賠償。你仲記唔記得當時買咗咩貨品呢？\n"
                "如果客户话唔记得 → 唔使紧，我哋会帮你核对订单，直接去下一步问平台。\n"
                "如果客户讲出咗货品 → 认真听，简单回应一声，然后去下一步问平台。\n"
                "如果客户质疑点解会遗失 → 承认係我哋打包环节嘅失误，诚恳道歉，唔好推卸；答完再带返货品问题。\n"
                "如果客户好嬲/闹紧 → 先安抚：完全明白你嘅不满，我哋会负责到底；等佢情绪平复啲先继续问。\n"
                "如果客户问遗失嘅係咩货 → 话要核对订单先可以确认到，去下一步问平台。\n"
                "如果客户问你哋係咪呃人/点解有佢电话 → 话係集运仓按收件登记资料来电核实，语气诚恳唔好驳；答完带返货品问题。"
            )},
            {"goal": "核實購買平台", "say": False, "ref": (
                "咁你係喺拼多多、淘寶定京東買㗎？你喺你嘅訂單入面就可以睇到。\n"
                "如果客户话唔记得边个平台 → 提佢睇下手机入面最近嘅购物订单。\n"
                "如果客户问点解要核对 → 话要确认清楚係边件遗失先赔得准。\n"
                "如果客户问点样赔偿 → 简单话按香港速递条例标准赔、最低 300 蚊起，细节到赔偿方案嗰步讲；答完继续问平台。\n"
                "注意:客户喺第啲平台買（小紅書、微信小店等）都算答到，照常推进。"
            )},
            {"goal": "通知：賠償方案確認", "say": True, "ref": (
                "我哋會按香港速遞條例賠償俾你，最低 300 蚊起。你睇呢個方案可以接受嗎？\n"
                "如果客户问具体点赔/赔几多 → 逐档讲：金额不足 100 蚊，申请 300 到 600 蚊赔偿；大于 200 蚊，按货价 2 至 3 倍；超过 1000 蚊，按原价赔再加 300 蚊速递保险补偿。\n"
                "如果客户问点解咁赔 → 简单讲係按香港速递条例同速递保险标准。\n"
                "如果客户嫌少/唔接受 → 唔好承诺额外金额，话会帮你争取最高标准，记录意见。\n"
                "如果客户问几时收到 → 核实好订单金额之后就办理。\n"
                "注意:三档金额跟足呢个标准讲，唔好自创金额。\n"
                "注意:呢步只讲赔偿方案，唔使收号码，号码喺下一步办理先收。"
            )},
            {"goal": "辦理收號（傳截圖）", "say": False, "ref": (
                "辦理理賠要用{聯絡方式}傳訂單截圖核對。唔該你留個{聯絡方式}號碼，銀聯理賠專員會加你收截圖。\n"
                "如果客户话有时间但未俾号码 → 提醒传截图要用{聯絡方式}，请佢报号码。\n"
                "如果客户话冇用/唔方便/而家冇空 → 问佢几时方便或者用咩方式，约好再跟进，唔好硬要。\n"
                "如果客户问点解要号码 → 话传截图同理赔核对都係经{聯絡方式}由专员跟进。\n"
                "如果客户报出号码(数字串) → 复述确认一次，话专员会加佢收截图，然后推进下一步交代办理要求。\n"
                "注意:方向係请客户报自己嘅号码、由专员加客户；唔係叫客户加我哋。"
            )},
            {"goal": "通知：辦結確認", "say": True, "ref": (
                "好嘅，收到你嘅號碼喇。我哋而家即刻提交資料，稍後銀聯理賠專員會加你，你留意通知就得。\n"
                "如果客户仲有疑问 → 先答疑问，答完提醒留意专员通知。"
            )},
            {"goal": "通知：辦理須知", "say": True, "ref": (
                "兩點預先講清楚：專員加咗你之後，將對應嘅商品訂單發俾佢核對；發完之後打字回覆「貨件遺失申請理賠」登記。\n"
                "如果客户问要等几耐 → 话大约一分钟，专员收到资料会直接喺{聯絡方式}打语音通话俾你，你接听就得。\n"
                "如果客户话记唔住口令 → 放慢再讲一遍「貨件遺失申請理賠」，提醒照原样打字回复。\n"
                "注意:口令字面三语统一用「貨件遺失申請理賠」，唔好换成简体或者英文。"
            )},
            {"goal": "收線道別", "say": False, "ref": (
                "好嘅，唔該晒你今日嘅時間，再見！\n"
                "如果客户仲有其他问题 → 先答完再道别收线。\n"
                "如果客户话唔想办/拒绝 → 讲声唔紧要，礼貌收线。\n"
                "注意:道别之后唔好再开新话题，等客户挂线或者按收线流程结束。"
            )},
        ],
    },
    # ---- en 80afec7f2f92 ----
    "80afec7f2f92": {
        "hotwords": "Amazon,Temu,eBay,UnionPay,claims,screenshot,小红书",
        "steps": [
            {"goal": "Confirm identity", "say": False, "ref": (
                "Hello, may I speak to {name}?\n"
                "If the customer didn't hear you and asks you to repeat → ask again slowly, don't introduce yourself yet.\n"
                "If the customer says wrong number / not the person → apologise politely and end the call.\n"
                "If the customer asks who is calling or why → say it's the consolidation warehouse calling, then continue confirming identity."
            )},
            {"goal": "Notice: parcel lost, ask about the item", "say": True, "ref": (
                "This is the consolidation warehouse. One of your parcels went missing while being packed, and we will compensate you. Do you remember what you ordered?\n"
                "If the customer doesn't remember → no problem, we'll help verify the orders, go to the next step (platform).\n"
                "If the customer names the item → listen carefully, acknowledge briefly, then go to the next step (platform).\n"
                "If the customer questions how it got lost → admit the packing mistake, apologise sincerely, don't shift blame; then bring the conversation back.\n"
                "If the customer is angry → calm them first: you fully understand their frustration and will take responsibility; continue once they've settled.\n"
                "If the customer asks what was lost → say we need to verify the orders to confirm, go to the next step (platform).\n"
                "If the customer asks if this is a scam / how you got their number → say we're calling on the registered recipient details to verify, stay sincere, don't argue; then bring the conversation back."
            )},
            {"goal": "Verify the shopping platform", "say": False, "ref": (
                "May I ask where you bought it — Amazon, Temu, or eBay? You can check it in your orders.\n"
                "If the customer doesn't remember the platform → suggest checking recent orders in the shopping apps.\n"
                "If the customer asks why we need this → say we must confirm which parcel was lost to compensate accurately.\n"
                "If the customer asks how the compensation works → briefly say it follows the Hong Kong courier regulations, starting from 300 dollars; details come at the compensation step.\n"
                "Note: other platforms (Xiaohongshu, WeChat store and so on) also count as an answer, advance as usual."
            )},
            {"goal": "Notice: compensation plan", "say": True, "ref": (
                "We'll compensate you under the Hong Kong courier regulations, starting from 300 dollars. Does this plan sound acceptable to you?\n"
                "If the customer asks exactly how it works → walk through the tiers: under 100 dollars, 300 to 600 dollars; over 200 dollars, 2 to 3 times the item price; over 1,000 dollars, the original price plus an extra 300 dollars of courier insurance.\n"
                "If the customer asks why → briefly say it follows the Hong Kong courier regulations and the insurance standard.\n"
                "If the customer says it's too little → don't promise extra amounts, say we'll seek the highest standard and note the feedback.\n"
                "If the customer asks when they'll get it → after the order value is verified, we'll process it.\n"
                "Note: quote exactly these three tiers, never invent amounts.\n"
                "Note: this step only explains compensation, don't collect any number yet, that's the next step."
            )},
            {"goal": "Collect the number (for screenshots)", "say": False, "ref": (
                "To verify, our specialist needs your recent unfulfilled order screenshots via {contact}. Could you give me your {contact} number so the UnionPay claims specialist can add you?\n"
                "If the customer has time but hasn't given the number → remind them the screenshot goes via {contact}, then ask again for the number.\n"
                "If the customer doesn't use it / isn't convenient / is busy → ask when is convenient or by what means, follow up later, don't push.\n"
                "If the customer asks why you need it → say the screenshot and the claim verification both go through {contact} with the specialist.\n"
                "If the customer gives a number (digit string) → read it back to confirm, say the specialist will add them, then advance to the next step for the handling instructions.\n"
                "Note: ask for the customer's own number so the specialist can add them — never ask the customer to add us."
            )},
            {"goal": "Notice: submitted", "say": True, "ref": (
                "Great, I've got your number. We're submitting your details now — our UnionPay claims specialist will add you shortly, please watch for the notification.\n"
                "If the customer still has questions → answer first, then remind them to watch for the specialist."
            )},
            {"goal": "Notice: two instructions", "say": True, "ref": (
                "Two things to note: once the specialist adds you, send them your order for verification; then reply with the text 「貨件遺失申請理賠」 to register.\n"
                "If the customer asks how long → about a minute; once the specialist receives the details they will call you directly on {contact}, just answer.\n"
                "If the customer can't remember the phrase → say 「貨件遺失申請理賠」 again slowly, ask them to reply with exactly the same text.\n"
                "Note: the registration phrase is exactly 「貨件遺失申請理賠」 for all languages, never translate or simplify it."
            )},
            {"goal": "Closing farewell", "say": False, "ref": (
                "Thank you for your time today, goodbye!\n"
                "If the customer still has other questions → answer them first, then close.\n"
                "If the customer refuses → say no problem, close politely.\n"
                "Note: after the farewell, don't start new topics; wait for the customer to hang up or follow the closing flow."
            )},
        ],
    },
}


def validate(tid: str, spec: dict) -> list[str]:
    errs: list[str] = []
    steps = parse_steps(json.dumps(spec["steps"], ensure_ascii=False))
    if len(steps) != 8:
        errs.append(f"{tid}: expected 8 steps, got {len(steps)}")
    is_en = tid == "80afec7f2f92"
    for i, s in enumerate(steps, 1):
        parts = parse_step_ref(s.ref)
        if not parts.script:
            errs.append(f"{tid} step{i}: no script line")
        if s.goal.startswith("Notice") and not s.say:
            errs.append(f"{tid} step{i}: Notice goal should be say=1")
        if s.say:
            if is_en:
                n = len(parts.script.split())
                if n > SAY_LIMIT_EN_WORDS:
                    errs.append(f"{tid} step{i}: say script {n} words > {SAY_LIMIT_EN_WORDS}: {parts.script[:60]}")
            else:
                if len(parts.script) > SAY_LIMIT_ZH:
                    errs.append(f"{tid} step{i}: say script {len(parts.script)} chars > {SAY_LIMIT_ZH}: {parts.script[:40]}")
        if not s.say and not s.goal.startswith("Notice") and "通知" in s.goal and not s.say:
            pass
    # hotwords 长度护栏 ≤120 字符
    if len(spec["hotwords"]) > 120:
        errs.append(f"{tid}: hotwords {len(spec['hotwords'])} > 120")
    return errs


def main() -> None:
    apply = "--apply" in sys.argv
    all_errs = []
    for tid, spec in TEMPLATES.items():
        all_errs += validate(tid, spec)
    if all_errs:
        print("VALIDATION FAILED:")
        for e in all_errs:
            print(" -", e)
        sys.exit(1)
    print("validation OK: 3 templates x 8 steps, say-step length gates passed")
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    for tid, spec in TEMPLATES.items():
        row = cur.execute(
            "SELECT name, language FROM conversation_templates WHERE id=?", (tid,)
        ).fetchone()
        if not row:
            print(f"!! {tid} not found in DB, skip")
            continue
        steps_json = json.dumps(spec["steps"], ensure_ascii=False)
        print(f"{tid} ({row[0]} / {row[1]}): 8 steps, hotwords='{spec['hotwords'][:40]}…'")
        if not apply:
            continue
        cur.execute(
            "UPDATE conversation_templates SET steps_json=?, hotwords=? WHERE id=?",
            (steps_json, spec["hotwords"], tid),
        )
        rev = cur.execute(
            "SELECT COUNT(*) FROM conversation_template_revisions WHERE template_id=?", (tid,)
        ).fetchone()[0] + 1
        snapshot = cur.execute(
            "SELECT * FROM conversation_templates WHERE id=?", (tid,)
        ).fetchone()
        cols = [d[0] for d in cur.description]
        snap = json.dumps(dict(zip(cols, snapshot)), ensure_ascii=False)
        cur.execute(
            "INSERT INTO conversation_template_revisions (id, template_id, revision, snapshot, updated_at)"
            " VALUES (?,?,?,?,datetime('now'))",
            (f"rev-{tid}-{rev}", tid, rev, snap),
        )
        print(f"  applied + revision #{rev} snapshot")
    if apply:
        conn.commit()
        print("DB committed.")
    else:
        print("dry-run only (pass --apply to write)")
    conn.close()


if __name__ == "__main__":
    main()
