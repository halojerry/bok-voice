"""逐段量度实际注入 LLM 的 system 体积（KV-cache 前置的验收工具）。

运行：runtime/python/bin/python scripts/measure_prompt.py   # 带真实 token 计量
      .venv312/bin/python scripts/measure_prompt.py         # 只有字数（无 mlx_lm）

验收目标（模板绑定的粤语理赔 call）：
- 稳定前缀(指令+话术+当前步) + 人设 base ≤ ~2K 字；
- 每请求 ≤ ~2.5K 字（瘦身前 ~3-4.4K）；
- 前缀段逐轮字节不变（知识/记忆只动尾部）→ 吃 mlx_lm KV-cache。

2026-09-22 扩充：**每轮真花钱的是尾块，不是前缀。**
真栈 `LLM_TTFT_MS … cached=N/M` 实测：前缀整场命中（cached 逐轮递增），每轮未命中
≈ 尾块 + 助手回复 + 用户话；TTFT ≈ **465ms 固定头 + 2.21ms × 未命中 token**（近 300 轮
最小二乘；无争用档 402ms + 1.58ms/tok，争用档 795ms + 2.00ms/tok）。所以本工具改为：
① 按【节】拆前缀与尾块（谁大谁小一眼可见）；
② 给真实 token（用 4B 自己的 tokenizer，中文 ≈1.2 字/token）；
③ 按未命中 token 折算尾块的 TTFT 预算；
④ 用**晚通话真实态**（记忆 6 行满额/会中事实/重复锚）而非空尾——空尾会把读数
   系统性压低（321 tok vs 997 tok），是会漏掉真问题的假绿姿势。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "packages" / "core"))
sys.path.insert(0, str(_ROOT / "apps" / "agent"))

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

# 晚通话真实态：记忆行按真实上限（add_summary max_char=200）、事实 4 条、锚 12 字、WA 已捕获
_MEM_LINE = (
    "客户确认咗身份同埋係拼多多買嘅，上個禮拜三已經寄出，物流停咗冇再更新過，"
    "我哋話會幫佢查同埋按平台規則處理賠償，佢問幾時有結果，我哋答今日內一定回覆佢，"
    "佢仲提過想用 WhatsApp 收截圖同埋要專員跟進。"
)
_FACTS = [
    "客户讲：平台係拼多多",
    "客户讲：单号尾号 7890",
    "客户讲：上个礼拜三已经寄出",
    "客户讲：要求用 WhatsApp 联络",
]
_ANCHOR = "你好，請問係咪陳先生本人？我哋係順丰，有個包裹單號尾號……"

# 真栈实测（2026-09-22，近 300 轮 LLM_TTFT_MS 最小二乘）
FIXED_HEAD_MS = 465.0
MS_PER_UNCACHED_TOKEN = 2.21


def build_ctx(
    step_idx: int,
    *,
    rag: bool = False,
    memory_lines: int = 0,
    facts: int = 0,
    wa: str = "",
    anchor: str = "",
    advance_to: int | None = None,
) -> ContextState:
    """装配一个 mid-call 的 ContextState。memory_lines/facts 用来控尾块的真实度。"""
    fc = FlowController.from_template({"steps_json": json.dumps(STEPS, ensure_ascii=False)}, OBJ)
    target = step_idx if advance_to is None else advance_to
    for _ in range(target):  # 推进到第 target 步(0-based)
        fc.advance()
    ctx = ContextState()
    ctx.set_user_language("cantonese")
    ctx.set_flow(fc.flow_overview(), fc.current_step_text())
    if rag:
        ctx.rag_enabled = True
        ctx.set_knowledge([{"text": "博克集运提供中美集运、香港自提、运费险理赔服务。" * 4}])
        ctx.set_web(["Wikipedia:集运(parcel forwarding)是把多个包裹合并转运的服务。"])
    for i in range(memory_lines):
        ctx.add_summary("user" if i % 2 == 0 else "assistant", _MEM_LINE)
    for f in _FACTS[:facts]:
        ctx.add_call_fact(f)
    if wa:
        ctx.set_whatsapp_note(wa)
    if anchor:
        ctx.set_last_reply(anchor)
    return ctx


_CHUNKS: dict[str, str] = {}


def _sections(text: str) -> list[tuple[str, int]]:
    """按【节】切分并计量，返回 [(节名, 字数)]；同时把每节原文留在 _CHUNKS 供逐节量 token。"""
    global _CHUNKS
    _CHUNKS = {}
    out: list[tuple[str, int]] = []
    for chunk in re.split(r"(?=【)", text):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        head = chunk.splitlines()[0]
        name = head.split("】")[0] + "】" if "】" in head else head[:16]
        if name in _CHUNKS:  # 同名重复（多条规则各带【】）→ 合并计量
            _CHUNKS[name] += "\n" + chunk
            out = [(n, c + len(chunk) + 1) if n == name else (n, c) for n, c in out]
            continue
        _CHUNKS[name] = chunk
        out.append((name, len(chunk)))
    return out


def _tokenizer():
    """4B 自己的 tokenizer（只读 tokenizer 文件，不载权重）。缺 mlx_lm 时返回 None。"""
    try:
        sys.path.insert(0, str(_ROOT / "tools"))
        import bok  # noqa: PLC0415
        from mlx_lm.utils import load_tokenizer  # noqa: PLC0415

        path = bok.model_path(bok.MODELS[bok.platform_key()], "llm")
        return load_tokenizer(Path(path))
    except Exception as exc:  # noqa: BLE001 - 缺依赖不该让量度工具挂掉
        print(f"（token 计量不可用，只有字数：{type(exc).__name__}）")
        return None


def _measure(tok, text: str) -> str:
    return f"{len(text):>5} 字" + (f" ≈ {len(tok.encode(text)):>4} tok" if tok else "")


def main() -> None:
    tok = _tokenizer()
    print("=== 模拟真实 mid-call 的 system 装配（粤语·4步理赔话术·已到第3步）===")

    empty = build_ctx(2)
    late = build_ctx(2, memory_lines=6, facts=4, wa="6432543", anchor=_ANCHOR)
    p_late = late.render_instruction_prefix()
    t_empty, t_late = empty.render_context_tail(), late.render_context_tail()

    print("\n[前缀整场静态（逐轮 cached，不花每轮时间）]")
    for name, _chars in _sections(p_late):
        print(f"  {name:<34} {_measure(tok, _CHUNKS[name])}")
    print(f"  {'合计':<34} {_measure(tok, p_late)}")

    print("\n[尾块每轮重拼（每轮真花钱）]　空尾 vs 晚通话真实态")
    print(f"  空尾（旧口径，会低估）                {_measure(tok, t_empty)}")
    print(f"  晚通话真实态（记忆6行+事实4条+WA+锚） {_measure(tok, t_late)}")

    print("\n[晚通话尾块的分节增量]")
    base = build_ctx(2).render_context_tail()
    variants = [
        ("【现在这一步】/【·继续】", build_ctx(2, advance_to=2).render_context_tail()),
        ("【本通对话记忆】6 行满额", build_ctx(2, memory_lines=6).render_context_tail()),
        ("【通话中客户已讲】4 条", build_ctx(2, facts=4).render_context_tail()),
        ("【已记录客户 WhatsApp】", build_ctx(2, wa="6432543").render_context_tail()),
        ("【你上一句】锚(12字头)", build_ctx(2, anchor=_ANCHOR).render_context_tail()),
    ]
    for name, text in variants:
        delta = len(text) - len(base)
        if delta <= 0:
            continue
        print(f"  {name:<34} +{delta:>4} 字 ≈ +{round(delta / 1.2):>4} tok")
    print(f"  {'（节头和≈合计，误差来自节头换行）':<34} 合计 {_measure(tok, t_late)}")

    print("\n[每轮新 token 与 TTFT 预算]")
    tail_tok = len(tok.encode(t_late)) if tok else round(len(t_late) / 1.2)
    reply_tok, user_tok = 40, 15  # 实测 gen=13-59；用户话通常 5-15 字
    per_turn = tail_tok + reply_tok + user_tok
    print(f"  尾块 ≈{tail_tok} tok + 助手回复 ≈{reply_tok} + 用户话 ≈{user_tok} = ≈{per_turn} tok/轮")
    print(
        f"  按真栈回归（{FIXED_HEAD_MS:.0f}ms + {MS_PER_UNCACHED_TOKEN:.2f}ms/token）"
        f"→ **TTFT ≈ {FIXED_HEAD_MS + per_turn * MS_PER_UNCACHED_TOKEN:.0f}ms**"
    )
    print(
        "\n结论：前缀整场 cached（免费）；**每轮 TTFT ≈ 固定头 + 尾块 prefill**。"
        "记忆行是本通内单调增长的那一项 → 通话越到后面越慢。"
    )
    print("验收: 绑话术 ≈ ≤2.5K字; 前缀段不含知识/记忆 → 同一步内逐轮字节不变(吃 KV-cache)")


if __name__ == "__main__":
    main()
