"""QA 匹配器 golden 回归集测试（词面段全离线;语义段 live 门控）。

数据源 ``tests/qa_golden_set.json``：真实词条 + 真实通话问法（含 ASR 同音错字
族 赔→裴/陪）+ 邻近负样本（adjacent,误命中测试的承重墙）。挖掘与出处见
json ``meta.provenance``（真库只读抽样 + agent.log QA_FASTPATH/QA_SEM 实录）。

通道分工（2026-09-25 审计定案,golden 标定实测）:
- **verbatim 族归词面 rank**（拼音+双向子串通道,实测 100% top-8）;
- **paraphrase/homophone 族归语义召回腿**（QaSemanticIndex.rank,live :8789
  才跑——实测 paraphrase 95%/homophone 40%;同音+改写叠加族的根治在沉淀引擎
  的同音归一表,不在匹配端）;
- adjacent 在 0.90 快道下不得命中、digits/refuse 靠 ``qa_exclude_reason``
  闸拦、short_ack 靠分数挡。

验收线按实测钉（回归检测,不钉幻想值）:verbatim 词面 ≥0.95、paraphrase 语义
≥0.90、homophone 语义 ≥0.30、并池整体 ≥0.85。语义段 ``:8789`` 不可达时
skip（离线 CI 零依赖）;汇总读数跑 ``pytest -s`` 可见;阈值标定工具
``scripts/qa_match_report.py``。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))
sys.path.insert(0, str(ROOT / "packages" / "core"))

from agent_runtime.qa_gate import QaIndex, qa_exclude_reason  # noqa: E402

GOLDEN_PATH = Path(__file__).resolve().parent / "qa_golden_set.json"
GOLDEN: dict = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
ENTRIES: list[dict] = GOLDEN["entries"]
POSITIVES: list[dict] = GOLDEN["positives"]
NEGATIVES: list[dict] = GOLDEN["negatives"]

RANK_K = 8
RANK_FLOOR = 0.40  # qa_gate.qa_recall_floor() 新默认（golden 标定拐点,与实现单点对齐）
FASTLANE_THRESHOLD = 0.90  # qa_gate.qa_threshold() 默认值,显式钉死防 env 漂移
SEM_TOP_K = 3
VERBATIM_LEXICAL_BAR = 0.95  # 实测 50/50
SEM_PARAPHRASE_BAR = 0.90  # 实测 38/40
SEM_HOMOPHONE_BAR = 0.30  # 实测 4/10（根治在沉淀引擎同音归一表,此处只钉下限防退化）
UNION_OVERALL_BAR = 0.85  # 词面∪语义并池 实测 92/100

_VALID_KINDS = {"verbatim", "paraphrase", "homophone"}
_VALID_REASONS = {"digits", "short_ack", "refuse", "adjacent", "offscript"}
_VALID_LANGS = {"zh", "cantonese", "en"}


@pytest.fixture(scope="module")
def index() -> QaIndex:
    return QaIndex(ENTRIES)


def _rank_available() -> bool:
    fn = getattr(QaIndex, "rank", None)
    return callable(fn)


def _rank_entries(idx: QaIndex, query: str, lang: str) -> list[tuple[float, str]]:
    """调 rank 并归一返回 (score, entry_id) 元组列（宽容实现期笔误的倒置元组）。"""
    rows = idx.rank(query, k=RANK_K, floor=RANK_FLOOR, lang=(lang or None))
    out: list[tuple[float, str]] = []
    for row in rows:
        score, eid = row[0], row[1]
        if isinstance(score, str) and not isinstance(eid, str):
            score, eid = eid, score  # (entry_id, score) 倒置容错
        out.append((float(score), str(eid)))
    return out


def _match_id(idx: QaIndex, query: str, lang: str) -> str | None:
    entry, _score = idx.match(query, lang=lang, threshold=FASTLANE_THRESHOLD)
    return None if entry is None else str(entry.get("id") or "")


# ---- 集合自洽（守护 golden 集本身的质量,rank 无关,永不 skip） ----

def test_golden_set_self_consistent():
    assert 30 <= len(ENTRIES) <= 60, f"entries 数量 {len(ENTRIES)} 超出 30-60 契约"
    assert 60 <= len(POSITIVES) <= 120, f"positives 数量 {len(POSITIVES)} 超出 60-120 契约"
    assert 30 <= len(NEGATIVES) <= 50, f"negatives 数量 {len(NEGATIVES)} 超出 30-50 契约"

    ids = {str(e["id"]) for e in ENTRIES}
    assert len(ids) == len(ENTRIES), "entries 存在重复 id"
    for e in ENTRIES:
        assert e["lang"] in _VALID_LANGS, f"entry {e['id']} 非法 lang={e['lang']}"
        assert str(e.get("question_text") or "").strip(), f"entry {e['id']} 空 question_text"

    for p in POSITIVES:
        assert p["expect"] in ids, f"positive 指向不存在的条目: {p['expect']}"
        assert p["kind"] in _VALID_KINDS, f"positive 非法 kind: {p['kind']}"
        target = next(e for e in ENTRIES if str(e["id"]) == p["expect"])
        assert target["lang"] == p["lang"], (
            f"positive lang 与目标条目不一致: {p['query']!r} lang={p['lang']} "
            f"expect={p['expect']}({target['lang']})——运行时按通话语言过滤,不一致必假阳"
        )

    adjacent = [n for n in NEGATIVES if n["reason"] == "adjacent"]
    assert len(adjacent) >= 15, f"adjacent 负样本 {len(adjacent)} < 15（承重墙缺料）"
    for n in NEGATIVES:
        assert n["reason"] in _VALID_REASONS, f"negative 非法 reason: {n['reason']}"
        if n.get("lang"):
            assert n["lang"] in _VALID_LANGS, f"negative 非法 lang: {n['lang']}"


def test_homophone_family_present():
    homophones = [p for p in POSITIVES if p["kind"] == "homophone"]
    assert len(homophones) >= 8, "homophone 正样本 < 8——同音验收线缺料"
    assert all(p["note"] for p in homophones), "homophone 样本必须带出处 note（可追溯）"


# ---- 快道（0.90 词面档）行为——rank 未就位也必须过的现状红线 ----

def test_adjacent_never_matched_at_fastlane(index: QaIndex):
    """adjacent 负样本在 0.90 快道下不得命中：误命中=播错答案录音,代价最高。"""
    bad = []
    for n in (x for x in NEGATIVES if x["reason"] == "adjacent"):
        hit = _match_id(index, n["query"], n.get("lang", ""))
        if hit is not None:
            bad.append(f"{n['query']!r} 被 {hit} 误吃")
    assert not bad, f"adjacent 在 0.90 快道被误命中 {len(bad)} 条:\n" + "\n".join(bad)


def test_offscript_never_matched_at_fastlane(index: QaIndex):
    bad = []
    for n in (x for x in NEGATIVES if x["reason"] == "offscript"):
        hit = _match_id(index, n["query"], n.get("lang", ""))
        if hit is not None:
            bad.append(f"{n['query']!r} 被 {hit} 误吃")
    assert not bad, f"offscript 在 0.90 快道被误命中 {len(bad)} 条:\n" + "\n".join(bad)


def test_short_ack_blocked_by_score_not_gate(index: QaIndex):
    """short_ack：闸不拦（verdict 空放行,无数字无拒绝词）——靠 0.90 分数挡。
    逐条钉死「机制归属」:gate 必须放行（==""）+ match 必须未命中。"""
    problems = []
    for n in (x for x in NEGATIVES if x["reason"] == "short_ack"):
        gate = qa_exclude_reason(n["query"])
        if gate != "":
            problems.append(f"{n['query']!r} 现在被闸拦了（reason={gate}）——golden 口径失真,请更新 note")
        hit = _match_id(index, n["query"], n.get("lang", ""))
        if hit is not None:
            problems.append(f"{n['query']!r} 被 {hit} 误吃（分数挡失效）")
    assert not problems, "\n".join(problems)


def test_digits_gate_or_score(index: QaIndex):
    """digits：gate=='digits' 的样本断言闸拦;gate=='score' 的样本（英文数字词等
    闸拦不到的纯问句场景）断言 match 未命中——note 已标注机制归属。"""
    problems = []
    for n in (x for x in NEGATIVES if x["reason"] == "digits"):
        gate = qa_exclude_reason(n["query"])
        if n.get("gate") == "digits":
            if gate != "digits":
                problems.append(f"{n['query']!r} 期望闸拦 digits,实测 reason={gate!r}")
        else:
            if gate != "":
                problems.append(f"{n['query']!r} 标注 gate=score 但实测被闸拦（reason={gate}）——请更新 golden")
            hit = _match_id(index, n["query"], n.get("lang", ""))
            if hit is not None:
                problems.append(f"{n['query']!r} 被 {hit} 误吃（分数挡失效）")
    assert not problems, "\n".join(problems)


def test_refuse_gate(index: QaIndex):
    for n in (x for x in NEGATIVES if x["reason"] == "refuse"):
        assert qa_exclude_reason(n["query"]) == "refuse", f"{n['query']!r} 未被 refuse 闸拦"


# ---- 召回车道验收：词面 rank 管逐字族；改写/同音族归语义召回腿（live 门） ----

@pytest.fixture(scope="module")
def _sem_index():
    """live 语义索引（:8789 不可达→None=语义段整组 skip,离线 CI 零依赖）。"""
    if not _rank_available():
        return None
    from agent_runtime.qa_gate import QaSemanticIndex
    from agent_runtime.intent_semantic import EmbedClient

    async def _build():
        client = EmbedClient()
        probe = await client.embed(["探活"])
        if probe is None:
            return None
        return await QaSemanticIndex.build(client, ENTRIES)

    try:
        return asyncio.run(_build())
    except Exception:  # noqa: BLE001 - 语义车道不可用=skip,绝不红
        return None


def test_recall_verbatim_bar(index: QaIndex):
    """verbatim 族 ≥95% 进词面 rank top-8（实测 100%）。"""
    if not _rank_available():
        pytest.skip("rank not yet available")
    verbatim = [p for p in POSITIVES if p["kind"] == "verbatim"]
    hits, misses = 0, []
    for p in verbatim:
        rows = _rank_entries(index, p["query"], p.get("lang", ""))
        if any(eid == p["expect"] for _s, eid in rows):
            hits += 1
        else:
            misses.append(f"{p['query']!r} 未召回 {p['expect']}")
    rate = hits / len(verbatim)
    assert rate >= VERBATIM_LEXICAL_BAR, (
        f"verbatim 词面召回 {rate:.0%} < {VERBATIM_LEXICAL_BAR:.0%}:\n" + "\n".join(misses)
    )


def test_recall_semantic_kinds_bar(_sem_index):
    """paraphrase ≥90% / homophone ≥30% 进语义召回 top-3（live :8789 才跑）。

    homophone 硬族=同音+改写叠加,匹配端只兜近逐字同音;根治在沉淀引擎的
    同音归一表（自动学 ASR 混淆对）——此线只防退化,不是终态验收。
    """
    if _sem_index is None:
        pytest.skip("semantic lane unreachable (:8789 down or build failed)")

    async def _run():
        per: dict[str, list[bool]] = {}
        for p in POSITIVES:
            if p["kind"] == "verbatim":
                continue
            rows = await _sem_index.rank(p["query"], k=SEM_TOP_K, lang=p.get("lang", ""))
            per.setdefault(p["kind"], []).append(
                any(eid == p["expect"] for _s, eid in rows)
            )
        return per

    per = asyncio.run(_run())
    bars = {"paraphrase": SEM_PARAPHRASE_BAR, "homophone": SEM_HOMOPHONE_BAR}
    problems = []
    for kind, flags in sorted(per.items()):
        rate = sum(flags) / len(flags)
        print(f"[qa_golden] semantic {kind:<10} {sum(flags)}/{len(flags)} = {rate:.0%}")
        if rate < bars[kind]:
            problems.append(f"{kind} 语义召回 {rate:.0%} < {bars[kind]:.0%}")
    assert not problems, ";\n".join(problems)


def test_recall_union_overall_bar(index: QaIndex, _sem_index):
    """并池（词面∪语义）整体 ≥85%——生产车道的真实候选供给形状。"""
    if not _rank_available():
        pytest.skip("rank not yet available")
    if _sem_index is None:
        pytest.skip("semantic lane unreachable (:8789 down or build failed)")

    async def _run():
        hits = 0
        for p in POSITIVES:
            rows = _rank_entries(index, p["query"], p.get("lang", ""))
            lex_ok = any(eid == p["expect"] for _s, eid in rows)
            sem_ok = False
            if p["kind"] != "verbatim":
                srows = await _sem_index.rank(p["query"], k=SEM_TOP_K, lang=p.get("lang", ""))
                sem_ok = any(eid == p["expect"] for _s, eid in srows)
            if lex_ok or sem_ok:
                hits += 1
        return hits

    hits = asyncio.run(_run())
    rate = hits / len(POSITIVES)
    print(f"[qa_golden] union recall {hits}/{len(POSITIVES)} = {rate:.0%}")
    assert rate >= UNION_OVERALL_BAR, f"并池整体召回 {rate:.0%} < {UNION_OVERALL_BAR:.0%}"


def test_recall_summary_and_no_cross_lang_leak(index: QaIndex):
    """词面汇总读数（-s 可见）+ 语言隔离检查:rank 带 lang 过滤时不得跨语言出线。"""
    if not _rank_available():
        pytest.skip("rank not yet available")
    per_kind: dict[str, list[int]] = {}
    cross_lang = []
    for p in POSITIVES:
        rows = _rank_entries(index, p["query"], p.get("lang", ""))
        hit = any(eid == p["expect"] for _s, eid in rows)
        per_kind.setdefault(p["kind"], []).append(int(hit))
        # 语言隔离:命中列表里不应出现其它语言的条目（rank 支持 lang=None 不过滤,
        # 但传了 lang 就必须真过滤——与 match 的 _survives_turn 同判据）。
        entry_lang = {str(e["id"]): e["lang"] for e in ENTRIES}
        wrong = [eid for _s, eid in rows if entry_lang.get(eid) not in (None, p["lang"])]
        if wrong:
            cross_lang.append(f"{p['query']!r} 召回跨语言条目 {wrong}")
    print("\n[qa_golden] lexical recall@%d (floor=%.2f):" % (RANK_K, RANK_FLOOR))
    for kind, flags in sorted(per_kind.items()):
        print(f"  {kind:<10} {sum(flags)}/{len(flags)} = {sum(flags) / len(flags):.0%}")
    assert not cross_lang, "rank(lang=...) 语言过滤失效:\n" + "\n".join(cross_lang)


def test_fastlane_summary(index: QaIndex):
    """快路现状读数（-s 可见）:0.90 档在正样本上的命中与在负样本上的误命中。"""
    pos_hits = sum(
        1 for p in POSITIVES if _match_id(index, p["query"], p.get("lang", "")) is not None
    )
    neg_queries = [
        n for n in NEGATIVES
        if n["reason"] in ("adjacent", "offscript", "short_ack")
        or (n["reason"] == "digits" and n.get("gate") == "score")
    ]
    neg_hits = [n["query"] for n in neg_queries if _match_id(index, n["query"], n.get("lang", "")) is not None]
    print(
        f"\n[qa_golden] fast-lane(0.90): positives 命中 {pos_hits}/{len(POSITIVES)}"
        f" = {pos_hits / len(POSITIVES):.0%};"
        f" 负样本误命中 {len(neg_hits)}/{len(neg_queries)} {neg_hits}"
    )
    assert not neg_hits, f"0.90 快道负样本误命中: {neg_hits}"
