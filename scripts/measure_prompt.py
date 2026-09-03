"""逐段量度实际注入 LLM 的 system 体积（KV-cache 前置的验收工具）。

运行：.venv312/bin/python scripts/measure_prompt.py
（不需要 sidecar；纯本地装配，模拟真实 mid-call 状态）

验收目标（模板绑定的粤语理赔 call）：
- 稳定前缀(指令+话术+当前步) + 人设 base ≤ ~2K 字；
- 每请求 ≤ ~2.5K 字（瘦身前 ~3-4.4K）；
- 前缀段逐轮字节不变（知识/记忆只动尾部）→ 吃 mlx_lm KV-cache。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.flow import FlowController  # noqa: E402
from agent_runtime.providers.livekit_plugins import ContextState  # noqa: E402

# 模拟 DB 里的理赔话术（4 步）+ 对象变量（近似真实长度）
STEPS = [
    {"goal": "确认包裹是不是{姓名}本人的", "ref": "你好，請問係咪{姓名}？我哋係{物流公司}，有個包裹單號尾號{快递尾号}運輸途中唔見咗，想同你核對下。\n如果客戶唔記得 → 提佢地址/電話幫佢回憶"},
    {"goal": "核对购买平台+拿WhatsApp+叫传订单截图", "ref": "你件貨係喺邊個平台買嘅？拼多多/淘寶/京東？為咗核對清楚，你俾個 WhatsApp 號碼我哋加專員聯絡；將最近買咗未收到嘅訂單截圖喺 WhatsApp 傳過嚟，我哋核對到就即時同你辦。"},
    {"goal": "说明赔偿标准三档", "ref": "按平台規則：拼多多300-600、淘寶2-3倍、京東原價+300；唔好亂講具體金額，客戶問到先按平台講範圍。"},
    {"goal": "引导办理+加专员", "ref": "加銀聯專員 WhatsApp 對接，全程 AI 同客戶傾，唔好轉專人/叫客戶提供資料。"},
]
OBJ = {"display_name": "陈先生", "role_template": "buyer", "tracking_no": "7890", "courier": "顺丰", "address": "香港九龍灣宏照道"}


def build_ctx(step_idx: int, with_rag: bool) -> ContextState:
    fc = FlowController.from_template({"steps_json": __import__("json").dumps(STEPS, ensure_ascii=False)}, OBJ)
    for _ in range(step_idx):  # 推进到第 step_idx 步(0-based)
        fc.advance()
    ctx = ContextState()
    ctx.set_user_language("cantonese")
    ctx.set_flow(fc.flow_overview(), fc.current_step_text())
    if with_rag:
        ctx.set_knowledge([{"text": "博克集运提供中美集运、香港自提、运费险理赔服务。" * 4}])
        ctx.set_web(["Wikipedia:集运(parcel forwarding)是把多个包裹合并转运的服务。"])
    for i in range(4):
        ctx.add_summary("user" if i % 2 == 0 else "assistant", f"第{i+1}轮:客户确认咗身份，提到拼多多買。")
    return ctx


def main() -> None:
    print("=== 模拟真实 mid-call 的 system 装配（粤语·4步理赔话术·已到第3步）===")
    for with_rag, label in ((False, "绑话术(默认无RAG)"), (True, "开放咨询(RAG)")):
        ctx = build_ctx(2, with_rag)
        prefix = ctx.render_instruction_prefix()
        tail = ctx.render_context_tail()
        print(f"\n[{label}]")
        print(f"  稳定指令前缀: {len(prefix)} 字")
        print(f"  易变参考尾部: {len(tail)} 字")
        # 模拟 ContextAwareLLM merge: prefix + 人设base(~600字) + tail
        head_len = 600  # _instructions + facts_line 近似
        total = len(prefix) + head_len + len(tail)
        print(f"  人设 base(近似): {head_len} 字")
        print(f"  → 每请求 system 合计 ≈ {total} 字")
    print("\n验收: 绑话术 ≈ ≤2.5K字; 前缀段不含知识/记忆 → 同一步内逐轮字节不变(吃 KV-cache)")


if __name__ == "__main__":
    main()
