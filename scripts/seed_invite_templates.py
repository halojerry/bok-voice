#!/usr/bin/env python3
"""三语「客服邀约」话术种子（2026-09-24 云端腿测试专用）。

中性场景：家电延保服务回访邀约——**不含赔偿/货损/平台核实域**，用于
「都用云端（MiniMax LLM+TTS）」TTFT 腿与邀约话术通用链路验证。

用法：
  python scripts/seed_invite_templates.py [--api http://127.0.0.1:8000]
幂等：同名同语言模板已存在则复用（打印 id），不重复建。
"""
from __future__ import annotations

import argparse
import json
import os

import httpx

API_DEFAULT = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")
ACCOUNT = os.environ.get("BOK_ACCOUNT", "acc-001")

TEMPLATES: list[dict] = [
    {
        "name": "邀约-延保服务回访-普",
        "language": "zh",
        "hotwords": "延保,回访,上门,检测,免费,预约,工程师,家电,更换,配件",
        "steps": [
            {
                "goal": "确认身份",
                "ref": "您好，请问是{姓名}吗？\n如果客户不是本人→请对方转告，稍后再联系",
            },
            {
                "goal": "自报家门与来意",
                "say": True,
                "ref": "您好，我是品牌客户服务中心的专员。这次来电是想邀请您参加我们为老客户安排的免费延保服务回访，顺便帮您的家电做一次免费检测。",
            },
            {
                "goal": "说明回访内容",
                "ref": "这次回访大概需要二十分钟，工程师会上门检查电器运行情况，并免费更换易损配件。请问您平时家里方便接待上门服务吗？\n如果客户问要收费吗→全程免费，不收任何费用\n注意:一次只问一件事",
            },
            {
                "goal": "约定上门时间",
                "ref": "那请问您这周哪天方便呢？周六周日我们都可以安排。\n如果客户说最近都没空→问下周三之前哪天合适",
            },
            {
                "goal": "登记联系方式",
                "ref": "好的，我帮您登记。请问您的{联系方式}是多少？预约成功后我们把确认信息发给您。\n如果客户不愿意留→告知号码只用于发送预约确认",
            },
            {
                "goal": "收尾告别",
                "ref": "好的，已经帮您登记好了，感谢您的支持，祝您生活愉快，再见。",
            },
        ],
    },
    {
        "name": "邀约-延保服務回訪-粵",
        "language": "cantonese",
        "hotwords": "延保,回訪,上門,檢測,免費,預約,工程師,家電,更換,配件",
        "steps": [
            {
                "goal": "確認身份",
                "ref": "您好，請問係{姓名}嗎？\n如果客戶唔係本人→請對方轉告，遲啲再聯絡",
            },
            {
                "goal": "自報家門與來意",
                "say": True,
                "ref": "您好，我係品牌客戶服務中心嘅專員。今次打嚟係想邀請您參加我哋為舊客戶安排嘅免費延保服務回訪，順便幫您嘅家電做一次免費檢測。",
            },
            {
                "goal": "說明回訪內容",
                "ref": "今次回訪大概需要二十分鐘，工程師會上門檢查電器運作，仲會免費更換易損配件。請問您平時屋企方便接待上門服務嗎？\n如果客戶問要唔要收費→全程免費，唔收任何費用\n注意:一次只問一件事",
            },
            {
                "goal": "約定上門時間",
                "ref": "咁請問您今個禮拜邊日得閒呢？禮拜六禮拜日我哋都可以安排。\n如果客戶話最近都冇時間→問下星期三之前邊日合適",
            },
            {
                "goal": "登記聯絡方式",
                "ref": "好嘅，我幫您登記。請問您嘅{聯絡方式}係幾多？預約成功之後我哋發確認信息俾您。\n如果客戶唔願意留→告知號碼只用嚟發預約確認",
            },
            {
                "goal": "收尾告別",
                "ref": "好嘅，已經幫您登記好喇，多謝您嘅支持，祝您生活愉快，再見。",
            },
        ],
    },
    {
        "name": "Invite-Warranty-Followup-EN",
        "language": "en",
        "hotwords": "warranty,follow-up,visit,engineer,appointment,free,check-up,appliance,parts,Saturday,Sunday",
        "steps": [
            {
                "goal": "Confirm identity",
                "ref": "Hello, may I speak with {name} please?\n如果客户不是本人→No problem, I will call back at a better time.",
            },
            {
                "goal": "Introduce and state purpose",
                "say": True,
                "ref": "Hello, this is the customer service center of your appliance brand. We are calling to invite you to a free extended-warranty follow-up visit, including a free check-up for your appliance.",
            },
            {
                "goal": "Explain the visit",
                "ref": "The visit takes about twenty minutes. Our engineer will check your appliance and replace any worn parts for free. Would it be convenient for us to visit your home?\n如果客户问要收费吗→The whole service is completely free of charge.\n注意:一次只问一件事",
            },
            {
                "goal": "Schedule the visit",
                "ref": "Great. Which day this week works best for you? Saturday and Sunday are also available.\n如果客户说最近都没空→No problem, any day before next Wednesday also works for us.",
            },
            {
                "goal": "Register contact",
                "ref": "Perfect, let me register that for you. May I have your {contact} so we can send you the confirmation?\n如果客户不愿意留→No worries, we only use it to send your booking confirmation.",
            },
            {
                "goal": "Close the call",
                "ref": "All set, you are registered. Thank you for your support, and have a great day. Goodbye.",
            },
        ],
    },
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=API_DEFAULT)
    args = parser.parse_args()
    headers = {}
    token = os.environ.get("BOK_CP_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(timeout=30, headers=headers) as client:
        existing = client.get(
            f"{args.api}/api/templates", params={"account_id": ACCOUNT}
        ).json()
        existing = existing.get("items", existing) if isinstance(existing, dict) else existing
        by_name = {str(t.get("name")): str(t.get("id")) for t in existing}
        for tpl in TEMPLATES:
            if tpl["name"] in by_name:
                print(f"{tpl['language']}: {tpl['name']} -> 复用 {by_name[tpl['name']]}")
                continue
            body = {
                "name": tpl["name"],
                "language": tpl["language"],
                "steps_json": json.dumps(tpl["steps"], ensure_ascii=False),
                "hotwords": tpl["hotwords"],
            }
            resp = client.post(
                f"{args.api}/api/templates", params={"account_id": ACCOUNT}, json=body
            )
            resp.raise_for_status()
            created = resp.json()
            print(f"{tpl['language']}: {tpl['name']} -> 新建 {created.get('id')}")


if __name__ == "__main__":
    main()
