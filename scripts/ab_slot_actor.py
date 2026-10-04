#!/usr/bin/env python3
"""D1 槽位化 actor 四臂 A/B 台架（2026-10-01，第一性原理重构计划 §三 D1）。

四臂（env 差异是唯一自变量；其余不动）：
  ARM1 现状：9B + 全剧本 prompt（system 前缀+两段式尾巴）+ 全守卫。
  ARM2 槽位 prompt + 守卫关：BOK_SLOT_ACTOR=1 BOK_REPEAT_GUARD=0
       BOK_REPEAT_CROSS_TURN=0 BOK_STALL_LADDER=0。
  ARM3 全剧本 + 守卫关：只关守卫、无 SLOT（测 9B 残余病理——复读/stall 是
       模型缺陷还是守卫喂出来的复制素材）。
  ARM4 槽位 prompt + 守卫关 + 云端 LLM：a_reply 车道经 CP API PUT 到 DeepSeek
       （凭据=环境 DeepSeek 键；跑完还原原值）。

三模式：
  --offline  零栈依赖：真模板 fixture（scripts/migrate_templates_8step_0913.py
             的 febeeeebac97）模拟 20 轮通话，产出两形态请求尺寸表——
             ARM1 形状用**现渲染函数**（flow.FlowController + ContextState
             前缀/两段式尾巴，W-A 代码不在也能跑），槽位形状用 stub 渲染器
             （W-A 真实现落地前的占位；契约=system 角色卡 ~400c + 任务块
             100-250c + 纯历史，编排职责全撤出 prompt）。
             输出 per-round chars/token 估算（CJK≈1tok/字校准）与累计
             uncached prefill 估算（311 tok/s=M4 Pro 9B prefill 天花板，
             计划 §七 bench_9b_direct 定案换算秒数）。
  --direct   只打 :1237（+可选 DeepSeek），不重启栈：从 offline 语料抽 8-10 个
             代表性轮（身份质疑/平台反问/赔偿追问/碎片转写/WA 报号/跳步后首轮），
             两形态各发一遍，量 TTFT/tps/回包长度 + 回包 sanity（非空/粤语轮
             粤语作答/与该步正稿相似度≥0.9=念稿病理计数）。
             模型字段从 DB global_settings.model_routing_json 读真值并解析成
             绝对路径——mlx_lm server 短 id 触发 HF 在线下载（计划 §七 陷阱）。
  --live     逐臂 down→带 env serve→等健康→跑 FLOW20+soak→汇总 JSON。
             **本波只实现不执行**；执行前安全闸：call_sessions 有
             active/ringing/paused 即拒绝（不杀在途通话）；全部臂跑完恢复原栈。
             `--live-dry-run` 只打印执行清单（主线择窗用）。

输出：reports/ab-slot-actor/（JSON + markdown 摘要）。

用法（三行）：
  .venv312/bin/python scripts/ab_slot_actor.py --offline
  .venv312/bin/python scripts/ab_slot_actor.py --direct            # 需 :1237 在场
  .venv312/bin/python scripts/ab_slot_actor.py --live --live-dry-run   # 择窗执行清单
"""
from __future__ import annotations
# --- scripts import bootstrap (G1) ---
# sys.path 引导(G1 迁移解耦,见 docs/superpowers/plans/2026-10-04-repo-governance-plan.md §3.1):
# 同层时是 no-op;文件挪进任何桶后裸 import 兄弟模块继续解析。
import sys as _sys, pathlib as _pathlib
_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))


import argparse
import difflib
import importlib.util
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
# 仓库内纯渲染依赖（flow/livekit_plugins）在函数内懒加载：纯逻辑函数（臂 env/
# 聚合/尺寸计算）不依赖它们，单测可全 mock 不碰栈。
for _p in ("apps/agent", "packages/core"):
    _sp = str(ROOT / _p)
    if _sp not in sys.path:
        sys.path.insert(0, _sp)

DEFAULT_OUT_DIR = ROOT / "reports" / "ab-slot-actor"
TEMPLATE_ID = "febeeeebac97"  # 真模板 fixture：理赔·分步（粤语示例），8 步
# M4 Pro 9B prefill 天花板（计划 §七：bench_9b_direct 同栈实测 5888tok=18.9s）。
MODEL_PREFILL_TOK_S = 311.0
DIRECT_MAX_TOKENS = 160

# ---------------------------------------------------------------------------
# 四臂定义（env 单点表）
# ---------------------------------------------------------------------------
ARM1, ARM2, ARM3, ARM4 = "ARM1", "ARM2", "ARM3", "ARM4"
ARMS: tuple[str, ...] = (ARM1, ARM2, ARM3, ARM4)

# 守卫族关档：复读三代（BOK_REPEAT_GUARD=自我复读/BOK_REPEAT_CROSS_TURN=跨轮）
# + stall 阶梯（degrade/bypass/close）。其余 env 一律不动（铁律：差异只有自变量）。
GUARD_OFF_ENV: dict[str, str] = {
    "BOK_REPEAT_GUARD": "0",
    "BOK_REPEAT_CROSS_TURN": "0",
    "BOK_STALL_LADDER": "0",
}
# W-A 并行实现的槽位渲染核心总闸（env 名已锁死：默认 "0"）。
SLOT_ENV: dict[str, str] = {"BOK_SLOT_ACTOR": "1"}

ARM_SPECS: dict[str, dict] = {
    ARM1: {
        "label": "现状：9B + 全剧本 + 全守卫",
        "shape": "full_script",
        "env": {},
        "guards": True,
        "llm": "local",
    },
    ARM2: {
        "label": "槽位 prompt + 守卫关（9B）",
        "shape": "slot",
        "env": {**SLOT_ENV, **GUARD_OFF_ENV},
        "guards": False,
        "llm": "local",
    },
    ARM3: {
        "label": "全剧本 + 守卫关（9B，测残余病理）",
        "shape": "full_script",
        "env": dict(GUARD_OFF_ENV),
        "guards": False,
        "llm": "local",
    },
    ARM4: {
        "label": "槽位 prompt + 守卫关 + 云端（DeepSeek）",
        "shape": "slot",
        "env": {**SLOT_ENV, **GUARD_OFF_ENV},
        "guards": False,
        "llm": "cloud",
    },
}


def arm_env(arm: str) -> dict[str, str]:
    """臂 env 覆盖表（子进程/启动注入用）。ARM1=空=零漂移。"""
    return dict(ARM_SPECS[arm]["env"])


def arm_shape(arm: str) -> str:
    return str(ARM_SPECS[arm]["shape"])


# ---------------------------------------------------------------------------
# token 估算与请求尺寸（纯函数，单测直测）
# ---------------------------------------------------------------------------
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def est_tokens(text: str) -> int:
    """CJK≈1 tok/字 校准；拉丁/数字/标点按 ~4 字符/token 折算。

    系数 0.93/4.0 由本波 direct 实测校准（18 请求对照 mlx 服务端 usage.
    prompt_tokens：校准前 ARM1 高估 16.8%/槽位高估 4.7%；校准后两形态残差
    ±6% 内——Qwen tokenizer 常见中文双字合并 + ASCII run 合并，单一线性式
    无法同时精确命中两形态，报告里附实测 est/real 比值）。
    """
    s = str(text or "")
    if not s:
        return 0
    cjk = len(_CJK_RE.findall(s))
    rest = len(s) - cjk
    return int(round(cjk * 0.93 + rest / 4.0))


def serialize_messages(messages: list[dict]) -> str:
    """确定性序列化（跨轮前缀比较用）：逐条 role+content 拼接。"""
    return "\n".join(f"<{m.get('role', '')}>{m.get('content', '')}" for m in messages or [])


def request_size_row(messages: list[dict]) -> dict:
    text = serialize_messages(messages)
    return {"n_messages": len(messages or []), "chars": len(text), "est_tokens": est_tokens(text)}


def longest_common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def uncached_est(prev_text: str, cur_text: str) -> dict:
    """上一请求→本请求的 uncached 增量估算（KV 前缀复用模型）。

    mlx LRU prompt cache 只复用公共前缀（严格前缀轮=公共前缀就是上一请求全文）；
    历史摊销截断/系统改写会令公共前缀变短 → reanchor=True（本请求按公共前缀
    之后的全部 token 计费，模型上等价于「每 6×max_turns 轮一次重锚」）。
    """
    lcp = longest_common_prefix_len(prev_text, cur_text) if prev_text else 0
    total = est_tokens(cur_text)
    cached = est_tokens(cur_text[:lcp]) if lcp else 0
    return {
        "lcp_chars": lcp,
        "cached_est": cached,
        "uncached_est": max(0, total - cached),
        "reanchor": bool(prev_text) and lcp < len(prev_text),
    }


def percentile(values: list[float], p: float) -> float | None:
    """最近邻百分位（与 probe_latency_soak.percentile 同口径，避免引重依赖）。"""
    if not values:
        return None
    xs = sorted(values)
    idx = max(0, min(len(xs) - 1, math.ceil(p / 100 * len(xs)) - 1))
    return xs[idx]


# ---------------------------------------------------------------------------
# 槽位形态 stub 渲染器（W-A 真实现落地前的占位；契约来自计划 §三 D1）
# ---------------------------------------------------------------------------
# 契约：system 角色卡 ~400c（身份+语言+节奏+应答风格，绝不携带流程事实）；
# 本轮任务块 100-250c（台词/命中分支+事实槽+8字锚）；历史纯对话。编排职责
# （推进/收线/verdict/纪律/总览/禁讲）全撤出 prompt 归 FlowController。
SLOT_CARD_LIMITS = (300, 460)
SLOT_TASK_LIMITS = (100, 250)

