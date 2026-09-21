"""E1 snippet 轨首批词表候选挖掘（2026-09-21，vocab-skill 范式）。

机制已落地于 packages/core/bok_voice_core/snippets.py（数字铁律 / 空 trigger /
≥2 字 / 长度守卫）。本脚本只产出**候选清单**给人工确认——
**绝不写进运行时代码或词表**（人工确认是铁律，本脚本零 side effect）。

两步法（plan §26 / vocab-skill 范式）:
  1. 领域词表（正形）：业务=粤语/中文快递集运外呼。可用业务库
     conversation_templates.hotwords 与 object_profiles.courier 佐证。
  2. 对每个正形词调本地 LLM 生成 3-8 个「ASR 可能听错的形态」，
     再拿候选 trigger 去真实转写语料里**实证回查**——只有真实出现过
     （evidence>=1）的候选才入选；纯 LLM 幻想进 discarded。

模型档（2026-09-21 批次 3）：默认 4B（:1235）。4B 变体召回不足（首批实测
candidates=1/discarded=77），故提供 `--model-9b`（:1237 huihui 9B）换生成端；
生成端只影响候选覆盖面，实证回查与守卫两条闸不因换模型放松。

语言分域（2026-09-21 批次 3，配合 snippets.SnippetRule.lang）:此前「形态多数
出现在非 zh 通话」= 直接丢弃（`lang_context_correct`）——那在只有单份语言无关
词表的年代是对的（全局替换会改坏粤语通话的正确繁体）。现在规则可带 `lang`，
3b 升级为三档（证据驱动，fail-safe 方向「宁可漏改、不可改坏」）:

  * zh 证据=0 且非 zh 出现过 → 仍是 `lang_context_correct` 丢弃（那是别语言的
    正确形态，收窄成 zh 只会变成永不命中的死规则）；
  * 非 zh 出现过 **且** zh 也出现过 → 入选但打 `lang="zh"`（粤语通话逐字不动、
    中文通话才改）。**不设「非 zh 占比」门槛**：占比再低也证明该形态真实存在于
    那些语言；改坏正确转写（污染下游匹配面）的代价 > 漏改一个已存在的错
    （漏改=维持现状）；
  * 非 zh 零出现 → 全语言（`lang=""`），安全。

数据源（全只读）:
  - 业务库 turns 用户轮（role in user/me/other，排除测试对象前缀）
  - /tmp/r1_gaps_raw.json（若缺则从 CP :8000 llm-gaps 只读拉）

安全边界:
  - 网络仅限白名单守卫后的 http://127.0.0.1:{1235|1237} 与 :8000（host/port 双验）。
  - 术语：语言值只用 zh/cantonese/en（不使用任何旧的粤语拼写字面量）。

用法:
  python3 scripts/snippet_seed_mining.py                 # 4B 挖掘 + 落盘
  python3 scripts/snippet_seed_mining.py --model-9b      # 9B 挖掘（变体召回更高档）
  python3 scripts/snippet_seed_mining.py --dry-run       # 只打印不落盘

**本脚本产出仅供人工确认，绝不写进运行时代码或词表**（`_SNIPPET_SEED` 由人工
确认后灌入）——脚本零 side effect 是指对仓库/运行时零写入，落盘的候选 JSON
只是审计产物。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "packages" / "core") not in sys.path:
    sys.path.insert(0, str(ROOT / "packages" / "core"))

from bok_voice_core.snippets import (  # noqa: E402
    SnippetRule,
    normalize_key,
    validate_rule,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DB_PATH = Path("/Users/halo/Library/Application Support/BokVoice/bok_voice.db")
GAPS_PATH = Path("/tmp/r1_gaps_raw.json")
OUT_PATH = ROOT / "scripts" / ".snippet_candidates.20260921.json"

LLM_HOST = "127.0.0.1"
LLM_PORT = 1235
LLM_PORT_9B = 1237  # 后台重活专线（AGENTS.md）：settle/judge 同款 9B，生成端换档用
CP_HOST = "127.0.0.1"
CP_PORT = 8000
LLM_MODEL = "/Users/halo/.lmstudio/models/avan-ag/Qwen3.5-4B-Uncensored-MLX-4bit"
LLM_MODEL_9B = "/Users/halo/.lmstudio/models/huihui-ai/Huihui-Qwen3.5-9B-abliterated-mlx-4bit"

MAX_VARIANTS_PER_WORD = 8
LLM_MAX_TOKENS = 400

# 测试对象前缀族（AGENTS.md 同款；本机实际字典里出现过的变体一并收）
_TEST_OBJECT_RE = re.compile(
    r"^(E2E-|soak\d*-?|并发|LOAD-|边角-|多轮-|probe|PROBE-|探针-|幽灵-)", re.I
)

# 领域正形词表（正形）。来源=任务给定 20 词，业务库热词/快递公司佐证见 build_canonical()。
CANONICAL_WORDS: tuple[str, ...] = (
    "拼多多",
    "京东",
    "顺丰",
    "集运",
    "转运",
    "理赔",
    "赔偿",
    "运单",
    "单号",
    "仓库",
    "专员",
    "快递",
    "包裹",
    "到货",
    "发货",
    "WhatsApp",
    "微信",
    "截图",
    "赔偿金",
    "物流",
)

_WS_RE = re.compile(r"\s+")

_SYSTEM_PROMPT = (
    "你是中文语音识别（ASR）纠错专家，业务是粤语/普通话快递集运外呼电话。\n"
    "给定一个领域正形词，你要预测 ASR 转写时可能出现的【错误形态】——"
    "即模型把正确词听错后输出的字符串。\n"
    "错误类型只有五类：\n"
    "1) 同音替换（如 赔偿→培偿、单号→单浩）\n"
    "2) 音近替换（如 集运→集雲、专员→专園）\n"
    "3) 繁简误出（正形是简体但 ASR 出了繁体，如 单号→單號、快递→快遞）\n"
    "4) 边界误切（多字/少字/串字，如 顺丰→顺风、理赔→李赔）\n"
    "5) CJK-Latin 混淆（如 WhatsApp→whatsapp、微信→WeChat）\n"
    "要求：给出最多 8 条、尽量各不相同、覆盖不同错误类型的错形；"
    "禁止把正形原样输出。\n"
    '只输出纯 JSON，形如 {"variants": ["错形1", "错形2"]}，'
    "不要解释，不要 markdown 围栏。"
)


# ---------------------------------------------------------------------------
# 白名单守卫（发请求前必过）
# ---------------------------------------------------------------------------

class UrlGuardError(RuntimeError):
    """URL 未通过白名单守卫。"""


def guard_url(url: str, allowed: tuple[tuple[str, int], ...]) -> str:
    """校验 URL：只允许 http、host/port 命中白名单。返回原 URL，否则 raise。

    这是「网络仅限 127.0.0.1:1235/:8000」这条硬边界的单点执行处。
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http":
        raise UrlGuardError(f"scheme not allowed: {parsed.scheme!r}")
    host = parsed.hostname or ""
    port = parsed.port
    if (host, port) not in allowed:
        raise UrlGuardError(f"host/port not allowed: {host}:{port}")
    return url


