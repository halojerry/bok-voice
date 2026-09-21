#!/usr/bin/env python3
"""R1 真实标注集构建（2026-09-21）。

三个只读来源：
  G = /tmp/r1_gaps_raw.json   CP /api/stats/llm-gaps（AI 轮走了 LLM 的前一条客户轮，漏网轮）
  T = /tmp/r1_turns_raw.json  业务库 turns 最近用户轮（排除 E2E/soak 等测试对象，只读）
  K = /tmp/r1_turns_kw.json   业务库关键词富集（质疑/情绪/赔偿/单号族，定向非 iid）

标注 = 本轮代理人工判读（非第三方金标）：规则族（清晰意图）+ 显式覆盖（逐条核对）+
默认「其他」（碎片/应承/回声串/报号/配合——12 选项表里没有「提供号码/答平台」这类意图，
它们本来就该落「其他」）。

输出 scripts/.r1_gold.20260921.json（worktree 内，与既有 .probe_*.json 同位）：
[{src, txt, gold, step, lang}]
"""
from __future__ import annotations

import json
import re
from pathlib import Path

OUT = Path(__file__).resolve().parent / ".r1_gold.20260921.json"

ECHO_RE = re.compile(r"(運通|运通).{0,40}(核實|核实)")  # 热词回声串的指纹


def label(txt: str) -> str:
    t = txt.strip()
    if ECHO_RE.search(t):
        return "其他"  # ASR 热词回声串（echo artifact），不是意图
    low = t.lower()
    # 清晰族（按序判定，先具体后泛化）
    if re.search(r"(怎么赔|怎么賠|如何赔|点样赔|點樣賠|赔多少|賠多少|赔偿方式|賠償方式|怎么赔偿|怎么賠償|赔给|賠給|赔偿标准|赔两倍|賠兩倍|有冇得保)", t):
        return "问赔偿金额"
    if re.search(r"(单号系|單號系|单号可以|單號可以|帮我查下|幫我查下|这个单号|這個單號)", t):
        return "问能否查单号"
    if re.search(r"(什么货|什麼貨|咩貨|什么快递|什么包裹|件貨到|件货到|到咗未)", t):
        return "查件去向"
    if re.search(r"(再见|再見|bye\s?bye|bye\.)", low):
        return "拒绝继续"
    if re.search(r"who are you", low):
        return "诈骗质疑"
    return "其他"


# 显式覆盖：规则判不准的逐条改（核对过原文后手写）
OVERRIDES: dict[str, str] = {
    "我网上赔。": "其他",            # 说自己线上处理，不是问赔偿
    "啊，两百块。": "其他",           # 报金额，不是问
    "你好，我是快递公司。": "其他",    # 客户复读 agent 开场白（回声）
    "你好，我是快递公司的专员。": "其他",
    "Let me check that for you.": "其他",
    "的专员。": "其他",
    "司的专员。": "其他",
    "专员。单号。": "其他",
    "我。单号。": "其他",
    "元。单号。": "其他",
    "单号。单号。": "其他",
    "好的，淘宝拼。单号。": "其他",
    "是批。單號。": "其他",
    "我听到你说话了。": "其他",
    "My God, what's it going on?": "其他",
    "哦，可以啊，可以啊，那你赔给我。": "问赔偿金额",
    "嗯，对的，对的，这个单号怎么？": "问能否查单号",
    "查下我账单，玛莎，你帮我退下。": "其他",
    "呃，我好像没有。下单。": "其他",
    "应该没有买。吧。": "其他",
    "得了，我应该没有。购买吧。": "其他",
    "呃，我要回复什么？我。名字。": "其他",
    "我不记得买什么货品呢。": "其他",   # 答「不记得什么货」，不是问货况
}


def main() -> None:
    out = []
    for src, path in (("G", "/tmp/r1_gaps_raw.json"),
                      ("T", "/tmp/r1_turns_raw.json"),
                      ("K", "/tmp/r1_turns_kw.json")):
        rows = json.load(open(path, encoding="utf-8"))
        for r in rows:
            txt = (r.get("customer_text") or r.get("txt") or "").strip()
            if not txt:
                continue
            gold = OVERRIDES.get(txt, label(txt))
            out.append({"src": src, "txt": txt, "gold": gold,
                        "step": r.get("step"), "lang": r.get("lang") or "?"})
    seen, uniq = set(), []
    for r in out:
        if r["txt"] in seen:
            continue
        seen.add(r["txt"])
        uniq.append(r)
    OUT.write_text(json.dumps(uniq, ensure_ascii=False, indent=1), encoding="utf-8")
    import collections
    print(f"真实标注集 n={len(uniq)} -> {OUT}")
    print(f"  来源分布={collections.Counter(r['src'] for r in uniq)}")
    print(f"  金标分布={collections.Counter(r['gold'] for r in uniq)}")
    for r in uniq:
        if r["gold"] != "其他":
            print(f"  [{r['gold']}] {r['txt'][:56]!r}")


if __name__ == "__main__":
    main()