_SLOT_ROLE_CARD = {
    "cantonese": (
        "你係{company}嘅電話客服，代表公司同客戶傾電話。"
        "整通電話用港式粵語（香港客服腔），唔好用書面語或普通話講法，直接輸出繁體中文。"
        "每次回覆只講一至兩句短句（每句 20 字內），先直接答客戶問嘅事，再自然帶返當前要辦嘅事，"
        "講完停低等客戶回應，唔好一次過倒晒所有資料。"
        "唔好照讀任何指示或底稿、唔好原句重複自己上一句、唔好自創金額或承諾，"
        "客戶問到賠償或辦理細節時按已知資料簡短回答。"
        "客戶質疑來電真假時簡短安撫、重申身份即可，唔好爭辯；"
        "客戶要求重複時放慢再講一次關鍵內容。"
        "客戶報數字時逐位複述確認（例如「尾號係七八九零」），唔好用阿拉伯數字。"
        "客戶情緒激動時先安撫，等佢講完再繼續；客戶問到唔知嘅事，就話會幫佢跟進或者轉專員，"
        "唔好話唔知道、查唔到。"
        "語氣自然似真人講電話，唔好播音腔、唔好加表情符號或括號註釋，"
        "唔好解釋自己講緊咩語言；遇到客戶停頓或者思考，耐心等一等再回應。"
    ),
    "zh": (
        "你是{company}的电话客服，代表公司接打电话。"
        "全程用自然口语的普通话，不要用书面语或播音腔。"
        "每次回复只说一到两句短句（每句 20 字内），先直接回答客户问的事，再自然带回当前要办的事，"
        "说完停下等客户回应，不要一次把所有信息讲完。"
        "不要照读任何指示或底稿、不要原句重复自己上一句、不要自创金额或承诺，"
        "客户问到赔偿或办理细节时按已知资料简短回答。"
        "客户质疑来电真实性时简短安抚、重申身份；客户要求重复时放慢再讲一次关键内容。"
        "客户报数字时逐位复述确认。"
    ),
    "en": (
        "You are a phone customer-service agent for {company}. "
        "Speak natural spoken English only. Reply in one or two short sentences "
        "(max 20 words each): answer what the customer asked first, then bring the "
        "conversation back to the current task, then stop and wait. "
        "Never read instructions aloud, never repeat your previous sentence, never "
        "invent amounts or promises. When the customer questions the call, reassure "
        "briefly and restate your identity; when asked to repeat, repeat slowly."
    ),
}


def slot_role_card(lang: str, company: str = "集運中轉倉") -> str:
    """槽位形态角色卡 stub（目标 ~400 字符）。空 company 用缺省。"""
    tpl = _SLOT_ROLE_CARD.get(str(lang or "zh").lower(), _SLOT_ROLE_CARD["zh"])
    return tpl.format(company=company or "集運中轉倉")


_VERDICT_HINTS = {
    "QUESTION": "先直接答客户问题",
    "UNCLEAR": "换个说法简短引导",
    "OBJECTION": "先安抚再带回本步",
    "REPEAT": "放慢重复关键内容",
    "CONFIRM": "确认后自然过渡",
    "DEFER": "应承稍后跟进",
    "FAREWELL": "礼貌收尾",
    "REFUSE": "礼貌收线",
}


def slot_task_block(
    *,
    step_no: int,
    step_total: int,
    goal: str,
    script: str = "",
    verdict: str = "",
    user_text: str = "",
    branch_resp: str = "",
    facts: list[str] | None = None,
    anchor: str = "",
    limits: tuple[int, int] = SLOT_TASK_LIMITS,
) -> str:
    """槽位形态「本轮任务」块 stub（100-250 字符，确定性）。

    内容=本步目标+台词（正稿首行）+命中分支应答+事实槽（≤2 条）+verdict 提示
    +客户原话截 20 字+8 字锚；超上限硬截断（确定性），不足下限补一句纪律到
    下限之上。**不含任何编排职责**（推进/收线/纪律/总览由 FlowController 管）。
    """
    lo, hi = limits
    parts: list[str] = [f"【本轮任务·第{step_no}/{step_total}步】{str(goal or '').strip()}"]
    script = str(script or "").strip()
    if script:
        parts.append("台词：" + script)
    branch_resp = str(branch_resp or "").strip()
    if branch_resp:
        parts.append("客户这样问：" + branch_resp)
    for f in (facts or [])[:2]:
        f = str(f or "").strip()
        if f:
            parts.append("已知：" + f)
    hint = _VERDICT_HINTS.get(str(verdict or "").upper(), "")
    if hint:
        parts.append("回应类型：" + hint)
    user_text = str(user_text or "").strip()
    if user_text:
        parts.append("客户刚说：" + user_text[:20])
    anchor = str(anchor or "").strip()
    if anchor:
        parts.append("你上一句开头：「" + anchor[:8] + "」")
    text = "；".join(p for p in parts if p)
    if len(text) > hi:
        text = text[: hi - 1] + "…"
    if len(text) < lo:
        # 不足下限（任务块过薄）时循环补纪律句直到 ≥lo（确定性，段表轮转）——
        # 保证两形态对照里槽位块下界可达（契约 100-250c）。
        pad = (
            "。要求：先答客户问嘅事，再自然带回本步，口语短句。",
            "如有需要只问一个问题，讲完停低等客户回应。",
        )
        k = 0
        while len(text) < lo and k < 8:
            text += pad[k % len(pad)]
            k += 1
        if len(text) > hi:
            text = text[: hi - 1] + "…"
    return text


def slot_stub_messages(
    *,
    lang: str,
    company: str,
    history: list[dict],
    user_text: str,
    task_block: str,
) -> list[dict]:
    """槽位形态请求组装 stub：角色卡 system + 纯历史（+冻结旧任务块）+ 本轮。

    KV 契约与现架构同款（append-only 冻结）：历史 user 消息带**当时**的任务块
    （见 simulate_call 的冻结纪律），本轮的块由 task_block 提供。
    """
    system = slot_role_card(lang, company)
    msgs: list[dict] = [{"role": "system", "content": system}]
    for h in history:
        msgs.append({"role": h["role"], "content": h["content"]})
    body = user_text if not task_block else f"{user_text}\n\n{task_block}"
    msgs.append({"role": "user", "content": body})
    return msgs


# ---------------------------------------------------------------------------
# 20 轮剧本（真模板 febeeeebac97 的粤语全流程 + 三类压力轮）
# ---------------------------------------------------------------------------
OFFLINE_ROUNDS: list[dict] = [
    {"text": "你好", "note": "开场/身份步"},
    {"text": "我係陳大文", "note": "身份确认→平台步"},
    {"text": "點解要問我邊個平台買嘅？", "tag": "platform_question", "note": "平台反问（离稿）"},
    {"text": "拼多多買嘅", "note": "平台答→赔偿直念步"},
    {"text": "可以點樣賠啊", "note": "赔偿步首轮"},
    {"text": "咁你即係賠幾多錢", "tag": "compensation_followup", "note": "金额追问"},
    {"text": "你哋係咪呃人？點解有我電話？", "tag": "identity_challenge", "note": "身份质疑"},
    {"text": "好啦我接受", "note": "接受→办理收号步"},
    {
        "text": "我唔想等咁耐，你哋直接同我搞掂佢",
        "tag": "post_jump",
        "jump_to": 7,  # 0-based：跳第 8 步（跳过 6、7 步）——模拟话术图 jump 命中
        "note": "跳步（直达收线步）后首轮",
    },
    {"text": "我個WhatsApp係六四三二二二三三", "tag": "wa_number", "note": "WA 报号（直捕罐头确认）"},
    {"text": "你會點跟我跟進", "note": "跟进问"},
    {"text": "唔好意思頭先冇聽清，你講多次點賠", "tag": "repeat_request", "note": "显式重问"},
    {"text": "呃…咁樣…我…", "tag": "fragmented", "note": "碎片转写（ASR 低置信）"},
    {"text": "會唔會有短信通知我", "note": "短信问"},
    {"text": "咁快啲幫我跟啦", "tag": "r15", "note": "催办（R15 对照位）"},
    {"text": "我報返個號碼，六五三二二一八八", "note": "补报号"},
    {"text": "唔該晒你", "note": "感谢"},
    {"text": "你哋服務幾好", "note": "闲聊"},
    {"text": "冇嘢問啦", "note": "收线前"},
    {"text": "好，拜拜", "note": "告别→收线"},
]

# --direct 代表轮（tag → 必含）：身份质疑/平台反问/赔偿追问/碎片转写/WA 报号/
# 跳步后首轮 + 早期轮/重问轮补足 8-10 个。
DIRECT_WANTED_TAGS: tuple[str, ...] = (
    "platform_question",
    "compensation_followup",
    "identity_challenge",
    "post_jump",
    "wa_number",
    "repeat_request",
    "fragmented",
    "r15",
)

# fixture 人设/对象（离线确定性；真模板来自 migrate 脚本，人物卡用夹具并标注）。
FIXTURE_PERSONA = {
    "name": "小九",
    "company": "集運中轉倉",
    "tone": "禮貌專業",
    "language": "cantonese",
}
FIXTURE_OBJECT = {
    "display_name": "陳大文",
    "language": "cantonese",
    "tracking_no": "SF1234567890",
    "contact_channel": "WhatsApp",
    "role_template": "客户",
}
FIXTURE_OBJECT_BRIEF = "陳大文，香港客戶，貨件 SF1234567890 遺失，等待賠償跟進。"


class _Msg(dict):
    """dict + .role 属性——复用 livekit_plugins._truncate_chat_items 的 getattr。"""

    @property
    def role(self) -> str:
        return str(self.get("role") or "")


