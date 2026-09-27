"""QaIndex.rank() 召回排序单测(2026-09-25):Laya QA 验证车道的候选供给面。

钉住三件事:
1. 双向子串——「超集句」查询(客户话⊃词条)在 rank 里有分,在旧 match() 0.90
   快道语义下**不**命中(证明快道零漂移);
2. 拼音通道——ASR 同音错字(赔→裴)由 pypinyin bigram Dice 补位;
   ``BOK_QA_PINYIN=0`` 时通道分恒 0、权重按 0.60/0.25 比例归一;
3. 幸存面与折组纪律同 match——lang/step 过滤、簇代表 team_head 出线、
   floor/k 截断与降序。

全离线纯函数(词面哈希向量 + pypinyin),零网络零模型。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))
sys.path.insert(0, str(ROOT / "packages" / "core"))

from agent_runtime import qa_gate as qa_gate_mod  # noqa: E402
from agent_runtime.qa_gate import (  # noqa: E402
    QaIndex,
    _bidir_substring_hit,
    _pinyin_bigram_score,
    _pinyin_syllables,
    qa_pinyin_enabled,
    qa_recall_floor,
    qa_recall_k,
)
from bok_voice_core.embeddings import HybridLexicalEmbedding  # noqa: E402
from bok_voice_core.qa_text import normalize_question  # noqa: E402


def _e(eid: str, q: str, *, lang: str = "cantonese", scope: str = "global", **kw) -> dict:
    d = {"id": eid, "question_text": q, "answer_text": "答覆。", "lang": lang, "scope": scope}
    d.update(kw)
    return d


# ---- 纯函数:双向子串 / 拼音通道 ----

def test_bidir_substring_both_directions():
    assert _bidir_substring_hit("赔几多", "赔几多,而家就帮你查")  # user ⊂ entry
    assert _bidir_substring_hit("我想先问下赔几多", "赔几多")  # entry ⊂ user(单向档的缺口)
    assert not _bidir_substring_hit("幾時送到", "赔几多")
    assert not _bidir_substring_hit("", "赔几多")
    assert not _bidir_substring_hit("赔几多", "")


def test_pinyin_helpers():
    assert _pinyin_syllables("赔几多？") == ["pei", "ji", "duo"]  # 标点被滤掉
    if qa_gate_mod.lazy_pinyin is None:  # pragma: no cover - 缺库环境
        pytest.skip("pypinyin 未安装")
    # 同音错字:赔/裴 同拼音,音节表逐字同
    assert _pinyin_syllables("裴") == _pinyin_syllables("赔")
    # Dice:完全同音节表 → 1.0;无重叠 → 0.0
    a = _pinyin_syllables("赔偿点样赔")
    assert _pinyin_bigram_score(a, a) == 1.0
    assert _pinyin_bigram_score(a, _pinyin_syllables("幾時送到")) == 0.0
    assert _pinyin_bigram_score(["pei"], a) == 0.0  # <2 音节恒 0
    # 三音节 vs 三音节:bigram 交 1 → Dice = 2×1/(2+2) = 0.5
    assert _pinyin_bigram_score(["a", "b", "c"], ["b", "c", "d"]) == pytest.approx(0.5)


# ---- 1. 双向子串:超集句查询 ----

def test_rank_superset_query_and_match_unchanged():
    idx = QaIndex(
        [
            _e("qa-pei", "赔几多"),
            _e("qa-oth", "幾時送到"),
        ]
    )
    query = "我想先问下赔几多？"
    rows = idx.rank(query, lang="cantonese", floor=0.0)
    assert rows, "超集句查询必须有召回"
    assert rows[0][1] == "qa-pei"
    scores = {eid: s for s, eid in rows}
    assert "qa-oth" in scores, "floor=0 时无关节词也要在召回面里"
    assert scores["qa-pei"] - scores["qa-oth"] > 0.4, "目标词条须显著高于无关节词"
    # 默认 floor 下也进 top-k
    assert idx.rank(query, lang="cantonese")[0][1] == "qa-pei"
    # 旧 match() 0.90 快道语义:同查询不命中(单向子串拿不到分,余弦不够阈值)
    hit, top = idx.match(query, lang="cantonese")
    assert hit is None
    assert top < 0.90


# ---- 2. 拼音通道:同音错字补位 + kill-switch ----

def _lex_terms(idx: QaIndex, query: str, eid: str) -> tuple[float, float]:
    """(词面余弦, 双向子串0/1)——与 rank 同一套向量与归一,用于钉权重契约。"""
    q = normalize_question(query)
    qv = idx._embed.embed([q])[0]
    for entry, _q, vec in idx._items:
        if str(entry.get("id") or "") == eid:
            e_q = normalize_question(str(entry.get("question_text") or ""))
            cos = qa_gate_mod._cos(qv, vec)
            sub = 1.0 if _bidir_substring_hit(q.lower(), e_q.lower()) else 0.0
            return cos, sub
    raise AssertionError(f"entry {eid} 不在索引")


def test_rank_pinyin_homophone_and_killswitch(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BOK_QA_PINYIN", raising=False)
    idx = QaIndex(
        [
            _e("qa-bc", "赔偿点样赔俾我", lang="zh"),
            _e("qa-z", "查詢單號進度", lang="zh"),
        ]
    )
    query = "裴偿点样赔俾我"  # 赔→裴 同音错字
    rows = idx.rank(query, lang="zh")
    assert rows[0][1] == "qa-bc", "拼音通道拉起同音错字目标,进 top-8"
    cos, sub = _lex_terms(idx, query, "qa-bc")
    assert sub == 0.0, "错字轮双向子串不该命中"
    dice = _pinyin_bigram_score(_pinyin_syllables(query), _pinyin_syllables("赔偿点样赔俾我"))
    assert dice > 0
    on_score = rows[0][0]
    assert on_score == pytest.approx(0.60 * cos + 0.25 * sub + 0.15 * dice, abs=1e-9)

    # kill-switch:通道分恒 0,余两项按 0.60/0.25 比例归一(→ 0.706/0.294)
    monkeypatch.setenv("BOK_QA_PINYIN", "0")
    assert not qa_pinyin_enabled()
    off_rows = idx.rank(query, lang="zh")
    off_score = {eid: s for s, eid in off_rows}["qa-bc"]
    assert off_score == pytest.approx((0.60 / 0.85) * cos + (0.25 / 0.85) * sub, abs=1e-9)
    assert off_score == pytest.approx(0.7058823529 * cos, abs=1e-6)  # sub=0 时即纯余弦归一
    # 通道贡献在 ON 档真实为正(0.15×dice),与 OFF 档不可比是两态权重面各自归一
    assert on_score - (0.60 * cos + 0.25 * sub) == pytest.approx(0.15 * dice, abs=1e-9)


# ---- 3. floor 过滤 / k 截断 / 降序 ----

def test_rank_floor_k_and_desc_order():
    idx = QaIndex(
        [
            _e("qa-far", "開戶口要咩文件"),
            _e("qa-mid", "幾時送到"),
            _e("qa-near", "赔几多錢"),
            _e("qa-top", "赔几多"),
        ]
    )
    query = "我想问下赔几多"
    rows = idx.rank(query, lang="cantonese", k=3, floor=0.0)
    assert len(rows) <= 3 and len(rows) >= 2
    scores = [s for s, _ in rows]
    assert scores == sorted(scores, reverse=True), "必须降序"
    assert rows[0][1] == "qa-top"
    ids = [eid for _, eid in rows]
    assert len(ids) == len(set(ids)), "同簇折组后不得重复出线"
    # floor 过滤:高地板只留逐字命中者(=1.0;「赔几多錢」因余弦+拼音打折 ≈0.82 被滤)
    assert [eid for _, eid in idx.rank("赔几多", lang="cantonese", floor=0.9)] == ["qa-top"]
    # k=1 截断
    assert idx.rank(query, lang="cantonese", k=1, floor=0.0) == rows[:1]


# ---- 4. 幸存面:lang / step 过滤 ----

def test_rank_survives_turn_filters():
    idx = QaIndex(
        [
            _e("qa-zh", "赔几多", lang="zh"),
            _e("qa-can", "賠幾多", lang="cantonese"),
            _e("qa-step", "可唔可以退換", lang="cantonese", scope="step", step_index=2),
        ]
    )
    query = "我想问下赔几多"
    lang_rows = {eid for _, eid in idx.rank(query, lang="cantonese", floor=0.0)}
    assert "qa-zh" not in lang_rows, "错语言词条不得出线"
    assert "qa-can" in lang_rows
    assert "qa-step" not in lang_rows, "step 作用域条目在无步骤上下文时滤出"
    step_rows = {eid for _, eid in idx.rank(query, lang="cantonese", step_index=2, floor=0.0)}
    assert "qa-step" in step_rows
    step0 = {eid for _, eid in idx.rank(query, lang="cantonese", step_index=0, floor=0.0)}
    assert "qa-step" not in step0


# ---- 5. 折组纪律:变体折到簇代表 ----

def test_rank_folds_variant_to_team_head(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BOK_QA_ROTATION", raising=False)
    idx = QaIndex(
        [
            _e("head-1", "幾時送到"),
            _e("var-1", "邊度先攞到件貨", cluster_head_id="head-1"),
        ]
    )
    query = "邊度先攞到件貨"  # 变体问法逐字命中
    rows = idx.rank(query, lang="cantonese", floor=0.0)
    assert [eid for _, eid in rows] == ["head-1"], "变体命中须由簇代表出线"
    assert rows[0][0] == pytest.approx(1.0, abs=1e-9)  # 余弦1+子串1+拼音1
    # 折组关 → 独立条目出线(裸胜者档,变体自己赢)
    monkeypatch.setenv("BOK_QA_ROTATION", "0")
    rows_off = idx.rank(query, lang="cantonese", floor=0.0)
    assert rows_off[0][1] == "var-1"
    assert "head-1" in {eid for _, eid in rows_off}, "折组关后簇头退独立条目照常参评"


# ---- 6. env 缺省 / 坏值回默认 ----

def test_rank_env_defaults_and_bad_values(monkeypatch: pytest.MonkeyPatch):
    assert qa_recall_k() == 8 and qa_recall_floor() == pytest.approx(0.40)
    monkeypatch.setenv("BOK_QA_RECALL_K", "abc")
    monkeypatch.setenv("BOK_QA_RECALL_FLOOR", "abc")
    assert qa_recall_k() == 8 and qa_recall_floor() == pytest.approx(0.40)
    monkeypatch.setenv("BOK_QA_RECALL_K", "2")
    monkeypatch.setenv("BOK_QA_RECALL_FLOOR", "0")
    idx = QaIndex(
        [
            _e("a1", "赔几多"),
            _e("a2", "赔几多錢"),
            _e("a3", "幾時送到"),
            _e("a4", "開戶口文件"),
        ]
    )
    rows = idx.rank("我想问下赔几多", lang="cantonese")  # k/floor 缺省读 env
    assert len(rows) == 2
    # 显式参数优先于 env
    assert len(idx.rank("我想问下赔几多", lang="cantonese", k=4, floor=0.0)) == 4


# ---- 7. 空面 ----

def test_rank_empty_faces():
    idx = QaIndex([])
    assert idx.rank("赔几多") == []
    idx2 = QaIndex([_e("a1", "赔几多")])
    assert idx2.rank("") == []
    assert idx2.rank("   ") == []
