#!/usr/bin/env python3
"""E4 改口检测真实频次扫描（只读、可重跑、纯 stdlib）。

问题（决定接线优先级）：真实通话里「客户显式改口」出现率多少？
口径来自 ``packages/core/bok_voice_core/correction_intent.py`` 的三条纯函数：
``explicit_correction_ranges`` / ``contains_explicit_correction`` /
``span_after_last_correction``（type4me IntelliSense 移植；词表里没有「不是」，
故「不是A是B」天然不出区间）。

数据源：本机 BokVoice SQLite，只读打开（``file:...?mode=ro``）。
  表 turns（role='user'）join call_sessions → object_profiles，
  排除测试对象 display_name 前缀族。

合规口径：
  role='user' 且 transcript 非空；其通话在 call_sessions 有对象档案
  （object_profiles 内连接——对象已删的**合成/压测**通话天然落空，见 [N] 节）；
  display_name 不以测试前缀族开头。

用法：
  .venv312/bin/python scripts/e4_frequency_scan.py
  BOK_DB_PATH=/path/to/bok_voice.db .venv312/bin/python scripts/e4_frequency_scan.py

术语：语言相关字面量只用 zh / cantonese / en（AGENTS.md 术语铁律）。
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "core"))

from bok_voice_core.correction_intent import (  # noqa: E402
    explicit_correction_ranges,
    span_after_last_correction,
)

DEFAULT_DB = (
    Path.home() / "Library" / "Application Support" / "BokVoice" / "bok_voice.db"
)
DB_PATH = Path(os.environ.get("BOK_DB_PATH") or DEFAULT_DB)

# 测试对象 display_name 前缀族（AGENTS.md clean-testdata 同款族）+ 中文等价名「探针」
# （脚本实测：探针-品牌 / 探针-E4 / 探针-快语速 即 probe_* 的中文命名族）。
TEST_PREFIXES: tuple[str, ...] = (
    "E2E-",
    "soak",
    "并发",
    "LOAD-",
    "边角-",
    "多轮-",
    "probe",
    "探针",
)

# 标记类型归类：在命中区间**文本**上判别（一个区间可命中多个标记，如「改口，换成 X」）。
MARKER_LABELS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("不对", re.compile(r"不对")),
    ("哦不", re.compile(r"哦不")),
    ("改口", re.compile(r"改口")),
    ("算了", re.compile(r"算了")),
    ("重说", re.compile(r"重说")),
    ("i mean", re.compile(r"i\s+mean", re.IGNORECASE)),
    ("sorry", re.compile(r"sorry", re.IGNORECASE)),
    ("改成", re.compile(r"改成")),
    ("换成", re.compile(r"换成")),
    ("应该是", re.compile(r"应该是")),
)

# span_after 含数字：ASCII 数字 + 中文数字（WA 报号可能报汉字数字）。
_NUMERAL = re.compile(r"[0-9零〇一二三四五六七八九十百千万两]")
_ASCII_DIGIT = re.compile(r"[0-9]")

# 合规用户轮：role='user'、transcript 非空、其通话在 object_profiles 有档案。
COMPLIANT_SQL = """
SELECT t.id            AS turn_id,
       t.call_id       AS call_id,
       t.transcript    AS transcript,
       t.language      AS language,
       t.created_at    AS created_at,
       o.display_name  AS display_name
FROM turns t
JOIN call_sessions c   ON c.id = t.call_id
JOIN object_profiles o ON o.id = c.object_id
WHERE t.role = 'user' AND TRIM(COALESCE(t.transcript, '')) <> ''
"""

# 对照面：对象档案缺失的通话（合成/压测族，display_name 已随对象删除）。
ORPHAN_SQL = """
SELECT t.id            AS turn_id,
       t.call_id       AS call_id,
       t.transcript    AS transcript,
       t.language      AS language,
       t.created_at    AS created_at,
       ''              AS display_name