def load_template_fixture(template_id: str = TEMPLATE_ID) -> dict:
    """从迁移脚本导入真模板 fixture（不执行其 main；只读模块级 TEMPLATES）。"""
    spec = importlib.util.spec_from_file_location(
        "_ab_slot_migrate_templates", SCRIPTS / "migrate_templates_8step_0913.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - 文件在场恒真
        raise RuntimeError("migrate_templates_8step_0913.py 无法加载")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    spec_dict = mod.TEMPLATES[template_id]
    return {
        "id": template_id,
        "language": "cantonese",
        "steps_json": json.dumps(spec_dict["steps"], ensure_ascii=False),
        "hotwords": spec_dict.get("hotwords", ""),
    }


def _load_renderers():
    """仓库内渲染依赖（非栈依赖）：现渲染函数 + canned 台词助手。

    只 import 纯函数/类，不建连接、不起进程——offline 模式零栈依赖的含义是
    「不需要 serve」，不是「不需要仓库代码」。
    """
    from agent_runtime import flow as flow_mod
    from agent_runtime.agent import _instructions, _say_step_cap, _wa_number_line
    from agent_runtime.providers import livekit_plugins as lp

    return flow_mod, lp, _instructions, _say_step_cap, _wa_number_line


def simulate_call(
    *,
    template: dict,
    rounds: list[dict],
    persona: dict | None = None,
    object_card: dict | None = None,
    object_brief: str = "",
    lang: str = "cantonese",
    history_turns: int = 6,
    memory_chars: int = 180,
) -> dict:
    """模拟 20 轮通话，逐轮产出两形态请求（ARM1 形状 / 槽位形状）。

    忠实点（按 agent.py 轮钩子顺序复刻）：
      1. rule_verdict + last_user_text/last_digits；
      2. 话术图跳转（剧本 jump_to 模拟 graph 命中，副作用包=同款 set_flow_current）；
      3. WA 探测（推进前跑，与 agent 同序）；
      4. 规则推进（should_auto_advance → advance；CONFIRM 走 WA 闸）；
      5. WA 直捕罐头确认（StopResponse 语义：本轮无 LLM、不推进）；
      6. say 直念快路（note_step_said + set_flow_current，本轮无 LLM）；
      7. LLM 轮：render_context_tail() 冻进当轮 user 消息（append-only），
         下轮历史原样重放——与 ContextAwareLLM 的账本纪律逐字节同构。
    近似点（报告 assumptions 里标注）：
      - 模拟回复=本步正稿首句截 40 字（真回复长度由模型定，历史里影响小）；
      - 碎片轮按 LLM 轮计（真栈可能落 garbled-reask 罐头——保守取 LLM）；
      - 槽位形态任务块按「冻进历史」建模（KV 严格前缀契约的必要条件）。
    """
    flow_mod, lp, _instructions, _say_step_cap, _wa_number_line = _load_renderers()
    persona = dict(persona or FIXTURE_PERSONA)
    object_card = dict(object_card or FIXTURE_OBJECT)

    fc = flow_mod.FlowController.from_template(template, object_card)
    ctx = lp.ContextState(account_id="acc-001", max_summary_chars=memory_chars)
    ctx.set_user_language(lang)
    if object_brief:
        ctx.set_object_brief(object_brief)
    ctx.set_flow(fc.flow_overview(), fc.current_step_text())
    fc.opening_played = True
    opening = fc.opening_text()

    prefix = ctx.render_instruction_prefix()
    head = _instructions(persona=persona, object_card=object_card, template=template)
    arm1_system = f"{prefix}\n\n{head}" if head else prefix
    slot_system = slot_role_card(lang, str(persona.get("company") or ""))

    company = str(persona.get("company") or "")
    arm1_history: list[dict] = [_Msg(role="assistant", content=opening)] if opening else []
    slot_history: list[dict] = [_Msg(role="assistant", content=opening)] if opening else []
    call_facts: list[str] = []
    wa_captured = False
    prev_req = {"ARM1": "", "slot": ""}
    rows: list[dict] = []

    def _truncate(items: list[dict]) -> list[dict]:
        # 复用真截断函数（滞回：>6×max_turns 条才剪回 2×max_turns 条）。
        return lp._truncate_chat_items([_Msg(**dict(it)) for it in items], max_turns=history_turns)

    for idx, r in enumerate(rounds, start=1):
        text = str(r.get("text") or "")
        verdict = fc.rule_verdict(text)
        fc.last_verdict = verdict
        fc.last_user_text = text
        fc.last_digits = flow_mod._digit_runs_in(text)
        say_pending_before = fc.pending_say_text() != ""
        step_at = fc.current + 1 if fc.has_steps else 0

        # 1) 话术图跳转（离线代步：真栈由 graph intent 触发；副作用包同款）。
        jumped = False
        if r.get("jump_to") is not None and not fc.closing:
            before = fc.current
            fc.jump_to(int(r["jump_to"]))
            if fc.current != before:
                jumped = True
            ctx.set_flow_current(fc.current_step_text())

        # 2) WA 探测（推进前跑——agent 同序，step context 还是旧步）。
        goal0, ref0 = fc.current_goal_ref()
        wa_sig = flow_mod.detect_whatsapp_signal(
            text,
            step_goal=goal0,
            step_ref=ref0,
            facts=fc.vars_map,
            already_captured=wa_captured,
        )

        # 3) 规则推进。
        if not fc.done and not fc.closing and not say_pending_before:
            _g, _r = fc.current_goal_ref()
            say_step = bool(
                fc.has_steps
                and 0 <= fc.current < len(fc.steps)
                and fc.steps[fc.current].say
                and (
                    "通知" in (fc.steps[fc.current].goal or "")
                    or "notice" in (fc.steps[fc.current].goal or "").lower()
                )
            )
            _auto = flow_mod.should_auto_advance(
                current=fc.current,
                goal=_g,
                ref=_r,
                user_text=text,
                verdict=verdict,
                wa=(wa_sig[0] if wa_sig else None),
                say_step=say_step,
            )
            if _auto:
                fc.advance()
            elif verdict == flow_mod.CONFIRM:
                _wa_satisfied = (
                    wa_sig is not None and wa_sig[0] in ("captured", "captured_implicit")
                ) or wa_captured
                if not (wa_sig is not None and wa_sig[0] == "offered") and flow_mod.wa_confirm_advance_allowed(
                    goal=_g, ref=_r, captured=_wa_satisfied
                ):
                    fc.advance()
            ctx.set_flow_current(fc.current_step_text())

        # 4) WA 捕获记账（agent 侧：set_whatsapp_note + canned 确认）。
        wa_captured_now = False
        wa_num = ""
        if wa_sig and wa_sig[0] in ("captured", "captured_implicit"):
            wa_captured = True
            wa_num = str(wa_sig[1] or "")
            if wa_num:
                ctx.set_whatsapp_note(wa_num)
                if wa_sig[0] == "captured":
                    wa_captured_now = True
        # 会中事实沉淀（extract_fact_updates 单源）。
        try:
            for fact, needle in flow_mod.extract_fact_updates(text, facts=fc.vars_map, enabled=True):
                if needle:
                    ctx.supersede_call_fact(needle, fact)
                else:
                    ctx.add_call_fact(fact)
                if fact and fact not in call_facts:
                    call_facts.append(fact)
                    if len(call_facts) > 4:
                        call_facts.pop(0)
        except Exception:  # noqa: BLE001 - 沉淀失败不影响尺寸表
            pass

        # 5) 车道判定：WA 直捕罐头 → say 直念 → LLM。
        lane = "llm"
        reply = ""
        step_obj = fc.steps[fc.current] if fc.has_steps and not fc.done else None
        parts = flow_mod.parse_step_ref(step_obj.ref) if step_obj else None
        script_line = ""
        if step_obj:
            rendered = flow_mod.render_template_text(parts.script or step_obj.goal, fc.vars_map)
            for ln in rendered.splitlines():
                ln = ln.strip()
                if ln and not re.search(r"\{[^{}]+\}", ln):
                    script_line = ln
                    break

        if wa_captured_now:
            lane = "wa-confirm"
            reply = _wa_number_line(lang, wa_num)
        else:
            say_now = _say_step_cap(fc.pending_say_text())
            if say_now:
                fc.note_step_said()
                ctx.set_flow_current(fc.current_step_text())
                lane = "say"
                reply = say_now

        # 6) 两形态本轮 user 消息（LLM 轮=冻结尾部/任务块；脚本轮=裸体 EX-1）。
        arm1_extra = ""
        slot_task = ""
        if lane == "llm":
            tail = ctx.render_context_tail()
            arm1_extra = tail
            branch_resp = ""
            if step_obj and parts is not None:
                m = flow_mod.match_step_branch(parts, text, verdict)
                if m:
                    # 分支应答渲染变量（{聯絡方式} 等），截 40 字——槽位块只放能用的。
                    branch_resp = flow_mod.render_template_text(str(m[1]), fc.vars_map)[:40]
            slot_task = slot_task_block(
                step_no=fc.current + 1 if fc.has_steps else 0,
                step_total=len(fc.steps),
                goal=flow_mod.render_template_text(step_obj.goal, fc.vars_map) if step_obj else "",
                script=script_line,
                verdict=verdict,
                user_text=text,
                branch_resp=branch_resp,
                facts=call_facts,
                anchor=ctx.last_reply[:8],
            )

        arm1_content = f"{text}\n\n{arm1_extra}" if arm1_extra else text
        slot_content = f"{text}\n\n{slot_task}" if slot_task else text

        # 7) 请求组装（system + 截断历史 + 本轮；两种形态各自序列化）。
        arm1_items = _truncate(
            [_Msg(role="system", content=arm1_system)] + list(arm1_history) + [_Msg(role="user", content=arm1_content)]
        )
        slot_items = _truncate(
            [_Msg(role="system", content=slot_system)] + list(slot_history) + [_Msg(role="user", content=slot_content)]
        )
        arm1_msgs = [{"role": it["role"], "content": it["content"]} for it in arm1_items]
        slot_msgs = [{"role": it["role"], "content": it["content"]} for it in slot_items]

        row: dict = {
            "round": idx,
            "text": text,
            "note": str(r.get("note") or ""),
            "tag": str(r.get("tag") or ""),
            "lane": lane,
            "step": (fc.current + 1) if fc.has_steps else 0,
            "step_total": len(fc.steps),
            "verdict": verdict,
            "jumped": jumped,
            "wa_captured": wa_captured,
            "shapes": {},
        }

        if lane == "llm":
            for shape, msgs, prev_key in (("ARM1", arm1_msgs, "ARM1"), ("slot", slot_msgs, "slot")):
                size = request_size_row(msgs)
                cur_text = serialize_messages(msgs)
                unc = uncached_est(prev_req[prev_key], cur_text)
                prev_req[prev_key] = cur_text
                row["shapes"][shape] = {
                    **size,
                    **unc,
                    "extra_chars": len(arm1_extra if shape == "ARM1" else slot_task),
                    "uncached_seconds": round(unc["uncached_est"] / MODEL_PREFILL_TOK_S, 3),
                    "messages": msgs,
                }
        else:
            # 脚本轮：真栈无 LLM 请求（本轮零请求成本），历史照常追加。
            for shape in ("ARM1", "slot"):
                row["shapes"][shape] = {
                    "n_messages": 0,
                    "chars": 0,
                    "est_tokens": 0,
                    "uncached_est": 0,
                    "uncached_seconds": 0.0,
                    "lcp_chars": 0,
                    "cached_est": 0,
                    "reanchor": False,
                    "extra_chars": 0,
                    "messages": [],
                    "skipped_no_llm": True,
                }

        # 8) 历史推进（冻结尾部/任务块；脚本轮裸体）。
        arm1_history.append(_Msg(role="user", content=arm1_content))
        slot_history.append(_Msg(role="user", content=slot_content))
        if lane == "llm":
            # 模拟回复：本步正稿首句截 40 字（近似，见 docstring）。
            sim_reply = script_line[:40] or ("好嘅，收到。" if lang == "cantonese" else "好的，收到。")
            if fc.closing:
                sim_reply = "好嘅，唔該晒你今日嘅時間，再見！" if lang == "cantonese" else "好的，谢谢您，再见！"
            reply = sim_reply
        arm1_history.append(_Msg(role="assistant", content=reply))
        slot_history.append(_Msg(role="assistant", content=reply))
        if reply:
            ctx.set_last_reply(reply)
            ctx.add_summary("客服", reply)
        if text:
            ctx.add_summary("客户", text)

        rows.append(row)

    return {
        "template": {"id": template.get("id"), "steps": len(fc.steps)},
        "system_chars": {"ARM1": len(arm1_system), "slot": len(slot_system)},
        "rows": rows,
        "opening": opening,
    }


# ---------------------------------------------------------------------------
# offline 聚合（纯函数，单测直测）
# ---------------------------------------------------------------------------
def _row_shape(row: dict, shape: str) -> dict:
    return (row.get("shapes") or {}).get(shape) or {}


def aggregate_offline(rows: list[dict]) -> dict:
    """两形态汇总：R2/R15 对照位 + 累计 uncached tok/秒 + 峰值请求。"""
    out: dict = {}
    for shape in ("ARM1", "slot"):
        llm_rows = [r for r in rows if r.get("lane") == "llm"]
        req_tokens = [int(_row_shape(r, shape).get("est_tokens") or 0) for r in llm_rows]
        uncached = [int(_row_shape(r, shape).get("uncached_est") or 0) for r in llm_rows]
        total_uncached = sum(uncached)
        peak = max(req_tokens) if req_tokens else 0
        peak_row = llm_rows[req_tokens.index(peak)]["round"] if req_tokens else None
        out[shape] = {
            "n_llm_rounds": len(llm_rows),
            "n_script_rounds": len(rows) - len(llm_rows),
            "total_uncached_tokens": total_uncached,
            "total_uncached_seconds": round(total_uncached / MODEL_PREFILL_TOK_S, 2),
            "peak_request_tokens": peak,
            "peak_request_seconds": round(peak / MODEL_PREFILL_TOK_S, 2),
            "peak_request_round": peak_row,
            "median_request_tokens": percentile([float(v) for v in req_tokens], 50),
            "median_uncached_tokens": percentile([float(v) for v in uncached], 50),
        }
    return out


def pick_round(rows: list[dict], n: int) -> dict | None:
    """取第 n 轮行（1-based）；越界回 None（报告用 R2/R15 对照）。"""
    for r in rows:
        if int(r.get("round") or 0) == int(n):
            return r
    return None


def pick_representative_rounds(rows: list[dict], wanted_tags: tuple[str, ...], limit: int = 9) -> list[dict]:
    """按 tag 确定性抽取代表轮（tag 顺序=wanted_tags 顺序），不足时按轮序补。"""
    picked: list[dict] = []
    for tag in wanted_tags:
        for r in rows:
            if r.get("tag") == tag and r.get("lane") == "llm" and r not in picked:
                picked.append(r)
                break
        if len(picked) >= limit:
            break
    if len(picked) < limit:
        for r in rows:
            if r.get("lane") == "llm" and r not in picked:
                picked.append(r)
            if len(picked) >= limit:
                break
    return picked[:limit]


# ---------------------------------------------------------------------------
# 回包 sanity（纯函数，单测直测）
# ---------------------------------------------------------------------------
# 强粤语特征字（在普通话文本里结构性缺席——唔/嘅/哋/咗/喺/冇/嚟/啲/㗎/喇/嘢/佢）。
# 阈值 ≥1：短回复（「好嘅，收到。」）只带 1-2 个特征字，≥2 会假阴（direct 首跑
# R12 ARM1 实弹：真粤语回复被记粤语=False）。
_CANTONESE_STRONG_RE = re.compile(r"[嘅唔哋咗喺冇嚟啲㗎喇嘢佢]")


def cantonese_markers(text: str) -> int:
    """粤语强特征字计数 + 系词「係」（粗判「粤语轮粤语作答」）。

    「係」单用（「係專員會加你」）=粤语系词信号；繁体书面语常见词 關係/聯繫/
    係數 里的「係」不是——按词计数扣除（direct 二跑 slot R7 实弹：真粤语回复
    「原來係我剛才講錯，係專員會加你WhatsApp」只带系词，被记 False 的假阴来源）。
    """
    t = str(text or "")
    n = len(_CANTONESE_STRONG_RE.findall(t))
    n += max(0, t.count("係") - t.count("關係") - t.count("聯繫") - t.count("係數"))
    return n


def cantonese_like(text: str) -> bool:
    return cantonese_markers(text) >= 1


def _norm_for_sim(text: str) -> str:
    return re.sub(r"[\s。，,．.！!？?～~、；;：:…—\-「」『』()（）]", "", str(text or "")).lower()


def similarity_ratio(a: str, b: str) -> float:
    """difflib 相似度（归一后）；任一为空回 0.0。"""
    na, nb = _norm_for_sim(a), _norm_for_sim(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def script_read_stats(reply: str, script: str, threshold: float = 0.9) -> dict:
    """念稿病理：与正稿逐句/整段比对（difflib）。

    ≥0.9 整段=念稿病理 1 例；逐句 ≥0.9 的句数同样计入（整段相似度会被短回复
    稀释，句级更敏感）。
    """
    whole = similarity_ratio(reply, script)
    sentences = [s for s in re.split(r"[。！？!?…\n]+", str(reply or "")) if _norm_for_sim(s)]
    over = 0
    max_sent = 0.0
    for s in sentences:
        r = similarity_ratio(s, script)
        max_sent = max(max_sent, r)
        if r >= threshold:
            over += 1
    return {
        "whole_ratio": round(whole, 3),
        "max_sentence_ratio": round(max_sent, 3),
        "sentences_over_threshold": over,
        "n_sentences": len(sentences),
        "whole_over_threshold": whole >= threshold,
    }


def sanity_check(reply: str, *, lang: str, script: str = "", error: str = "") -> dict:
    """回包 sanity：非空/粤语轮粤语作答/念稿病理计数（全部确定性）。"""
    text = str(reply or "")
    stats = script_read_stats(text, script)
    return {
        "error": error,
        "non_empty": bool(text.strip()),
        "chars": len(text),
        "cantonese_markers": cantonese_markers(text),
        "cantonese_ok": cantonese_like(text) if lang == "cantonese" else None,
        "script": stats,
    }


# ---------------------------------------------------------------------------
# direct：模型目标解析（DB 真值 + 绝对路径解析）
# ---------------------------------------------------------------------------
def _default_db_path() -> Path:
    override = os.environ.get("BOK_DB_PATH", "").strip()
    if override:
        return Path(override)
    return Path.home() / "Library" / "Application Support" / "BokVoice" / "bok_voice.db"


def read_routing_a_reply(db_path: Path) -> dict:
    """读 DB global_settings.model_routing_json 的 a_reply 车道真值（只读）。

    用共享契约 parse_routing 解析（不在台架里重写解析逻辑）；DB 不可读/无表
    → 空 dict（调用方回退 env 缺省链）。
    """
    try:
        from bok_voice_core.model_routes import parse_routing

        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT model_routing_json FROM global_settings WHERE id='global'"
            ).fetchone()
        finally:
            con.close()
        parsed = parse_routing(str((row[0] if row else "") or ""))
        cfg = (parsed.get("lanes") or {}).get("a_reply") or {}
        out = {
            "provider": str(cfg.get("provider") or ""),
            "base_url": str(cfg.get("base_url") or ""),
            "model": str(cfg.get("model") or ""),
            "api_key": str(cfg.get("api_key") or ""),
        }
        # 空行/未配置（全空值）与「读不到」同语义：回 {} 让调用方走 env 缺省链。
        return out if any(out.values()) else {}
    except Exception:  # noqa: BLE001 - 只读探测失败=回退 env，绝不炸台架
        return {}


def resolve_model_path(routing_model: str, model_ids: list[str], home: Path | None = None) -> str:
    """把路由表的 model 值解析成 mlx_lm server 认的**绝对路径**。

    计划 §七 陷阱：短 repo id 会被当 HF repo 触发在线下载（实测卡死 300s）。
    解析序：绝对路径原样 > /v1/models 里的绝对路径候选（后缀匹配）> 服务端
    已注册的短 id（服务端认得=无下载风险）> ~/.lmstudio/models/<id> 存在。
    解析不出回 ""（调用方报错跳过，绝不发短 id 冒险）。
    """
    m = str(routing_model or "").strip()
    if not m:
        return ""
    if m.startswith("/"):
        return m
    tail = m.split("/")[-1]
    for mid in model_ids or []:
        mid = str(mid or "")
        if mid.startswith("/") and (mid.endswith("/" + m) or Path(mid).name == tail):
            return mid
    if m in (model_ids or []):
        return m
    home = home or Path.home()
    cand = home / ".lmstudio" / "models" / m
    if cand.exists():
        return str(cand)
    return ""


def fetch_model_ids(client, base_url: str) -> list[str]:
    """GET {base}/models 取 id 列表（探活失败回空表，调用方降级）。"""
    try:
        r = client.get(f"{base_url.rstrip('/')}/models", timeout=10.0)
        if r.status_code != 200:
            return []
        return [str(it.get("id") or "") for it in (r.json().get("data") or [])]
    except Exception:  # noqa: BLE001
        return []


def post_stream(client, *, base_url: str, model: str, messages: list[dict],
                api_key: str = "", max_tokens: int = DIRECT_MAX_TOKENS, timeout: float = 120.0) -> dict:
    """流式 POST /chat/completions：TTFT/tps/usage/全文（bench_9b_direct 同协议）。"""
    t0 = time.perf_counter()
    ttft = None
    parts: list[str] = []
    usage: dict = {}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with client.stream(
            "POST",
            f"{base_url.rstrip('/')}/chat/completions",
            json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.0,
                "stream": True,
                "stream_options": {"include_usage": True},
            },
            headers=headers,
            timeout=timeout,
        ) as r:
            if r.status_code != 200:
                body = r.read().decode("utf-8", "replace")[:300]
                return {"error": f"HTTP {r.status_code}: {body}"}
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if obj.get("usage"):
                    usage = obj["usage"]
                delta = ""
                try:
                    delta = obj["choices"][0]["delta"].get("content") or ""
                except (KeyError, IndexError, TypeError):
                    delta = ""
                if delta:
                    if ttft is None:
                        ttft = time.perf_counter() - t0
                    parts.append(delta)
    except Exception as exc:  # noqa: BLE001 - 网络错误=数据不是异常
        return {"error": f"{type(exc).__name__}: {exc}"}
    total = time.perf_counter() - t0
    text = "".join(parts)
    comp = int(usage.get("completion_tokens") or 0)
    prompt = int(usage.get("prompt_tokens") or 0)
    return {
        "ttft_ms": round(ttft * 1000, 1) if ttft is not None else None,
        "total_ms": round(total * 1000, 1),
        "prompt_tokens": prompt,
        "completion_tokens": comp,
        "tps": round(comp / (total - (ttft or 0)), 1) if comp and ttft else None,
        "text": text,
        "usage": usage,
    }


