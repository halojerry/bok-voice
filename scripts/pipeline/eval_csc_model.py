#!/usr/bin/env python3
"""CSC 模型统一评测入口（位置感知判分，2026-09-27）。

把 ``/tmp/csc`` harness 的位置感知判分逻辑收编进仓，作为**将来任何自训
CSC 模型**（char-level MLM 双头或任何等长改写模型）的统一准入口径。
本脚本**与模型无关**：不加载任何模型，只吃预测结果文件。

## 判分口径（等长纠错契约）

- **同位替换 = 正向纠对**：`ops(input, ref)` 是 gold 编辑集，`ops(input, pred)`
  里命中 gold 的一项 = 纠对（`n_correct`）。
- **增/删 = 负向编辑（结构违约）**：任何 `insert`/`delete` 操作都计入
  `n_false` 且单独计 `n_structural`——等长输出契约下增删一律是错误。
- **语言漂移 = 粤语特征字丢失计数**：输入里的粤语特征字在输出中消失
  （`yue_markers_lost`）。特征字集合 import 运行时单源
  `bok_voice_core.asr_polish._CANTONESE_MARKERS`（不复制）。
- 另有 `keep_sentences_changed`：`group=="keep"`（必须原样通过的负样本）
  中被改动的条数——**这是最危险的一类**，越低越好。

## 输入

- `--predictions`：JSONL，每行 `{"src": <原文>, "pred": <模型输出>}`，
  也接受 `{"id": <item id>, "pred": ...}`（二选一，id 优先）。
  可选 `"lane"` 字段仅作透传，判分以评测集 item 的 lang 为准。
- `--testset`：默认 `data/csc/csc_testset.json`（由 `prepare_csc_data.py`
  从 `/tmp/csc/testset.json` 拷入仓的种子评测集）。

## 输出

人类可读汇总 + 可选 `--json-out` 落 JSON 报告；`--quiet` 只打汇总。

用法：

    python scripts/pipeline/eval_csc_model.py --predictions preds.jsonl
    python scripts/pipeline/eval_csc_model.py --predictions preds.jsonl --json-out report.json
"""

from __future__ import annotations
# --- scripts import bootstrap (G1) ---
# sys.path 引导(G1 迁移解耦,见 docs/superpowers/plans/2026-10-04-repo-governance-plan.md §3.1):
# 同层时是 no-op;文件挪进任何桶后裸 import 兄弟模块继续解析。
import sys as _sys, pathlib as _pathlib
_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))


import argparse
import difflib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_CORE = ROOT / "packages" / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

# 粤语特征字单源(运行时冻结集,不复制)。
from bok_voice_core.asr_polish import _CANTONESE_MARKERS  # noqa: E402

YUE_MARKERS = _CANTONESE_MARKERS  # 兼容 /tmp/csc harness 的命名
_DEFAULT_TESTSET = ROOT / "data" / "csc" / "csc_testset.json"


def _is_cantonese(lang: str) -> bool:
    return str(lang).lower() in ("cantonese", "yue", "zh-yue")


