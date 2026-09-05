"""对话流程控制:话术模板分步推进 + 对象变量渲染。

职责划分(agent 编排层):
- 通话建立:把对象变量(姓名/单号/物流公司)+ 模板步骤渲染成"流程蓝图"。
- 每轮用户说完:规则判定客户状态与是否推进,维护 current_step。
- 把"当前这一步"的 goal/ref 注入本轮 system,让 LLM 只回应当前步。
绝不把整份话术当逐字稿塞给 LLM/TTS。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# 客户状态判定结果
CONFIRM = "confirm"       # 确认/认可当前步 → 可推进下一步
OBJECTION = "objection"   # 有异议/否认/不配合 → 停留本步应对
QUESTION = "question"     # 提问/要解释 → 停留本步解答
OFFTOPIC = "offtopic"     # 明显无关/要挂断/怀疑诈骗 → 不强推
UNCLEAR = "unclear"       # 判断不清 → 停留,自然应对
REFUSE = "refuse"         # 明确拒绝/告别/要收线 → 收尾态:一句礼貌再见后结束通话


@dataclass
class FlowStep:
    goal: str = ""
    ref: str = ""


def parse_steps(steps_json: str) -> list[FlowStep]:
    """解析模板 steps_json 为步骤列表(兼容 dict 或空)。"""
    if not steps_json:
        return []
    try:
        arr = json.loads(steps_json)
    except Exception:
        return []
    if not isinstance(arr, list):
        return []
    out: list[FlowStep] = []
    for s in arr:
        if isinstance(s, dict):
            out.append(FlowStep(goal=str(s.get("goal") or ""), ref=str(s.get("ref") or "")))
        elif isinstance(s, str):
            out.append(FlowStep(goal="", ref=s))
    return [s for s in out if s.goal.strip() or s.ref.strip()]


# 粤语数字逐个读法:0 读「零」;1-9 对应汉字。数字串/单号要逐个读,
# 不要按多位数值读,所以这里做「字符级」映射(7890 → 七八九零),而非数值转换。
_CANTONESE_DIGITS = {
    "0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
    "5": "五", "6": "六", "7": "七", "8": "八", "9": "九",
}


def digits_to_cantonese(text: str) -> str:
    """把字符串里的数字逐位转成粤语汉字(适合单号/电话/尾号逐字读)。

    只转「纯数字串」或数字与字母混排里的数字部分(如 SF7890 → SF七八九零);
    不去动汉字/其它内容。金额/日期等需按位值读的场景不适用(调用方按需用)。
    """
    if not text:
        return text
    return "".join(_CANTONESE_DIGITS.get(ch, ch) for ch in str(text))


# 四段 → 步骤的 goal 标签(用于把旧式四段模板转成分步,逐步推进不念稿)
_LEGACY_STEP_GOALS = {
    "opening": "开场:自报家门,说明来意,向客户确认身份/包裹",
    "core": "核心:向客户说明处理方案/关键信息,争取客户认可",
    "objection": "异议:针对客户疑虑/拒绝,解释并稳住客户",
    "closing": "收尾:确认客户意愿,礼貌收尾,不强推",
}


def template_to_steps(template: dict | None) -> list[FlowStep]:
    """把模板转成对话步骤。

    steps_json 优先(分步蓝图);若为空但有旧式四段(opening/core/objection/closing),
    自动转成分步流程——否则四段整段塞给 LLM 会被当逐字稿一口气念完
    (实测:开场把确认身份→一赔二→重新下单→拜拜全讲了)。
    """
    steps = parse_steps((template or {}).get("steps_json", ""))
    if steps:
        return steps
    tpl = template or {}
    out: list[FlowStep] = []
    for key, goal in _LEGACY_STEP_GOALS.items():
        text = str(tpl.get(key) or "").strip()
        if text:
            out.append(FlowStep(goal=goal, ref=text))
    return out


def object_vars(object_card: dict | None) -> dict[str, str]:
    """从对象卡提取话术变量;缺的留空(由上层标"待确认",不编造)。

    单号/尾号/电话的数字逐位转成粤语汉字(7890 → 七八九零):无论这些文字最后
    进 LLM 还是被直接念,都不会被 TTS 读成普通话数字/错误发音。
    """
    oc = object_card or {}
    name = str(oc.get("display_name") or "").strip()
    tracking = str(oc.get("tracking_no") or "").strip()
    courier = str(oc.get("courier") or "").strip()
    address = str(oc.get("address") or "").strip()
    tail = tracking[-4:] if len(tracking) >= 4 else tracking
    phone = str(oc.get("phone") or "").strip()
    return {
        "姓名": name,
        "名字": name,
        "快递单号": digits_to_cantonese(tracking),
        "快递尾号": digits_to_cantonese(tail),
        "物流公司": courier,
        "快递公司": courier,
        "收货地址": address,
        "地址": address,
        "电话": digits_to_cantonese(phone),
    }


def render_template_text(text: str, vars_map: dict[str, str]) -> str:
    """替换 {变量} 占位;缺的变量保留原占位(LLM 会向客户询问而非编造)。"""
    if not text:
        return text
    def _repl(m: "re.Match[str]") -> str:
        key = m.group(1).strip()
        val = vars_map.get(key, "")
        return val if val else m.group(0)
    return re.sub(r"\{([^{}]+)\}", _repl, text)


def facts_line(object_card: dict | None) -> str:
    """生成已知事实段:姓名/单号/物流公司/收货地址,缺的标'待确认'。"""
    v = object_vars(object_card)
    parts = []
    parts.append(f"姓名:{v['姓名'] or '待确认'}")
    parts.append(f"快递单号:{v['快递单号'] or '待确认'}")
    parts.append(f"物流公司:{v['物流公司'] or '待确认'}")
    parts.append(f"收货地址:{v['收货地址'] or '待确认'}")
    return "已知客户信息:" + " ".join(parts) + "。信息缺失时向客户确认,不要编造。"


# ---- 每轮推进判定(规则为主,轻量;避免每轮额外一次 LLM 调用) ----

# 客户"确认/认可当前步"的信号(按语言)
_CONFIRM_RE = re.compile(
    r"(是我的|是我|是的|对的|对|没错|嗯嗯|嗯好|好啊|可以|好的|没问题|系啊|係啊|系我|係我|系既|係既|"
    r"是我的快递|对呀|对阿|是的呀|嗯|系|係|yes|yeah|correct|right)",
    re.IGNORECASE,
)
# 否认/不是本人/没买过 → 异议
_DENY_RE = re.compile(
    r"(不是|没有|不是我|我没|唔系|唔係|唔关我事|不关我事|没买过|冇买过|没有买过|骗子|诈骗|报警|"
    r"no|not me|wrong|never)",
    re.IGNORECASE,
)
# 提问/要解释 → question(在 confirm 之后判,避免"是吗"被当确认)
_QUESTION_RE = re.compile(
    r"(\?|？|怎么|如何|为啥|为什么|几时|几耐|多久|边度|哪里|点解|为什么赔|怎么赔|要多久|真假|"
    r"what|how|why|when|where|really)",
    re.IGNORECASE,
)
# 强异议/不想继续/威胁 → objection/offtopic
_HANGUP_RE = re.compile(r"(不用了|不需要|别再打|别打|不要打|挂|拉黑|投诉|再见|拜拜|唔使|唔使啦|"
    r"stop|don't call|leave me|bye)", re.IGNORECASE)
# 明确拒绝/婉拒(唔需要/唔办/我唔要/拒绝…) → REFUSE:直接收尾话术+结束通话,唔停留挽留。
# 注意社交软语「唔使担心/唔使客气」等唔算拒绝(见 _REFUSE_SOFT_GUARD_RE)。
_REFUSE_RE = re.compile(
    r"(唔需要|唔辦|唔办|唔好办|唔好辦|我唔要|唔要啦|唔要喇|唔要嘎|唔要咗|唔要了|不要啦|不要喇|不要了|"
    r"唔使喇|唔使啦|唔使再打|唔好再打|唔好再嚟|唔好再來|别再打|別再打|唔好搵我|唔好煩我|唔好骚扰|"
    r"拒绝|拒絕|收线啦|收線啦|收工啦|唔好搞我)",
    re.IGNORECASE,
)
# 「唔使X」嘅社交关心/客套短语——唔係拒绝,唔好当 REFUSE(旧实测:「唔使担心」曾误判)。
_REFUSE_SOFT_GUARD_RE = re.compile(r"唔使(担心|擔心|客气|客氣|怕|緊張|紧张|多心|挂住|掛住)")
# 多字「强确认」:疑问句里出现都算确认(「係我,然後呢?」);单字「係/好/嗯/对/可以」
# 喺疑问句(「係咩?」「可以點做?」)唔当确认,靠 _CONFIRM_RE 只喺非疑问句时兜底。
_STRONG_AFFIRM_RE = re.compile(
    r"(是我的|係我|係你|係我哋|係你哋|系我|係我嘅|係既|係嘅|係呀|係架|系呀|"
    r"没错|沒錯|对呀|對呀|对啊|是的|嗯好|好的|可以可以|没問題|冇問題|冇問題啊|"
    r"yes|yeah|correct|right|确认|確認|agree)",
    re.IGNORECASE,
)


def _digit_normalize(text: str) -> str:
    """把汉字数字/空白归一成可比对串:七八九零 → 7890(繁体简体数字都收)。"""
    table = {"零": "0", "一": "1", "二": "2", "三": "3", "四": "4",
             "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
             "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
             "５": "5", "６": "6", "７": "7", "８": "8", "９": "9"}
    return "".join(table.get(ch, "" if ch.isspace() else ch) for ch in str(text))


def _matches_known_fact(user_text: str, facts: dict | None) -> bool:
    """客户话入面覆述啱关键资料(姓名/尾号/完整单号)→ 当确认,唔使净靠社交词。

    只取「强身份锚点」:姓名、快递尾号、完整单号。物流公司太弱(agent 开场先讲咗)唔用。
    否认句由上层 _DENY_RE 先行挡走,唔会喺度误判。
    """
    if not facts:
        return False
    t = _digit_normalize(user_text)
    for key, min_len in (("姓名", 2), ("快递尾号", 2)):
        val = str(facts.get(key) or "").strip()
        if len(val) >= min_len and _digit_normalize(val) in t:
            return True
    full = str(facts.get("快递单号") or "").strip()
    if len(full) >= 6 and _digit_normalize(full) in t:
        return True
    return False


# ---- WhatsApp 对接触发侦测 ----
# 客户喺通话俾出 WhatsApp(读出号码 / 应承加专员)→ 上报 control-plane → 操作台爆闪横幅。
_WHATSAPP_STEP_HINTS = ("whatsapp", "微信", "加專員", "加我哋", "工作人員", "聯絡方式", "帳號", "加你", "加我")
# 冇 WhatsApp / 唔想加 → 唔触发 offered
_WHATSAPP_DECLINE = re.compile(r"(冇whatsapp|冇用whatsapp|無whatsapp|唔用whatsapp|冇微信|無微信|唔用微信|"
    r"唔方便加|冇得加|無得加|唔識加|唔加|唔想加|冇電話|無電話)", re.IGNORECASE)
# offered 两路:①明確叫加(你加我/我加/加咗/搵我/發俾我);②纯短应承(成句好短,唔係答其他内容)。
_WHATSAPP_ADD_VERB = re.compile(r"(你(哋|地)?加我|加我|我加咗|我加|加咗|加啦|加喇|搵我|你(哋|地)?發俾我|發俾我|快啲加|嚟加)", re.IGNORECASE)
# 俾號語境:「我俾個號你 / 俾號碼你」→ 唔好淨靠 號碼/号码 字眼(「俾個號」冇「碼」都會走漏)。
_WHATSAPP_GIVE_NUM_RE = re.compile(r"俾.{0,6}[號号]")
# 明確自報:「我WhatsApp(就)係/是 <數字>」→ 號碼即 WhatsApp,就算撞已知電話/單號都當佢自報。
# 系詞後必須直接跟數字(漢字/阿拉伯皆可):「WhatsApp 就是绑定…」唔算(嗰係綁定來電,交 caller_bound)。
_WHATSAPP_NUM_ANNOUNCE_RE = re.compile(
    r"(whatsapp|whats app|wa|微信)\s*(就?係|就是|是)\s*[0-9一二三四五六七八九零]", re.IGNORECASE
)
_WHATSAPP_ACK_WORDS = ("好呀", "好丫", "好既", "好嘅", "好阿", "可以", "冇問題", "沒問題", "没问题", "無問題", "都得", "得呀", "嗯", "好", "得", "ok", "okay", "嗯嗯", "好呀好呀", "可以可以", "好嘅好嘅")
# 句子提及其他话题(单号/电话/自己身份/地址/订单)→ 唔係应承加,唔触发 offered
_WHATSAPP_TOPIC_MARK = re.compile(r"(單號|单号|號碼|号码|電話|电话|地址|訂單|订单|貨件|货件|貨|件野|我係|我是|包裹|速遞|物流)", re.IGNORECASE)
# captured_implicit:WhatsApp 步度客戶話「WhatsApp 就係綁定呢個來電/呢個號碼/我而家呢個電話」——
# 號碼喺系統(來電/對象電話)度,唔使等佢逐個讀出 8-13 位;由 caller 攞對象電話上報 captured。
_WHATSAPP_CALLER_BOUND = re.compile(
    r"(whatsapp|微信)[^\n。！？!?；;]{0,15}(就係|就是|係|是|绑定|綁定|用)[^\n。！？!?]{0,10}(來電|来电|呢個(?:電話|號碼|号|號)?|這個(?:電話|號碼|号|號)?|我而家|我電話|我手機|手機號|手机号|電話號碼|电话号码)"
    r"|(就係|就是|就用|用)(我而家|我)(?:呢個|这个|呢个)?(?:來電|来电)?(?:電話|电话|手機|手机|號碼|号码)(?:號|号|碼)?",
    re.IGNORECASE,
)


def _is_pure_ack(text: str) -> bool:
    """句子去標點後淨係應承詞疊加(好/可以/嗯/ok)先算純短應承。"""
    cleaned = re.sub(r"[\s,，、.!！。?？~～]+", "", (text or "").lower())
    while cleaned:
        hit = False
        for w in _WHATSAPP_ACK_WORDS:
            if cleaned.startswith(w):
                cleaned = cleaned[len(w):]
                hit = True
                break
        if not hit:
            return False
    return True


# ---- 規則級「必定推進」override(唔靠 LLM judge,防止卡死) ----
# 網購平台名:話術引導核實步(問「喺邊個平台買」)嘅關鍵答案——客戶答到就夠,唔使等確認。
_PLATFORM_RE = re.compile(
    r"(拼多多|淘宝|淘寶|京东|京東|天貓|天猫|虾皮|蝦皮|shopee|lazada|亞馬遜|亚马逊|amazon|"
    r"唯品會|唯品会|抖音|快手|pdd|京東|蘇寧|苏宁|当当|當當|官网|官網|直播間|直播间)",
    re.IGNORECASE,
)


def should_auto_advance(*, current: int, goal: str, ref: str, user_text: str, verdict: str, wa: str | None = None) -> bool:
    """規則級「一定要推進」override,唔靠 LLM judge(judge 慢/唔穩會卡死)。

    - 開場步(current==0,開場白+問個開啟問題):客戶俾咗任何實質回應
      (唔記得/唔知/確認/俾資料,但唔係純提問或拒絕)→ 即過,由下一步承接。
      「唔記得」係開場問題嘅完整答案,下一步(引導核實)正正承接——唔應該滯留。
    - 引導核實步:若呢步兼要「攞WhatsApp/傳截圖」(ref含 whatsapp/微信/截圖/帳號),
      必須客戶已俾到 WhatsApp(wa=captured/綁定來電 captured_implicit)先推;
      offered(應承加未俾號)則停留;淨係答到平台 → 停留喺本步,繼續叫客戶俾WhatsApp/傳截圖。
      若只係純核對平台(冇 WhatsApp 要求)→ 答到平台即過。
    """
    if verdict in (OBJECTION, REFUSE):
        return False
    if current == 0:
        # 純提問(客問「你哋邊間公司?」)要喺開場步答,唔推;其他實質回應都推。
        return verdict != QUESTION
    ctx = f"{goal} {ref}"
    # 兼要攞WhatsApp/截圖嘅核實步:客戶已俾號碼(captured)或話 WhatsApp 綁定來電
    # (captured_implicit) → 一定推(去下一步承接);offered(應承加但未俾號碼)→ 唔推,
    # 留喺本步等號碼。淨係答到平台 → 停留。
    wa_step = any(h in ctx for h in ("whatsapp", "WhatsApp", "微信", "帳號", "截圖", "加專員", "加你"))
    if wa_step and wa in ("captured", "captured_implicit"):
        return True
    if wa_step:
        return False  # 要攞WhatsApp/截圖,未攞到 → 唔好跳去下一步
    if verdict == QUESTION:
        return False
    if ("平台" in ctx or "核實" in ctx or "核实" in ctx or "邊個平台" in ctx) and _PLATFORM_RE.search(user_text):
        return True
    return False
# 已知资料键:若号码 run 命中佢哋 → 唔当新 WhatsApp(覆述单号/电话)
_KNOWN_NUM_KEYS = ("快递单号", "快递单號", "快递尾号", "電話", "电话", "電話號碼")


def _looks_like_whatsapp_step(goal: str, ref: str) -> bool:
    ctx = f"{goal} {ref}".lower()
    return any(h.lower() in ctx for h in _WHATSAPP_STEP_HINTS)


def _valid_digit_runs(norm: str) -> list[str]:
    """攞 6–13 位数字串(WhatsApp 號碼長度唔固定:香港8位/內地11位/帶區號13位;
    6位容錯 ASR 少聽多位/口誤短號,真實 case「我WhatsApp是六四三二五四三」曾因長度走漏)。
    短過6(單號尾4等)唔算,長過13(成串乱码)唔算;已知單號/電話另有 known-number 過濾兜底。"""
    return [r for r in re.findall(r"[0-9]{6,13}", norm)]


def _run_is_known_number(run: str, facts: dict | None) -> bool:
    """号码 run 命中已知 单号/尾号/电话 → 客户係覆述已知资料,唔係俾新 WhatsApp。"""
    if not facts:
        return False
    for k in _KNOWN_NUM_KEYS:
        raw = str(facts.get(k) or "").strip()
        if not raw:
            continue
        nv = _digit_normalize(raw)
        if nv and (nv == run or (len(nv) >= 8 and nv.endswith(run)) or run in nv):
            return True
    return False


def detect_whatsapp_signal(
    user_text: str,
    *,
    step_goal: str = "",
    step_ref: str = "",
    facts: dict | None = None,
    already_captured: bool = False,
) -> tuple[str, str] | None:
    """偵測客戶係咪俾出 WhatsApp。返回 ("captured", 號碼) | ("captured_implicit", "") | ("offered", "") | None。

    - captured:客戶讀出 6–13 位號碼(長度唔固定:港8/內地11/帶區號13;6位容錯 ASR 少聽),
      且①當前步係引導辦理(問WhatsApp)或②句中明顯提 whatsapp/微信/俾號/加我;或③明確自報
      「我WhatsApp(就)係 XXXX」(撞已知電話/單號都算)。號碼若命中已知 單號/尾號/電話 則唔當
      (覆述已知資料)。
    - captured_implicit:WhatsApp 步客戶話號碼綁定「呢個來電/呢個號碼」(號喺系統度)——
      唔使讀出 8-13 位;caller 攞對象電話上報 captured。防死鎖:唔會因號碼俾 ASR
      聽亂 / 撞單號就永遠入唔到 captured、AI 無限重複要號。
    - offered:喺引導辦理步、客戶冇俾號碼但应承加(好/可以/加咗),又冇話冇WhatsApp。
      由 caller 喺「上一步啱啱確認推入辦理步嗰輪」唔好 call 呢個 offered 分支(嗰輪客係
      應承接受,唔係應承加)──偵測放喺 flow 推進前跑、step context 係舊步,天然避開。
    - already_captured:本通已 captured 過號碼 → 之後嘅純短應承/叫加唔再判 offered
      (號碼已喺手,嗰啲係對當前步嘅確認;再判 offered 會令確認輪鎖死唔推進,
      4B 就把自己上一句原樣再講一次——2026-09-06 call-e6e5f18e 實證)。新號碼/
      綁定來電照樣 captured(客戶可以改口俾另一個號)。
    """
    t = (user_text or "").strip()
    if not t:
        return None
    if _WHATSAPP_DECLINE.search(t.lower()):
        return None
    norm = _digit_normalize(t)
    in_wa_step = _looks_like_whatsapp_step(step_goal, step_ref)
    low = t.lower()
    # WhatsApp 語境:句中明確講 whatsapp/微信/俾號/加我 → 出現嘅號碼優先當客戶俾嘅號。
    wa_ctx = ("whatsapp" in low) or ("微信" in t) or bool(_WHATSAPP_GIVE_NUM_RE.search(t)) or ("号碼" in t) or ("号码" in t) or ("加我" in t)
    runs = _valid_digit_runs(norm)
    caller_bound = in_wa_step and _WHATSAPP_CALLER_BOUND.search(t)
    if runs:
        fresh = [r for r in runs if not _run_is_known_number(r, facts)]
        # ① 客戶俾出「唔係已知單號/尾號/電話」嘅新號碼 → captured。
        if fresh and (in_wa_step or wa_ctx):
            return ("captured", fresh[0])
        # ①' 明確自報「我WhatsApp(就)係 XXXX」→ 號碼即 WhatsApp;撞已知電話/單號都照捕
        #    (真實 case:「我WhatsApp是六四三二五四三」7位+撞對象電話,雙重走漏→唔爆閃、重複追問)。
        if wa_ctx and _WHATSAPP_NUM_ANNOUNCE_RE.search(t):
            return ("captured", runs[0])
        # ② WhatsApp 步 + 明確俾號語境(加我/俾號碼)即使撞已知單號 → 仍當佢俾嘅 WhatsApp 號。
        #    真係覆述單號(「單號係…」冇 whatsapp/加我)→ 由 caller_bound 兜,唔喺度誤判。
        if in_wa_step and wa_ctx and _WHATSAPP_ADD_VERB.search(t):
            return ("captured", runs[0])
    # ③ WhatsApp 步客戶話 WhatsApp 綁定呢個來電/號碼(號喺系統)→ captured_implicit。
    if caller_bound:
        return ("captured_implicit", "")
    if in_wa_step and not runs:
        if already_captured:
            # 已捕获过号码:纯应承/叫加都係对当前步嘅确认,唔再当 offered
            # (确认轮锁死→逐字重复根因,见 docstring);让 rule_verdict 正常推进。
            return None
        # offered:明確叫加,或纯短应承(冇提其他話題)。
        if _WHATSAPP_ADD_VERB.search(t):
            return ("offered", "")
        if not _WHATSAPP_TOPIC_MARK.search(t) and _is_pure_ack(t):
            return ("offered", "")
    return None


def extract_call_facts(user_text: str, *, facts: dict | None = None) -> list[str]:
    """从客户话里抽可沉淀的关键事实(平台/号码)——会中记忆只增唔重问。

    「忘记」的直接机制(2026-09-06 行为取证):客户早轮讲过的事实只住在
    6 行×200 字滚动记忆+8 轮历史里,16 轮内先后蒸发,模型重新追问
    (call-701c180b 同一句「報姓名同單號」問了三遍)。这里抽「答过就该
    记住」的最小集:购物平台、非已知资料的号码串(命中已知 单号/尾号/电话
    唔重复沉淀);由 agent 每轮喂 ContextState.add_call_fact(去重有界),
    渲染进尾部【通话中客户已讲】。号码经 digits_to_cantonese 逐位转汉字
    (TTS 安全+防 LLM 凭空改号)。
    """
    t = (user_text or "").strip()
    if not t:
        return []
    out: list[str] = []
    m = _PLATFORM_RE.search(t)
    if m:
        out.append(f"客户讲过在{m.group(1)}买")
    norm = _digit_normalize(t)
    for run in _valid_digit_runs(norm):
        if _run_is_known_number(run, facts):
            continue
        out.append(f"客户报过号码:{digits_to_cantonese(run)}")
    return out


def decide_advance(user_text: str, *, facts: dict | None = None) -> str:
    """判定客户对当前这一步的反应,决定停留/推进。

    facts=对象已知资料(vars_map:姓名/单号/尾号等)时,客户覆述啱关键资料
    (「七八九零啊」「我係林先生」)都算 confirm;纯 echo 提问(「係咪你講嗰個
    七八九零?」)唔算——答得啱先当确认,唔係淨係「佢有冇講到個冧巴」。
    """
    t = user_text.strip()
    if not t:
        return UNCLEAR
    # 1) 明确拒绝/告别/要收线 → REFUSE(收尾态:一句礼貌再见后结束通话)。
    #    拒绝优先于一切(含否认/提问):「唔係我,唔好再打」主体係收线。
    if (_REFUSE_RE.search(t) or _HANGUP_RE.search(t)) and not _REFUSE_SOFT_GUARD_RE.search(t):
        return REFUSE
    # 2) 明确否认/不是本人 → objection(优先于确认词,避免"不是,是我…"误判)
    if _DENY_RE.search(t):
        return OBJECTION
    is_question = bool(_QUESTION_RE.search(t))
    strong_affirm = bool(_STRONG_AFFIRM_RE.search(t))
    fact_match = _matches_known_fact(t, facts)
    # 3) 提问且冇「多字确认」→ question(唔好因为句中出现已知尾号/单字係就当确认)
    if is_question and not strong_affirm:
        return QUESTION
    # 4) 确认/认可(社交词、多字确认、或答啱资料)→ confirm(先于提问:客户"是我的,然后呢?"主体是确认)
    if strong_affirm or fact_match or _CONFIRM_RE.search(t):
        return CONFIRM
    # 5) 纯提问 → question(停留本步解答)
    if is_question:
        return QUESTION
    return UNCLEAR


# 核对类步骤:goal/ref 含「确认身份/核实/係咪…」等 → 注入「客户答唔到」的后备指引,
# 等 LLM 唔会在原地重複/乱应承,而係先帮回忆、再转专员。纯说明/引导步骤唔注入。
_VERIFY_HINTS = (
    "确认", "確認", "核实", "核實", "核对", "核對", "验证", "驗證",
    "係咪", "是不是", "是否本人", "对一下", "對一下", "确认身份", "確認身份",
)


def _is_identity_verification_step(goal: str, ref: str) -> bool:
    text = f"{goal} {ref}"
    return any(h in text for h in _VERIFY_HINTS)


@dataclass
class FlowController:
    """一通通话内的流程状态:载入步骤、按客户话推进。"""

    steps: list[FlowStep] = field(default_factory=list)
    current: int = 0  # 0-based;== len(steps) 表示流程已走完
    closing: bool = False  # 客户明确拒绝/告别 → 收尾态:只讲收尾话术,唔再推进

    @classmethod
    def from_template(cls, template: dict | None, object_card: dict | None) -> "FlowController":
        # steps_json 或旧式四段都转成步骤(见 template_to_steps),保证分步推进不念稿。
        steps = template_to_steps(template)
        fc = cls(steps=steps)
        fc.vars_map = object_vars(object_card)
        return fc

    def __post_init__(self) -> None:
        self.vars_map: dict[str, str] = {}
        self._just_advanced = False  # 上一轮确认推进咗 → 注入「新一步」提示,提醒 LLM 换步

    @property
    def has_steps(self) -> bool:
        return len(self.steps) > 0

    @property
    def done(self) -> bool:
        return self.has_steps and self.current >= len(self.steps)

    def on_user_turn(self, user_text: str) -> None:
        """每轮用户话后调用:结合当前步决定是否推进。"""
        if not self.has_steps or self.done or self.closing:
            return
        verdict = self.rule_verdict(user_text)
        if verdict == CONFIRM:
            self.advance()

    def enter_closing(self) -> None:
        """客户明确拒绝/告别 → 进入收尾态:之后只讲收尾话术,唔再推进/唔再按步走。"""
        self.closing = True
        self._just_advanced = False

    def closing_text(self) -> str:
        """收尾态注入:一句礼貌告别,唔推销、唔挽留、唔转话题、唔问问题。"""
        return (
            "【收尾】客户已明确拒绝/表示要结束,现在只做礼貌收尾:用客户正在讲的语言讲一句告别"
            "(多谢+再见,例如「好嘅,唔打扰你嘞,多谢你时间,拜拜」),"
            "一句讲完就停——绝不推销、绝不挽留、绝不问任何问题、绝不转新话题、绝不再提流程。"
        )

    def rule_verdict(self, user_text: str) -> str:
        """规则判定(唔改动状态):只有"确认/认可当前步"先算可推进。"""
        # facts=vars_map:客户覆述啱已知资料(姓名/尾号/单号)都算确认,唔净靠社交词。
        return decide_advance(user_text, facts=self.vars_map)

    def advance(self) -> None:
        """推进到下一步(最后一步确认后即完成,唔越界)。"""
        if not self.has_steps or self.done:
            return
        if self.current < len(self.steps):
            self.current += 1
            self._just_advanced = True

    def apply_judge_verdict(self, verdict: str) -> None:
        """LLM 语义判定结果落状态(advance→推进;其它唔郁)。"""
        if verdict == CONFIRM:
            self.advance()

    def current_goal_ref(self) -> tuple[str, str]:
        """当前步的 (goal, ref) —— 畀 LLM 推进判定器睇(冇流程/行完返回空)。"""
        if not self.has_steps or self.done:
            return "", ""
        s = self.steps[self.current]
        return s.goal, s.ref

    def next_goal(self) -> str:
        """下一步目标(推進後嗰步) —— 判定器判斷「客嘅話係咪通去下一步」用。"""
        if not self.has_steps or self.current + 1 >= len(self.steps):
            return ""
        s = self.steps[self.current + 1]
        g = render_template_text(s.goal, self.vars_map) if s.goal else s.ref
        return g

    def current_step_text(self) -> str:
        """渲染当前步(含变量替换)给本轮 system;流程完成则空;收尾态则注入收尾话术。"""
        if self.closing:
            return self.closing_text()
        if not self.has_steps or self.done:
            # 话术走完 ≠ 收线:继续如常答疑/跟进,主动再见只准出现在 REFUSE/
            # 沉默收线(否则客户问「接下来怎么」会被 LLM 拜拜,实测 2026-09-06)。
            return (
                "话术流程已走完。唔好主动讲再见或收线；继续如常回答客户问题、"
                "确认后续安排（专员联系/到账时间），客户有问必答，等客户自然结束。"
            )
        step = self.steps[self.current]
        lines = [f"流程第 {self.current + 1}/{len(self.steps)} 步"]
        if self._just_advanced and self.current > 0:
            lines.append(
                "【新一步】客户啱啱确认咗上一步，而家已经进入呢一步。"
                "立即按呢一步嘅目标嚟讲——唔好讲「等我查下再覆你」「幾分鐘內覆你」呢類拖延话术"
                "(你手上已经有足够资料讲呢一步)，亦唔好延续上一步话题或继续自己头先应承过嘅嘢。"
            )
            self._just_advanced = False
        if step.goal:
            lines.append(f"这一步要达成:{render_template_text(step.goal, self.vars_map)}")
        if step.ref:
            lines.append(f"参考要点(内部指示,勿念给客户):{render_template_text(step.ref, self.vars_map)}")
        lines.append(
            "只围绕当前这一步回应,说清楚就停下等用户,不要替用户答或自行跳到下一步;"
            "客户问及后续可先简短回应再把话题带回当前步。"
            "参考要点是内部指示,用自己的口语讲,绝不把原文整段念出来,也不要把方案/金额一次倒光;"
            "「如果客户…→ 就…」呢類分支只在出现对应情况时照做,绝不把「如果」指示念给客户。"
            "不要索取电话/WhatsApp/微信等联系方式,除非当前步参考明确要你加(如引导加办理专员);"
            "核实资料用选项式引导(「你係咪喺拼多多、淘寶定京東買㗎?」),客户答到关键资料就确认并自然过渡,不无限追问。"
        )
        if _is_identity_verification_step(step.goal, step.ref):
            lines.append(
                "【核對/引導資料後備(內部指示,唔好讀出嚟)】全程你自己同客戶傾,"
                "唔好用「我幫你查完再覆你」「幾分鐘內覆你」「轉俾同事/專人跟進」呢類拖延話術——"
                "除非呢步本身就係要轉介。客戶答啱→自然確認停低;答唔到/唔記得/資料唔齊→唔好重複問,"
                "用訂單平台/截圖等引導(問喺邊個平台買、叫佢開訂單、傳最近未收到嘅貨截圖);"
                "中途問任何嘢→簡短答完帶返當前步。金額未核實前唔好講死具體賠幾多。"
            )
        return "\n".join(lines)

    def flow_overview(self) -> str:
        """流程总览(注入基础 system,让 LLM 知道全貌但不照读)。

        为 1.5s 延迟预算瘦身 prefill：后续步只保留「第N步:目标」一行标题/要点，
        不再带每步参考长文（当前步全文由 current_step_text 注入）。LLM 只需知道
        大致顺序与"下一步"方向，细节按轮给。
        """
        if not self.has_steps:
            return ""
        lines = [f"对话按 {len(self.steps)} 步流程推进,每步等用户确认后再进下一步:"]
        lines += self.overview_goal_lines()
        return "\n".join(lines)

    def overview_goal_lines(self) -> list[str]:
        """淨係「第N步:目標」嘅行,畀推進判定器當 roadmap。"""
        if not self.has_steps:
            return []
        out: list[str] = []
        for i, s in enumerate(self.steps, 1):
            g = render_template_text(s.goal, self.vars_map) if s.goal else s.ref
            out.append(f"第{i}步:{g}")
        return out


# ---- LLM 推进判定(处理规则搞唔掂嘅「模糊轮」,见 agent.py on_user_turn_completed) ----
# 规则版 decide_advance 对「随意应变」式对话覆盖有限(客問後續/答唔記得/答咗個開放問題),
# 呢啲模糊轮由本地 LLM 睇住「當前步目標 + 客戶原話」判 advance/stay/objection,準過死字面。

def build_judge_messages(
    *,
    current_index: int,
    total: int,
    overview_lines: list[str],
    goal: str,
    ref: str,
    next_goal: str,
    user_text: str,
    facts: dict | None,
) -> list[dict]:
    """组推进判定器嘅 messages:简短 + 少少例子,4B 先跟得準(太長會亂答)。"""
    sys = (
        "你是粤语客服通话流程推进器。客服按步骤和客户沟通，当前在第"
        f"{current_index}步（共{total}步）：{goal or '(无)'}"
    )
    if next_goal:
        sys += f"。下一步（若推进）：{next_goal}"
    sys += (
        "。客户说完一句话，判断客服是否该进入下一步。只输出三个词之一：advance / stay / objection。\n"
        "advance=客户已答完/确认当前步，或客户问/讲的正正是下一步内容，"
        "或当前步在引导核实资料而客户已答出关键资料（例如讲到在哪个平台买，即使商品/金额未完全对上提示）"
        "且下一步正是承接这个答案的动作；或当前步在向客户提问而他给了明确答案"
        "（包括答「唔知」「唔記得」「冇」「唔清楚」——只要下一步正是承接呢个答案嘅动作）。\n"
        "stay=客户答/问的仍是当前步要处理的（关键资料还没给到，如在核实步未讲到平台、在问当前步该交代的事）。\n"
        "objection=客户否认/拒绝/不关事/想挂线。\n"
        "全程是AI客服自己和客户聊，客户答不出不等于要转真人，判断只管推进流程。\n"
        "例子：\n"
        "客户：「好，没问题，係我嘅」-> advance\n"
        "客户：「你哋係邊間公司㗎？」-> stay\n"
        "客户：「唔好再打嚟！」-> objection\n"
        "客户：「我喺拼多多買嘅」且当前係引導核實 -> advance\n"
        "客户：「我冇訂單，唔記得喺邊買」且当前係引導核實(要答平台先過) -> stay\n"
        "客户：「我唔記得買咗咩」且当前係開場問記憶、下一步係引導核實 -> advance"
    )
    if facts:
        known = " ".join(f"{k}={v}" for k, v in facts.items() if v)
        sys += f"\n已知客戶資料:{known}"
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": f"客戶:「{user_text}」\n淨係輸出 advance / stay / objection 其中一個字。"},
    ]


def parse_judge_output(text: str) -> str:
    """把 LLM 判定器輸出對返 verdict(advance→CONFIRM,objection→OBJECTION,其它→UNCLEAR)。"""
    t = (text or "").strip().lower()
    if "advance" in t:
        return CONFIRM
    if "objection" in t:
        return OBJECTION
    return UNCLEAR