# ---------------------------------------------------------------------------
# live：安全闸 + 执行清单 + 汇总聚合（纯函数可测）
# ---------------------------------------------------------------------------
LIVE_ACTIVE_STATUSES = ("active", "ringing", "paused")


def active_call_blockers(db_path: Path) -> list[dict]:
    """安全闸：查 call_sessions 有无 active/ringing/paused（有即拒绝执行 live）。

    只读探测；DB 不可读=返回 [{"status": "unknown", ...}] 由调用方决定
    （live 是重启栈的破坏性操作，探测失败按「可能有在途通话」处理更安全）。
    """
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT id, status, created_at FROM call_sessions "
                f"WHERE status IN ({','.join('?' * len(LIVE_ACTIVE_STATUSES))})",
                LIVE_ACTIVE_STATUSES,
            ).fetchall()
        finally:
            con.close()
        return [{"id": str(r[0]), "status": str(r[1]), "created_at": str(r[2])} for r in rows]
    except Exception as exc:  # noqa: BLE001
        return [{"id": "", "status": "unknown", "created_at": "", "error": repr(exc)}]


def build_live_plan(
    *,
    python: str,
    repo: Path,
    arms: tuple[str, ...] = ARMS,
    local_tts: bool = True,
    soak_scenario: str = "soak-canto",
) -> list[dict]:
    """live 执行清单（逐臂命令；主线择窗按此执行/复核）。

    每臂：down → 带 env serve（detached）→ 等健康 → [ARM4: PUT 路由] →
    FLOW20 → soak → 收尾 down。全部臂跑完恢复原栈（无臂 env serve）。
    """
    plan: list[dict] = []
    for arm in arms:
        env = arm_env(arm)
        if local_tts:
            # 探针要 :8788 合成客户刺激音频（E2E 姿势），与臂自变量正交。
            env = {**env, "BOK_LOCAL_TTS": "1"}
        plan.append(
            {
                "arm": arm,
                "label": ARM_SPECS[arm]["label"],
                "env": env,
                "commands": {
                    "down": [python, str(repo / "tools" / "bok.py"), "down"],
                    "serve": [python, str(repo / "tools" / "bok.py"), "serve"],
                    "flow20": [python, str(repo / "scripts" / "probe_flow_20rounds.py")],
                    "soak": [
                        python,
                        str(repo / "scripts" / "probe_latency_soak.py"),
                        "--scenario",
                        soak_scenario,
                        "--budget-first-ms",
                        "2500",
                        "--budget-perceived-ms",
                        "3000",
                    ],
                    "arm4_routing_put": (
                        ["PUT", "/api/model-routing", "a_reply -> DeepSeek（凭据=环境 DeepSeek 键）"]
                        if arm == ARM4
                        else []
                    ),
                },
            }
        )
    plan.append(
        {
            "arm": "RESTORE",
            "label": "恢复原栈（无臂 env）",
            "env": {"BOK_LOCAL_TTS": "1"} if local_tts else {},
            "commands": {
                "down": [python, str(repo / "tools" / "bok.py"), "down"],
                "serve": [python, str(repo / "tools" / "bok.py"), "serve"],
                "restore_routing": ["PUT", "/api/model-routing", "还原运行前快照"],
            },
        }
    )
    return plan