CP_ALLOW: tuple[tuple[str, int], ...] = ((CP_HOST, CP_PORT),)


def llm_allow(port: int) -> tuple[tuple[str, int], ...]:
    """生成端白名单：只放行本机 1235（4B）/ 1237（9B）两个已知端口。

    换档（--model-9b）也必须过守卫——端口是显式参数，不是「白名单外随便填」。
    """
    if port not in (LLM_PORT, LLM_PORT_9B):
        raise UrlGuardError(f"LLM port not allowed: {port}")
    return ((LLM_HOST, port),)


def _post_json(url: str, payload: dict, timeout: int, allowed: tuple) -> dict:
    guard_url(url, allowed)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (guarded)
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str, timeout: int, allowed: tuple) -> object:
    guard_url(url, allowed)
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (guarded)
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# 语料装载
# ---------------------------------------------------------------------------

def load_turns_corpus(db_path: Path) -> list[tuple[str, str]]:
    """业务库用户轮（role in user/me/other），排除测试对象前缀。只读连接。

    返回 [(transcript, language)]——language 用于反向安全检查：某个形态若多数
    出现在非正形语言的通话里（如繁体出现在粤语通话），它就是该语言的正确输出，
    全局替换会改坏正确转写。
    """
    if not db_path.exists():
        return []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            """
            SELECT t.transcript, op.display_name, cs.language
            FROM turns t
            JOIN call_sessions cs ON cs.id = t.call_id
            LEFT JOIN object_profiles op ON op.id = cs.object_id
            WHERE t.role IN ('user', 'me', 'other')
            """
        ).fetchall()
    finally:
        con.close()
    out: list[tuple[str, str]] = []
    for transcript, display_name, language in rows:
        if display_name and _TEST_OBJECT_RE.match(str(display_name)):
            continue  # 测试对象前缀：整通剔除
        text = str(transcript or "").strip()
        if text:
            out.append((text, str(language or "")))
    return out


