#!/usr/bin/env python3
"""QA 快路词库命中率探针:真实转写挖出的问法 vs 现库,报 would-hit 率与 top 未命中。

数据源:
  - 现库条目  GET /api/qa-entries(enabled=1)
  - 真实问法  GET /api/reports/qa-pairs?min_calls=1&limit=200(服务端 mine_qa_pairs)

命中判定与 agent.py 命中块同源:QaIndex.match 实返回 (entry|None, score)
(keyword-only 签名,阈值在 match 内部把守),entry is not None 即 would-hit;
None 一律计 miss。未命中清单按通话数降序 = Task 10 补词条工单。

用法:
    BOK_CP_URL=http://127.0.0.1:8000 .venv312/bin/python scripts/probe_qa_hit.py
    .venv312/bin/python scripts/probe_qa_hit.py --priority-duel   # 优先级三档对照(离线零栈)
    .venv312/bin/python scripts/probe_qa_hit.py --rotation-duel   # 折组+轮换三断言(离线零栈)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

CP = os.environ.get("BOK_CP_URL", "http://127.0.0.1:8000")
from urlguard_gate import gate  # SSRF 守卫（2026-09-23，Mimosa）：云端测试设 BOK_PROBE_EXTRA_HOSTS

gate(CP)

DUEL_QUERY = "怎么退款"
DUEL_THRESHOLD = 0.3  # 显式低阈:让异问法低分条目也过关,优先级才真压过分(默认 0.90 下它永不出线)


def duel_verdicts() -> dict:
    """优先级对决腿(Phase 3.1,2026-09-18):同一夹具三档胜者对照,离线零栈。

    夹具 = 异问法双条目(old=精确匹配分数 1.0 / pinned=近义问法分数更低),
    threshold=0.3 双双过关——「优先级压过分数差」才被真实行使。
    legacy   = 两条目均无 priority 键(=线上现状:纯分数,高分 old 胜)
    priority = pinned priority 1 压过 old 10(阈值过关者中小者先)
    kill     = BOK_QA_PRIORITY=0 回纯分数档(old 胜)
    主判据:legacy=old / priority=pinned / kill=old 三断言全过 exit 0。
    """
    from agent_runtime.qa_gate import QaIndex

    def _e(qid: str, q: str, **kw):
        return {"id": qid, "question_text": q, "answer_text": "ans",
                "lang": "zh", "scope": "global", **kw}

    def _winner(rows: list[dict]) -> str:
        hit, _ = QaIndex(rows).match(DUEL_QUERY, threshold=DUEL_THRESHOLD)
        return str(hit.get("id")) if hit else ""

    saved = os.environ.get("BOK_QA_PRIORITY")
    try:
        os.environ.pop("BOK_QA_PRIORITY", None)
        legacy = _winner([_e("old", DUEL_QUERY), _e("pinned", "退款要怎么弄啊")])
        priority = _winner([_e("old", DUEL_QUERY, priority=10), _e("pinned", "退款要怎么弄啊", priority=1)])
        os.environ["BOK_QA_PRIORITY"] = "0"
        kill = _winner([_e("old", DUEL_QUERY, priority=10), _e("pinned", "退款要怎么弄啊", priority=1)])
    finally:
        if saved is None:
            os.environ.pop("BOK_QA_PRIORITY", None)
        else:
            os.environ["BOK_QA_PRIORITY"] = saved
    return {"legacy": legacy, "priority": priority, "kill": kill}


# 轮换对决腿夹具(Phase 3.2):query 用变体 v1 的原话——生产默认阈值(0.90)下
# 精确问法命中 v1 满分,折组后由 head 代表出场;head/v2 是陪跑(同簇成员表)。
ROTATION_QUERY = "怎么退款啊"       # 变体 v1 问法(= 命中查询)
ROTATION_HEAD_Q = "怎么退款"        # 簇头(head)问法
ROTATION_V2_Q = "退款咋弄"          # 变体 v2 问法


def rotation_duel() -> dict:
    """折组+轮换对决腿(Phase 3.2,2026-09-18):head+2 变体同簇,离线零栈。

    夹具 = 三成员同义簇(head 自身 + v1/v2 两变体),阈值用生产默认;
    断言三档(全过才 PASS):
      ① fold  = 变体问法 match → head 代表出场(折组语义,不是裸胜者 v1)
         zero  = 零账本 pick_rotation_member(members, []) → head(插入序首)
      ② ledger= 账本 [head] → 本通最少播放者 v1(轮换真位移)
      ③ kill  = BOK_QA_ROTATION=0 → 变体自己赢(旧档裸索引竞争者)
    返回 dict 供纯函数测试逐键断言;env 自管 save/restore。
    """
    from agent_runtime.qa_gate import QaIndex, pick_rotation_member

    def _e(qid: str, q: str, head: str = ""):
        return {"id": qid, "question_text": q, "answer_text": f"ans-{qid}",
                "lang": "zh", "scope": "global", "cluster_head_id": head}

    idx = QaIndex([_e("head", ROTATION_HEAD_Q),
                   _e("v1", ROTATION_QUERY, head="head"),
                   _e("v2", ROTATION_V2_Q, head="head")])
    saved = os.environ.get("BOK_QA_ROTATION")
    try:
        os.environ.pop("BOK_QA_ROTATION", None)
        hit, _ = idx.match(ROTATION_QUERY)          # 变体问法命中 → 折组
        fold = str(hit.get("id")) if hit else ""
        members = idx.cluster_members("head")
        zero = str(pick_rotation_member(members, []).get("id"))
        ledger = str(pick_rotation_member(members, ["head"]).get("id"))
        os.environ["BOK_QA_ROTATION"] = "0"
        kill_hit, _ = idx.match(ROTATION_QUERY)     # 旧档:变体自己赢
        kill = str(kill_hit.get("id")) if kill_hit else ""
    finally:
        if saved is None:
            os.environ.pop("BOK_QA_ROTATION", None)
        else:
            os.environ["BOK_QA_ROTATION"] = saved
    return {"fold": fold, "zero": zero, "ledger": ledger, "kill": kill}


def _get(path: str):
    req = urllib.request.Request(CP + path)
    tok = os.environ.get("BOK_CP_TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def summarize(rows: list[dict], pairs: list[dict]) -> dict:
    from agent_runtime.qa_gate import QaIndex  # 延迟 import(需 PYTHONPATH 指向 apps/agent)

    idx = QaIndex(rows)
    hit = 0
    misses = []
    for p in pairs:
        lang = p.get("lang") or "zh"
        step = p.get("step_index")
        # qa_gate.QaIndex.match 实签名 keyword-only、返回 (entry|None, score):
        # 命中即 entry 非 None(与 agent.py:2299 命中块同款判定,阈值 match 内部已把守)。
        entry, _score = idx.match(p["question"], lang=lang, step_index=step)
        if entry is not None:
            hit += 1
        else:
            misses.append({"question": p["question"], "calls": p.get("calls", 0), "lang": lang})
    misses.sort(key=lambda e: -e["calls"])
    total = len(pairs)
    return {"pairs_total": total, "would_hit": hit,
            "would_hit_rate": (hit / total) if total else 0.0,
            "top_misses": misses[:20]}


def main() -> int:
    if "--priority-duel" in sys.argv:
        v = duel_verdicts()
        print(f"priority-duel: query=「{DUEL_QUERY}」 legacy={v['legacy']} "
              f"priority={v['priority']} kill={v['kill']}")
        ok = v == {"legacy": "old", "priority": "pinned", "kill": "old"}
        print("PRIORITY_DUEL", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    if "--rotation-duel" in sys.argv:
        v = rotation_duel()
        print(f"rotation-duel: query=「{ROTATION_QUERY}」 簇=[head v1 v2]")
        print(f"  ① 零账本   变体命中 fold={v['fold']}  pick={v['zero']}")
        print(f"  ② 账本[head]            pick={v['ledger']}")
        print(f"  ③ kill(BOK_QA_ROTATION=0) 裸胜者={v['kill']}")
        ok = v == {"fold": "head", "zero": "head", "ledger": "v1", "kill": "v1"}
        print("ROTATION_DUEL", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    data = _get("/api/qa-entries")
    rows = data.get("items", data) if isinstance(data, dict) else data
    rows = [r for r in rows if r.get("enabled", 1)]
    rep = _get("/api/reports/qa-pairs?min_calls=1&limit=200")
    pairs = rep.get("pairs", rep) if isinstance(rep, dict) else rep
    out = summarize(list(rows), list(pairs))
    print(f"库={len(rows)} 条 | 真实问法={out['pairs_total']} | would-hit {out['would_hit']}"
          f" ({out['would_hit_rate']:.0%})")
    for m in out["top_misses"]:
        print(f"  MISS[{m['calls']}通] ({m['lang']}) {m['question']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