# 守卫触发日志行（压制面计量——非 env 门守卫留下的压制计数，病理指标不是缺陷）。
GUARD_MARKERS: tuple[str, ...] = (
    "REPEAT_SELF_SUPPRESSED",
    "REPEAT_CROSS_TURN_SUPPRESSED",
    "REPEAT_CROSS_TURN_EMPTY",
    "REPEAT_SUPPRESSED",
    "REPEAT_GUARD_HEAD_FORCE_RELEASE",
    "REPEAT_GUARD_CANCEL_DROP",
    "REPEAT_GUARD_CANCEL_EMPTY",
    "TAIL_ANCHOR_MIMIC_SUPPRESSED",
    "[stall-ladder]",
)
# 坏标记（flow20 同源 + 真日志串；stall 阶梯真串=[stall-ladder]）。
BAD_MARKERS: tuple[str, ...] = (
    "STALL_DEGRADE",
    "STALL_BYPASS",
    "STALL_CLOSE",
    "LLM_FALLBACK_TEXT",
    "LLM_FIRST_TOKEN_TIMEOUT",
    "LLM_LATE_ANSWER",
    "REPEAT_CROSS_TURN_EMPTY",
)

RE_LLM_TTFT = re.compile(
    r"LLM_TTFT_MS (\d+(?:\.\d+)?) \(official\) cached=(\d+)/(\d+) prompt=(\d+) gen=(\d+) tps=(\d+(?:\.\d+)?)"
)
RE_PERCEIVED = re.compile(r"PERCEIVED_MS total=(\d+) \(eou=(\d+) llm=(\d+) tts=(\d+)\)")


def count_markers(window: str, markers: tuple[str, ...]) -> dict:
    return {m: str(window or "").count(m) for m in markers}


def parse_llm_ttft(window: str) -> list[dict]:
    """解析 LLM_TTFT_MS 行（ttft/cached/prompt/gen/tps）——live 的 TTFT 与
    uncached 估算直接来源（uncached=prompt−cached）。"""
    out: list[dict] = []
    for line in str(window or "").splitlines():
        m = RE_LLM_TTFT.search(line)
        if not m:
            continue
        ttft, cached, prompt, gen, tps = (
            float(m.group(1)),
            int(m.group(2)),
            int(m.group(3)),
            int(m.group(5)),
            float(m.group(6)),
        )
        out.append(
            {
                "ttft_ms": ttft,
                "cached": cached,
                "prompt": prompt,
                "gen": gen,
                "tps": tps,
                "uncached": max(0, prompt - cached),
            }
        )
    return out


def parse_perceived(window: str) -> list[dict]:
    out: list[dict] = []
    for line in str(window or "").splitlines():
        m = RE_PERCEIVED.search(line)
        if m:
            out.append(
                {
                    "total": int(m.group(1)),
                    "eou": int(m.group(2)),
                    "llm": int(m.group(3)),
                    "tts": int(m.group(4)),
                }
            )
    return out


def aggregate_live_arm(
    *,
    flow20_stdout: str = "",
    soak_stdout: str = "",
    soak_json: dict | list | None = None,
    log_window: str = "",
) -> dict:
    """逐臂汇总：TTFT/first_audio/PERCEIVED p50/p95 + 坏标记/守卫触发计数 + uncached。"""
    ttft_rows = parse_llm_ttft(log_window)
    ttft = [r["ttft_ms"] for r in ttft_rows]
    uncached = [r["uncached"] for r in ttft_rows]
    prompts = [r["prompt"] for r in ttft_rows]

    perceived: list[dict] = parse_perceived(log_window)
    first_audio: list[float] = []
    soak_results: list[dict] = []
    if isinstance(soak_json, list):
        soak_results = soak_json
    elif isinstance(soak_json, dict):
        soak_results = [soak_json]
    for res in soak_results:
        for p in res.get("perceived") or []:
            if isinstance(p, dict) and "total" in p:
                perceived.append(p)
        for m in res.get("measures") or []:
            if isinstance(m, dict) and m.get("first_audio_ms") is not None:
                first_audio.append(float(m["first_audio_ms"]))

    flow20_line = ""
    for line in str(flow20_stdout or "").splitlines():
        if "FLOW20" in line:
            flow20_line = line.strip()

    return {
        "n_llm_requests": len(ttft_rows),
        "ttft_ms": {
            "n": len(ttft),
            "p50": percentile(ttft, 50),
            "p95": percentile(ttft, 95),
            "max": max(ttft) if ttft else None,
        },
        "tps": {
            "p50": percentile([r["tps"] for r in ttft_rows], 50),
            "p95": percentile([r["tps"] for r in ttft_rows], 95),
        },
        "prompt_tokens": {
            "p50": percentile([float(v) for v in prompts], 50),
            "p95": percentile([float(v) for v in prompts], 95),
            "max": max(prompts) if prompts else None,
        },
        "uncached_tokens": {
            "n": len(uncached),
            "p50": percentile([float(v) for v in uncached], 50),
            "p95": percentile([float(v) for v in uncached], 95),
            "total": sum(uncached),
            "total_seconds": round(sum(uncached) / MODEL_PREFILL_TOK_S, 2),
        },
        "first_audio_ms": {
            "n": len(first_audio),
            "p50": percentile(first_audio, 50),
            "p95": percentile(first_audio, 95),
            "max": max(first_audio) if first_audio else None,
        },
        "perceived_ms": {
            "n": len(perceived),
            "p50": percentile([float(p["total"]) for p in perceived], 50),
            "p95": percentile([float(p["total"]) for p in perceived], 95),
            "max": max((p["total"] for p in perceived), default=None),
        },
        "bad_markers": count_markers(log_window, BAD_MARKERS),
        "guard_markers": count_markers(log_window, GUARD_MARKERS),
        "flow20_line": flow20_line,
        "flow20_pass": ("PASS" in flow20_line) if flow20_line else None,
    }


def agent_log_path() -> Path:
    """agent.log 路径（e2e_real_customer._default_log_dir 同口径，避免引 livekit）。"""
    override = os.environ.get("BOK_LOG_DIR", "").strip()
    if override:
        return Path(override) / "agent.log"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "BokVoice" / "logs" / "agent.log"
    return Path.home() / ".local" / "share" / "BokVoice" / "logs" / "agent.log"


