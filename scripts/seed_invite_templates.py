#!/usr/bin/env python3
"""三语「客服邀约」话术种子（2026-09-24 精炼版）。

中性场景：家电延保服务回访邀约——**不含赔偿/货损/平台核实域**，用于
邀约话术通用链路验证与延迟基线。

2026-09-24 人感精炼改写（-38%/-36%/-24% 字数）：砍官僚腔、动词直给、
每步 ≤2 短句；语气词只用真词（好的/唔緊要/No problem），不用任何
声音标记——标记只活在 LLM 轮 prompt 规则与合成层过滤（P1 人感包域）。

用法：
  python scripts/seed_invite_templates.py [--api http://127.0.0.1:8000]
幂等 upsert：同名模板已存在 → PUT 更新 steps_json/hotwords（不重复建、
不留旧版）；不存在 → 新建。
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
                "ref": "您好，请问是{姓名}吗？\n如果客户不是本人→麻烦您转告他，我稍后再打来。",
            },
            {
                "goal": "自报家门与来意",
                "say": True,
                "ref": "您好，我是您家电品牌的客服。想请您参加免费的延保回访，顺便给家电做个免费检测。",
            },
            {
                "goal": "说明回访内容",
                "ref": "工程师上门约二十分钟，检查电器，免费更换易损配件。您家里方便吗？\n如果客户问要收费吗→对，全程免费，不收任何费用\n注意:一次只问一件事",
            },
            {
                "goal": "约定上门时间",
                "ref": "您这周哪天方便？周六周日都可以。\n如果客户说最近都没空→没关系，那下周三之前哪天合适呢？",
            },
            {
                "goal": "登记联系方式",
                "ref": "好的，我帮您登记。您的{联系方式}是多少？\n如果客户不愿意留→这个号码只用来发预约确认，您放心。",
            },
            {
                "goal": "收尾告别",
                "ref": "登记好了，确认信息稍后发给您。祝您愉快，再见。",
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
                "ref": "您好，請問係{姓名}嗎？\n如果客戶唔係本人→麻煩您話返俾佢聽，我遲啲再打嚟。",
            },
            {
                "goal": "自報家門與來意",
                "say": True,
                "ref": "您好，我係您家電品牌嘅客服。想邀請您參加免費嘅延保回訪，順便幫您嘅家電做個免費檢測。",
            },
            {
                "goal": "說明回訪內容",
                "ref": "工程師上門大約二十分鐘，檢查電器，免費更換易損配件。您屋企方便嗎？\n如果客戶問要唔要收費→係，全程免費，唔收任何費用\n注意:一次只問一件事",
            },
            {
                "goal": "約定上門時間",
                "ref": "您今個禮拜邊日得閒？禮拜六禮拜日都得。\n如果客戶話最近都冇時間→唔緊要，咁下星期三之前邊日得閒呢？",
            },
            {
                "goal": "登記聯絡方式",
                "ref": "好嘅，我幫您登記。您嘅{聯絡方式}係幾多？\n如果客戶唔願意留→呢個號碼淨係用嚟發預約確認，您放心。",
            },
            {
                "goal": "收尾告別",
                "ref": "登記好喇，確認信息遲啲發俾您。祝您愉快，再見。",
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
                "ref": "Hello, may I speak with {name} please?\n如果客户不是本人→No problem, could you let them know? I'll call back later.",
            },
            {
                "goal": "Introduce and state purpose",
                "say": True,
                "ref": "Hello, this is customer service from your appliance brand. We'd like to invite you to a free extended-warranty follow-up visit, with a free check-up.",
            },
            {
                "goal": "Explain the visit",
                "ref": "The engineer visits your home for about twenty minutes, checks your appliance, and replaces worn parts for free. Would that be convenient for you?\n如果客户问要收费吗→Yes, it's completely free, no charge at all\n注意:一次只问一件事",
            },
            {
                "goal": "Schedule the visit",
                "ref": "Which day this week suits you? Saturday and Sunday are both fine.\n如果客户说最近都没空→No problem, any day before next Wednesday also works.",
            },
            {
                "goal": "Register contact",
                "ref": "Okay, let me register that. What's your {contact}?\n如果客户不愿意留→No worries, we only use it to send your booking confirmation.",
            },
            {
                "goal": "Close the call",
                "ref": "All set, the confirmation will be sent shortly. Have a great day. Goodbye.",
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
            body = {
                "steps_json": json.dumps(tpl["steps"], ensure_ascii=False),
                "hotwords": tpl["hotwords"],
            }
            if tpl["name"] in by_name:
                tid = by_name[tpl["name"]]
                resp = client.put(
                    f"{args.api}/api/templates/{tid}",
                    params={"account_id": ACCOUNT},
                    json=body,
                )
                resp.raise_for_status()
                print(f"{tpl['language']}: {tpl['name']} -> 更新 {tid}")
            else:
                created_body = {
                    "name": tpl["name"],
                    "language": tpl["language"],
                    **body,
                }
                resp = client.post(
                    f"{args.api}/api/templates", params={"account_id": ACCOUNT}, json=created_body
                )
                resp.raise_for_status()
                created = resp.json()
                print(f"{tpl['language']}: {tpl['name']} -> 新建 {created.get('id')}")


if __name__ == "__main__":
    main()
