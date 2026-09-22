#!/usr/bin/env python3
"""意图候选挖掘 dry-run 探针（执行计划 §48 P2.1，纯只读 + 本地 LLM）。

## 要证明什么

系统有确定性意图图 `graph_json`（`intents[]{id,name,keywords[],priority}` + 绑定边），
但生产 12 个模板的图全是空的——**没有数据就没有意图引擎**。本探针从历史通话的
客户轮转写里用本地 LLM 挖出可用的意图候选集，并离线回放覆盖率，回答：

  「这批转写里到底能不能挖出 ≥8 个意图、关键词能否覆盖 ≥60% 的真实客户话？」

## 四步（全程只读，不写库、不提交 git）

1. **抽样**：取通话数最多的模板，其全部通话的客户轮（`line='a' AND
   speaker='customer'`），按归一化文本去重 + 频次排序，过滤 <3 字，取 200-400 条。
2. **挖掘**：分批喂本地 9B（默认 :1237），让它聚类成意图，输出严格 JSON：
   `[{id(英文snake_case <主体>_<关系>), name(中文), keywords(摘自原话,3-8), example(2条)}]`。
   prompt 明确防碎片误判（单字/语气词/纯数字/断句碎片不算意图）。解析宽容（剥围栏/补尾括号）。
3. **覆盖回放**（不打 LLM）：对全部抽样客户话逐条测是否命中任一候选意图关键词——
   归一化语义与生产 `flow_graph.normalize_graph_text` 逐字节同款（双侧剥标点/空格 + casefold
   子串），算覆盖率与逐意图命中数。
4. **报告**：stdout + `reports/intent-mine/*.json`：候选全表 + 覆盖率 + 未覆盖 top20。

## 安全（Mimosa SSRF 加固）

本探针是本地诊断工具，LLM 端点只允许环回 127.0.0.1/localhost + http(s)；拒绝其他一切主机。
数据库以 `mode=ro` URI 打开，结构性只读。

## 门槛

候选意图 ≥8 且覆盖率 ≥60% 记为 PASS；不达标也把未覆盖榜带回来留给下轮迭代。

## 用法

    .venv312/bin/python scripts/probe_intent_mine.py
    .venv312/bin/python scripts/probe_intent_mine.py --limit 300 --batch-size 40
    .venv312/bin/python scripts/probe_intent_mine.py --candidates-file reports/intent-mine/x.json  # 只回放
"""

from __future__ import annotations

import argparse
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
- 内容类：查询物流/单号、询问赔偿、确认购买平台、加 WhatsApp/联系方式、商品细节、退款进度；
- 会话类：确认应承、否定拒绝、听不懂要求重说、告别结束、找真人/转接、催促跟进、身份与真实性质疑。

硬性要求：
- keywords 必须**逐字摘自**客户原话（连写、不带标点空格），不要自己编词、不要翻译；
- keywords 取 2-6 字的**常用说法**（能泛化到同义说法），不要整句照抄、不要单字，不要纯数字串；
- 宁可少而准，不要滥：泛词（你/我/佢/嘅/係/啦/啊 等虚词）不算关键词；
- 单字、语气词、纯数字串、ASR 断句碎片（如「咁」「誒」「六四三二」「就一」）**不算意图**，直接忽略；
- 同一意图只输出一条，**不要给每条原话各造一个意图**，id 里不要带编号（勿出现 xxx1/xxx2）；
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


def mine_candidates(samples: list[dict], *, model: str, batch_size: int) -> tuple[list[dict], list[str]]:
    """分批喂 LLM 挖候选；返回 (合并候选, 每批解析条数日志)。"""
    batches = [samples[i:i + batch_size] for i in range(0, len(samples), batch_size)]
    parsed: list[list[dict]] = []
    log: list[str] = []
    for idx, batch in enumerate(batches, 1):
        prompt = build_mine_prompt(batch)
        try:
            raw = chat([{"role": "system", "content": _MINE_SYSTEM},
                        {"role": "user", "content": prompt}], model=model)
        except Exception as exc:  # noqa: BLE001 - 单批失败不毁整跑（网络/超时可重跑）
            log.append(f"batch {idx}/{len(batches)}: LLM error {type(exc).__name__}: {exc}")
            continue
        cands = parse_intent_candidates(raw)
        parsed.append(cands)
        log.append(f"batch {idx}/{len(batches)}: {len(cands)} intents, raw {len(raw)} chars")
    return merge_candidates(parsed), log


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #

def build_report(*, template_id: str, template_calls: int, rows: list[str], samples: list[dict],
                 candidates: list[dict], coverage: dict, model: str, thresholds: dict,
                 batch_log: list[str]) -> dict:
    """组装报告 dict（写盘与 stdout 共用同一份）。"""
    per_intent = coverage["per_intent"]
    cand_rows = []
    for cand in candidates:
        hits = int(per_intent.get(cand["id"], 0))
        cand_rows.append({
            "id": cand["id"], "name": cand["name"], "keywords": cand["keywords"],
            "example": cand.get("example", []), "hits": hits,
            "sample_rate": (hits / coverage["total"]) if coverage["total"] else 0.0,
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
    ap.add_argument("--min-intents", type=int, default=8)
    ap.add_argument("--min-coverage", type=float, default=0.60)
    ap.add_argument("--candidates-file", default="", help="只回放既有候选，不打 LLM")
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
    if args.candidates_file:
        candidates, model = load_candidates_file(args.candidates_file)
        print(f"[replay] {len(candidates)} candidates from {args.candidates_file}")
    else:
        model = resolve_model()
        t0 = time.monotonic()
        candidates, batch_log = mine_candidates(samples, model=model, batch_size=int(args.batch_size))
        for line in batch_log:
            print(f"[llm] {line}")
        print(f"[llm] model={model} {time.monotonic() - t0:.1f}s, merged {len(candidates)} intents")

    coverage = coverage_replay(samples, candidates)
    rep = build_report(template_id=template_id, template_calls=calls, rows=rows, samples=samples,
                       candidates=candidates, coverage=coverage, model=model,
                       thresholds={"min_intents": int(args.min_intents),
                                   "min_coverage": float(args.min_coverage)},
                       batch_log=batch_log)
    print_report(rep)
    path = write_json(rep, Path(args.out_dir))
    print(f"报告已写入：{path}")
    return 0 if rep["pass"]["overall"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
