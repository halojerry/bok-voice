"""CSC 自训数据管道单测（2026-09-27）。

覆盖 `scripts/seed/prepare_csc_data.py` 与 `scripts/pipeline/eval_csc_model.py` 的纯函数面：

- prepare 小样本（count=500）：条数/车道分布/identity 占比（yue identity ≥ 配比）、
  注入与 identity 的 src/tgt 关系、数字冻结（中文数字词句 src/tgt 数字段一致）、
  seed 复现（同 seed 两跑逐行一致）。
- write_outputs：sample 行数上限、testset 拷贝。
- eval scorer：纠对 / 负向编辑 / 结构违约（增删）/ 粤语漂移四类计数各有用例，
  以及按 src/id 匹配与未命中处理。

离线零网络：prepare 用 `--no-db/--no-replay/--no-char-inject` 档（只吃内置种子），
不依赖 /tmp 或本机业务库。eval 用显式构造 testset/预测，不读 /tmp。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    # 注册进 sys.modules:dataclass 装饰器要按 __module__ 反查模块命名空间。
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


prepare = _load("prepare_csc_data_under_test", "scripts/seed/prepare_csc_data.py")
evalu = _load("eval_csc_model_under_test", "scripts/pipeline/eval_csc_model.py")


def _build(count: int = 500, seed: int = 1234):
    return prepare.build_dataset(
        count=count,
        identity_ratio=0.40,
        replay_ratio=0.0,
        seed=seed,
        data_dir="/tmp/__no_such_csc_data_dir__",
        db_path=None,
        use_db=False,
        use_replay=False,
        use_char_inject=False,
    )


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

def test_seed_pools_meet_minimums():
    # 内置种子是零依赖保底源,任务要求 ≥300 句。
    assert len(prepare.EMBEDDED_SEED_ZH) + len(prepare.EMBEDDED_SEED_YUE) >= 300


def test_count_and_origins():
    rows, stats = _build(500)
    assert len(rows) == 500
    assert stats["count_actual"] == 500
    # identity 占比 = 配置值(0.40)
    assert stats["identity"]["count"] == 200
    assert abs(stats["identity"]["ratio"] - 0.40) < 0.02
    # 主料是注入,identity 是强负样本,无 replay(replay 关)
    assert stats["by_origin"]["inject"] == 300
    assert stats["by_origin"]["identity"] == 200
    assert "replay" not in stats["by_origin"]


def test_lane_distribution_and_relations():
    rows, stats = _build(500)
    assert stats["by_lane"]["zh"] > 0
    assert stats["by_lane"]["yue"] > 0
    for r in rows:
        assert r["lane"] in ("zh", "yue")
        assert r["origin"] in ("inject", "identity", "replay")
        if r["origin"] == "identity":
            assert r["src"] == r["tgt"]
        else:
            assert r["src"] != r["tgt"]


def test_yue_identity_ratio_and_markers():
    rows, stats = _build(500)
    # yue identity 占 identity ≥40%(默认按 50% 配)
    assert stats["identity"]["yue_ratio"] >= 0.40
    yue_id = [r for r in rows if r["origin"] == "identity" and r["lane"] == "yue"]
    assert yue_id
    # identity 的 yue 句必须含粤语特征字(强负样本判据面)
    for r in yue_id:
        assert prepare._count_markers(r["src"]) > 0


def test_digit_freeze_no_mismatch_and_present():
    rows, _ = _build(500)
    numeric = [
        r for r in rows
        if prepare._DIGIT_RUN_RE.search(r["src"]) or prepare._CN_NUM_RUN_RE.search(r["src"])
    ]
    assert numeric, "样本里应有含数字(阿拉伯或中文数字词)的句子以覆盖冻结面"
    for r in numeric:
        assert prepare._num_signature(r["src"]) == prepare._num_signature(r["tgt"])


def test_seed_reproducible():
    rows_a, stats_a = _build(500, seed=99)
    rows_b, stats_b = _build(500, seed=99)
    assert rows_a == rows_b
    assert stats_a["by_origin"] == stats_b["by_origin"]
    # 换 seed 应产生不同语料(概率极高;这里只验证 seed 参数真的生效)
    rows_c, _ = _build(500, seed=100)
    assert rows_a != rows_c


def test_write_outputs_sample_limit_and_testset(tmp_path):
    rows, stats = _build(120)
    src_testset = tmp_path / "seed_testset.json"
    src_testset.write_text(json.dumps({"items": [{"id": "x", "input": "a", "ref": "a"}]}),
                           encoding="utf-8")
    written = prepare.write_outputs(rows, stats, tmp_path / "csc", sample_limit=50,
                                    testset_src=src_testset)
    train_lines = (tmp_path / "csc" / "csc_train.jsonl").read_text(encoding="utf-8").splitlines()
    sample_lines = (tmp_path / "csc" / "sample.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(train_lines) == 120
    assert len(sample_lines) == 50
    assert (tmp_path / "csc" / "csc_testset.json").exists()
    assert written["testset"].endswith("csc_testset.json")


def test_inject_never_touches_placeholders_or_empty():
    # 内置种子无占位符;注入结果非空且与原文不同。
    rows, _ = _build(300)
    for r in rows:
        assert r["src"].strip()
        assert r["tgt"].strip()
        assert "{" not in r["src"] and "}" not in r["src"]


# ---------------------------------------------------------------------------
# eval scorer
# ---------------------------------------------------------------------------

_ITEM_FIX = {"id": "zh-fix-1", "lang": "zh", "group": "fix",
             "input": "我的快第单号是三七七八九零", "ref": "我的快递单号是三七七八九零"}
_ITEM_KEEP = {"id": "zh-keep-1", "lang": "zh", "group": "keep",
              "input": "拼多多和京东都可以发货", "ref": "拼多多和京东都可以发货"}
_ITEM_YUE = {"id": "yue-keep-1", "lang": "cantonese", "group": "keep",
             "input": "你哋公司喺香港，我係用顺丰嘅", "ref": "你哋公司喺香港，我係用顺丰嘅"}
_ITEM_FIX2 = {"id": "zh-fix-2", "lang": "zh", "group": "fix",
              "input": "请问是拼多多还是京冬发货", "ref": "请问是拼多多还是京东发货"}


def test_scorer_correct_edit():
    s = evalu.score_item(_ITEM_FIX, "我的快递单号是三七七八九零")
    assert s.exact is True
    assert s.n_correct >= 1
    assert s.n_false == 0
    assert s.n_structural == 0


def test_scorer_negative_edit_on_keep_sentence():
    # keep 句被误改 → 负向编辑;最危险一类。
    s = evalu.score_item(_ITEM_KEEP, "拼多多和京冬都可以发货")
    assert s.n_correct == 0
    assert s.n_false >= 1
    assert s.n_structural == 0  # 同位替换,不是增删


def test_scorer_structural_edits():
    # 插入一个字 → 结构违约(等长契约被破)。
    s = evalu.score_item(_ITEM_KEEP, "拼多多和京东都可以发货的")
    assert s.n_structural >= 1
    assert s.n_false >= 1


def test_scorer_yue_marker_drift():
    # 粤语特征字丢失(哋/嘅/係少一个)→ 漂移计数 ≥1。
    s = evalu.score_item(_ITEM_YUE, "你地公司喺香港，我係用顺丰嘅")
    assert s.yue_markers_lost >= 1


def test_scorer_no_drift_for_mandarin():
    s = evalu.score_item(_ITEM_FIX, "我的快递单号是三七七八九零")
    assert s.yue_markers_lost == 0


def test_evaluate_match_by_src_and_id_and_unmatched():
    testset = [_ITEM_FIX, _ITEM_KEEP, _ITEM_FIX2]
    preds = [
        {"src": _ITEM_FIX["input"], "pred": _ITEM_FIX["ref"]},   # 按 src 命中
        {"id": "zh-keep-1", "pred": "拼多多和京东都可以发货"},     # 按 id 命中(原样)
        # zh-fix-2 故意不给 → unmatched,按原样计(未纠 = 不产生 false edit)
    ]
    scores, summary = evalu.evaluate(testset, preds, model_name="toy")
    assert summary["n_items"] == 3
    assert summary["n_matched"] == 2
    assert summary["n_unmatched"] == 1
    assert summary["exact_match"] == 2
    unmatched = [s for s in scores if not s.matched][0]
    assert unmatched.id == "zh-fix-2"
    assert unmatched.n_false == 0
    assert unmatched.n_correct == 0


def test_summarize_aggregates():
    testset = [_ITEM_FIX, _ITEM_KEEP, _ITEM_YUE]
    preds = [
        {"id": "zh-fix-1", "pred": _ITEM_FIX["ref"]},
        {"id": "zh-keep-1", "pred": "拼多多和京冬都可以发货"},  # 违约
        {"id": "yue-keep-1", "pred": "你地公司喺香港，我係用顺丰嘅"},  # 漂移
    ]
    _, summary = evalu.evaluate(testset, preds, model_name="toy")
    assert summary["positive_edit_rate_pct"] > 0
    assert summary["negative_edit_rate_pct"] > 0
    assert summary["keep_sentences_changed"] == "2/2"  # keep 被改 + 粤语漂移句被改
    assert summary["yue_marker_loss"] >= 1


def test_ops_insert_delete_replace_tags():
    made = evalu.ops("abc", "abxc")  # insert
    assert made and made[0][0] == "insert"
    made = evalu.ops("abc", "ac")    # delete
    assert made and made[0][0] == "delete"
    made = evalu.ops("abc", "axc")   # replace
    assert made and made[0][0] == "replace"