def load_gaps_corpus(gaps_path: Path) -> list[tuple[str, int, str]]:
    """漏网轮语料：本地文件优先；缺失则从 CP 只读拉。

    返回 [(customer_text, weight, lang)]，weight 取报告里的 count（近似出现次数）。
    """
    raw: object | None = None
    if gaps_path.exists():
        try:
            raw = json.loads(gaps_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
    if raw is None:
        url = (
            f"http://{CP_HOST}:{CP_PORT}/api/stats/llm-gaps"
            f"?account_id=acc-001&min_calls=1&limit=80"
        )
        try:
            raw = _get_json(url, timeout=30, allowed=CP_ALLOW)
        except (urllib.error.URLError, UrlGuardError, ValueError):
            raw = []
    items = raw if isinstance(raw, list) else raw.get("gaps", []) if isinstance(raw, dict) else []
    out: list[tuple[str, int, str]] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        text = str(it.get("customer_text") or "").strip()
        if not text:
            continue
        try:
            weight = int(it.get("count") or 1)
        except (TypeError, ValueError):
            weight = 1
        out.append((text, max(1, weight), str(it.get("lang") or "")))
    return out


# ---------------------------------------------------------------------------
# LLM 变体生成
# ---------------------------------------------------------------------------

def _extract_variants(raw_text: str) -> list[str]:
    """从 4B 输出里稳健抽取变体列表。

    4B 在 temp=0 下常吐到 max_tokens 截断/尾段复读，故不依赖整体 JSON 合法：
    先试 json.loads；失败则 regex 抽全部双引号串（丢掉键名 'variants'）。
    """
    text = str(raw_text or "").strip()
    variants: list[str] = []
    parsed = None
    try:
        parsed = json.loads(text)
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
            except ValueError:
                parsed = None
    if isinstance(parsed, dict):
        got = parsed.get("variants")
        if isinstance(got, list):
            variants = [str(v) for v in got]
    elif isinstance(parsed, list):
        variants = [str(v) for v in parsed]
    if not variants:
        variants = [
            m for m in re.findall(r'"([^"\n]{1,40})"', text) if m != "variants"
        ]
    # 去重（保序）+ 去掉等于正形的（调用方再补正形过滤）
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        v = v.strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
        if len(out) >= MAX_VARIANTS_PER_WORD:
            break
    return out


def generate_variants(word: str, model: str, port: int) -> list[str]:
    """调本地 LLM 生成一个正形词的错形变体；失败重试 1 次后返回 []。

    生成端（model/port）由调用方给定：4B(:1235) 默认档，9B(:1237) 高召回档。
    """
    url = f"http://{LLM_HOST}:{port}/v1/chat/completions"
    allowed = llm_allow(port)
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"正形词：「{word}」"},
    ]
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": LLM_MAX_TOKENS,
    }
    for attempt in range(2):  # 1 次 + 重试 1 次
        try:
            resp = _post_json(url, payload, timeout=120, allowed=allowed)
            content = resp["choices"][0]["message"]["content"]
            variants = _extract_variants(content)
            if variants:
                return variants
        except (urllib.error.URLError, UrlGuardError, KeyError, IndexError, ValueError):
            pass
    return []


