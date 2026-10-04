"""意向事实 12 键 parity（C4，2026-10-04）——core INTENT_FACTS ↔ web 条件编辑器源级钉。

背景:意向规则的条件行编辑器（web /calls 页折叠卡 + studio「客户意向」tab）在
``apps/web/components/intent-rules-card.tsx`` 自带一份 fact 目录拷贝（键+中文标签），
注释自称「与 CP core `intent_rules.INTENT_FACTS` 手工同步」;而 core 的
``INTENT_FACTS`` 是三方共享契约（agent 挂断快照 ``_intent_facts_snapshot`` /
CP ``validate_conditions`` / 规则评估 ``eval_intent_rules``）。手工同步=漂移温床:
Python 侧增删键或改标签而 web 未跟,下拉里就会出现引擎不认的 fact（保存即 400）
或新事实永远选不到。本测试族钉三件事:

1. Python 侧键集恰 12 键并钉死字面集（防增删漂移）;
2. 源级 pin:web 常量表的 (键, 标签) 与 ``INTENT_FACTS`` 逐项相等。TS 无法
   import Python,取 test_branch_syntax_parity.py 的源级读文件先例（改一处不改
   另一处即红）;
3. 反拷贝哨:fact 键的带引号字面量只允许出现在该组件（防第二份手工同步拷贝回潮）。

2026-10-04 落库时发现的真实漂移（已按「以 INTENT_FACTS 为准」修 web 标签,11 处）:
心跳/看门狗/风暴/verdict 各计数/最大步/捕获/求助 的中文标签 web 侧曾是缩短版,
与本文件钉住的 core 标签逐字不一致——当时的「手工同步」注释名不副实。
"""
from __future__ import annotations

import re
from pathlib import Path

from bok_voice_core.intent_rules import INTENT_FACTS

ROOT = Path(__file__).resolve().parents[1]
WEB_CARD = ROOT / "apps" / "web" / "components" / "intent-rules-card.tsx"

# 2026-10-04 钉死的 12 键字面集（增删即红——须同步 agent 快照/CP 校验/web 下拉,
# 并更新本集合后再改契约）。
EXPECTED_FACT_KEYS = frozenset({
    "duration_s",
    "nudge_fired",
    "watchdog_fired",
    "storm_rounds",
    "repeat_count",
    "refuse_count",
    "objection_count",
    "confirm_count",
    "question_count",
    "step_max",
    "wa_captured",
    "graph_notifies",
})

# web 常量块与逐对字面量。块锚点=`const INTENT_FACTS: [string, string][] = [ ... ];`
# （非贪婪到第一个 `];`，块内键值对形如 `["key", "标签"],`）。
_WEB_BLOCK_RE = re.compile(r"const INTENT_FACTS: \[string, string\]\[\] = \[(.*?)\];", re.S)
_WEB_PAIR_RE = re.compile(r'\[\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\]')


def _web_fact_pairs() -> list[tuple[str, str]]:
    src = WEB_CARD.read_text(encoding="utf-8")
    blocks = _WEB_BLOCK_RE.findall(src)
    assert len(blocks) == 1, (
        f"{WEB_CARD.relative_to(ROOT)} 里 INTENT_FACTS 常量块缺失或重复（提取锚点漂移,"
        "本 pin 已失效——请按新形状更新 _WEB_BLOCK_RE）"
    )
    pairs = _WEB_PAIR_RE.findall(blocks[0])
    assert pairs, "web INTENT_FACTS 常量块未提取到任何 [键, 标签] 对（格式漂移）"
    return [(k, zh) for k, zh in pairs]


# ---- 1. Python 契约:12 键字面集稳定 ----


def test_intent_facts_exactly_twelve_keys_pinned():
    assert isinstance(INTENT_FACTS, dict)
    assert len(INTENT_FACTS) == 12, f"INTENT_FACTS 应为 12 键,实际 {len(INTENT_FACTS)}"
    assert set(INTENT_FACTS) == EXPECTED_FACT_KEYS, (
        "INTENT_FACTS 键集漂移（agent 快照/CP 校验/规则评估三方共享契约）:"
        f" 多={sorted(set(INTENT_FACTS) - EXPECTED_FACT_KEYS)}"
        f" 缺={sorted(EXPECTED_FACT_KEYS - set(INTENT_FACTS))}"
    )


def test_intent_facts_labels_are_nonempty_strings():
    for key, label in INTENT_FACTS.items():
        assert isinstance(label, str) and label.strip(), f"{key} 缺中文标签"


# ---- 2. 源级 pin:web (键, 标签) ≡ INTENT_FACTS ----


def test_web_fact_pairs_match_core_intent_facts():
    pairs = _web_fact_pairs()
    assert len(pairs) == 12, f"web fact 目录应为 12 条,实际 {len(pairs)}"
    web = dict(pairs)
    assert len(web) == len(pairs), "web fact 目录有重复键（下拉会出现重复项）"
    label_diffs = {
        k: (web[k], INTENT_FACTS[k])
        for k in sorted(set(web) & set(INTENT_FACTS))
        if web[k] != INTENT_FACTS[k]
    }
    assert web == INTENT_FACTS, (
        "web INTENT_FACTS 与 core 漂移（只允许键/标签逐字一致）:\n"
        f"  仅 web 有的键={sorted(set(web) - set(INTENT_FACTS))}\n"
        f"  仅 core 有的键={sorted(set(INTENT_FACTS) - set(web))}\n"
        f"  标签不一致 (web, core)={label_diffs}"
    )


# ---- 3. 反拷贝哨:fact 键字面量只允许在该组件 ----


def test_no_second_hand_synced_fact_copy_in_web():
    key_re = re.compile(r"""["'](?:%s)["']""" % "|".join(sorted(EXPECTED_FACT_KEYS)))
    hits: list[str] = []
    for sub in ("app", "components", "hooks", "lib", "test"):
        root = ROOT / "apps" / "web" / sub
        if not root.is_dir():
            continue
        for p in list(root.rglob("*.ts")) + list(root.rglob("*.tsx")):
            if p == WEB_CARD:
                continue
            if key_re.search(p.read_text(encoding="utf-8")):
                hits.append(str(p.relative_to(ROOT)))
    assert not hits, (
        "web 出现第二份 fact 键拷贝（手工同步单源已漂移过一次,禁止再复制）:"
        f" {hits}"
    )
