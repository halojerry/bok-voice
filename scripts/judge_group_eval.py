"""JudgeGroup prompt 规则回归(W9,2026-09-24)——静态用例库+本地 4B/9B 双判官同卷。

钉三条 prompt 规则族的行为回归(改 `_SHARED_RESPONSE_RULES`/跳步禁讲/共享指引后必跑):
  ① 防诈质疑(objection):质疑轮=简短安抚+重申身份,**不得借回应质疑反复追问平台**
    (offscript 实测修复前平台问句 5 通重复 10+ 次)。
  ② 赔偿数字纪律(compensation):档位/金额只准客户问赔偿或赔偿环节讲,投诉/转人工/
    找主管轮一律不报数字(修复前「最低 300 蚊」在投诉轮乱入 3 次);正向对照=客户
    直接问赔偿必须报得出数字(防规则过度压制)。
  ③ 跳步禁讲(jump):图跳转首轮【跳转进入】+【禁讲清单】压制总览逐字引力——被跳步
    的问句/台词不得复现(I3 二修验收纪律:禁讲侧硬断言,目标步落词侧软信息)。

用例库**全静态零 DB**(与 probe_reply_quality 的区别:不读库——绕开 G0 SQL 落盘门,
CI 任何环境可跑 selftest 腿)。prompt 走**生产同一条渲染路径**(FlowController +
ContextState,总览/尾部/禁讲清单全部真渲染),钉的是生产 prompt 而非副本。

用法:
  .venv312/bin/python scripts/judge_group_eval.py --selftest        # 离线:库校验+渲染标记
  .venv312/bin/python scripts/judge_group_eval.py                    # 4B 跑轮(栈在跑时)
  .venv312/bin/python scripts/judge_group_eval.py --models 4b,9b     # 双判官同卷
  .venv312/bin/python scripts/judge_group_eval.py --min-pass 0.9     # 门槛覆盖
结果落 scripts/.judge_group_eval.json;退出码非 0 = 过线率不达标。
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.flow import FlowController  # noqa: E402
from agent_runtime.providers.livekit_plugins import ContextState  # noqa: E402

# ---------------------------------------------------------------------------
# 判官车道:本地 4B 回复线(:1235)与 9B 判定线(:1237)。模型 id 钉默认值——
# /v1/models 是 HF 缓存扫描不可信(W5 教训),真载以进程 argv 为准。
# ---------------------------------------------------------------------------
LANES: dict[str, dict] = {
    "4b": {
        "url": os.environ.get("JUDGE_GROUP_4B_URL", "http://127.0.0.1:1235/v1"),
        "model": os.environ.get(
            "JUDGE_GROUP_4B_MODEL",
            "/Users/halo/.lmstudio/models/avan-ag/Qwen3.5-4B-Uncensored-MLX-4bit",
        ),
        "name": "4B-reply-lane",
    },
    "9b": {
        "url": os.environ.get("JUDGE_GROUP_9B_URL", "http://127.0.0.1:1237/v1"),
        "model": os.environ.get(
            "JUDGE_GROUP_9B_MODEL",
            "/Users/halo/.lmstudio/models/huihui-ai/Huihui-Qwen3.5-9B-abliterated-mlx-4bit",
        ),
        "name": "9B-judge-lane",
    },
}

# ---------------------------------------------------------------------------
# 静态六步模板(镜像生产三语模板形状:身份/通知(say)/平台/赔偿(say)/办理/收尾),
# 档位数字与真实模板同款量纲(100-200/300-600/2-3 倍)——赔偿纪律的禁词才有意义。
# ---------------------------------------------------------------------------
STEPS_JSON = json.dumps(
    [
        {
            "goal": "确认身份",
            "ref": "您好，请问是{姓名}吗？\n如果客户不是本人→请告知方便的时间再联系",
        },
        {
            "goal": "通知货件遗失",
            "say": True,
            "ref": "您好，这里是集运中转仓的客服。这次来电是想通知您：您有一件货件在我们中转仓打包期间遗失了，非常抱歉。",
        },
        {
            "goal": "核实购买平台",
            "ref": "为了核实，请问您是在拼多多、淘宝还是京东买的呢？\n如果客户说不记得→引导他打开订单页面查看\n注意:一次只问一项，语气自然",
        },
        {
            "goal": "告知赔偿方案",
            "say": True,
            "ref": "关于赔偿：按货值分三档，100-200元的按1倍赔付，300-600元的按2倍赔付，最高可以到2-3倍。具体方案会按流程给您确认。",
        },
        {
            "goal": "登记办理并取得联系方式",
            "ref": "接下来为您登记办理。请问您的{联系方式}是多少？我们添加您之后把赔偿方案发给您。\n如果客户不愿意添加→改用短信把方案发给他",
        },
        {
            "goal": "收尾告别",
            "ref": "感谢您的接听，办理结果会尽快通知您，再见。",
        },
    ],
    ensure_ascii=False,
)

# 图模板(jump 族用):一个投诉意图→直达第 4 步。非空 intents 令总览渲染
# 「跳转係常态」规则行(flow.py I3);jump_to 直接驱动,不走图求值。
GRAPH_JSON = json.dumps(
    {
        "version": 1,
        "intents": [{"id": "int_c0ffee01", "label": "投诉", "keywords": ["投诉", "离谱"]}],
        "bindings": [
            {"id": "bnd_c0ffee02", "intent": "int_c0ffee01", "action": "jump_step", "step": 4}
        ],
    },
    ensure_ascii=False,
)

OBJECT_CARD = {
    "display_name": "普哥",
    "phone": "13800006789",
    "tracking_no": "SF12346789",
    "courier": "顺丰",
    "language": "zh",
}

OPENING = "您好，请问是普哥吗？"
NOTIFY_TEXT = (
    "您好，这里是集运中转仓的客服。这次来电是想通知您："
    "您有一件货件在我们中转仓打包期间遗失了，非常抱歉。"
)
COMP_TEXT = (
    "关于赔偿：按货值分三档，100-200元的按1倍赔付，300-600元的按2倍赔付，"
    "最高可以到2-3倍。具体方案会按流程给您确认。"
)
HANDLE_TEXT = "好的，那为您登记办理。请问您的微信是多少？我们添加您之后把赔偿方案发给您。"
PLATFORM_TEXT = "为了核实，您是在拼多多、淘宝还是京东买的呢？"

# 赔偿档位禁词族(补偿纪律 off-round 用):数字串+倍数表述。
_TIER_FORBID = ["100-200", "300-600", "2-3", "1倍", "2倍", "3倍"]
# 平台追问禁词族(质疑轮/跳步轮用):枚举问句素材。
_PLATFORM_FORBID = ["拼多多", "淘宝", "京东", "哪个平台", "在哪买"]

# ---------------------------------------------------------------------------
# 用例库。字段:
#   family     objection|compensation|jump
#   lang       zh|cantonese(进 set_user_language,语言规则一次钉死)
#   flow_step  0-based 当前步;jump 族恒 0(由 jump_to 驱动)
#   jump_to    (jump 族)jump_to 目标步 0-based
#   verdict    last_verdict(question/unclear/objection...)
#   said       预置 said_steps(0-based;say 步已直念的语境)
#   user       客户原话(本轮)
#   last_reply 上一句 AI 回复(入历史+重复锚)
#   forbid     硬断言:归一化回复中任一命中即 FAIL
#   expect_any 硬断言(expect_soft=True 时降为信息位):至少一词命中
#   max_chars  硬断言:归一化长度上限(None=不限)
# ---------------------------------------------------------------------------
BANK: list[dict] = [
    # ---- ① 防诈质疑:安抚+身份在场,不追问平台 ----
    {
        "id": "obj-zh-fraud",
        "family": "objection",
        "lang": "zh",
        "flow_step": 0,
        "verdict": "unclear",
        "user": "你们是不是诈骗电话？我怎么知道你是不是真的？",
        "last_reply": OPENING,
        "forbid": list(_PLATFORM_FORBID),
        "expect_any": ["官方", "回拨", "核实", "身份", "中转仓", "集运", "客服", "真"],
        "max_chars": 120,
    },
    {
        "id": "obj-zh-robot",
        "family": "objection",
        "lang": "zh",
        "flow_step": 1,
        "said": [1],
        "verdict": "question",
        "user": "你是机器人吧？让真人来跟我讲。",
        "last_reply": NOTIFY_TEXT,
        "forbid": list(_PLATFORM_FORBID),
        "expect_any": ["真人", "人工", "我", "客服"],
        "max_chars": 80,
    },
    {
        "id": "obj-canto-fraud",
        "family": "objection",
        "lang": "cantonese",
        "flow_step": 1,
        "said": [1],
        "verdict": "unclear",
        "user": "你係咪呃人㗎？我點知你係唔係真嘅",
        "last_reply": NOTIFY_TEXT,
        "forbid": list(_PLATFORM_FORBID),
        "expect_any": ["官方", "回撥", "回拨", "核实", "核實", "中转仓", "中轉倉", "客服", "唔好意思", "抱歉"],
        "max_chars": 120,
    },
    {
        # 单轮可钉的=简短重申(安抚在场)+不逐字复读上一句;「反复追问平台」的
        # 多轮模式(5 通 10+ 次)归 probe_offscript_soak 真语音 5×10 轮面,单轮
        # 用例钉不住——规则原文允许「随即自然把话题带回办理流程(含确认平台)」。
        "id": "obj-zh-repeat",
        "family": "objection",
        "lang": "zh",
        "flow_step": 2,
        "verdict": "objection",
        "user": "我说了，你就是骗子，我不想再说一遍了。",
        "last_reply": PLATFORM_TEXT,
        "forbid": [],
        "expect_any": ["理解", "明白", "放心", "没关系", "身份", "客服", "官方", "抱歉"],
        "max_chars": 80,
        "max_sim_last": 0.9,
    },
    # ---- ② 赔偿数字纪律:off-round 不报数;正向对照要报得出 ----
    {
        "id": "comp-zh-complain",
        "family": "compensation",
        "lang": "zh",
        "flow_step": 3,
        "said": [3],
        "verdict": "objection",
        "user": "你们什么效率！东西丢了还让我等，我要投诉你们！",
        "last_reply": COMP_TEXT,
        "forbid": list(_TIER_FORBID),
        "expect_any": ["抱歉", "对不起", "理解", "记录", "反馈", "跟进", "投诉", "放心", "马上", "帮您", "核对", "赔"],
        "max_chars": 100,
    },
    {
        "id": "comp-zh-human",
        "family": "compensation",
        "lang": "zh",
        "flow_step": 4,
        "verdict": "objection",
        "user": "别废话了，给我转人工，马上！",
        "last_reply": HANDLE_TEXT,
        "forbid": list(_TIER_FORBID),
        "expect_any": ["转", "人工", "专员", "记录", "登记"],
        "max_chars": 80,
    },
    {
        "id": "comp-zh-supervisor",
        "family": "compensation",
        "lang": "zh",
        "flow_step": 2,
        "verdict": "objection",
        "user": "我不关心什么平台，我要找你们主管说话。",
        "last_reply": PLATFORM_TEXT,
        "forbid": list(_TIER_FORBID),
        "expect_any": ["主管", "专员", "记录", "反馈", "转", "理解", "没关系", "帮您"],
        "max_chars": 80,
    },
    {
        "id": "comp-canto-angry",
        "family": "compensation",
        "lang": "cantonese",
        "flow_step": 4,
        "verdict": "objection",
        "user": "你哋公司好离谱啊，貨唔见咗仲要我自己搞，我要投诉！",
        "last_reply": HANDLE_TEXT,
        "forbid": list(_TIER_FORBID),
        "expect_any": ["唔好意思", "不好意思", "抱歉", "理解", "记录", "跟进", "投诉"],
        "max_chars": 100,
    },
    {
        # 正向对照(防规则过度压制):客户直接问赔偿,回复必须**engage 具体数字面**
        # ——报得出档位,或反问决定档位的货值(半合理路径);禁的是拿 off-round 的
        # 拖延话术(「方案会按流程向您确认」)挡回去=规则把合规应答也压死了。
        "id": "comp-zh-ask",
        "family": "compensation",
        "lang": "zh",
        "flow_step": 3,
        "said": [3],
        "verdict": "question",
        "user": "那到底能赔多少？你直接说个数字。",
        "last_reply": COMP_TEXT,
        "forbid": ["按流程向您确认", "流程给您确认", "按流程确认"],
        "expect_any": ["100-200", "300-600", "倍", "元", "货值", "多少钱", "金额"],
        "max_chars": None,
    },
    # ---- ③ 跳步禁讲:被跳步问句/台词不复现(禁讲硬,落词软) ----
    {
        "id": "jump-zh-complain",
        "family": "jump",
        "lang": "zh",
        "flow_step": 0,
        "jump_to": 4,
        "verdict": "objection",
        "user": "我等了很久了！你们必须马上给我解决，别再问了。",
        "last_reply": OPENING,
        "forbid": _PLATFORM_FORBID + ["打包期间", "100-200", "300-600", "2-3"],
        "expect_any": ["办", "登记", "联系", "添加", "微信", "方案", "赔"],
        "expect_soft": True,
        "max_chars": None,
    },
    {
        "id": "jump-canto-direct",
        "family": "jump",
        "lang": "cantonese",
        "flow_step": 0,
        "jump_to": 4,
        "verdict": "unclear",
        "user": "唔使问咁多啦，直接帮我搞掂佢。",
        "last_reply": OPENING,
        "forbid": _PLATFORM_FORBID + ["打包期间", "100-200", "300-600", "2-3"],
        "expect_any": ["办", "登记", "联系", "添加", "微信", "方案", "赔"],
        "expect_soft": True,
        "max_chars": None,
    },
    {
        "id": "jump-zh-one-skip",
        "family": "jump",
        "lang": "zh",
        "flow_step": 0,
        "jump_to": 2,
        "verdict": "question",
        "user": "好，那你直接问我吧，我赶时间。",
        "last_reply": OPENING,
        # 只跳过第 2 步(通知):平台词=目标步内容**允许**——同词反极性,证禁讲
        # 是「被跳步语境」不是全局词表封禁。禁讲=通知台词复播。
        "forbid": ["打包期间", "遗失"],
        "expect_any": ["拼多多", "淘宝", "京东", "平台", "买", "核实"],
        "expect_soft": True,
        "max_chars": None,
    },
]

JSON_OUT = ROOT / "scripts" / ".judge_group_eval.json"


def _norm(t: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", t or "")


def _sim(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(a=na, b=nb).ratio()


def _build_controller(case: dict) -> FlowController:
    fc = FlowController.from_template(
        {"steps_json": STEPS_JSON, "graph_json": GRAPH_JSON}, OBJECT_CARD
    )
    fc.opening_played = True
    for s in case.get("said") or []:
        fc.said_steps.add(s)
    fc.last_verdict = str(case.get("verdict") or "")
    fc.last_user_text = str(case.get("user") or "")
    if "jump_to" in case:
        fc.jump_to(int(case["jump_to"]))
    else:
        fc.current = int(case.get("flow_step") or 0)
    return fc


def _build_messages(case: dict) -> list[dict]:
    fc = _build_controller(case)
    ctx = ContextState(account_id="judge-group")
    ctx.set_user_language(str(case.get("lang") or "zh"))
    ctx.set_flow(fc.flow_overview(), fc.current_step_text())
    ctx.set_last_reply(str(case.get("last_reply") or ""))
    ctx.set_object_brief("客户：普哥，顺丰集运件，尾号六七八九。")

    system = ctx.render_instruction_prefix() + "\n\n你是话术客服小普，语气亲切自然。"
    tail = ctx.render_context_tail()
    msgs = [{"role": "system", "content": system}]
    msgs.append({"role": "assistant", "content": OPENING})
    msgs.append({"role": "user", "content": "啊。"})
    msgs.append({"role": "assistant", "content": str(case.get("last_reply") or "")})
    msgs.append({"role": "user", "content": str(case.get("user") or "") + "\n\n" + tail})
    return msgs


def _chat(lane: dict, msgs: list[dict]) -> str:
    r = httpx.post(
        f"{lane['url']}/chat/completions",
        json={
            "model": lane["model"],
            "messages": msgs,
            "max_tokens": 200,
            "temperature": 0.3,
        },
        timeout=90,
    )
    r.raise_for_status()
    raw = str(r.json()["choices"][0]["message"]["content"] or "")
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    for stop in ("<|im_end|>", "<|im_start|>"):
        raw = raw.split(stop)[0]
    return raw.strip()


def _grade(case: dict, reply: str) -> dict:
    n = _norm(reply)
    hits = [w for w in (case.get("forbid") or []) if w and w in n]
    forbid_ok = not hits
    expect = case.get("expect_any") or []
    expect_hit = [w for w in expect if w and w in n]
    soft = bool(case.get("expect_soft"))
    expect_ok = bool(expect_hit) or soft
    max_chars = case.get("max_chars")
    len_ok = max_chars is None or len(n) <= int(max_chars)
    # 逐字复读门(重复锚行为的单轮投影):与上一句回复归一化相似度过线=复读。
    max_sim = case.get("max_sim_last")
    sim_last = _sim(reply, str(case.get("last_reply") or ""))
    sim_ok = max_sim is None or sim_last < float(max_sim)
    ok = forbid_ok and expect_ok and len_ok and sim_ok
    return {
        "ok": ok,
        "forbid_hit": hits,
        "expect_hit": expect_hit,
        "expect_soft": soft,
        "chars": len(n),
        "len_ok": len_ok,
        "sim_last": round(sim_last, 3),
        "sim_ok": sim_ok,
    }


def selftest() -> int:
    """离线腿:库 schema + 生产渲染标记(零 HTTP,CI 无模型可跑)。"""
    errs: list[str] = []
    ids = [c.get("id") for c in BANK]
    if len(ids) != len(set(ids)):
        errs.append("case id 重复")
    fams = {"objection", "compensation", "jump"}
    for c in BANK:
        if c.get("family") not in fams:
            errs.append(f"{c.get('id')}: family 非法")
        if not (c.get("forbid") or c.get("expect_any")):
            errs.append(f"{c.get('id')}: forbid 与 expect 双空=空断言")
        if not str(c.get("user") or "").strip():
            errs.append(f"{c.get('id')}: user 空")
    # 渲染标记①:共享应答规则恒在总览(静态前缀)——objection/compensation 族的
    # 断言锚。改 _SHARED_RESPONSE_RULES 文案时这里会红,同步改锚词。
    sys_msgs = _build_messages(BANK[0])
    system = sys_msgs[0]["content"]
    for marker in ("【身份与来电质疑】", "【赔偿数字纪律】", "不得借回应质疑反复追问平台"):
        if marker not in system:
            errs.append(f"system 缺共享规则标记: {marker}")
    # 渲染标记②:赔偿正向对照——say 步已念语境渲染分支提示而非底稿重发。
    ctl = _build_controller(next(c for c in BANK if c["id"] == "comp-zh-ask"))
    tail_ask = _build_messages(next(c for c in BANK if c["id"] == "comp-zh-ask"))[-1]["content"]
    if "【通知已念】" not in tail_ask:
        errs.append("comp-zh-ask 尾部缺【通知已念】(say 步已念语境错位)")
    if ctl.current != 3 or 3 not in ctl.said_steps:
        errs.append("comp-zh-ask 步位/said 语境错位")
    # 渲染标记③:跳步首轮——【跳转进入】点名被跳步 +【禁讲清单】照录被跳步原话
    # +总览「跳转係常态」行(图模板专用)。j1: 0→4 跳过第 2、3、4 步。
    j1 = next(c for c in BANK if c["id"] == "jump-zh-complain")
    fc = _build_controller(j1)
    if fc._jump_skipped != [2, 3, 4]:
        errs.append(f"jump 记账错位: _jump_skipped={fc._jump_skipped}")
    overview = fc.flow_overview()
    if "直接跳入后面某一步" not in overview:
        errs.append("总览缺「跳转係常态」图模板规则行")
    tail_j = _build_messages(j1)[-1]["content"]
    for marker in ("【跳转进入】", "【禁讲清单】", "拼多多、淘宝还是京东"):
        if marker not in tail_j:
            errs.append(f"jump 尾部缺标记: {marker}")
    # 渲染标记④:同词反极性对照(j3)——平台词在禁讲清单里出现(被跳步=第2步?
    # 不,j3 只跳第 2 步,平台步=目标步,禁讲清单只含通知台词)。
    j3 = next(c for c in BANK if c["id"] == "jump-zh-one-skip")
    fc3 = _build_controller(j3)
    if fc3._jump_skipped != [2]:
        errs.append(f"j3 跳步记账错位: {fc3._jump_skipped}")
    tail_j3 = _build_messages(j3)[-1]["content"]
    if "【禁讲清单】" not in tail_j3 or "打包期间" not in tail_j3:
        errs.append("j3 禁讲清单未含被跳步通知台词")
    for e in errs:
        print(f"SELFTEST FAIL: {e}")
    if errs:
        return 1
    print(f"SELFTEST PASS ({len(BANK)} cases; objection/compensation/jump 渲染标记全在)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="离线腿:库校验+渲染标记,零 HTTP")
    ap.add_argument("--models", default="4b", help="判官车道,逗号分隔(4b|9b)")
    ap.add_argument("--min-pass", type=float, default=0.85, help="过线率门槛")
    ap.add_argument("--json-out", default=str(JSON_OUT))
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    lanes = []
    for key in [m.strip().lower() for m in args.models.split(",") if m.strip()]:
        if key not in LANES:
            print(f"未知车道: {key}(可用: {','.join(LANES)})")
            return 2
        lanes.append((key, LANES[key]))

    results: list[dict] = []
    worst = 1.0
    for key, lane in lanes:
        passed = 0
        total = 0
        for case in BANK:
            total += 1
            try:
                reply = _chat(lane, _build_messages(case))
            except Exception as exc:  # noqa: BLE001 - 车道失联=该轮 FAIL 不炸整轮
                g = {
                    "ok": False,
                    "forbid_hit": [f"lane_error:{type(exc).__name__}"],
                    "expect_hit": [],
                    "expect_soft": False,
                    "chars": 0,
                    "len_ok": False,
                    "sim_last": 0.0,
                    "sim_ok": False,
                }
                reply = ""
            else:
                g = _grade(case, reply)
            passed += 1 if g["ok"] else 0
            results.append({"id": case["id"], "family": case["family"], "lane": key, "reply": reply, **g})
            print(
                f"[{'PASS' if g['ok'] else 'FAIL'}] {case['id']} @{lane['name']}"
                + (f" forbid_hit={g['forbid_hit']}" if g["forbid_hit"] else "")
                + (f" chars={g['chars']}>{case.get('max_chars')}" if not g["len_ok"] else "")
                + (f" sim_last={g['sim_last']}" if not g.get("sim_ok", True) else "")
                + (f" expect_miss={case.get('expect_any')}" if not g['ok'] and g.get("expect_hit") == [] and not g.get("expect_soft") else "")
                + f"\n      reply={reply[:110]!r}"
            )
        rate = passed / total if total else 0.0
        worst = min(worst, rate)
        print(f"== {lane['name']}: {passed}/{total} ({rate:.0%})")
    Path(args.json_out).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    verdict = "ALL PASS" if worst >= args.min_pass else f"BELOW {args.min_pass:.0%}"
    print(f"{verdict} (worst {worst:.0%}) → {args.json_out}")
    return 0 if worst >= args.min_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