FROM turns t
JOIN call_sessions c   ON c.id = t.call_id
LEFT JOIN object_profiles o ON o.id = c.object_id
WHERE t.role = 'user' AND TRIM(COALESCE(t.transcript, '')) <> ''
  AND o.id IS NULL
"""


def is_test_object(display_name: str) -> str | None:
    """返回命中的测试前缀，非测试返回 None。"""
    name = (display_name or "").strip()
    for prefix in TEST_PREFIXES:
        if name.startswith(prefix):
            return prefix
    return None


def marker_labels(span_text: str) -> list[str]:
    return [label for label, pat in MARKER_LABELS if pat.search(span_text)]


def snippet(text: str, limit: int = 30) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else clean[:limit] + "…"


def lang_key(language: str) -> str:
    value = (language or "").strip()
    return value if value else "(空)"


def pct(numerator: int, denominator: int) -> str:
    if not denominator:
        return "n/a"
    return f"{numerator / denominator * 100:.2f}%"


def scan_rows(rows: list[sqlite3.Row]) -> dict:
    """逐轮跑显式改口检测，返回命中明细与聚合。"""
    hits: list[dict] = []
    label_counter: Counter[str] = Counter()
    lang_total: Counter[str] = Counter()
    lang_hit: Counter[str] = Counter()
    span_nonempty = span_numeral = span_ascii_digit = 0

    for row in rows:
        text = row["transcript"]
        lang = lang_key(row["language"])
        lang_total[lang] += 1
        ranges = explicit_correction_ranges(text)
        if not ranges:
            continue
        lang_hit[lang] += 1
        spans_text = " | ".join(text[s:e] for s, e in ranges)
        labels = marker_labels(spans_text)
        label_counter.update(labels)
        span = span_after_last_correction(text)
        if span:
            span_nonempty += 1
            if _NUMERAL.search(span):
                span_numeral += 1
            if _ASCII_DIGIT.search(span):
                span_ascii_digit += 1
        hits.append(
            {
                "call_id": row["call_id"],
                "lang": lang,
                "labels": labels,
                "span": span,
                "text": text,
                "created_at": row["created_at"],
            }
        )

    return {
        "rows": len(rows),
        "calls": len({r["call_id"] for r in rows}),
        "hits": hits,
        "hit_turns": len(hits),
        "hit_calls": len({h["call_id"] for h in hits}),
        "labels": label_counter,
        "lang_total": lang_total,
        "lang_hit": lang_hit,
        "span_nonempty": span_nonempty,
        "span_numeral": span_numeral,
        "span_ascii_digit": span_ascii_digit,
    }


def pick_samples(hits: list[dict], limit: int = 10) -> list[dict]:
    """样例优先每个通话一条，不足再从剩余命中补齐。"""
    samples: list[dict] = []
    seen_calls: set[str] = set()
    for hit in hits:
        if hit["call_id"] in seen_calls:
            continue
        seen_calls.add(hit["call_id"])
        samples.append(hit)
        if len(samples) >= limit:
            return samples
    for hit in hits:
        if hit in samples:
            continue
        samples.append(hit)
        if len(samples) >= limit:
            break
    return samples


def main() -> int:
    if not DB_PATH.exists():
        print(f"DB 不存在：{DB_PATH}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(COMPLIANT_SQL).fetchall()
        orphan_rows = conn.execute(ORPHAN_SQL).fetchall()
    finally:
        conn.close()

    total_joined = len(rows) + len(orphan_rows)
    excluded_family: Counter[str] = Counter()
    compliant: list[sqlite3.Row] = []
    for row in rows:
        hit_prefix = is_test_object(row["display_name"])
        if hit_prefix is None:
            compliant.append(row)
        else:
            excluded_family[hit_prefix] += 1

    stats = scan_rows(compliant)
    orphan_stats = scan_rows(orphan_rows)

    n = stats["hit_turns"]
    total_calls = stats["calls"]
    hit_calls = stats["hit_calls"]
    call_rate = hit_calls / total_calls if total_calls else 0.0

    # —— 输出 ——
    print("=" * 68)
    print("E4 改口检测真实频次扫描（correction_intent，只读）")
    print("=" * 68)
    print(f"DB            : {DB_PATH}")
    print()

    print("[A] 样本面")
    print(f"  role='user' 且 transcript 非空（含测试/对象已删）: {total_joined} 轮")
    for prefix, count in excluded_family.most_common():
        print(f"    排除 测试前缀 {prefix!r:<8}: {count} 轮")
    print(f"  合规用户轮（排除测试对象后）: {len(compliant)}")
    print(f"  合规通话数                  : {total_calls}")
    print(f"  对照：对象档案缺失的通话    : {orphan_stats['rows']} 轮 / {orphan_stats['calls']} 通话")
    print()

    print("[B] 命中总览（轮级）")
    print(f"  命中轮数 / 合规轮数 : {n} / {stats['rows']}  ({pct(n, stats['rows'])})")
    print()

    print("[C] 命中通话（一通至少一次）")
    print(f"  命中通话 / 合规通话 : {hit_calls} / {total_calls}  ({pct(hit_calls, total_calls)})")
    print()

    print("[D] 标记类型分布（区间文本归类，可多标记）")
    if stats["labels"]:
        for label, count in stats["labels"].most_common():
            print(f"  {label:<8}: {count}")
    else:
        print("  (无命中)")
    print()

    print("[E] span_after_last_correction（改口后段，WA 改口相关）")
    print(f"  非空             : {stats['span_nonempty']} / {n}  ({pct(stats['span_nonempty'], n)})")
    print(f"  含数字(含中文数字): {stats['span_numeral']} / {n}  ({pct(stats['span_numeral'], n)})")
    print(f"  含 ASCII 数字     : {stats['span_ascii_digit']} / {n}  ({pct(stats['span_ascii_digit'], n)})")
    print()

    print("[F] 语言分布（命中率 = 该语言命中轮 / 该语言合规轮）")
    for lang in sorted(stats["lang_total"], key=lambda k: -stats["lang_total"][k]):
        total = stats["lang_total"][lang]
        hits = stats["lang_hit"].get(lang, 0)
        print(f"  {lang:<10}: 轮 {hits} / {total}  ({pct(hits, total)})")
    print()

    print("[G] 样例（≤10 条，文本 ≤30 字）")
    samples = pick_samples(stats["hits"])
    if not samples:
        print("  (无命中)")
    for idx, hit in enumerate(samples, 1):
        labels = ",".join(hit["labels"]) or "-"
        span = hit["span"] or "-"
        print(f"  {idx:>2}. [{hit['lang']}] 标记={labels} span={snippet(span, 20)!r}")
        print(f"      {hit['text'][:30]!r}  (call {hit['call_id']})")
    print()

    print("[N] 对照：对象档案缺失的通话（合成/压测族，未计入合规面）")
    print("  这些通话的对象档案已删（load_audio_concurrency / e2e 固定脚本：")
    print("  「有冇人知道灣仔活道係點去㗎？」「I would like to know more about your…」")
    print("  「六四三二零一一一」等），INNER JOIN 天然落空。列出以验稳健性。")
    print(f"    其命中轮数            : {orphan_stats['hit_turns']} / {orphan_stats['rows']}"
          f"  ({pct(orphan_stats['hit_turns'], orphan_stats['rows'])})")
    combined_calls = total_calls + orphan_stats["calls"]
    combined_hit_calls = hit_calls + orphan_stats["hit_calls"]
    print(f"  若把对照面并入（仅信息位，非判读口径）: "
          f"{combined_hit_calls} / {combined_calls} 通话  "
          f"({pct(combined_hit_calls, combined_calls)})")
    print()

    if call_rate < 0.02:
        verdict = "接线优先级低（观测项）"
    elif call_rate <= 0.10:
        verdict = "接线优先级中"
    else:
        verdict = "接线优先级高"
    print(f"[判读] 通话级命中率 {pct(hit_calls, total_calls)} → {verdict}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
