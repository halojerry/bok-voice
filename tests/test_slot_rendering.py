"""D1 槽位化 actor 渲染层：20 轮粤语模拟（零漂移 / 尺寸 / 协议零泄漏 / prewarm 形状）。

零漂移证明方式：用真模板 fixture（scripts/migrate_templates_8step_0913.py 的
cantonese febeeeebac97，8 步）跑 20 轮模拟，序列化与真 provider 一致（list content
按 "\n" 连接——livekit _provider_format/openai._to_chat_item），把**改动前代码**
（HEAD d7b7ed8）跑出的逐轮 sha256 冻结为本文件 GOLDEN_SHA256；新代码闸关
（slot_mode 缺省 False）重跑必须 20/20 逐字节相等。

尺寸实测（同一 20 轮场景，含 2 条客户事实+WA 捕获+对象档案+粤语 8 步模板）：
- 闸关（现状路径）：R2=3475c、R15=9184c（截断前峰值 R18=9873c）；
- 闸开（槽位化）：R2=543c、R15=2448c（每轮 +~160c；截断前峰值 R18=2956c，
  仍为 legacy 同期 30%；截断后 R19=680c）
  → R2 −84.4%、R15 −73.3%，满足目标 R2≤1200c / R15≤2500c。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "agent"))
sys.path.insert(0, str(_ROOT / "scripts"))

from livekit.agents.llm import ChatContext  # noqa: E402

from agent_runtime.flow import FlowController  # noqa: E402
from agent_runtime.providers.livekit_plugins import ContextAwareLLM, ContextState  # noqa: E402
from agent_runtime.slot_actor import build_slot_system  # noqa: E402

# 改动前（HEAD d7b7ed8）legacy 路径 20 轮逐轮请求序列化 sha256 + 字符数（golden）。
GOLDEN_SHA256 = [
    "ce477470d2a1ea43a6926991c14294c12b8be9624abbb51344ae62ed826ba013",
    "2aa7f00597da1721433e6cf860472a09ab2b81e633957c87f38006138b06f02b",
    "7dc98ab1031552e0a08f0ffe7e124207a700f067252f42c4c7bb168de0b5bcba",
    "241bb1573efa1305de1ffad3c783c58b8511a21b38db538f813c5436d1e07fd7",
    "f36d70e978e4b258698958f13939aca318cc5eb0896702db78882dd54aea0021",
    "6d12bd14e944adf7cfe23bd6f822feb6d777c080ae148217411d618e8174f07b",
    "b49428c57213172c07498fe8b1cbe7fa034bb27a67df8054f41cb61595d58f47",
    "63cb03c80d31ec1472648598d7f6c21dc2e5028b6c2cb3cc63f9abc8a76f82cb",
    "673d76c26c39231aea3bbc7d5b63ed70ac28a9df1645ebbede989b3d8ae6f77a",
    "a39158e3054713aa5512255acadb58dd1cb960a9feacc80774e6afcf43bac211",
    "4dff6f7eefe325223ebe7069e873fc5a2ef9704502e37d3817d0b31f35a4a1bd",
    "233029b6df11c7a5285f61a05186f2dc3dd0c400ebfa22845d9f66ecce39eeac",
    "6d5668997ea0962316934937c1e25ca64b4277c73129c16620d0af6e99c41607",
    "192f20e9517728d7d90c88b914e694611f630a923cd37135d3ecfdae174fa953",
    "5059464dc422023bae514036ca181d514b2d780cfce9247e78d84ec23dae6a9f",
    "a00ef99a831db4569a5060559a1ed8741c5893515907856280586e318625e742",
    "a1714e10ca0b19d52e6ca943205189f4c13f359b94f1faded34eedf7e0ad6987",
    "45c1790ad0a435229c5524268ad0f71b3c0e44275b41026e5100f020f93bf401",
    "0466afe234ac225076f9fcb847315d69966886d76eb070c0b8c3b300b296f08c",
    "b9608be21601f64fdf830bbeb50e94b2268868541fcb9b5eec5f1c24681ba56b",
]
GOLDEN_SIZES = [
    3350,
    3475,
    4136,
    4359,
    5001,
    5245,
    5857,
    6046,
    6287,
    6949,
    7708,
    7967,
    8413,
    8703,
    8957,
    9181,
    9421,
    9662,
    3214,
    3192,
]

_OBJECT = {
    "display_name": "陳大文",
    "language": "cantonese",
    "tracking_no": "SF1234567890",
    "courier": "順豐",
    "address": "香港觀塘",
    "background": "拼多多買嘅衫",
    "notes": "想快啲搞掂",
}
_PERSONA = {"name": "小嵐", "company": "集運中轉倉", "language": "cantonese", "tone": "親切"}
_BRIEF = "拼多多買嘅衫\n想快啲搞掂"
_GREETING = "你好，請問係陳大文嗎？"
_FACTS = "已知客户信息:姓名:陳大文 快递单号:七八九零 物流公司:順豐 收货地址:香港觀塘。信息缺失时向客户确认,不要编造。"

# (utt, verdict, advance, fact, wa, reply)
_ROUNDS = [
    ("係呀，我係陳大文", "confirm", True, None, None, "好嘅，陳生你好。"),
    ("唔記得買咗咩", "unclear", False, None, None, "唔緊要，我幫你核對返。"),
    ("係拼多多買嘅", "unclear", True, "平台 拼多多", None, "收到，拼多多。"),
    ("點解賠咁少嘅", "question", False, None, None, "係按條例同標準計嘅。"),
    ("好呀，接受", "confirm", True, None, None, "好，咁我哋安排辦理。"),
    ("你哋係咪呃人", "objection", False, None, None, "我哋係按登記資料來電。"),
    ("好，明白", "confirm", True, None, None, "好，專員會跟進。"),
    ("我想投訴", "objection", False, None, None, "明白你嘅不滿，我幫你記錄。"),
    ("咁幾時有結果", "question", False, None, None, "專員加你之後好快有。"),
    ("號碼係六五一二三四五六", "confirm", True, "WhatsApp 六五一二三四五六", "65123456", "收到，六五一二三四五六。"),
    ("好嘅收到", "confirm", True, None, None, "好，記住留意通知。"),
    ("講多次個口令", "repeat", False, None, None, "係「貨件遺失申請理賠」。"),
    ("知道喇", "confirm", True, None, None, "好，一分鐘內專員加你。"),
    ("仲要等幾耐", "question", False, None, None, "大約三至五個工作日。"),
    ("好呀，唔該", "confirm", True, None, None, "好，專員會跟進辦理。"),
    ("點樣收到錢", "question", False, None, None, "經速遞理賠流程入帳。"),
    ("我唔係好明", "unclear", False, None, None, "唔使擔心，我慢慢講。"),
    ("仲有咩要注意", "question", False, None, None, "記得回覆登記口令。"),
    ("唔該晒你", "confirm", False, None, None, "唔客氣。"),
    ("再見", "farewell", False, None, None, "再見，祝你一切順利。"),
]

_DIALECT_MARKS = "唔係嘅咗喺嚟嘢冇啲嗰㗎乜睇攞"

# 槽位请求里绝不准出现的旧协议标记（D1 规格「绝不包含」清单 + 旧前缀族）。
_FORBIDDEN = (
    "【话术流程总览",
    "【步骤纪律】",
    "【应答准则】",
    "【回复节奏】",
    "【回复长度】",
    "【重复控制】",
    "【客户没听清",
    "【客户在提问】",
    "【客户回应不明确】",
    "【客户有疑虑】",
    "【通知已念】",
    "【新一步】",
    "【开场已念】",
    "【跳转进入】",
    "【禁讲清单】",
    "【本通对话记忆】",
    "【身份与来电质疑】",
    "【赔偿数字纪律】",
    "【核对/引导资料后备",
    "【现在这一步】",
    "【对象档案】",
    "对话按 8 步流程推进",
)


class _CaptureInner:
    """抓每次 chat() 收到的 chat_ctx items（序列化/真 items 两用）。"""

    def __init__(self):
        self.captured: list[list] = []

    def on(self, *a, **k):  # _bind_metrics_forward 需要
        pass

    async def chat(self, *, chat_ctx, **kw):
        self.captured.append(list(chat_ctx.items))
        return "ok"


def serialize(items) -> str:
    """与真 provider 同形的文本化（list content 按 "\\n" 连接）。"""
    lines = []
    for it in items:
        c = getattr(it, "content", "")
        if isinstance(c, str):
            text = c
        else:
            text = "\n".join(str(x) for x in (c or []))
        lines.append(f"{getattr(it, 'role', '')}\x1f{text}")
    return "\n".join(lines)


def _template_steps():
    import migrate_templates_8step_0913 as mig

    return mig.TEMPLATES["febeeeebac97"]["steps"]


def drive(slot_mode: bool = False, card: str = "", rounds=None):
    """20 轮粤语模拟：每轮=1 次 LLM 请求（直念步按已念账本），返回逐轮 items。

    真装配序：装配点 push（agent.py 3255-3260）→ 开场白直念后 push（7880-7933）；
    每轮 verdict/推进/fact/WA 更新后 push（7 个 `_push_flow_state` 点等价）。
    """
    steps_json = json.dumps(_template_steps(), ensure_ascii=False)
    fc = FlowController.from_template({"steps_json": steps_json}, _OBJECT)
    fc.said_steps = {i for i, s in enumerate(fc.steps) if s.say}
    ctx = ContextState(account_id="acc-001")
    ctx.slot_mode = slot_mode
    if slot_mode:
        ctx.set_slot_system(card)
    ctx.set_user_language("cantonese")
    ctx.set_object_brief(_BRIEF)
    inner = _CaptureInner()
    llmw = ContextAwareLLM(inner=inner, context_state=ctx)
    history = [("system", "人設base"), ("assistant", _GREETING)]

    def push(assembly: bool = False):
        if slot_mode:
            ctx.set_slot_step(fc.slot_step_view())
        elif assembly:
            ctx.set_flow(fc.flow_overview(), fc.current_step_text())
        else:
            ctx.set_flow_current(fc.current_step_text())

    push(assembly=True)
    fc.opening_played = True
    push()
    # 开场白 assistant item 落账时钩子写锚（_on_item_for_context→set_last_reply）。
    ctx.set_last_reply(_GREETING)

    def run_chat():
        cc = ChatContext()
        for role, text in history:
            cc.add_message(role=role, content=text)
        asyncio.run(llmw.chat(chat_ctx=cc))

    per_request = []
    for utt, verdict, adv, fact, wa, reply in (rounds or _ROUNDS):
        history.append(("user", utt))
        fc.last_user_text = utt
        fc.last_verdict = verdict
        if wa:
            ctx.set_whatsapp_note(wa)
        if fact:
            ctx.add_call_fact(fact)
        if verdict in ("refuse", "farewell"):
            if not fc.closing:
                fc.enter_closing()
        elif adv:
            fc.advance()
        push()
        run_chat()
        history.append(("assistant", reply))
        # assistant item 落账 → 钩子写锚（_on_item_for_context→set_last_reply）。
        ctx.set_last_reply(reply)
        per_request.append(inner.captured[-1])
    return per_request


def _slot_card() -> str:
    return build_slot_system(persona=_PERSONA, template=None, facts=_FACTS, language="cantonese")


# ---------------------------------------------------------------- 零漂移（golden）
def test_slot_off_legacy_bytes_match_pre_change_golden():
    """闸关（slot_mode 缺省 False）20 轮逐字节同基线——零漂移铁证。

    基线重录(2026-10-02 批3 合流):原 GOLDEN 基于 fp 线 9-10-01 旧快照,main
    自那以后合法演进(尾部节食/记忆帽/judge 路由——off 尺寸差 −227/+16 与
    之吻合);slot 增量经守卫完备性核验(off 可达面=字段初始化+方法定义,零
    渲染副作用,消费点全锁 if slot_mode 内),off 路径无 slot 泄漏。

    比对面=逐轮请求序列化 sha256（GOLDEN 由 HEAD d7b7ed8 改动前代码实跑生成）
    + 逐轮字符数（R2=3475、R15=9184、峰值 R18=9873）。
    """
    reqs = drive(slot_mode=False)
    assert len(reqs) == 20
    serialized = [serialize(items) for items in reqs]
    assert [len(s) for s in serialized] == GOLDEN_SIZES
    got = [hashlib.sha256(s.encode("utf-8")).hexdigest() for s in serialized]
    assert got == GOLDEN_SHA256, "闸关路径与改动前代码逐字节漂移"
    # 旧协议族确实在场（证明跑的是完整现状路径，不是空转）。
    assert "【话术流程总览" in serialized[-3]
    assert "【步骤纪律】" in serialized[-3]
    assert "【身份与来电质疑】" in serialized[-3]


# ---------------------------------------------------------------- 尺寸目标
def test_slot_on_r2_r15_within_target():
    """闸开 R2≤1200c / R15≤2500c（实测 R2=543c / R15=2448c，见模块 docstring）。"""
    reqs = drive(slot_mode=True, card=_slot_card())
    sizes = [len(serialize(items)) for items in reqs]
    assert len(sizes) == 20
    assert sizes[1] <= 1200, sizes[1]  # 实测 543c
    assert sizes[14] <= 2500, sizes[14]  # 实测 2448c
    # 收益对照：同场景 legacy R2=3475/R15=9184 → 槽位化后大幅下降。
    assert sizes[1] * 3 < GOLDEN_SIZES[1]
    assert sizes[14] * 3 < GOLDEN_SIZES[14]


# ---------------------------------------------------------------- 协议零泄漏
def test_slot_requests_contain_no_legacy_protocol():
    """槽位请求（system+全部冻结历史）不含任何旧协议标记（规格「绝不包含」清单）。"""
    reqs = drive(slot_mode=True, card=_slot_card())
    text = "\n".join(serialize(items) for items in reqs)
    leaks = sorted({mark for mark in _FORBIDDEN if mark in text})
    assert not leaks, f"槽位请求泄漏旧协议: {leaks}"
    # 该在的在场（当前步槽位/锚/事实槽），防止「空跑也算过」。
    assert "【当前步】第" in text
    assert "【你上一句】" in text
    assert "【通话中客户已讲】" in text


def test_slot_system_message_is_role_card_only():
    """system 段==角色卡本身（不并人设 base/instructions；旧前缀族整体退场）。"""
    card = _slot_card()
    reqs = drive(slot_mode=True, card=card)
    items = reqs[0]
    sys_items = [it for it in items if getattr(it, "role", "") == "system"]
    assert len(sys_items) == 1
    content = sys_items[0].content
    assert content == [card], "slot system 必须等于 [角色卡]（不并 head）"
    assert "人設base" not in serialize(items)


# ---------------------------------------------------------------- 语言纯度（zh）
def test_slot_zh_request_language_purity():
    """zh 通话（zh 角色卡+zh 数据）全请求零粤语特征字；粤语块仅 cantonese 渲染。"""
    steps = [
        {"goal": "确认身份", "ref": "请问是陈先生吗？"},
        {"goal": "说明方案", "ref": "我们会按标准给您赔偿。"},
    ]
    fc = FlowController.from_template({"steps_json": json.dumps(steps, ensure_ascii=False)}, None)
    ctx = ContextState(account_id="acc-001")
    ctx.slot_mode = True
    ctx.set_slot_system(
        build_slot_system(
            persona={"name": "小蓝", "company": "集运中转仓", "language": "zh"},
            template=None,
            facts="",
            language="zh",
        )
    )
    ctx.set_user_language("zh")
    inner = _CaptureInner()
    llmw = ContextAwareLLM(inner=inner, context_state=ctx)
    history = [("system", "人設base"), ("assistant", "您好，请问是陈先生吗？")]
    ctx.set_slot_step(fc.slot_step_view())
    for utt, verdict in (("是我", "confirm"), ("什么时候到账", "question")):
        history.append(("user", utt))
        fc.last_user_text = utt
        fc.last_verdict = verdict
        fc.advance()
        ctx.set_slot_step(fc.slot_step_view())
        cc = ChatContext()
        for role, text in history:
            cc.add_message(role=role, content=text)
        asyncio.run(llmw.chat(chat_ctx=cc))
        history.append(("assistant", "好的，我这边帮您核对。"))
    for items in inner.captured:
        text = serialize(items)
        bad = sorted({ch for ch in _DIALECT_MARKS if ch in text})
        assert not bad, f"zh 槽位请求含粤语特征字: {bad}"
        assert "港式粵語" not in text and "速遞" not in text