# ---------------------------------------------------------------------------
# 实证回查 + 反向安全检查
# ---------------------------------------------------------------------------

def _compact(text: str) -> str:
    """去全部空白 + lower（镜像 snippets.normalize_key 的匹配面）。"""
    return normalize_key(text)


# 4B 偶发把「错形→正形」这种非转写物当变体输出；这些不是真实 ASR 错形，先剔除。
_MALFORMED_MARKERS = ("→", "->", "=>", "[", "]", "→", "：")


def is_malformed_variant(variant: str) -> bool:
    return any(m in variant for m in _MALFORMED_MARKERS)


# 词表 dump 行的结构性判定（2026-09-21 批次 3 实弹发现）：挖掘语料里混着 ASR
# 「热词失焦」行——整行是热词表本身被抄出来（如「單號，運單，賠償，運費，專員，
# 集運，時效，上門，追蹤，核實，WhatsApp，微信。」）。那不是客户话，拿它当变体
# 的实证会把 dump 伪影**自我放大**（本轮第一批候选 專員/京東 的证据行全是这种行）。
# E2（hotword_leak）在运行时已把这类行清洗掉/丢弃，挖掘侧必须同样前置剔除。
# 判定走**结构性**而非词表比对：dump 行真实长什么样与具体词表无关，且此处拿不到
# 该通通话的 effective 词表（turns 表不存词表）。签名=「标点切出的 token 很多（≥6）、
# 且绝大多数（≥80%）是短块（≤4 字）」——真实客户话要么 token 少、要么夹杂长句块。
# 阈值取密度而非「最长 token ≤ N」：dump 行里混着 `WhatsApp`（8 字符）这种长英词，
# 按最长块判会整批漏网（首版实弹实测：3459 行语料只逮到 2 行）。
_DUMP_SPLIT_RE = re.compile(r"[，,、；;。.]+")
DUMP_MIN_TOKENS = 6
DUMP_SHORT_CHARS = 4
DUMP_SHORT_RATIO = 0.8
# 报号句与 dump 行同形（都是「一串短块」），但它是**真实客户话**且语义关键
# （WhatsApp/单号捕获路径）：`WhatsApp係一、二、三、四、五、六、七。係。` 首版
# 被误判成 dump。故纯数词块不计入短块——报号行因此达不到短块密度而落选。
_NUMERAL_TOKEN_RE = re.compile(r"^[0-9０-９一二三四五六七八九十百千萬万零両两]+$")


def looks_like_hotword_dump(text: str) -> bool:
    """整行是否呈词表 dump 形态（见上方说明）。"""
    tokens = [t.strip() for t in _DUMP_SPLIT_RE.split(str(text or "")) if t.strip()]
    if len(tokens) < DUMP_MIN_TOKENS:
        return False
    short = sum(
        1
        for t in tokens
        if len(t) <= DUMP_SHORT_CHARS and not _NUMERAL_TOKEN_RE.match(t)
    )
    return short / len(tokens) >= DUMP_SHORT_RATIO


def count_occurrences(compact_text: str, compact_trigger: str) -> int:
    if not compact_trigger:
        return 0
    return compact_text.count(compact_trigger)


def build_freq_vocab(compact_texts: list[str], threshold: int = 3, top: int = 200) -> set[str]:
    """用语料 n-gram（2-4 字）词频 top-N 近似「高频正确词」。"""
    counter: Counter[str] = Counter()
    for text in compact_texts:
        n = len(text)
        for size in (2, 3, 4):
            for i in range(n - size + 1):
                counter[text[i : i + size]] += 1
    frequent = [gram for gram, c in counter.most_common() if c >= threshold]
    return set(frequent[:top])


