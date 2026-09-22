#!/usr/bin/env python3
"""意图候选挖掘 dry-run 探针（执行计划 §48 P2.1，纯只读 + 本地 LLM）。

## 要证明什么

系统有确定性意图图 `graph_json`（`intents[]{id,name,keywords[],priority}` + 绑定边），
但生产 12 个模板的图全是空的——**没有数据就没有意图引擎**。本探针从历史通话的
客户轮转写里用本地 LLM 挖出可用的意图候选集，并离线回放覆盖率，回答：

  「这批转写里到底能不能挖出 ≥8 个意图、关键词能否覆盖 ≥60% 的真实客户话？」

## 七步（全程只读，不写库、不提交 git）

1. **抽样**：取通话数最多的模板，其全部通话的客户轮（`line='a' AND
   speaker='customer'`），按归一化文本去重 + 频次排序，过滤 <3 字，取 200-400 条。
2. **挖掘**：分批喂本地 9B（默认 :1237），让它聚类成意图，输出严格 JSON：
   `[{id(英文snake_case <主体>_<关系>), name(中文), keywords(摘自原话,3-8), example(2条)}]`。
   prompt 明确防碎片误判（单字/语气词/纯数字/断句碎片不算意图）。解析宽容（剥围栏/补尾括号）。
   **默认挖 2 遍**（`--passes`）：9B 单遍方差大（同一份样本三跑实测 27/29/31 个意图、
   覆盖率 57%/64%/57%），跨遍按 id 合并是稳定读数的前提。
3. **覆盖回放**（不打 LLM）：对全部抽样客户话逐条测是否命中任一候选意图关键词——
   归一化语义与生产 `flow_graph.normalize_graph_text` 逐字节同款（双侧剥标点/空格 + casefold
   子串），算覆盖率与逐意图命中数。
4. **去重合并**（P2.1 下轮迭代，纯函数 `dedupe_candidates`）：9B 跨批会造同义双 id
   （`session_affirm`/`session_confirm`）与 name/id 漂移（`identity_verify` 名叫「确认应承」）。
   合并规则保守（宁可少并不可错并）：①同 id 已由 `merge_candidates` 并集；②id 近义
   （下划线不敏感 + 小词表同义映射，如 affirm/confirm、trace/track）→ 并；③归一 name
   全等 → 并；④词集 Jaccard ≥0.8 且共有 ≥3 词（同一句话被造了两个 id 的铁证）→ 并。
   漂移消解在同名组内先做：保留与 name 最贴合的 id，其余**以 id 为准改 name**
   （id 词典可译则译，不可译则 `name（id）` 形态，绝不静默错并）。合并取字典序最小的
   id、keywords 保序并集（上限=生产 `MAX_KEYWORDS` 32）、命中数相加。
5. **平台意图钉死**（`pin_platform_intent`）：hard-add 固定意图 `platform_identify`
   （品牌词表，不走 LLM、**永远在场**），并把 9B 造出的其它 `platform_*` 候选吸收掉
   （只吸收 id，其关键词另记台账不并进钉死表——`呢度/喺这里` 类指示词进图会乱触发）。
6. **图对齐腿**（`build_graph_doc` + `validate_flow_graph`）：把合并后的候选逐条配一条
   **保守绑定建议**（默认 `notify_human` 打铃不抢话；收线类 `jump_step`→收尾步）+ 建议的
   `"*"` 兜底行（keywords=[]），跑生产严格校验——**必须零错误**（每个常规意图都有绑定，
   孤儿门结构性不触发）。
7. **报告**：stdout + `reports/intent-mine/*.json`：合并前后意图数对照 + 候选全表 +
   覆盖率 + **可直接导入清单 vs 需人审清单** + 未覆盖 top10。

## 门槛

候选意图 ≥8 且覆盖率 ≥70%（P2.1 下轮目标，上轮 64%）记为 PASS；
不达标也把未覆盖榜带回来留给下轮迭代。

## 安全（Mimosa SSRF 加固）

本探针是本地诊断工具，LLM 端点只允许环回 127.0.0.1/localhost + http(s)；拒绝其他一切主机。
数据库以 `mode=ro` URI 打开，结构性只读。

## 用法

    .venv312/bin/python scripts/probe_intent_mine.py
    .venv312/bin/python scripts/probe_intent_mine.py --limit 300 --batch-size 30 --passes 2
    .venv312/bin/python scripts/probe_intent_mine.py --passes 1          # 单遍对照
    .venv312/bin/python scripts/probe_intent_mine.py --candidates-file a.json --candidates-file b.json  # 回放+多遍合并
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "packages" / "core"))
sys.path.insert(0, str(_ROOT / "scripts"))

from bok_voice_core.flow_graph import normalize_graph_text as normalize_text  # noqa: E402

# 本地诊断端点白名单：只允许环回（探针用途本身=打本机 sidecar）。
_ALLOWED_HOSTS = ("127.0.0.1", "localhost")
BASE = os.environ.get("BOK_INTENT_MINE_URL", "http://127.0.0.1:1237")
MODEL = os.environ.get("BOK_INTENT_MINE_MODEL", "")

DEFAULT_DB = str(Path.home() / "Library" / "Application Support" / "BokVoice" / "bok_voice.db")
_OUT_DIR = _ROOT / "reports" / "intent-mine"

# 候选意图字段约束（与生产 `validate_flow_graph` 的意图形状对齐，但本探针只读不落库）。
_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
MIN_KEYWORD_CHARS = 2  # 归一化后 1 字关键词=「你/我/係」级泛词，宁少勿滥直接丢
MIN_KEYWORDS = 3
MAX_KEYWORDS = 8
# 两级词数上限（刻意分离，勿合并）：
#   MAX_KEYWORDS         = 单批 LLM **输出**契约（prompt 指导 3-8 词）；解析层据此清洗。
#   CANDIDATE_MAX_KEYWORDS = 多批**合并后**候选的词数上限，对齐生产 `flow_graph.MAX_KEYWORDS`
#                            （=32）。合并是「同一意图跨批发现的不同说法」的并集，是真实信号，
#                            不该被输出契约砍掉；但也不能无界。报告里 PRIMARY 覆盖率按 32 档，
#                            同时给出「每意图截到 8 词」的稳健性读数（见 coverage_sensitivity）。
CANDIDATE_MAX_KEYWORDS = 32
MAX_SAMPLES = 400
MIN_SAMPLES = 200

# --------------------------------------------------------------------------- #
# P2.1 下轮迭代：平台意图钉死 / 意图 id 词典 / 噪声与合并阈值
# --------------------------------------------------------------------------- #

# **平台意图钉死**：9B 反复漏掉「客户自报购买平台」（上轮「拼多多买嘅」「京东买嘅」等
# 14 条样本零覆盖），而运营面要的是「客户从哪个平台买的」这条确定性事实——不赌 LLM
# 稳定性，hard-add 固定意图、不走 LLM、永远在场。
# 词表=任务给定品牌表 + **真实转写补词**（`拼多` 在 386 条客户轮里出现 20 次，
# 且是 `拼多多` 的子串，一条词覆盖 `拼多多买嘅/拼多多。我/拼拼多。多多` 全部变体）。
PLATFORM_INTENT_ID = "platform_identify"
PLATFORM_INTENT_NAME = "询问购买平台"
PLATFORM_INTENT_KEYWORDS = (
    "拼多多", "拼多", "淘宝", "京东", "天猫", "闲鱼", "唯品会", "苏宁", "亚马逊",
    "aliexpress", "taobao", "jd", "pdd",
)
# 被钉死意图吸收的同类 id 前缀（9B 自造的 `platform_confirm` 等）——只吸收 id，
# 关键词不并入（`点站/呢度/喺这里` 类指示词进图会在无关轮乱触发）。
PLATFORM_ABSORB_PREFIX = "platform_"
# 兜底意图（生产 `flow_graph.CATCHALL_INTENT_ID`，P2.2 起为保留 id `"*"`）。
CATCHALL_ID = "*"
CATCHALL_NAME = "兜底（以上都没接住）"
# 模板收尾步（三语模板 6 步；`jump_step` 建议的默认目标）。
CLOSE_STEP_DEFAULT = 6

# id 小词表同义映射（**刻意小**：只收「同一关系的不同英文写法」，不收语义近邻）。
# 用途=判断两个 id 是否「仅差近义词」，从而安全合并（`session_affirm`/`session_confirm`、
# `logistics_trace`/`logistics_track`）。宁少勿滥：映射表里没有的词绝不当同义。
_ID_TOKEN_SYNONYMS = {
    "affirm": "confirm", "accept": "confirm", "agree": "confirm", "ack": "confirm",
    "trace": "track", "lookup": "track", "query": "track", "inquire": "ask",
    "request": "ask", "reimburse": "refund", "urgent": "hurry", "urge": "hurry",
    "push": "hurry", "staff": "human", "agent": "human", "operator": "human",
    "person": "human", "supervisor": "manager", "goodbye": "close", "farewell": "close",
    "end": "close", "bye": "close", "touch": "contact", "suspect": "doubt",
    # 9B 实测的同义写法与误拼（第二类：词形变体）——实测 `session_negate`/`session_reject`
    # 同名「否定拒绝」各占一条、`session_urget` 是 `urgent` 的误拼；不登记就永远并不到一起。
    "negate": "reject", "deny": "reject", "refusal": "reject", "rejection": "reject",
    "urget": "hurry", "inquery": "ask", "comfirm": "confirm",
    "greet": "greeting", "followup": "follow",
}
# id 词 → 中文标签（**漂移改名**用：name 与 id 明显不符时以 id 为准起名）。
# 查不到的词 → 无法从 id 起名（走 `name（id）` 兜底形态），绝不猜。
_ID_TOKEN_LABELS = {
    "identity": "身份", "verify": "核实", "question": "质疑", "doubt": "质疑",
    "session": "会话", "confirm": "确认应承", "close": "告别", "confused": "听不懂",
    "reject": "否定", "greeting": "问候", "hesitate": "犹豫", "affirm": "确认应承",
    "ask": "询问", "refund": "退款", "progress": "进度", "compensation": "赔偿",
    "logistics": "物流", "track": "查询", "delay": "延迟", "contact": "联系",
    "whatsapp": "WhatsApp", "add": "添加", "manager": "主管", "complaint": "投诉",
    "formal": "正式", "product": "商品", "detail": "细节", "name": "名称",
    "follow": "跟进", "up": "催促", "hurry": "催促", "urgent": "加急",
    "transfer": "转人工", "human": "人工", "platform": "平台", "identify": "询问",
    "pickup": "自提", "date": "日期", "pay": "付款", "order": "订单",
}
# 双词短语优先（逐词直译会出「催促催促」这类叠词）：`follow_up` 是一个词不是两个。
_ID_PHRASE_LABELS = {"follow_up": "跟进"}
# 合并阈值（保守）：词集 Jaccard 与共有词数双门，宁少并不可错并。
KEYWORD_OVERLAP_JACCARD = 0.8
KEYWORD_OVERLAP_MIN_SHARED = 3
# 「可直接导入 vs 需人审」判据：噪声词占比超过此线 → 需人审（噪声词照留、只降级）。
NOISY_KEYWORD_RATIO_MAX = 0.34
# 噪声词判据用的语气/助词集（ASR 碎片高频尾部：「底细」「前双」级碎片难纯规则识破，
# 但「嘅/咗/啦」类语气词与纯数字串是可靠的抄词信号）。
_NOISE_PARTICLES = set("嘅咗啦呀啊嗯誒嘞喔喎㗎咁啲嘢咩啩")
_DIGIT_ONLY_RE = re.compile(r"^[0-9一二三四五六七八九十零两]+$")
# 冷僻词门：全库只命中 ≤1 条样本的**短词**（≤4 字），多半是抄自那一条的碎片
# （「前双」「底细处理」「六四三二」）。长词（≥5 字）更可能是真实罕见说法，不判噪声——
# 判据过度敏感会把清单全推给「需人审」，等于没有清单。
_RARE_KEYWORD_MAX_HITS = 1
_RARE_KEYWORD_MAX_CHARS = 4

_MINE_SYSTEM = (
    "你是电话客服对话的数据分析师。你的任务：把**客户**说过的话聚类成少数几个"
    "「客户意图」，供规则引擎做确定性关键词命中。只输出 JSON，不要任何解释。"
)

_MINE_RULES = """\
请把下面编号的客户原话聚类成意图。输出**严格 JSON 数组**，每个元素：
{
  "id": "英文 snake_case，格式 <主体>_<关系>，例如 compensation_ask / logistics_trace / whatsapp_contact",
  "name": "中文短名，例如 询问赔偿 / 查询物流 / 加联系方式",
  "keywords": ["从客户原话里逐字摘出的词，3-8 个"],
  "example": ["该意图下 2 条原话"]
}