def ops(a: str, b: str) -> list[tuple[str, int, str, str]]:
    """返回非 equal 的 `(tag, i1, wrong, right)`；tag ∈ replace/insert/delete。

    `i1` 是原文位置——位置感知判分靠它区分「同位替换」与「移位」。
    """
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    out: list[tuple[str, int, str, str]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        out.append((tag, i1, a[i1:i2], b[j1:j2]))
    return out


@dataclass
class ItemScore:
    id: str
    lang: str
    group: str
    input: str
    ref: str
    output: str
    latency_ms: float
    exact: bool
    n_gold: int
    n_correct: int        # 命中的 gold 编辑(纠对)
    n_missed: int         # gold 编辑未做
    n_false: int          # 做了但不在 gold(含结构违约)
    n_structural: int     # insert/delete(负向编辑)
    yue_markers_lost: int  # 输入有、输出丢的粤语特征字
    matched: bool = True   # 是否在预测文件里命中
    raw_note: str = ""

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def score_item(item: dict, output: str, latency_ms: float = 0.0,
               raw_note: str = "", matched: bool = True) -> ItemScore:
    """对单条 `(原文=item['input'], gold=item['ref'], 模型输出=output)` 打分。"""
    inp, ref = str(item.get("input", "")), str(item.get("ref", ""))
    gold = {(t, i1, w, r) for (t, i1, w, r) in ops(inp, ref)}
    made = ops(inp, output)
    n_correct = sum(1 for o in made if o in gold)
    n_false = sum(1 for o in made if o not in gold)
    n_structural = sum(1 for o in made if o[0] != "replace")
    n_missed = sum(1 for g in gold if g not in made)

    lost = 0
    if _is_cantonese(item.get("lang", "")):
        lost = sum(1 for ch in set(inp) if ch in YUE_MARKERS and ch not in output)

    return ItemScore(
        id=str(item.get("id", "")),
        lang=str(item.get("lang", "")),
        group=str(item.get("group", "")),
        input=inp, ref=ref, output=output, latency_ms=round(float(latency_ms), 1),
        exact=(output == ref), n_gold=len(gold), n_correct=n_correct,
        n_missed=n_missed, n_false=n_false, n_structural=n_structural,
        yue_markers_lost=lost, matched=matched, raw_note=raw_note,
    )


def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    return sorted_vals[min(len(sorted_vals) - 1, int(round((len(sorted_vals) - 1) * p)))]


def summarize(scores: list[ItemScore], model_name: str, extra: dict | None = None) -> dict:
    """汇总为统一口径报告(纠对率/负向率/结构违约/漂移/keep 违约)。"""
    gold = sum(s.n_gold for s in scores)
    correct = sum(s.n_correct for s in scores)
    missed = sum(s.n_missed for s in scores)
    false = sum(s.n_false for s in scores)
    structural = sum(s.n_structural for s in scores)
    exact = sum(1 for s in scores if s.exact)
    keep = [s for s in scores if s.group == "keep"]
    keep_violations = sum(1 for s in keep if s.n_false > 0)
    lat = sorted(s.latency_ms for s in scores)
    yue_loss = sum(s.yue_markers_lost for s in scores)
    return {
        "model": model_name,
        "n_items": len(scores),
        "n_matched": sum(1 for s in scores if s.matched),
        "n_unmatched": sum(1 for s in scores if not s.matched),
        "exact_match": exact,
        "edit_recall": f"{correct}/{gold}",
        "edit_recall_pct": round(100 * correct / gold, 1) if gold else 0.0,
        "positive_edit_rate_pct": round(100 * correct / len(scores), 1) if scores else 0.0,
        "missed_edits": missed,
        "false_edits": false,
        "negative_edit_rate_pct": round(100 * false / len(scores), 1) if scores else 0.0,
        "structural_edits": structural,
        "keep_sentences_changed": f"{keep_violations}/{len(keep)}",
        "keep_violation_rate_pct": round(100 * keep_violations / len(keep), 1) if keep else 0.0,
        "yue_marker_loss": yue_loss,
        "latency_ms": {"p50": _pct(lat, 0.5), "p90": _pct(lat, 0.9), "max": _pct(lat, 1.0),
                       "mean": round(sum(lat) / len(lat), 1) if lat else 0.0},
        **(extra or {}),
    }


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------

def load_testset(path: str | Path = _DEFAULT_TESTSET) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data.get("items") if isinstance(data, dict) else data
    return list(items or [])


def load_predictions(path: str | Path) -> list[dict]:
    out: list[dict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _index_predictions(preds: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    """返回 (id→pred, src→pred);后行覆盖前行。"""
    by_id: dict[str, str] = {}
    by_src: dict[str, str] = {}
    for rec in preds:
        pred = str(rec.get("pred", "") or "")
        if rec.get("id") is not None:
            by_id[str(rec["id"])] = pred
        if rec.get("src") is not None:
            by_src[str(rec["src"])] = pred
    return by_id, by_src


def evaluate(testset: list[dict], preds: list[dict], model_name: str = "unknown") -> tuple[list[ItemScore], dict]:
    by_id, by_src = _index_predictions(preds)
    scores: list[ItemScore] = []
    for item in testset:
        iid, inp = str(item.get("id", "")), str(item.get("input", ""))
        if iid and iid in by_id:
            pred, matched = by_id[iid], True
        elif inp in by_src:
            pred, matched = by_src[inp], True
        else:
            pred, matched = "", False  # 未命中 → 视为「原样不动」,计入 unmatched
        out = pred if matched else inp
        scores.append(score_item(item, out, matched=matched,
                                 raw_note="" if matched else "no_prediction"))
    return scores, summarize(scores, model_name, extra={"unmatched_treated_as_identity": True})


def _print_report(scores: list[ItemScore], summary: dict, *, quiet: bool) -> None:
    if not quiet:
        print(f"{'id':<14}{'lang':<10}{'grp':<7}{'exact':<6}{'corr':>5}{'miss':>5}{'false':>6}{'str':>4}{'drift':>6}")
        for s in scores:
            print(f"{s.id:<14}{s.lang:<10}{s.group:<7}{str(s.exact):<6}"
                  f"{s.n_correct:>5}{s.n_missed:>5}{s.n_false:>6}{s.n_structural:>4}{s.yue_markers_lost:>6}")
    print("[eval_csc_model] summary:")
    for k in ("model", "n_items", "n_matched", "n_unmatched", "exact_match",
              "edit_recall", "edit_recall_pct", "positive_edit_rate_pct",
              "missed_edits", "false_edits", "negative_edit_rate_pct",
              "structural_edits", "keep_sentences_changed", "keep_violation_rate_pct",
              "yue_marker_loss"):
        print(f"  {k} = {summary[k]}")
    print(f"  latency_ms = {summary['latency_ms']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CSC 模型统一位置感知评测")
    ap.add_argument("--predictions", required=True, help="预测 JSONL({src|id, pred})")
    ap.add_argument("--testset", default=str(_DEFAULT_TESTSET), help="评测集 JSON")
    ap.add_argument("--model-name", default=None, help="报告里的模型名(默认取文件名)")
    ap.add_argument("--json-out", default=None, help="汇总+逐条报告 JSON 输出路径")
    ap.add_argument("--quiet", action="store_true", help="只打汇总")
    args = ap.parse_args(argv)

    testset = load_testset(args.testset)
    preds = load_predictions(args.predictions)
    model = args.model_name or Path(args.predictions).stem
    scores, summary = evaluate(testset, preds, model_name=model)
    _print_report(scores, summary, quiet=args.quiet)
    if args.json_out:
        payload = {"summary": summary, "items": [s.as_dict() for s in scores]}
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")
        print(f"[eval_csc_model] wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