def decide_lang_scope(lang_weight: dict) -> tuple[str, str]:
    """按语言分布决定分域；返回 ``(rule_lang, discard_reason)``（reason 非空=丢弃）。

    三档（fail-safe 方向「宁可漏改、不可改坏」）：

    * **非 zh 零出现** → ``("", "")`` 全语言，安全；
    * **非 zh 出现过 且 zh 也出现过** → ``("zh", "")``：收窄 zh 专属。**不设「非 zh
      占比」门槛**——占比再低也证明该形态真实存在于那些语言；改坏正确转写（污染
      下游匹配面）的代价 > 漏改一个已存在的错（漏改=维持现状）；
    * **非 zh 出现过 但 zh 证据=0** → ``("", "lang_context_correct")`` 丢弃：那是
      别语言的正确形态，收窄成 zh 只会变成永不命中的死规则。
    """
    zh_weight = int(lang_weight.get("zh", 0) or 0)
    non_zh = sum(int(w or 0) for lg, w in lang_weight.items() if lg and lg != "zh")
    if non_zh <= 0:
        return "", ""
    if zh_weight <= 0:
        return "", "lang_context_correct"
    return "zh", ""


def reverse_safety_reason(
    trigger: str,
    canonical_words: list[str],
    freq_vocab: set[str],
) -> str:
    """反向安全检查：trigger 若会命中正确转写则丢弃。返回原因码（空串=通过）。

    - contains_canonical：trigger 含某个正形词（替换会伤到正确文本）
    - substring_of_canonical：trigger 是某个正形词的子串
    - freq_correct_word：trigger 本身就是语料高频词（正确用法）
    - substring_of_freq_word：trigger 是高频正确词的子串
    - identity：trigger 归一后等于某个正形词
    """
    t = _compact(trigger)
    if not t:
        return "empty_trigger"
    canon = {_compact(w) for w in canonical_words}
    if t in canon:
        return "identity"
    for c in canon:
        if c and c in t:
            return "contains_canonical"
        if c and t in c:
            return "substring_of_canonical"
    if t in freq_vocab:
        return "freq_correct_word"
    for fw in freq_vocab:
        if t != fw and t in fw:
            return "substring_of_freq_word"
    return ""


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def build_canonical() -> list[str]:
    return list(CANONICAL_WORDS)