意图须同时覆盖两类：
- 内容类：查询物流/单号、询问赔偿、确认购买平台（客户自报拼多多/京东/淘宝等）、加 WhatsApp/联系方式、商品细节、退款进度；
- 会话类：确认应承、否定拒绝、听不懂要求重说、**要求对方先听自己讲/别打断**、**打招呼问候**、告别结束、找真人/转接、催促跟进、身份与真实性质疑。

硬性要求：
- keywords 必须**逐字摘自**客户原话（连写、不带标点空格），不要自己编词、不要翻译；
- keywords 取 2-6 字的**常用说法**（能泛化到同义说法），不要整句照抄、不要单字，不要纯数字串；
- 宁可少而准，不要滥：泛词（你/我/佢/嘅/係/啦/啊 等虚词）不算关键词；
- 单字、语气词、纯数字串、ASR 断句碎片（如「咁」「誒」「六四三二」「就一」）**不算意图**，直接忽略；
- 同一意图只输出一条，**不要给每条原话各造一个意图**，id 里不要带编号（勿出现 xxx1/xxx2）；
- **name 必须与 id 同义**（宁可直接把 id 直译成中文，也不要把一个意图的名字写成另一个意图）：
  同一个中文名不准出现在两个不同 id 上；
- example 直接抄原话本身，不要带「编号. [N次]」前缀；
- 意图数量控制在 8-20 个之间，覆盖尽可能多的原话；
- 只输出 JSON 数组本身，不要 Markdown 围栏、不要前后文字。
"""


# --------------------------------------------------------------------------- #
# 纯函数（可单测）
# --------------------------------------------------------------------------- #

def guard(url: str) -> str:
    """SSRF 加固：只放行环回 http(s) 端点，其余一律 raise。"""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"scheme not allowed: {parts.scheme!r}")
    if (parts.hostname or "") not in _ALLOWED_HOSTS:
        raise ValueError(f"host not allowed (local-diag only): {parts.hostname!r}")
    return url


def dedup_samples(rows: list[str], *, min_chars: int = 3, limit: int = 300) -> list[dict]:
    """去重 + 计数 + 频次排序 + 过滤短句。返回 `[{"text", "count"}]`（频次降序，同频按文本）。

    `min_chars` 按**原始**文本判（剥首尾空白后），碎片过滤器；`limit` 上限 400（超出截断）。
    """
    counts: dict[str, int] = {}
    originals: dict[str, str] = {}
    for raw in rows:
        text = (raw or "").strip()
        key = normalize_text(text)
        if not key or len(text) < min_chars:
            continue
        counts[key] = counts.get(key, 0) + 1
        if key not in originals:
            originals[key] = text
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], originals[kv[0]]))
    cap = min(max(int(limit), 1), MAX_SAMPLES)
    return [{"text": originals[k], "count": c} for k, c in ordered[:cap]]


def _loads_json_lenient(text: str):
    """宽容 JSON 解析：剥围栏 → 直接 loads → `json_repair` 补未闭合括号重试。返 dict/list/None。"""
    from bok_voice_core.json_repair import close_unclosed_json

    src = (text or "").strip()
    if not src:
        return None
    # 剥 ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", src, re.S)
    if fence:
        src = fence.group(1).strip()
    try:
        return json.loads(src)
    except Exception:  # noqa: BLE001 - 坏 JSON 是预期输入，走剥壳/补齐路径
        pass
    # 从第一个 { 或 [ 起截（模型常加「好的，结果如下：」前言）
    start = min((i for i in (src.find("{"), src.find("[")) if i >= 0), default=-1)
    if start < 0:
        return None
    body = src[start:]
    try:
        return json.loads(body)
    except Exception:  # noqa: BLE001
        pass
    repaired = close_unclosed_json(body)
    if repaired is None:
        return None
    try:
        return json.loads(repaired)
    except Exception:  # noqa: BLE001 - 补不回来就是补不回来
        return None


def _salvage_json_objects(text: str) -> list[dict]:
    """从**被截断**的 JSON 输出里抢救已完整的顶层 `{...}`（9B 长批次实测会被 max_tokens 砍在
    对象中途，整串解析必然失败）。字符串感知的括号扫描，只收能独立 `json.loads` 成功的对象。
    """
    out: list[dict] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except Exception:  # noqa: BLE001 - 单条坏就丢单条
                        obj = None
                    if isinstance(obj, dict):
                        out.append(obj)
                    start = -1
    return out


def _sanitize_id(value: object) -> str:
    """候选 id 归一成合法 snake_case；非法/空返空串（该条被丢）。"""
    raw = str(value or "").strip().lower()
    raw = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    if not raw or not _ID_RE.match(raw):
        return ""
    return raw


def _sanitize_keywords(value: object, *, limit: int = MAX_KEYWORDS) -> list[str]:
    """关键词清洗：去空/去重（按归一化键）/丢 1 字泛词；保持出现顺序，最多 `limit` 个。"""
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        kw = str(item or "").strip()
        key = normalize_text(kw)
        if len(key) < MIN_KEYWORD_CHARS or key in seen:
            continue
        seen.add(key)
        out.append(kw)
        if len(out) >= limit:
            break
    return out


def _normalize_candidate(item: dict, *, max_keywords: int, min_keywords: int) -> dict | None:
    """把一条原始意图对象归一成候选；形状坏返 None（宁少勿滥）。"""
    cid = _sanitize_id(item.get("id") or item.get("intent") or item.get("name"))
    name = str(item.get("name") or item.get("label") or "").strip()
    # 回放自家旧报告时的幂等还原：上一轮漂移改名留下的 `名字（id）` 后缀剥掉，让本轮
    # 重新按 id 判定（否则「（id）」会当成真名一路传下去，同名合并再也并不到一起）。
    suffix = f"（{cid}）"
    if cid and name.endswith(suffix):
        name = name[: -len(suffix)].strip()
    keywords = _sanitize_keywords(item.get("keywords"), limit=max_keywords)
    examples = item.get("example") or item.get("examples") or []
    if not isinstance(examples, list):
        examples = [examples]
    examples = [str(e).strip() for e in examples if str(e or "").strip()][:2]
    if not cid or not name or len(keywords) < min_keywords:
        return None
    return {"id": cid, "name": name, "keywords": keywords, "example": examples}


def parse_intent_candidates(text: str) -> list[dict]:
    """宽容解析模型输出为候选意图表；形状坏的单条**丢弃**（宁少勿滥），不抛异常。

    接受裸数组，也接受 `{"intents": [...]}` 包裹。每条要求：合法 id、非空名、≥3 个合格
    关键词（清洗后，上限=单批输出契约 `MAX_KEYWORDS`）。返回 `[{"id","name","keywords","example"}]`。
    """
    data = _loads_json_lenient(text)
    if data is None:
        salvaged = _salvage_json_objects(text)  # 被 max_tokens 截断的数组：救已完整的对象
        if salvaged:
            data = salvaged
    if isinstance(data, dict):
        for key in ("intents", "candidates", "data", "result", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        cand = _normalize_candidate(item, max_keywords=MAX_KEYWORDS, min_keywords=MIN_KEYWORDS)
        if cand is not None:
            out.append(cand)
    return out


def merge_candidates(batches: list[list[dict]]) -> list[dict]:
    """合并多批候选：同 id 合并关键词（保序去重）/示例，name 取首个非空。

    合并后词数上限 `CANDIDATE_MAX_KEYWORDS`（=生产 `flow_graph.MAX_KEYWORDS` 32），
    高于单批输出契约 8——跨批发现的同义说法是真实信号，不该被输出契约砍掉。
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    for batch in batches:
        for cand in batch:
            cid = cand.get("id")
            if not cid:
                continue
            if cid not in merged:
                merged[cid] = {"id": cid, "name": cand.get("name", ""), "keywords": [],
                               "example": []}
                order.append(cid)
            slot = merged[cid]
            if not slot["name"] and cand.get("name"):
                slot["name"] = cand["name"]
            seen = {normalize_text(k) for k in slot["keywords"]}
            for kw in cand.get("keywords", []):
                key = normalize_text(kw)
                if key and key not in seen and len(slot["keywords"]) < CANDIDATE_MAX_KEYWORDS:
                    seen.add(key)
                    slot["keywords"].append(kw)
            for ex in cand.get("example", []):
                if ex and ex not in slot["example"] and len(slot["example"]) < 2:
                    slot["example"].append(ex)
    return [merged[cid] for cid in order if merged[cid]["keywords"]]