# ---------------------------------------------------------------------------
# 输出（JSON + markdown）
# ---------------------------------------------------------------------------
def write_json(out_dir: Path, name: str, payload) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def offline_markdown(result: dict) -> str:
    """offline 尺寸对照 markdown（ARM1 vs 槽位；R2/R15/累计在首表）。"""
    agg = result["aggregate"]
    rows = result["rows"]
    a1, sl = agg["ARM1"], agg["slot"]
    lines = [
        "# D1 槽位化 actor A/B —— offline 请求尺寸表（ARM1 现状 vs 槽位 stub）",
        "",
        f"- 生成时间：{result['ts_iso']}；模板 fixture：`{result['template']['id']}`"
        f"（{result['template']['steps']} 步，粤语）；人物=夹具（persona/object 见 JSON）。",
        f"- 估算口径：CJK≈1tok/字（`est_tokens`，校准系数 0.93 + 非 CJK ~4 字符/token；"
        f"direct 实测 est/real 残差 ±6%）；uncached 换算 `{MODEL_PREFILL_TOK_S:.0f} tok/s`"
        "（M4 Pro 9B prefill 天花板，计划 §七）。",
        "- 槽位形状=stub 渲染器（W-A 真实现落地前的占位：角色卡+任务块+纯历史）；"
        "ARM3 与 ARM1 同 prompt 形状（只关守卫），ARM4 与槽位同形状（只换模型）。",
        "",
        "## 首表：R2 / R15 / 累计 uncached",
        "",
        "| 指标 | ARM1（全剧本） | 槽位（ARM2/4 形状） | 差异 |",
        "|---|---:|---:|---:|",
    ]

    def _fmt(v, nd=0):
        return "-" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))

    r2 = pick_round(rows, 2) or {}
    r15 = pick_round(rows, 15) or {}
    for label, row in (("R2 请求 tok", r2), ("R15 请求 tok", r15)):
        v1 = _row_shape(row, "ARM1").get("est_tokens")
        v2 = _row_shape(row, "slot").get("est_tokens")
        lines.append(f"| {label} | {_fmt(v1)} | {_fmt(v2)} | {_fmt((v1 or 0) - (v2 or 0))} |")
    for label, key, nd in (
        ("累计 uncached tok", "total_uncached_tokens", 0),
        ("累计 uncached 秒", "total_uncached_seconds", 2),
        ("峰值请求 tok（全冷）", "peak_request_tokens", 0),
        ("峰值全冷秒", "peak_request_seconds", 2),
    ):
        v1, v2 = a1.get(key), sl.get(key)
        lines.append(f"| {label} | {_fmt(v1, nd)} | {_fmt(v2, nd)} | {_fmt((v1 or 0) - (v2 or 0), nd)} |")
    lines += [
        "",
        f"- LLM 轮数：ARM1 形状 {a1['n_llm_rounds']}；脚本轮（say/WA 罐头，真栈零 LLM 请求）"
        f"{a1['n_script_rounds']} 轮。",
        "",
        "## 逐轮明细（chars=序列化总字符；tok=est_tokens；unc=uncached 估算）",
        "",
        "| 轮 | 车道 | 步 | verdict | 标记 | ARM1 chars | ARM1 tok | ARM1 unc | 槽位 chars | 槽位 tok | 槽位 unc | 备注 |",
        "|---:|---|---:|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        s1, s2 = _row_shape(r, "ARM1"), _row_shape(r, "slot")
        lines.append(
            f"| {r['round']} | {r['lane']} | {r['step']} | {r['verdict']} | {r['tag'] or ''} "
            f"| {s1.get('chars', '-')} | {s1.get('est_tokens', '-')} | {s1.get('uncached_est', '-')} "
            f"| {s2.get('chars', '-')} | {s2.get('est_tokens', '-')} | {s2.get('uncached_est', '-')} "
            f"| {r['note']} |"
        )
    lines += [
        "",
        "## 假设与偏差",
        "",
    ]
    for a in result.get("assumptions", []):
        lines.append(f"- {a}")
    return "\n".join(lines) + "\n"


def direct_markdown(result: dict) -> str:
    lines = [
        "# D1 槽位化 actor A/B —— direct 实测（:1237 直打，两形态同轮对跑）",
        "",
        f"- 生成时间：{result['ts_iso']}；模型：`{result['target'].get('model')}`"
        f" @ `{result['target'].get('base_url')}`（{result['target'].get('source')}）。",
        "- 槽位形状=stub 渲染器（W-A 真实现未落地）；ARM1 形状=现渲染函数。"
        "首个 ARM1 请求是冷前缀首触（无预热），TTFT 含全量 prefill——真栈由"
        "LLM_PREFIX_PREWARM 在通话开始吸收同额成本。",
        "- 念稿病理只统计非收尾步（收尾步念正稿=正常业务行为）；整段/句级均以"
        "difflib 与**该步正稿首行**比对，≥0.9 记一例。",
        "",
        "## 汇总（按形状）",
        "",
        "| 形状 | n | TTFT p50/p95 (ms) | tps p50 | 回复字数 p50 | 念稿病理(整段≥0.9 / 句级≥0.9，n=非收尾轮) | sanity 过 | est/real tok |",
        "|---|---:|---|---:|---:|---|---:|---:|",
    ]
    for shape in ("ARM1", "slot"):
        s = (result["summary"] or {}).get(shape) or {}
        lines.append(
            f"| {shape} | {s.get('n', 0)} | {s.get('ttft_p50', '-')}/{s.get('ttft_p95', '-')} "
            f"| {s.get('tps_p50', '-')} | {s.get('chars_p50', '-')} "
            f"| {s.get('script_whole_over', 0)} / {s.get('script_sentences_over', 0)}（n={s.get('pathology_rows', 0)}） "
            f"| {s.get('sanity_pass', 0)}/{s.get('n', 0)} | {s.get('est_real_ratio', '-')} |"
        )
    lines += ["", "## 逐轮", ""]
    for r in result.get("rows", []):
        lines.append(
            f"### R{r['round']} [{r['tag'] or r['note']}] 步{r['step']} lane={r['lane']} "
            f"「{r['text']}」"
        )
        for shape in ("ARM1", "slot"):
            res = (r.get("results") or {}).get(shape) or {}
            san = res.get("sanity") or {}
            lines.append(
                f"- **{shape}** ttft={res.get('ttft_ms', '-')}ms tps={res.get('tps', '-')} "
                f"ptok={res.get('prompt_tokens', '-')} ctok={res.get('completion_tokens', '-')} "
                f"est={res.get('est_tokens', '-')} sanity={san.get('non_empty')}/"
                f"粤语={san.get('cantonese_ok')}/念稿={san.get('script', {}).get('whole_ratio')}"
                f" 句级≥0.9={san.get('script', {}).get('sentences_over_threshold')}"
            )
            if res.get("text"):
                lines.append(f"  - 回包：{res['text'][:120]}")
            if res.get("error"):
                lines.append(f"  - 错误：{res['error']}")
    if result.get("arm4_note"):
        lines += ["", f"- ARM4（DeepSeek）：{result['arm4_note']}"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# offline / direct / live 执行
# ---------------------------------------------------------------------------
def run_offline(args) -> int:
    template = load_template_fixture(TEMPLATE_ID)
    sim = simulate_call(
        template=template,
        rounds=OFFLINE_ROUNDS,
        persona=FIXTURE_PERSONA,
        object_card=FIXTURE_OBJECT,
        object_brief=FIXTURE_OBJECT_BRIEF,
        lang="cantonese",
    )
    agg = aggregate_offline(sim["rows"])
    result = {
        "mode": "offline",
        "ts": int(time.time()),
        "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        "template": {
            "id": template["id"],
            "language": template["language"],
            "steps": sim["template"]["steps"],
            "steps_json": "<omitted>",
        },
        "system_chars": sim["system_chars"],
        "aggregate": agg,
        "rows": sim["rows"],
        "assumptions": [
            "槽位形状为 stub 渲染器（W-A 真实现落地前占位）：system 角色卡 + 任务块(100-250c) + 纯历史；"
            "编排职责（推进/收线/verdict/纪律/总览/禁讲）已按契约撤出。",
            "ARM1 形状用现渲染函数逐轮渲染（flow.FlowController + ContextState 两段式尾巴），"
            "历史重放纪律=append-only 冻结（EX-1 洞消息裸体）。",
            "模拟回复=本步正稿首句截 40 字（近似；真回复长度由模型定，只影响下一轮历史体积）。",
            "碎片/重问轮按 LLM 轮计（真栈可能落 garbled-reask 罐头——保守取 LLM，尺寸表上界）。",
            "WA 报号轮按真栈语义计为直捕罐头轮（无 LLM 请求、不推进）。",
            "uncached 估算=与上一请求的公共前缀之外全部 token（含摊销截断重锚轮全额）。",
            "首轮冷 prefill 计入累计（真栈由 LLM_PREFIX_PREWARM 在通话开始吸收同额成本）；"
            "脚本轮（say/WA 罐头）真栈零 LLM 请求、其 user 消息按 EX-1 裸体冻结。",
            f"token 估算 CJK≈1tok/字 + 其它 ~3 字符/token；秒数按 {MODEL_PREFILL_TOK_S:.0f} tok/s 换算。",
            "ARM3 prompt 形状 ≡ ARM1（只关守卫不改 prompt）；ARM4 形状 ≡ 槽位（只换模型）。",
        ],
    }
    out = Path(args.out_dir)
    ts = time.strftime("%Y%m%d-%H%M%S")
    json_path = write_json(out, f"{ts}-offline.json", result)
    md_path = out / f"{ts}-offline.md"
    md_path.write_text(offline_markdown(result), encoding="utf-8")
    a1, sl = agg["ARM1"], agg["slot"]
    r2, r15 = pick_round(sim["rows"], 2), pick_round(sim["rows"], 15)
    print(f"[ab-slot-actor] offline 模板={template['id']} 轮数={len(sim['rows'])}")
    print(f"  system chars: ARM1={sim['system_chars']['ARM1']} slot={sim['system_chars']['slot']}")
    for label, row in (("R2", r2), ("R15", r15)):
        if not row:
            continue
        s1, s2 = _row_shape(row, "ARM1"), _row_shape(row, "slot")
        print(f"  {label} 请求 tok: ARM1={s1.get('est_tokens')} slot={s2.get('est_tokens')}"
              f" | uncached: ARM1={s1.get('uncached_est')} slot={s2.get('uncached_est')}")
    print(f"  累计 uncached tok/秒: ARM1={a1['total_uncached_tokens']}/{a1['total_uncached_seconds']}s "
          f"slot={sl['total_uncached_tokens']}/{sl['total_uncached_seconds']}s")
    print(f"  峰值请求 tok（全冷秒）: ARM1={a1['peak_request_tokens']}({a1['peak_request_seconds']}s) "
          f"slot={sl['peak_request_tokens']}({sl['peak_request_seconds']}s)")
    print(f"[ab-slot-actor] JSON → {json_path}")
    print(f"[ab-slot-actor] markdown → {md_path}")
    return 0


def _direct_target(args) -> dict:
    routing = read_routing_a_reply(Path(args.db))
    base_url = args.base_url or routing.get("base_url") or os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1")
    model = args.model or routing.get("model") or os.environ.get("MLX_LLM_MODEL", "")
    source = "cli" if args.base_url or args.model else ("db-routing" if routing.get("model") or routing.get("base_url") else "env")
    return {"base_url": base_url, "model": model, "routing": routing, "source": source}


def run_direct(args) -> int:
    import httpx

    template = load_template_fixture(TEMPLATE_ID)
    sim = simulate_call(
        template=template,
        rounds=OFFLINE_ROUNDS,
        persona=FIXTURE_PERSONA,
        object_card=FIXTURE_OBJECT,
        object_brief=FIXTURE_OBJECT_BRIEF,
        lang="cantonese",
    )
    rows = pick_representative_rounds(sim["rows"], DIRECT_WANTED_TAGS, limit=int(args.rounds))
    target = _direct_target(args)

    out: dict = {
        "mode": "direct",
        "ts": int(time.time()),
        "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        "target": {"base_url": target["base_url"], "model": target["model"], "source": target["source"]},
        "rows": [],
        "summary": {},
        "arm4_note": "",
    }

    with httpx.Client() as client:
        model_ids = fetch_model_ids(client, target["base_url"])
        resolved = resolve_model_path(target["model"], model_ids)
        if not resolved:
            print(f"[ab-slot-actor] 无法把 model={target['model']!r} 解析成绝对路径；"
                  f"/models 候选={model_ids[:6]}——拒绝直发（短 id 触发 HF 下载，计划 §七）。"
                  "可 --model 指绝对路径。")
            return 2
        if resolved != target["model"]:
            print(f"[ab-slot-actor] model 解析：{target['model']!r} → {resolved!r}（绝对路径，防 HF 下载）")
        out["target"]["model"] = resolved
        out["target"]["model_ids"] = model_ids

        from agent_runtime.flow import object_vars as _object_vars

        _vars = _object_vars(FIXTURE_OBJECT)
        for row in rows:
            entry = {
                "round": row["round"],
                "tag": row["tag"],
                "note": row["note"],
                "text": row["text"],
                "lane": row["lane"],
                "step": row["step"],
                "step_total": row["step_total"],
                # 收尾步（step==step_total）：念正稿=正常业务行为（告别话术就是正稿），
                # 不计入念稿病理（direct 首跑 R15 两形态均被记 pathology 的假阳来源）。
                "closing_step": bool(row["step"] == row["step_total"]),
                "verdict": row["verdict"],
                "results": {},
            }
            # 该步正稿首行（念稿病理比对基线；从请求最后一条 user 的上下文推不出，
            # 直接用模板解析——与 simulate_call 同源）。
            script = _step_script_line(template, row["step"], _vars)
            for shape in ("ARM1", "slot"):
                msgs = _row_shape(row, shape).get("messages") or []
                if not msgs:
                    continue
                res = post_stream(client, base_url=target["base_url"], model=resolved, messages=msgs)
                est = est_tokens(serialize_messages(msgs))
                res["est_tokens"] = est
                res["sanity"] = sanity_check(
                    res.get("text", ""), lang="cantonese", script=script, error=res.get("error", "")
                )
                entry["results"][shape] = res
                ttft = res.get("ttft_ms")
                print(
                    f"  R{row['round']} [{row['tag'] or row['note']}] {shape}: "
                    f"ttft={ttft}ms tps={res.get('tps')} ptok={res.get('prompt_tokens')} "
                    f"est={est} ctok={res.get('completion_tokens')} "
                    f"err={res.get('error', '')[:60]}"
                )
            out["rows"].append(entry)

        out["summary"] = aggregate_direct(out["rows"])

        # ARM4：DeepSeek 直打（凭据=环境 DeepSeek 键；缺键优雅跳过并标注）。
        ds_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if args.deepseek and not ds_key:
            out["arm4_note"] = "DEEPSEEK_API_KEY 缺席——ARM4 云端臂跳过（报告标注，待择窗补跑）"
            print(f"[ab-slot-actor] {out['arm4_note']}")
        elif args.deepseek:
            ds_base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
            ds_model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
            out["target_deepseek"] = {"base_url": ds_base, "model": ds_model}
            for row in rows[:3]:  # 云端只打 3 轮代表（成本纪律）
                entry = next(e for e in out["rows"] if e["round"] == row["round"])
                script = _step_script_line(template, row["step"], _vars)
                for shape in ("ARM1", "slot"):
                    msgs = _row_shape(row, shape).get("messages") or []
                    if not msgs:
                        continue
                    res = post_stream(
                        client,
                        base_url=ds_base,
                        model=ds_model,
                        messages=msgs,
                        api_key=ds_key,
                        timeout=90.0,
                    )
                    res["sanity"] = sanity_check(
                        res.get("text", ""), lang="cantonese", script=script, error=res.get("error", "")
                    )
                    entry.setdefault("deepseek", {})[shape] = res
                    print(
                        f"  R{row['round']} {shape} [deepseek]: ttft={res.get('ttft_ms')}ms "
                        f"err={res.get('error', '')[:60]}"
                    )

    out_dir = Path(args.out_dir)
    ts = time.strftime("%Y%m%d-%H%M%S")
    json_path = write_json(out_dir, f"{ts}-direct.json", out)
    md_path = out_dir / f"{ts}-direct.md"
    md_path.write_text(direct_markdown(out), encoding="utf-8")
    print("[ab-slot-actor] direct 汇总：")
    for shape, s in out["summary"].items():
        print(
            f"  {shape}: n={s['n']} ttft p50/p95={s['ttft_p50']}/{s['ttft_p95']}ms "
            f"tps p50={s['tps_p50']} 念稿(整段/句级)={s['script_whole_over']}/{s['script_sentences_over']} "
            f"sanity={s['sanity_pass']}/{s['n']} est/real={s['est_real_ratio']}"
        )
    if out.get("arm4_note"):
        print(f"  ARM4：{out['arm4_note']}")
    print(f"[ab-slot-actor] JSON → {json_path}")
    print(f"[ab-slot-actor] markdown → {md_path}")
    return 0


def _step_script_line(template: dict, step_no: int, vars_map: dict | None = None) -> str:
    """模板第 step_no 步（1-based）的正稿首行（变量按 vars_map 渲染——WA 步的
    {聯絡方式} 必须替换，否则整行被占位检测跳过、念稿基线为空）。"""
    from agent_runtime.flow import parse_step_ref, render_template_text

    try:
        steps = json.loads(template.get("steps_json") or "[]")
    except Exception:  # noqa: BLE001
        return ""
    if not (0 < int(step_no) <= len(steps)):
        return ""
    ref = str((steps[int(step_no) - 1] or {}).get("ref") or "")
    script = parse_step_ref(ref).script
    rendered = render_template_text(script, vars_map or {})
    for ln in rendered.splitlines():
        ln = ln.strip()
        if ln and not re.search(r"\{[^{}]+\}", ln):
            return ln
    return ""


def aggregate_direct(rows: list[dict]) -> dict:
    """direct 汇总（纯函数）：每形状 TTFT/tps/字数/念稿病理/sanity/est-real 比。

    念稿病理只统计非收尾步（closing_step 行排除——收尾步念正稿=正常）。
    """
    out: dict = {}
    for shape in ("ARM1", "slot"):
        res_list = [r["results"][shape] for r in rows if shape in (r.get("results") or {})]
        ok = [r for r in res_list if not r.get("error") and r.get("ttft_ms") is not None]
        ttft = [float(r["ttft_ms"]) for r in ok]
        tps = [float(r["tps"]) for r in ok if r.get("tps")]
        chars = [int(r.get("sanity", {}).get("chars") or 0) for r in ok]
        path_rows = [
            r for r in rows
            if not r.get("closing_step") and shape in (r.get("results") or {})
            and not r["results"][shape].get("error")
            and r["results"][shape].get("ttft_ms") is not None
        ]
        whole_over = sum(
            1 for r in path_rows
            if (r["results"][shape].get("sanity", {}).get("script", {}) or {}).get("whole_over_threshold")
        )
        sent_over = sum(
            int((r["results"][shape].get("sanity", {}).get("script", {}) or {}).get("sentences_over_threshold") or 0)
            for r in path_rows
        )
        sanity_pass = sum(
            1
            for r in ok
            if r["sanity"]["non_empty"]
            and (r["sanity"].get("cantonese_ok") is not False)
        )
        ratios = [
            float(r["est_tokens"]) / float(r["prompt_tokens"])
            for r in ok
            if r.get("est_tokens") and r.get("prompt_tokens")
        ]
        out[shape] = {
            "n": len(ok),
            "errors": len(res_list) - len(ok),
            "ttft_p50": percentile(ttft, 50),
            "ttft_p95": percentile(ttft, 95),
            "tps_p50": percentile(tps, 50),
            "chars_p50": percentile([float(c) for c in chars], 50),
            "pathology_rows": len(path_rows),
            "script_whole_over": whole_over,
            "script_sentences_over": sent_over,
            "sanity_pass": sanity_pass,
            "est_real_ratio": round(sum(ratios) / len(ratios), 3) if ratios else None,
        }
    return out


def _capture_arm_probes(step: dict, log_offset: int) -> dict:
    """跑 FLOW20+soak 捕获输出并聚合为指标（原始 stdout 不外泄到调用方作用域）。"""
    env = dict(os.environ, **step["env"])
    try:
        _p = subprocess.run(
            ["/usr/bin/env", *step["commands"]["flow20"]],
            env=env, cwd=str(ROOT), capture_output=True, text=True, timeout=1800,
        )
        flow20 = (_p.stdout or "") + ("\n[stderr]\n" + _p.stderr if _p.stderr else "")
    except Exception as exc:  # noqa: BLE001
        flow20 = f"[run-error] {exc!r}"
    try:
        _p = subprocess.run(
            ["/usr/bin/env", *step["commands"]["soak"]],
            env=env, cwd=str(ROOT), capture_output=True, text=True, timeout=1800,
        )
        soak = (_p.stdout or "") + ("\n[stderr]\n" + _p.stderr if _p.stderr else "")
    except Exception as exc:  # noqa: BLE001
        soak = f"[run-error] {exc!r}"
    window = _read_log_window(log_offset)
    soak_json = _latest_soak_json()
    return aggregate_live_arm(
        flow20_stdout=flow20, soak_stdout=soak, soak_json=soak_json, log_window=window
    )


def run_live(args, *, dry_run: bool) -> int:
    """live 逐臂执行（本波只实现；--live-dry-run 只出清单）。

    安全闸：call_sessions 有 active/ringing/paused → 拒绝（绝不杀在途通话）；
    探测失败按可能有在途处理（重启栈是破坏性操作，fail-closed）。
    """
    plan = build_live_plan(
        python=args.python or sys.executable,
        repo=ROOT,
        arms=tuple(a for a in ARMS if a in (args.arms or ARMS)),
        local_tts=not args.no_local_tts,
        soak_scenario=args.soak_scenario,
    )
    out_dir = Path(args.out_dir)
    if dry_run:
        payload = {"mode": "live-plan", "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S"), "plan": plan}
        ts = time.strftime("%Y%m%d-%H%M%S")
        p = write_json(out_dir, f"{ts}-live-plan.json", payload)
        print("[ab-slot-actor] live 执行清单（不执行）：")
        for step in plan:
            print(f"  {step['arm']}: env={step['env']}")
            for name, cmd in step["commands"].items():
                if cmd:
                    print(f"    {name}: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
        print(f"[ab-slot-actor] 清单 JSON → {p}")
        return 0

    blockers = active_call_blockers(Path(args.db))
    if blockers:
        print(f"[ab-slot-actor] 安全闸拒绝：call_sessions 存在在途通话/探测失败 {blockers[:5]}"
              "（live 需重启栈；先等通话结束或改用 --live-dry-run）")
        return 2

    # 真执行：逐臂 down→serve→健康→[ARM4 路由]→探针→收尾；全部臂后恢复原栈。
    # 本波（2026-10-01 W-B）只实现不执行——主线审计后择窗跑。
    routing_original = read_routing_raw_or_none(Path(args.db))
    results: list[dict] = []
    for step in plan:
        arm = step["arm"]
        if arm == "RESTORE":
            continue
        subprocess.call(["/usr/bin/env", *step["commands"]["down"]], env=dict(os.environ, **step["env"]), cwd=str(ROOT))
        subprocess.Popen(
            ["/usr/bin/env", *step["commands"]["serve"]], env=dict(os.environ, **step["env"]), cwd=str(ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not wait_healthy(timeout_s=args.health_timeout):
            print(f"[ab-slot-actor] {arm}: 栈健康超时——跳过本臂（记录后继续）")
            subprocess.call(["/usr/bin/env", *step["commands"]["down"]], env=dict(os.environ, **step["env"]), cwd=str(ROOT))
            results.append({"arm": arm, "error": "health-timeout"})
            continue
        # ARM4：a_reply 车道 PUT 到 DeepSeek（凭据=环境 DeepSeek 键；缺键跳过本臂）。
        arm4_routing = ""
        if arm == ARM4:
            ds_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
            if not ds_key:
                arm4_routing = "deepseek-key-missing"
                print("[ab-slot-actor] ARM4: DEEPSEEK_API_KEY 缺席——跳过本臂（不重启栈跑探针）")
                subprocess.call(["/usr/bin/env", *step["commands"]["down"]], env=dict(os.environ, **step["env"]), cwd=str(ROOT))
                results.append({"arm": arm, "error": "deepseek-key-missing"})
                continue
            ok = cp_put_model_routing(
                build_a_reply_route_payload(
                    base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
                    model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
                    api_key=ds_key,
                )
            )
            arm4_routing = "put-ok" if ok else "put-failed"
            print(f"[ab-slot-actor] ARM4: a_reply → DeepSeek（{arm4_routing}）")
        log_offset = agent_log_path().stat().st_size if agent_log_path().exists() else 0
        agg = _capture_arm_probes(step, log_offset)
        results.append({"arm": arm, "label": step["label"], "env": step["env"], **agg})
        # ARM4 跑完立即还原路由（不等全部臂结束——在途通话读的是当通装配值，
        # 但下一通必须回到原车道）。
        if arm == ARM4 and routing_original is not None:
            cp_put_model_routing(build_restore_route_payload(routing_original))
        subprocess.call(["/usr/bin/env", *step["commands"]["down"]], env=dict(os.environ, **step["env"]), cwd=str(ROOT))
    # 恢复原栈（无臂 env）。
    restore = plan[-1]
    if routing_original is not None:
        cp_put_model_routing(build_restore_route_payload(routing_original))
    subprocess.call(["/usr/bin/env", *restore["commands"]["down"]], env=dict(os.environ, **restore["env"]), cwd=str(ROOT))
    subprocess.Popen(
        ["/usr/bin/env", *restore["commands"]["serve"]], env=dict(os.environ, **restore["env"]), cwd=str(ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    payload = {
        "mode": "live",
        "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        "results": results,
        "routing_original_present": routing_original is not None,
    }
    ts = time.strftime("%Y%m%d-%H%M%S")
    p = write_json(out_dir, f"{ts}-live.json", payload)
    print(f"[ab-slot-actor] live 汇总 → {p}")
    return 0


def read_routing_raw_or_none(db_path: Path) -> str | None:
    """读路由表原始串（live 恢复用）；失败 None。"""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT model_routing_json FROM global_settings WHERE id='global'"
            ).fetchone()
        finally:
            con.close()
        return str((row[0] if row else "") or "")
    except Exception:  # noqa: BLE001
        return None


def build_a_reply_route_payload(*, base_url: str, model: str, api_key: str = "") -> dict:
    """ARM4 路由 PUT 体：a_reply → openai 档（DeepSeek）。

    api_key 空=CP 侧「保留旧值」语义（本台架恒传真键）；enable_thinking=False
    显式钉死（Qwen3.5 系思考全开陷阱的反向——DeepSeek 亦不应带思考段）。
    """
    return {
        "lanes": {
            "a_reply": {
                "provider": "openai",
                "base_url": str(base_url or ""),
                "model": str(model or ""),
                "api_key": str(api_key or ""),
                "extra": {"enable_thinking": False},
            }
        }
    }


def build_restore_route_payload(original_raw: str) -> dict:
    """还原体：只回 a_reply 车道（原始快照里的值）；无该车道=空 lanes（no-op）。"""
    try:
        from bok_voice_core.model_routes import parse_routing

        cfg = (parse_routing(original_raw).get("lanes") or {}).get("a_reply")
    except Exception:  # noqa: BLE001
        cfg = None
    if not cfg:
        return {"lanes": {}}
    return {"lanes": {"a_reply": {**cfg, "api_key": str(cfg.get("api_key") or "")}}}


def cp_put_model_routing(payload: dict) -> bool:
    """PUT /api/model-routing（CP API；BOK_CP_TOKEN 在场带机器通道 Bearer）。

    返回是否 2xx；失败=数据（记录后继续），绝不因路由臂失败炸整个 live。
    """
    url = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000").rstrip("/")
    headers = {}
    token = os.environ.get("BOK_CP_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        target = f"{url}/api/model-routing"
        # SSRF 闸门（与 httpx.put 同函数体就地校验）：仅 http/https、host 非空、无 userinfo。
        _parts = urllib.parse.urlsplit(target)
        _host = (_parts.hostname or "").lower()
        if not (
            _parts.scheme in ("http", "https")
            and (_host in _LOOPBACK_HOSTS or bool(_host))
            and not _parts.username
            and not _parts.password
        ):
            raise PermissionError(f"出站 URL 未过护栏（拒发）: {target}")
        r = _put_routing(target, payload, headers)
        return 200 <= r.status_code < 300
    except Exception:  # noqa: BLE001
        return False


def _put_routing(target: str, payload: dict, headers: dict) -> object:
    """PUT 单点（httpx.Client().send 形态：Request 对象直发，与 httpx.put 同义）。"""
    import httpx

    req = httpx.Request("PUT", target, json=payload, headers=headers)
    with httpx.Client(timeout=15.0) as client:
        return client.send(req)


def wait_healthy(timeout_s: float = 240.0, interval_s: float = 3.0) -> bool:
    """等栈健康：CP /health + LiveKit 7880 + ASR 8787 + 本地 TTS 8788（在即算）。

    只做 TCP/HTTP 探活，不依赖 livekit SDK；TTS 侧car 缺省姿态（全云端）跳过
    ——但 live 计划默认 BOK_LOCAL_TTS=1（探针要 :8788），此处只探 CP/LiveKit/ASR。
    """
    import httpx

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ok_cp = False
        try:
            ok_cp = httpx.get("http://127.0.0.1:8000/health", timeout=3.0).status_code == 200
        except Exception:  # noqa: BLE001
            ok_cp = False
        ok_lk = _tcp_open("127.0.0.1", 7880)
        ok_asr = _tcp_open("127.0.0.1", 8787)
        if ok_cp and ok_lk and ok_asr:
            return True
        time.sleep(interval_s)
    return False


def _tcp_open(host: str, port: int) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=2.0):
            return True
    except OSError:
        return False


def _read_log_window(offset: int) -> str:
    path = agent_log_path()
    try:
        with path.open("rb") as f:
            f.seek(offset)
            return f.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _latest_soak_json() -> list | dict | None:
    """最新一份 soak JSON（probe_latency_soak 落 reports/latency-soak/）。"""
    d = ROOT / "reports" / "latency-soak"
    try:
        files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if not files:
            return None
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="D1 槽位化 actor 四臂 A/B 台架（offline/direct/live）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--offline", action="store_true", help="零栈依赖：两形态 20 轮请求尺寸表")
    ap.add_argument("--direct", action="store_true", help="直打 :1237（+可选 DeepSeek）代表轮实测")
    ap.add_argument("--live", action="store_true", help="逐臂重启栈跑 FLOW20+soak（需择窗；有安全闸）")
    ap.add_argument("--live-dry-run", action="store_true", help="只输出 live 执行清单，不动栈")
    ap.add_argument("--arms", default=",".join(ARMS), help="live 臂子集（逗号分隔）")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--db", default=str(_default_db_path()))
    ap.add_argument("--base-url", default="", help="direct 覆盖（缺省=DB 路由 a_reply 车道）")
    ap.add_argument("--model", default="", help="direct 覆盖（缺省=DB 路由；非绝对路径会解析）")
    ap.add_argument("--rounds", type=int, default=9, help="direct 代表轮数（8-10）")
    ap.add_argument("--deepseek", action="store_true", default=True, help="ARM4 云端臂（默认开，缺键跳过）")
    ap.add_argument("--no-deepseek", dest="deepseek", action="store_false")
    ap.add_argument("--python", default="", help="live 子进程解释器（缺省=当前解释器）")
    ap.add_argument("--no-local-tts", action="store_true", help="live 不注入 BOK_LOCAL_TTS=1")
    ap.add_argument("--soak-scenario", default="soak-canto")
    ap.add_argument("--health-timeout", type=float, default=240.0)
    args = ap.parse_args(argv)

    args.arms = tuple(a.strip() for a in str(args.arms or "").split(",") if a.strip())
    if not (args.offline or args.direct or args.live or args.live_dry_run):
        ap.print_help()
        return 2
    rc = 0
    if args.offline:
        rc |= run_offline(args)
    if args.direct:
        rc |= run_direct(args)
    if args.live_dry_run:
        rc |= run_live(args, dry_run=True)
    elif args.live:
        rc |= run_live(args, dry_run=False)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