def main() -> int:
    parser = argparse.ArgumentParser(description="E1 snippet 词表候选挖掘")
    parser.add_argument("--dry-run", action="store_true", help="只打印不落盘")
    parser.add_argument("--db", default=str(DB_PATH), help="业务库路径（只读）")
    parser.add_argument(
        "--model-9b",
        action="store_true",
        help="生成端换 9B(:1237 huihui)——4B 变体召回不足时用（实证回查与守卫不放松）",
    )
    parser.add_argument("--out", default=str(OUT_PATH), help="候选清单落盘路径")
    args = parser.parse_args()

    canonical = build_canonical()

    turns = load_turns_corpus(Path(args.db))
    gaps = load_gaps_corpus(GAPS_PATH)
    print(f"[corpus] turns 用户轮={len(turns)}  gaps 条={len(gaps)}", file=sys.stderr)

    gen_model = LLM_MODEL_9B if args.model_9b else LLM_MODEL
    gen_port = LLM_PORT_9B if args.model_9b else LLM_PORT
    print(f"[llm] 生成端 model={gen_model} port={gen_port}", file=sys.stderr)

    # 实证语料：先剔词表 dump 行（不是客户话，留着会把 dump 伪影当变体证据），
    # 再 compact 成 (原文, 紧凑文, 权重, 语言)。
    raw_corpus = [(t, 1, lang) for t, lang in turns] + [(g, w, lang) for g, w, lang in gaps]
    kept: list[tuple[str, int, str]] = []
    dumps = 0
    for text, weight, lang in raw_corpus:
        if looks_like_hotword_dump(text):
            dumps += 1
            continue
        kept.append((text, weight, lang))
    corpus_texts: list[tuple[str, str, int, str]] = [
        (t, _compact(t), w, lang) for t, w, lang in kept
    ]
    print(
        f"[corpus] 剔词表 dump 行={dumps} 实证语料={len(corpus_texts)}",
        file=sys.stderr,
    )
    compact_all = [c for _, c, _, _ in corpus_texts if c]
    freq_vocab = build_freq_vocab(compact_all)
    print(f"[corpus] 高频正确词表(top200, n-gram 近似)={len(freq_vocab)}", file=sys.stderr)

    candidates: list[dict] = []
    discarded: list[dict] = []
    seen_triggers: set[str] = set()

    for word in canonical:
        variants = generate_variants(word, gen_model, gen_port)
        print(f"[llm] {word} -> {variants}", file=sys.stderr)
        for v in variants:
            key = _compact(v)
            if not key or key in seen_triggers:
                continue
            seen_triggers.add(key)

            # 0) 变体卫生：剔除 4B 偶发的「箭头解释 / 嵌套列表」非转写物
            if is_malformed_variant(v):
                discarded.append(
                    {"trigger": v, "replacement": word, "reason": "malformed_variant"}
                )
                continue

            # 1) snippets 守卫（数字/空/单字/长度）
            rule = SnippetRule(trigger=v, replacement=word, source="mined")
            reason = validate_rule(rule)
            if reason:
                discarded.append({"trigger": v, "replacement": word, "reason": reason})
                continue

            # 2) 反向安全检查
            rreason = reverse_safety_reason(v, canonical, freq_vocab)
            if rreason:
                discarded.append(
                    {"trigger": v, "replacement": word, "reason": rreason}
                )
                continue

            # 3) 实证回查：真实语料出现次数（并按通话语言分桶）
            evidence = 0
            lang_weight: Counter[str] = Counter()
            samples: list[str] = []
            for raw_text, ctext, weight, lang in corpus_texts:
                occ = count_occurrences(ctext, key)
                if occ <= 0:
                    continue
                evidence += occ * weight
                lang_weight[lang] += occ * weight
                if len(samples) < 3:
                    samples.append(raw_text[:80])
            if evidence < 1:
                discarded.append(
                    {"trigger": v, "replacement": word, "reason": "no_evidence"}
                )
                continue

            # 3b) 语言反向安全：形态在非 zh 通话里出现过 = 无法证明它在那些语言里是错的。
            # 判定抽成纯函数 decide_lang_scope（三档语义与 fail-safe 取向见该函数）。
            rule_lang, scope_reason = decide_lang_scope(lang_weight)
            if scope_reason:
                discarded.append(
                    {
                        "trigger": v,
                        "replacement": word,
                        "reason": scope_reason,
                        "langs": dict(lang_weight),
                    }
                )
                continue
            warnings: list[str] = []
            if rule_lang:
                detail = ",".join(f"{lg or '?'}={w}" for lg, w in lang_weight.most_common())
                warnings.append(f"lang_scoped_zh({detail})")

            candidates.append(
                {
                    "trigger": v,
                    "replacement": word,
                    "source": "mined",
                    "lang": rule_lang,
                    "evidence": evidence,
                    "langs": dict(lang_weight),
                    "samples": samples,
                    "warnings": warnings,
                }
            )

    candidates.sort(key=lambda c: (-int(c["evidence"]), c["trigger"]))

    result = {
        "generated_by": {"model": gen_model, "port": gen_port},
        "candidates": candidates,
        "discarded": discarded,
    }
    print(
        f"[result] candidates={len(candidates)} discarded={len(discarded)}",
        file=sys.stderr,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if not args.dry_run:
        out_path = Path(args.out)
        out_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"[result] wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