# --------------------------------------------------------------------------- #
# 去重合并 / 漂移消解（P2.1 下轮迭代，纯函数）
# --------------------------------------------------------------------------- #

def _lcs_len(a: str, b: str) -> int:
    """最长公共子序列长度（短串动态规划）：用于「哪个 id 的标签最贴合 name」。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            cur[j] = prev[j - 1] + 1 if ca == cb else max(prev[j], cur[j - 1])
        prev = cur
    return prev[-1]


def _id_tokens(intent_id: str) -> list[str]:
    return [t for t in str(intent_id or "").split("_") if t]


def label_from_id(intent_id: str) -> str:
    """由 id 词表译出中文标签（漂移改名用）；任一 token 查不到 → 空串（不猜）。

    贪心从左吃：先试双词短语（`follow_up`→跟进，逐词直译会叠成「跟进催促催促」），
    再单 token 查标签表，未中经同义映射回落（`push`→`hurry`→催促）。
    """
    tokens = _id_tokens(intent_id)
    labels: list[str] = []
    idx = 0
    while idx < len(tokens):
        pair = "_".join(tokens[idx:idx + 2])
        if idx + 2 <= len(tokens) and pair in _ID_PHRASE_LABELS:
            labels.append(_ID_PHRASE_LABELS[pair])
            idx += 2
            continue
        token = tokens[idx]
        label = _ID_TOKEN_LABELS.get(token) or _ID_TOKEN_LABELS.get(_ID_TOKEN_SYNONYMS.get(token, ""))
        if not label:
            return ""
        labels.append(label)
        idx += 1
    return "".join(labels)


def ids_are_synonymous(a: str, b: str) -> bool:
    """两个意图 id 是否「仅差下划线/近义」——合并的**强**证据（保守）。

    三条：①去掉下划线后逐字节相等（`follow_up_push`/`followuppush`）；②拆词后经
    小词表同义映射逐位相等且 ≥2 词（`session_affirm`↔`session_confirm`、
    `logistics_trace`↔`logistics_track`）。词数不等、或任一位既不同名也不同义 → False。
    **同 id 不算**（那是 `merge_candidates` 的并集路径）。
    """
    a, b = str(a or ""), str(b or "")
    if not a or not b or a == b:
        return False
    if a.replace("_", "") == b.replace("_", ""):
        return True
    ta = [_ID_TOKEN_SYNONYMS.get(t, t) for t in _id_tokens(a)]
    tb = [_ID_TOKEN_SYNONYMS.get(t, t) for t in _id_tokens(b)]
    return len(ta) >= 2 and len(ta) == len(tb) and ta == tb


def _keyword_jaccard(a: dict, b: dict) -> float:
    """两个候选的关键词集 Jaccard（归一化键）。任一侧 <3 词 → 0（不足以作证据）。"""
    sa = {normalize_text(k) for k in a.get("keywords", [])} - {""}
    sb = {normalize_text(k) for k in b.get("keywords", [])} - {""}
    if len(sa) < KEYWORD_OVERLAP_MIN_SHARED or len(sb) < KEYWORD_OVERLAP_MIN_SHARED:
        return 0.0
    union = sa | sb
    return len(sa & sb) / len(union) if union else 0.0


def _merge_reason(a: dict, b: dict) -> str:
    """两条候选该不该并（返回原因串，空=不并）。判据由强到弱，任一命中即并。"""
    if str(a.get("id") or "") and str(a.get("id") or "") == str(b.get("id") or ""):
        # 同 id = 同一个意图（多遍挖掘/多份候选文件的并集入口）。上游
        # `merge_candidates` 已按 id 并过一次，这里再兜一道：**本函数单独调用也必须
        # 产出无重复 id 的候选表**，否则组出来的 graph doc 会撞生产
        # `validate_flow_graph` 的 `id duplicated` 硬门（实测踩到）。
        return "same_id"
    na, nb = str(a.get("name") or "").strip(), str(b.get("name") or "").strip()
    if na and na == nb:
        return "name_equal"
    if ids_are_synonymous(str(a.get("id") or ""), str(b.get("id") or "")):
        return "id_synonym"
    if (
        _keyword_jaccard(a, b) >= KEYWORD_OVERLAP_JACCARD
        and len(set(map(normalize_text, a.get("keywords", [])))
                & set(map(normalize_text, b.get("keywords", [])))) >= KEYWORD_OVERLAP_MIN_SHARED
    ):
        return "keyword_overlap"
    return ""


def _index_clusters(count: int, linked) -> list[list[int]]:
    """按 `linked(i, j)` 谓词把 0..count-1 并成簇（保序，簇内按原始下标升序）。"""
    parent = list(range(count))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(count):
        for j in range(i + 1, count):
            if linked(i, j):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)
    groups: dict[int, list[int]] = {}
    for i in range(count):
        groups.setdefault(find(i), []).append(i)
    return [groups[k] for k in sorted(groups)]


def resolve_name_drift(candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    """消解 name/id 漂移（同名组内）：保留最贴合 name 的 id，其余**以 id 为准改名**。

    9B 会把几个不同 id 都命名成同一个中文（上轮 `session_affirm`/`session_confirm`/
    `identity_verify` 全叫「确认应承」，其中 `identity_verify` 的关键词是「真嘅/证明/
    唔系」=身份质疑，名字与 id 明显漂移）。做法：同名组内先按 id 近义并簇——只有一簇
    （=本来就会合并，名字无害）则不动；多簇且 **owner 的标签与 name 有公共子序列**
    （=有证据说明这个名字确实属于该 id）时，才去改其余簇的名字。

    **改名要「证明矛盾」**（否则会把本该按同名合并的意图劈开）：只有该簇 id 的词典
    标签与 name **完全无公共字符**（`LCS=0`，如 身份核实 vs 确认应承）才改名；标签与
    name 沾边（如 `session_hurry`→会话催促 vs「催促跟进」，`LCS=2`）一律不动——宁可让它
    按同名并成一个，也不做无证据的劈分。整组所有 id 的标签都与 name 无公共字符（或全译
    不出）时=无证据判谁是真主，整组不动。改不出词典标签时退 `name（id）` 形态（防与
    别人的名字撞车被错并，绝不静默沿用别人的名字）。

    返回 `(新候选表, 改名台账)`；不改输入。
    """
    by_name: dict[str, list[int]] = {}
    for idx, cand in enumerate(candidates):
        name = str(cand.get("name") or "").strip()
        if name:
            by_name.setdefault(name, []).append(idx)
    renamed: dict[int, tuple[str, str]] = {}
    for name, idxs in by_name.items():
        if len({str(candidates[i].get("id") or "") for i in idxs}) < 2:
            continue
        clusters = _index_clusters(
            len(idxs),
            lambda a, b: ids_are_synonymous(
                str(candidates[idxs[a]].get("id") or ""), str(candidates[idxs[b]].get("id") or "")
            ),
        )
        if len(clusters) <= 1:
            continue
        scored = []
        for cl in clusters:
            rep = min(str(candidates[idxs[k]].get("id") or "") for k in cl)
            scored.append((_lcs_len(label_from_id(rep), name), rep, cl))
        scored.sort(key=lambda t: (-t[0], t[1]))
        if scored[0][0] <= 0:  # 没有任何 id 的标签与 name 相干 = 无证据，整组不动
            continue
        for _score, _rep, cl in scored[1:]:  # 第一簇=owner，保持原 name
            for k in cl:
                old_id = str(candidates[idxs[k]].get("id") or "")
                label = label_from_id(old_id)
                if label and _lcs_len(label, name) > 0:
                    continue  # 标签与 name 沾边 = 不是漂移
                new_name = (label or f"{name}（{old_id}）")[:64]
                renamed[idxs[k]] = (name, new_name)
    log = [
        {"id": str(candidates[i].get("id") or ""), "old_name": old, "new_name": new,
         "reason": "name_id_drift"}
        for i, (old, new) in sorted(renamed.items())
    ]
    out: list[dict] = []
    for idx, cand in enumerate(candidates):
        if idx in renamed:
            out.append({**cand, "name": renamed[idx][1]})
        else:
            out.append(dict(cand))
    return out, log


def dedup_candidates(candidates: list[dict]) -> tuple[list[dict], dict]:
    """跨批/跨簇去重合并（纯函数）：返回 `(合并后候选表, 台账)`。

    合并规则见模块 docstring（name 全等 / id 近义 / 词集 Jaccard≥0.8）。合并形态：
    id=簇内字典序最小者、name=该 id 成员的 name（漂移消解后即 owner 名）、keywords
    保序并集去重（上限 `CANDIDATE_MAX_KEYWORDS`）、`hits` 相加、example 取前 2 条。
    台账含 `before/after/merges/renames`，供报告「合并前后意图数对照」。
    """
    cands = [c for c in candidates if str(c.get("id") or "")]
    drifted, renames = resolve_name_drift(cands)
    count = len(drifted)
    clusters_idx = _index_clusters(
        count,
        lambda a, b: bool(_merge_reason(drifted[a], drifted[b])),
    )
    merged: list[dict] = []
    merges: list[dict] = []
    for idxs in clusters_idx:
        members = [drifted[i] for i in idxs]
        ids = sorted(str(m.get("id") or "") for m in members)
        owner_id = ids[0]
        owner = next(m for m in members if str(m.get("id") or "") == owner_id)
        name = str(owner.get("name") or "").strip() or next(
            (str(m.get("name") or "").strip() for m in members if str(m.get("name") or "").strip()), "")
        keywords: list[str] = []
        seen: set[str] = set()
        for member in members:
            for kw in member.get("keywords", []):
                key = normalize_text(kw)
                if key and key not in seen and len(keywords) < CANDIDATE_MAX_KEYWORDS:
                    seen.add(key)
                    keywords.append(str(kw))
        examples: list[str] = []
        for member in members:
            for ex in member.get("example", []):
                if ex and ex not in examples and len(examples) < 2:
                    examples.append(str(ex))
        row = {"id": owner_id, "name": name, "keywords": keywords, "example": examples,
               "hits": sum(int(m.get("hits") or 0) for m in members)}
        if len(members) > 1:
            reasons: set[str] = set()
            for a in range(len(members)):
                for b in range(a + 1, len(members)):
                    reason = _merge_reason(members[a], members[b])
                    if reason:
                        reasons.add(reason)
            row["merged_from"] = ids
            merges.append({
                "id": owner_id, "name": name, "from": ids,
                "reasons": sorted(reasons),
                "pre_merge_hits": {str(m.get("id") or ""): int(m.get("hits") or 0) for m in members},
                "keywords_in": sum(len(m.get("keywords", [])) for m in members),
                "keywords_out": len(keywords),
            })
        merged.append(row)
    merges.sort(key=lambda r: (-len(r["from"]), r["id"]))
    log = {"before": count, "after": len(merged), "merged_groups": len(merges),
           "merged_away": count - len(merged), "merges": merges, "renames": renames}
    return merged, log


def pin_platform_intent(candidates: list[dict]) -> tuple[list[dict], dict]:
    """硬钉平台意图（**不走 LLM、永远在场**）：返回 `(候选表, 台账)`，钉死意图置顶。

    - 原表里同 id 的候选（9B 万一造出）→ 替换：keywords=钉死词表，原词记入台账；
    - 其它 `platform_*` 候选（如 `platform_confirm`）→ 吸收掉 id（只此一个平台意图），
      其关键词**不并入**（`点站/呢度/喺这里` 类指示词会乱触发），台账留痕可审。
    """
    kept: list[dict] = []
    replaced: dict | None = None
    already_pinned = False
    absorbed: list[dict] = []
    for cand in candidates:
        cid = str(cand.get("id") or "")
        if cid == PLATFORM_INTENT_ID:
            incoming = [str(k) for k in cand.get("keywords", [])]
            # 回放自家旧报告时，表里那条钉死意图的词表与 PLATFORM_INTENT_KEYWORDS 逐字相同
            # （上一跑就是它写进去的）——别把「幂等重放」记成「替换掉了什么」。
            same = ([normalize_text(k) for k in incoming]
                    == [normalize_text(k) for k in PLATFORM_INTENT_KEYWORDS])
            if same:
                already_pinned = True
            else:
                replaced = {"id": cid, "name": str(cand.get("name") or ""),
                            "keywords_dropped": incoming}
            continue
        if cid.startswith(PLATFORM_ABSORB_PREFIX):
            absorbed.append({"id": cid, "name": str(cand.get("name") or ""),
                             "keywords_dropped": list(cand.get("keywords", [])),
                             "hits": int(cand.get("hits") or 0)})
            continue
        kept.append(cand)
    pinned = {
        "id": PLATFORM_INTENT_ID,
        "name": PLATFORM_INTENT_NAME,
        "keywords": list(PLATFORM_INTENT_KEYWORDS),
        "example": [],
        "hits": 0,
        "pinned": True,
    }
    log = {"id": PLATFORM_INTENT_ID, "name": PLATFORM_INTENT_NAME,
           "keywords": list(PLATFORM_INTENT_KEYWORDS),
           "created": replaced is None and not already_pinned,
           "already_pinned": already_pinned, "replaced": replaced, "absorbed": absorbed}
    return [pinned, *kept], log


# --------------------------------------------------------------------------- #
# 图对齐腿（组 graph doc → 跑生产严格校验）/ 导入分级
# --------------------------------------------------------------------------- #

def _binding_id(intent_id: str) -> str:
    """确定性绑定 id（生产 `_ID_RE` 要求 `bnd_<8 hex>`；同输入恒同值=报告可复核）。"""
    return "bnd_" + hashlib.sha1(str(intent_id).encode("utf-8")).hexdigest()[:8]


def suggest_binding(intent_id: str, *, close_step: int = CLOSE_STEP_DEFAULT) -> dict:
    """给一条候选意图的**保守**绑定建议（人审前的默认姿势）。

    - 收线/告别类 → `jump_step` 到收尾步（命中即收线，语义明确）；
    - 其余（含转人工/主管/投诉与全部信息类）→ `notify_human` 打铃：W4 已定「打铃不
      抢话」，AI 照常兜话，**不因一条还未经人审的关键词改变通话流程**——最安全的默认。
    运营面可按需要改成 `play_qa`（罐头快答）或 `jump_step` 到具体步。
    """
    cid = str(intent_id or "")
    tokens = set(_id_tokens(cid))
    if tokens & {"close", "farewell", "goodbye", "end", "bye"}:
        return {"action": "jump_step", "step": int(close_step),
                "why": f"收线类意图 → 跳收尾步 {int(close_step)}"}
    return {"action": "notify_human",
            "why": "默认打铃不抢话（W4 语义）：先观测命中、人审后再改 play_qa/jump_step"}


def build_graph_doc(candidates: list[dict], *, close_step: int = CLOSE_STEP_DEFAULT,
                    catchall: bool = True) -> dict:
    """把候选意图组一份可以直接喂生产 `validate_flow_graph` 的 graph doc。

    每条意图配一条绑定建议（否则触发 P2.2 孤儿意图门）；`catchall=True` 时追加建议的
    兜底意图 `"*"`（keywords 必空、绑定 `notify_human` 不抢话——兜底跳收尾会把每个
    未命中轮都推向收线，太激进，故默认只打铃）。
    """
    intents: list[dict] = []
    bindings: list[dict] = []
    for cand in candidates:
        cid = str(cand.get("id") or "")
        if not cid:
            continue
        intents.append({"id": cid, "label": str(cand.get("name") or ""),
                        "keywords": [str(k) for k in cand.get("keywords", [])]})
        suggestion = suggest_binding(cid, close_step=close_step)
        binding = {"id": _binding_id(cid), "intent": cid,
                   "action": suggestion["action"], "priority": 10}
        if suggestion["action"] == "jump_step":
            binding["step"] = int(suggestion["step"])
        bindings.append(binding)
    if catchall:
        intents.append({"id": CATCHALL_ID, "label": CATCHALL_NAME, "keywords": []})
        bindings.append({"id": _binding_id(CATCHALL_ID), "intent": CATCHALL_ID,
                         "action": "notify_human", "priority": 1000, "once": False})
    return {"version": 1, "intents": intents, "bindings": bindings}


def validate_candidates_graph(candidates: list[dict], *, close_step: int = CLOSE_STEP_DEFAULT,
                              catchall: bool = True) -> dict:
    """组图 + 跑生产严格校验（**必须零错误**）。返回 doc/errors/ok/计数/warnings。"""
    from bok_voice_core.flow_graph import graph_warnings, validate_flow_graph

    doc = build_graph_doc(candidates, close_step=close_step, catchall=catchall)
    text = json.dumps(doc, ensure_ascii=False)
    errors = validate_flow_graph(text)
    return {
        "ok": not errors,
        "errors": errors,
        "intents": len(doc["intents"]),
        "bindings": len(doc["bindings"]),
        "catchall": bool(catchall),
        "close_step": int(close_step),
        "warnings": graph_warnings(text),
        "doc": doc,
    }


def write_import_graph_doc(candidates: list[dict], out_dir: Path, *, template_id: str,
                           close_step: int = CLOSE_STEP_DEFAULT) -> Path:
    """把「可直接导入」候选组一份现成 `graph_json` 落盘（运营可直接拿来用）。

    内容=`build_graph_doc` 的产物（每条意图一条绑定建议 + 建议兜底行），运营可直接
    `PUT /api/templates/{id}`（`graph_json` 字段）或粘进模板表单。**落盘前由调用方跑
    `validate_candidates_graph`**：这个文件是唯一会被人拿去写库的东西，宁可多验一次。
    """
    doc = build_graph_doc(candidates, close_step=close_step, catchall=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"graph-import-{template_id}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def keyword_hit_counts(samples: list[dict], candidates: list[dict]) -> dict[str, int]:
    """关键词（归一化）→ 命中样本数：喂「冷僻词=抄来的碎片」判据。"""
    norm = [normalize_text(s.get("text", "")) for s in samples]
    out: dict[str, int] = {}
    for cand in candidates:
        for kw in cand.get("keywords", []):
            key = normalize_text(kw)
            if not key or key in out:
                continue
            out[key] = sum(1 for text in norm if text and key in text)
    return out


def keyword_is_noisy(keyword: str, *, hits: int | None = None) -> bool:
    """噪声词判据（保守，只降级不删词）：太短/纯数字串/含语气助词/全库只中 ≤1 条的短词。

    「底细」级碎片纯规则识不破（2 字、不是语气词），冷僻门（≤1 命中且 ≤3 字）是
    替代信号：抄自某一条原话的词，通常只命中那一条。
    """
    key = normalize_text(keyword)
    if len(key) < MIN_KEYWORD_CHARS:
        return True
    if _DIGIT_ONLY_RE.match(key):
        return True
    if any(ch in _NOISE_PARTICLES for ch in key):
        return True
    if hits is not None and hits <= _RARE_KEYWORD_MAX_HITS and len(key) <= _RARE_KEYWORD_MAX_CHARS:
        return True
    return False


def classify_candidates(candidates: list[dict], *, coverage: dict, keyword_hits: dict[str, int],
                        renamed_ids: set[str] | None = None,
                        min_keywords: int = MIN_KEYWORDS) -> tuple[list[dict], list[dict]]:
    """把合并后候选分成「可直接导入」与「需人审」两清单（纯离线，判据全可复核）。

    需人审（任一命中）：①覆盖回放零命中（死意图）；②噪声词占比 > 34%；③关键词少于下限。
    **钉死意图豁免噪声判据**：`platform_identify` 的词表是人维护的品牌表，本模板语料里
    没有 淘宝/天猫 命中≠该词有问题，拿「抄碎片」判据去降级它纯属误伤（只保留零命中门）。
    漂移改过名的只作警示不降级（改名本身就是修好的动作）。
    """
    renamed_ids = renamed_ids or set()
    per_intent = coverage.get("per_intent", {})
    ready: list[dict] = []
    review: list[dict] = []
    for cand in candidates:
        cid = str(cand.get("id") or "")
        hits = int(per_intent.get(cid, 0))
        keywords = [str(k) for k in cand.get("keywords", [])]
        pinned = bool(cand.get("pinned"))
        noisy = [] if pinned else [
            k for k in keywords if keyword_is_noisy(k, hits=keyword_hits.get(normalize_text(k)))]
        row = {
            "id": cid, "name": str(cand.get("name") or ""), "hits": hits,
            "keyword_count": len(keywords), "noisy_keywords": noisy,
            "noisy_ratio": (len(noisy) / len(keywords)) if keywords else 1.0,
            "renamed": cid in renamed_ids, "pinned": pinned,
            "keywords": keywords,
        }
        reasons: list[str] = []
        if hits == 0:
            reasons.append("no_coverage_hits")
        if row["noisy_ratio"] > NOISY_KEYWORD_RATIO_MAX:
            reasons.append("noisy_keywords")
        if len(keywords) < int(min_keywords):
            reasons.append("few_keywords")
        row["reasons"] = reasons
        (review if reasons else ready).append(row)
    ready.sort(key=lambda r: (-r["hits"], r["id"]))
    review.sort(key=lambda r: (-r["hits"], r["id"]))
    return ready, review


def coverage_replay(samples: list[dict], candidates: list[dict]) -> dict:
    """离线覆盖回放（零 LLM）：逐条样本测是否命中任一意图关键词（归一化子串）。

    返回 `{"total","covered","rate","per_intent":{id:hits},"uncovered":[样本...]}`，
    uncovered 按频次降序（同频按文本）。
    """
    normalized = [(s, normalize_text(s.get("text", ""))) for s in samples]
    per_intent = {c["id"]: 0 for c in candidates}
    covered_keys: set[str] = set()
    for s, text in normalized:
        if not text:
            continue
        for cand in candidates:
            hit = False
            for kw in cand.get("keywords", []):
                token = normalize_text(kw)
                if token and token in text:
                    hit = True
                    break
            if hit:
                per_intent[cand["id"]] = per_intent.get(cand["id"], 0) + 1
                covered_keys.add(text)
    total = len(normalized)
    covered = len(covered_keys)
    uncovered = [s for s, text in normalized if text and text not in covered_keys]
    uncovered.sort(key=lambda s: (-int(s.get("count", 0)), str(s.get("text", ""))))
    return {
        "total": total,
        "covered": covered,
        "rate": (covered / total) if total else 0.0,
        "per_intent": per_intent,
        "uncovered": uncovered,
    }


def build_mine_prompt(samples: list[dict]) -> str:
    """把一批样本编号成 prompt（含频次，帮模型判主次）。"""
    lines = [f"{i + 1}. [{s['count']}次] {s['text']}" for i, s in enumerate(samples)]
    return f"{_MINE_RULES}\n客户原话（编号. [出现频次] 原话）：\n" + "\n".join(lines)


# --------------------------------------------------------------------------- #
# 数据源（只读）
# --------------------------------------------------------------------------- #

def open_ro(db_path: str) -> sqlite3.Connection:
    """以只读 URI 打开库（结构性只读，杜绝任何写副作用）。"""
    uri = f"file:{Path(db_path).expanduser()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def top_template_id(conn: sqlite3.Connection) -> tuple[str, int]:
    """通话数最多的 template_id（真库当前应是 febeeeebac97/105 通）。"""
    row = conn.execute(
        "SELECT template_id, COUNT(*) c FROM call_sessions "
        "WHERE template_id != '' GROUP BY template_id ORDER BY c DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise SystemExit("no call_sessions with a template_id found")
    return str(row[0]), int(row[1])


def customer_turns(conn: sqlite3.Connection, template_id: str) -> list[str]:
    """该模板全部通话的客户轮文本（A 线：line='a' AND speaker='customer'）。"""
    rows = conn.execute(
        "SELECT t.transcript FROM turns t JOIN call_sessions c ON t.call_id = c.id "
        "WHERE c.template_id = ? AND t.line = 'a' AND t.speaker = 'customer'",
        (template_id,),
    ).fetchall()
    return [str(r[0]) for r in rows if r and r[0]]


# --------------------------------------------------------------------------- #
# LLM（本地环回）
# --------------------------------------------------------------------------- #

def resolve_model() -> str:
    """env 优先；否则 GET /v1/models 取 9B（本机诊断端点三模型共存）。"""
    if MODEL:
        return MODEL
    with urlopen(guard(f"{BASE.rstrip('/')}/v1/models"), timeout=10) as r:
        ids = [m["id"] for m in json.load(r)["data"]]
    if not ids:
        raise SystemExit("no models at local endpoint")
    pick = next((i for i in ids if "9b" in i.lower()), None)
    if pick is None:
        pick = next((i for i in ids if not i.startswith("/")), ids[0])
    return pick


def chat(messages: list[dict], *, model: str, max_tokens: int = 4096,
         timeout: int = 300) -> str:
    """非流式一次 chat 调用；带 SSRF guard。"""
    req = Request(
        guard(f"{BASE.rstrip('/')}/v1/chat/completions"),
        data=json.dumps({
            "model": model, "messages": messages, "max_tokens": max_tokens,
            "temperature": 0.2, "stream": False,
        }).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=timeout) as r:
        payload = json.load(r)
    return str(payload["choices"][0]["message"]["content"])


def mine_candidates(samples: list[dict], *, model: str, batch_size: int,
                    passes: int = 1) -> tuple[list[dict], list[str], list[list[dict]]]:
    """分批喂 LLM 挖候选；**多 pass 合并**（默认 1，CLI 默认 2）。

    返回 `(跨 pass 按 id 合并后的候选, 日志, 每 pass 的候选表)`。

    为什么要多 pass：9B 单次挖掘方差很大（同一份 300 样本三跑实测 27/29/31 个意图、
    覆盖率 57%/64%/57%——批次解析率、是否逐条造意图随温度漂移）。多跑一遍再把
    `merge_candidates`（同 id 并集）+ `dedupe_candidates`（同义/同名/词集）合起来，
    语料里**真实存在**的说法才会被两遍都捞到，覆盖率也随之收敛（见报告 `passes`）。
    """
    batch_log: list[str] = []
    per_pass: list[list[dict]] = []
    for p in range(1, max(1, int(passes)) + 1):
        batches = [samples[i:i + batch_size] for i in range(0, len(samples), batch_size)]
        parsed: list[list[dict]] = []
        for idx, batch in enumerate(batches, 1):
            prompt = build_mine_prompt(batch)
            try:
                raw = chat([{"role": "system", "content": _MINE_SYSTEM},
                            {"role": "user", "content": prompt}], model=model)
            except Exception as exc:  # noqa: BLE001 - 单批失败不毁整跑（网络/超时可重跑）
                batch_log.append(
                    f"pass {p} batch {idx}/{len(batches)}: LLM error {type(exc).__name__}: {exc}")
                continue
            cands = parse_intent_candidates(raw)
            parsed.append(cands)
            line = f"pass {p} batch {idx}/{len(batches)}: {len(cands)} intents, raw {len(raw)} chars"
            batch_log.append(line)
            # 逐批即时打点（`flush=True`）：一次 2-pass 跑 ~17 分钟，攒到最后才打会被
            # 误当成「卡死」（本轮实测踩过：沉默 13 分钟其实在正常挖）。
            print(f"[llm] {line}", flush=True)
        per_pass.append(merge_candidates(parsed))
    return merge_candidates(per_pass), batch_log, per_pass


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #

def build_report(*, template_id: str, template_calls: int, rows: list[str], samples: list[dict],
                 candidates: list[dict], coverage: dict, model: str, thresholds: dict,
                 batch_log: list[str], merge: dict | None = None,
                 platform_pin: dict | None = None, alignment: dict | None = None,
                 import_ready: list[dict] | None = None,
                 needs_review: list[dict] | None = None,
                 passes: list[dict] | None = None) -> dict:
    """组装报告 dict（写盘与 stdout 共用同一份）。"""
    per_intent = coverage["per_intent"]
    cand_rows = []
    for cand in candidates:
        hits = int(per_intent.get(cand["id"], 0))
        cand_rows.append({
            "id": cand["id"], "name": cand["name"], "keywords": cand["keywords"],
            "example": cand.get("example", []), "hits": hits,
            "sample_rate": (hits / coverage["total"]) if coverage["total"] else 0.0,
            "merged_from": list(cand.get("merged_from", [])),
            "pinned": bool(cand.get("pinned")),
        })
    cand_rows.sort(key=lambda r: (-r["hits"], r["id"]))
    intents_ok = len(cand_rows) >= thresholds["min_intents"]
    coverage_ok = coverage["rate"] >= thresholds["min_coverage"]
    # 稳健性（零 LLM，纯离线重放）：多批合并会并出「超 8 词」的意图，单条大意图可能撑起
    # 整体覆盖率。这里把两种「瘦身」后的覆盖率一并记进报告，PASS 的余量一眼可读。
    drop_top = {"id": "", "rate": coverage["rate"]}
    if cand_rows:
        top_id = cand_rows[0]["id"]
        lean = [c for c in candidates if c["id"] != top_id]
        drop_top = {"id": top_id, "rate": coverage_replay(samples, lean)["rate"]}
    capped = [{**c, "keywords": c.get("keywords", [])[:MAX_KEYWORDS]}
              for c in candidates]
    cap_rate = coverage_replay(samples, capped)["rate"]
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "template_id": template_id,
        "template_calls": template_calls,
        "customer_turns_total": len(rows),
        "samples_used": len(samples),
        "model": model,
        "thresholds": thresholds,
        "intent_count": len(cand_rows),
        "candidates": cand_rows,
        "passes": list(passes or []),
        "merge": merge or {},
        "platform_pin": platform_pin or {},
        "graph_alignment": {
            "ok": bool((alignment or {}).get("ok")),
            "errors": list((alignment or {}).get("errors", [])),
            "intents": (alignment or {}).get("intents", 0),
            "bindings": (alignment or {}).get("bindings", 0),
            "catchall": (alignment or {}).get("catchall", False),
            "close_step": (alignment or {}).get("close_step", CLOSE_STEP_DEFAULT),
            "warnings": list((alignment or {}).get("warnings", [])),
            "import_doc_ok": (alignment or {}).get("import_doc_ok"),
            "import_doc_intents": (alignment or {}).get("import_doc_intents"),
            "import_doc_path": (alignment or {}).get("import_doc_path", ""),
        },
        "import_ready": list(import_ready or []),
        "needs_review": list(needs_review or []),
        "coverage": {"total": coverage["total"], "covered": coverage["covered"],
                     "rate": coverage["rate"]},
        "coverage_sensitivity": {
            "drop_top_intent": drop_top,
            "cap_keywords_per_intent": {"limit": MAX_KEYWORDS, "rate": cap_rate},
        },
        "keywords_per_intent": {"merged_cap": CANDIDATE_MAX_KEYWORDS,
                                "llm_output_contract": MAX_KEYWORDS},
        "uncovered_top20": coverage["uncovered"][:20],
        "batch_log": batch_log,
        "pass": {"intents": intents_ok, "coverage": coverage_ok,
                 "overall": bool(intents_ok and coverage_ok)},
    }


def print_report(rep: dict) -> None:
    """stdout 人类可读版。"""
    cov = rep["coverage"]
    print("=" * 72)
    print(f"意图候选挖掘 dry-run  模板={rep['template_id']}  通话={rep['template_calls']}  "
          f"客户轮={rep['customer_turns_total']}  抽样={rep['samples_used']}  模型={rep['model']}")
    print("=" * 72)
    for row in rep.get("passes", []):
        print(f"挖掘 pass {row['pass']}：{row['intents']} 个意图，单遍覆盖 {row['coverage'] * 100:.1f}%"
              f"（多遍合并后见下）")
    merge = rep.get("merge") or {}
    if merge:
        print(f"去重合并：{merge.get('before', 0)} → {merge.get('after', 0)} 个意图"
              f"（并掉 {merge.get('merged_away', 0)} 个，{merge.get('merged_groups', 0)} 组；"
              f"漂移改名 {len(merge.get('renames', []))} 条）")
        for group in merge.get("merges", []):
            print(f"  [{'+'.join(group['reasons'])}] {' + '.join(group['from'])} → {group['id']}"
                  f"  ({group['name']}, 词 {group['keywords_in']}→{group['keywords_out']})")
        for ren in merge.get("renames", []):
            print(f"  [drift] {ren['id']}: 「{ren['old_name']}」→「{ren['new_name']}」")
    pin = rep.get("platform_pin") or {}
    if pin:
        state = "新建" if pin.get("created") else ("幂等重放" if pin.get("already_pinned") else "替换旧同 id 候选")
        print(f"平台钉死：{pin['id']}（{pin['name']}）{state}，"
              f"词表 {len(pin.get('keywords', []))} 条"
              f"{'，吸收 ' + ','.join(a['id'] for a in pin.get('absorbed', [])) if pin.get('absorbed') else ''}")
    print(f"候选意图 {rep['intent_count']} 个（门槛 ≥{rep['thresholds']['min_intents']}）")
    print(f"{'id':<28}{'名称':<14}{'关键词':<6}{'命中':<6}{'覆盖率'}")
    for row in rep["candidates"]:
        print(f"{row['id']:<28}{row['name']:<14}{len(row['keywords']):<6}"
              f"{row['hits']:<6}{row['sample_rate'] * 100:5.1f}%  {','.join(row['keywords'])}")
    print("-" * 72)
    print(f"覆盖回放：{cov['covered']}/{cov['total']} = {cov['rate'] * 100:.1f}% "
          f"（门槛 ≥{rep['thresholds']['min_coverage'] * 100:.0f}%）")
    sens = rep.get("coverage_sensitivity")
    if sens:
        print(f"  稳健性：去掉命中第一的意图({sens['drop_top_intent']['id']}) → "
              f"{sens['drop_top_intent']['rate'] * 100:.1f}%；"
              f"每意图截到 {sens['cap_keywords_per_intent']['limit']} 词 → "
              f"{sens['cap_keywords_per_intent']['rate'] * 100:.1f}%")
    align = rep.get("graph_alignment") or {}
    if align:
        verdict = "零错误 ✓" if align.get("ok") else f"{len(align.get('errors', []))} 个错误 ✗"
        print(f"图对齐（validate_flow_graph）：{verdict}  "
              f"意图={align.get('intents')} 绑定={align.get('bindings')} "
              f"兜底={'*' if align.get('catchall') else '无'} 收尾步={align.get('close_step')}")
        if align.get("import_doc_ok") is not None:
            print(f"  可直接导入子集再验：{'零错误 ✓' if align['import_doc_ok'] else '有错误 ✗'}  "
                  f"意图={align.get('import_doc_intents')}  → {align.get('import_doc_path', '')}")
        for err in align.get("errors", []):
            print(f"  ! {err}")
    print(f"可直接导入 {len(rep.get('import_ready', []))} 个 / "
          f"需人审 {len(rep.get('needs_review', []))} 个")
    for row in rep.get("import_ready", []):
        print(f"  ✓ {row['id']:<28}{row['name']:<14} hits={row['hits']:<4} "
              f"→ {row.get('action')}{' ' + str(row.get('step')) if row.get('step') else ''}")
    for row in rep.get("needs_review", []):
        print(f"  ? {row['id']:<28}{row['name']:<14} hits={row['hits']:<4} "
              f"{','.join(row.get('reasons', []))}"
              f"{'  噪声词=' + ','.join(row.get('noisy_keywords', [])) if row.get('noisy_keywords') else ''}")
    print(f"未覆盖原话 top{min(10, len(rep['uncovered_top20']))}：")
    for s in rep["uncovered_top20"][:10]:
        print(f"  {s['count']:>3}×  {s['text']}")
    verdict = "PASS ✓" if rep["pass"]["overall"] else "FAIL ✗"
    print("-" * 72)
    print(f"结论：{verdict}  意图={rep['pass']['intents']} 覆盖={rep['pass']['coverage']}")
    print("=" * 72)


def write_json(rep: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"intent-mine-{rep['template_id']}-{stamp}.json"
    path.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_candidates_file(path: str) -> tuple[list[dict], str]:
    """从既有报告或候选 JSON 里读候选（支持整份报告或裸数组），只做回放。

    返回 `(候选, 模型标签)`——报告文件自带 `model` 时沿用，纯候选数组则标 `(replay)`。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    label = "(replay)"
    if isinstance(data, dict) and isinstance(data.get("candidates"), list):
        label = str(data.get("model") or label)
        data = data["candidates"]
    if not isinstance(data, list):
        raise SystemExit("candidates-file must be a JSON array or a report with .candidates")
    # 候选文件不是模型输出：按**候选级**上限（32）归一，别用单批输出契约（8）二次砍词
    # ——否则「跑一次 64% / 回放一次 59%」这类不可复现的假象就会冒出来。
    cands: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        cand = _normalize_candidate(item, max_keywords=CANDIDATE_MAX_KEYWORDS, min_keywords=1)
        if cand is not None:
            cands.append(cand)
    return cands, label


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="意图候选挖掘 dry-run 探针（只读）")
    ap.add_argument("--db", default=DEFAULT_DB, help="真库路径（以 mode=ro 打开）")
    ap.add_argument("--template", default="", help="模板 id；默认取通话数最多者")
    ap.add_argument("--limit", type=int, default=300, help=f"抽样上限（{MIN_SAMPLES}-{MAX_SAMPLES}）")
    ap.add_argument("--batch-size", type=int, default=30, help="每批喂 LLM 的样本数")
    ap.add_argument("--passes", type=int, default=2,
                    help="挖掘遍数（多遍按 id 合并再走去重；9B 单遍方差大，默认 2）")
    ap.add_argument("--min-intents", type=int, default=8)
    ap.add_argument("--min-coverage", type=float, default=0.70)
    ap.add_argument("--close-step", type=int, default=CLOSE_STEP_DEFAULT,
                    help="收线类意图的 jump_step 建议目标（模板收尾步）")
    ap.add_argument("--no-platform-pin", action="store_true",
                    help="关掉平台意图钉死（对照用，默认开）")
    ap.add_argument("--candidates-file", action="append", default=[], metavar="PATH",
                    help="只回放既有候选，不打 LLM（可给多次：多个文件=多遍挖掘结果合并）")
    ap.add_argument("--out-dir", default=str(_OUT_DIR))
    args = ap.parse_args(argv)

    limit = min(max(int(args.limit), 1), MAX_SAMPLES)
    if limit < MIN_SAMPLES:
        print(f"[warn] --limit {limit} < {MIN_SAMPLES}，样本偏少会低估覆盖率", file=sys.stderr)

    conn = open_ro(args.db)
    try:
        template_id, calls = (args.template, None) if args.template else top_template_id(conn)
        if calls is None:
            row = conn.execute("SELECT COUNT(*) FROM call_sessions WHERE template_id = ?",
                               (template_id,)).fetchone()
            calls = int(row[0]) if row else 0
        rows = customer_turns(conn, template_id)
    finally:
        conn.close()

    samples = dedup_samples(rows, min_chars=3, limit=limit)
    print(f"[data] template={template_id} calls={calls} customer_turns={len(rows)} "
          f"unique_samples={len(samples)}")

    batch_log: list[str] = []
    pass_rows: list[dict] = []
    if args.candidates_file:
        # 回放模式：可给多个文件（多遍挖掘的产物），按同一套合并管线处理——与
        # 「一次跑多遍」等价（同 id 并集 → 去重合并），区别只是 LLM 结果来自缓存。
        loaded: list[list[dict]] = []
        for path in args.candidates_file:
            cands, label = load_candidates_file(path)
            loaded.append(cands)
            model = label
            print(f"[replay] {len(cands)} candidates from {path}")
            pass_rows.append({"pass": len(pass_rows) + 1, "source": path,
                              "intents": len(cands),
                              "coverage": coverage_replay(samples, cands)["rate"]})
        raw = merge_candidates(loaded)
    else:
        model = resolve_model()
        passes = max(1, int(args.passes))
        t0 = time.monotonic()
        raw, batch_log, per_pass = mine_candidates(
            samples, model=model, batch_size=int(args.batch_size), passes=passes)
        print(f"[llm] model={model} passes={passes} {time.monotonic() - t0:.1f}s, "
              f"merged-by-id {len(raw)} intents")
        for idx, cands in enumerate(per_pass, 1):
            rate = coverage_replay(samples, cands)["rate"]
            pass_rows.append({"pass": idx, "source": f"llm-pass-{idx}",
                              "intents": len(cands), "coverage": rate})
            print(f"[pass {idx}] {len(cands)} intents, 单遍覆盖 {rate * 100:.1f}%")

    # 合并前逐意图命中数就地附着（合并要「命中数相加」；回放文件里的旧 hits 一律重算，
    # 免得拿上轮口径当本轮读数）。
    pre_cov = coverage_replay(samples, raw)
    raw = [{**c, "hits": int(pre_cov["per_intent"].get(c["id"], 0))} for c in raw]
    candidates, merge_log = dedup_candidates(raw)
    print(f"[dedup] {merge_log['before']} → {merge_log['after']} intents "
          f"(merged_away={merge_log['merged_away']}, renames={len(merge_log['renames'])})")
    pin_log: dict = {}
    if args.no_platform_pin:
        print("[pin] skipped (--no-platform-pin)")
    else:
        candidates, pin_log = pin_platform_intent(candidates)
        print(f"[pin] {PLATFORM_INTENT_ID} keywords={len(pin_log['keywords'])} "
              f"created={pin_log['created']} absorbed={[a['id'] for a in pin_log['absorbed']]}")

    coverage = coverage_replay(samples, candidates)
    kw_hits = keyword_hit_counts(samples, candidates)
    renamed_ids = {r["id"] for r in merge_log["renames"]}
    ready_rows, review_rows = classify_candidates(
        candidates, coverage=coverage, keyword_hits=kw_hits, renamed_ids=renamed_ids)
    suggestions = {c["id"]: suggest_binding(c["id"], close_step=int(args.close_step))
                   for c in candidates}
    import_ready = [{**row, **suggestions.get(row["id"], {})} for row in ready_rows]
    needs_review = [{**row, **suggestions.get(row["id"], {})} for row in review_rows]
    alignment = validate_candidates_graph(candidates, close_step=int(args.close_step),
                                          catchall=True)
    print(f"[validate] full-doc ok={alignment['ok']} "
          f"intents={alignment['intents']} bindings={alignment['bindings']} "
          f"errors={alignment['errors']}")
    if import_ready:
        ready_cands = [c for c in candidates if c["id"] in {r["id"] for r in import_ready}]
        sub = validate_candidates_graph(ready_cands, close_step=int(args.close_step),
                                        catchall=True)
        print(f"[validate] import-ready doc ok={sub['ok']} intents={sub['intents']} "
              f"errors={sub['errors']}")
        alignment["import_doc_ok"] = sub["ok"]
        alignment["import_doc_intents"] = sub["intents"]
        if sub["ok"]:
            doc_path = write_import_graph_doc(ready_cands, Path(args.out_dir),
                                              template_id=template_id,
                                              close_step=int(args.close_step))
            alignment["import_doc_path"] = str(doc_path)
            print(f"[graph] 可直接导入 graph_json 已落盘：{doc_path}")
    # graph doc 体积很大，报告只留对齐结论（doc 本身可由 build_graph_doc 随时重建）。
    alignment.pop("doc", None)

    rep = build_report(template_id=template_id, template_calls=calls, rows=rows, samples=samples,
                       candidates=candidates, coverage=coverage, model=model,
                       thresholds={"min_intents": int(args.min_intents),
                                   "min_coverage": float(args.min_coverage)},
                       batch_log=batch_log, merge=merge_log, platform_pin=pin_log,
                       alignment=alignment, import_ready=import_ready,
                       needs_review=needs_review, passes=pass_rows)
    print_report(rep)
    path = write_json(rep, Path(args.out_dir))
    print(f"报告已写入：{path}")
    return 0 if rep["pass"]["overall"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
