#!/usr/bin/env python3
"""R2 -- leave-out refit of the V-5 clarification-gate threshold (offline, stdlib only).

The old gate:  margin <= p30(margin)  OR  model == "other"
The p30 threshold was picked on the same data it was scored on.  Here we re-fit it
with stratified K-fold cross-validation on the 152 real labelled turns, per model
(4B / 9B), and compare three decision policies:

  A) percentile rule   -- threshold = 30th percentile of TRAIN-fold margins
  B) absolute refit    -- grid-search a margin threshold on TRAIN folds that
                          maximises capture subject to precision >= 0.90
  C) other-only        -- the "OR other" component alone (diagnostic floor)

The combined rule (margin <= thr OR other) is what is actually deployed, so both
precision and capture are computed on the combined rule.  The "OR other" component
is never tunable, so its share of triggers / captures is reported separately.

No model calls, no network, no writes outside the single output JSON.
"""
from __future__ import annotations

import json
import random
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

GOLD = SCRIPTS / ".r1_gold.20260921.json"
SERVER = {
    "4B": SCRIPTS / ".t56r1_server_4B.json",
    "9B": SCRIPTS / ".t56r1_server_9B.json",
}
OUT = SCRIPTS / ".r2_refit.20260921.json"

K = 5
SEED = 20260921
PCTL = 0.30
TARGET_PRECISION = 0.90


# --------------------------------------------------------------------------- io
def _load(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def build_dataset(server_path: Path, gold_rows):
    """real rows joined to gold by txt.  Returns (joined_rows, report)."""
    gold_map = {}
    for g in gold_rows:
        gold_map.setdefault(g["txt"], g)

    real = [r for r in _load(server_path) if r.get("corp") == "real"]
    n_real = len(real)

    # defensive de-dup on txt (kept first); correctness guard for the split
    seen, deduped = set(), []
    for r in real:
        t = r["txt"]
        if t in seen:
            continue
        seen.add(t)
        deduped.append(r)

    joined, unmatched = [], []
    for r in deduped:
        g = gold_map.get(r["txt"])
        if g is None:
            unmatched.append(r["txt"])
            continue
        joined.append(
            {
                "txt": r["txt"],
                "margin": float(r["old_margin"]),
                "other": bool(r["other"]),
                "ok": bool(r["ok"]),
                "gold": g["gold"],
            }
        )
    report = {
        "real_rows": n_real,
        "after_dedup": len(deduped),
        "dropped_duplicate_txt": n_real - len(deduped),
        "joined": len(joined),
        "unmatched": len(unmatched),
        "unmatched_samples": unmatched[:10],
    }
    return joined, report


# ------------------------------------------------------------------- statistics
def percentile(sorted_vals, q: float):
    """Linear-interpolation percentile (numpy 'linear' convention), stdlib only."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return float(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)


def _ratio(num, den):
    return None if den == 0 else round(num / den, 6)


def evaluate(rows, thr):
    """Evaluate the combined rule (margin <= thr OR other) on `rows`."""
    n = len(rows)
    errors = [r for r in rows if not r["ok"]]
    n_err = len(errors)
    triggered = [r for r in rows if r["margin"] <= thr or r["other"]]
    captured = [r for r in triggered if not r["ok"]]
    margin_trig = [r for r in rows if r["margin"] <= thr]
    other_rows = [r for r in rows if r["other"]]
    other_added = [r for r in other_rows if r["margin"] > thr]
    other_added_cap = [r for r in other_added if not r["ok"]]
    return {
        "n": n,
        "errors": n_err,
        "threshold": round(float(thr), 6),
        "triggers": len(triggered),
        "trigger_rate": _ratio(len(triggered), n),
        "captured_errors": len(captured),
        "capture_rate": _ratio(len(captured), n_err),
        "false_alarms": len(triggered) - len(captured),
        "precision": _ratio(len(captured), len(triggered)),
        # decomposition of the tunable margin leg vs the fixed OR-other leg
        "margin_triggers": len(margin_trig),
        "other_total": len(other_rows),
        "other_added_triggers": len(other_added),
        "other_added_captures": len(other_added_cap),
        "other_added_share_of_triggers": _ratio(len(other_added), len(triggered)),
        "other_added_share_of_captures": _ratio(
            len(other_added_cap), len(captured)
        ),
    }


# ------------------------------------------------------------------------- split
def stratified_folds(rows, k, rng):
    """Stratify by gold class; distribute each class round-robin across k folds."""
    buckets = {}
    for r in rows:
        buckets.setdefault(r["gold"], []).append(r)
    folds = [[] for _ in range(k)]
    for gold, members in sorted(buckets.items()):
        members = list(members)
        rng.shuffle(members)
        for i, r in enumerate(members):
            folds[i % k].append(r)
    return folds


def search_threshold(rows):
    """Train-fold grid search: max capture s.t. precision >= TARGET_PRECISION.

    Returns (threshold, eval_on_train, info).  If no grid point reaches the
    precision target (it can be structurally unreachable when the fixed OR-other
    leg drags correct turns in), falls back to the maximum-precision point and
    flags `feasible=False` so the caller can report it rather than hide it.
    """
    grid = sorted({r["margin"] for r in rows})
    if not grid:
        return None, None, {"feasible": False, "max_train_precision": None}
    candidates = [-1.0] + grid  # -1.0 => margin leg fires on nothing
    best, best_ev, fallback, fb_ev, max_prec = None, None, None, None, None
    for t in candidates:
        ev = evaluate(rows, t)
        if ev["precision"] is None:
            continue
        if max_prec is None or ev["precision"] > max_prec:
            max_prec = ev["precision"]
        if fb_ev is None or (
            ev["precision"],
            -ev["triggers"],
        ) > (fb_ev["precision"], -fb_ev["triggers"]):
            fallback, fb_ev = t, ev
        if ev["precision"] >= TARGET_PRECISION:
            key = (ev["capture_rate"], ev["precision"], -ev["triggers"], -t)
            if best_ev is None or key > (
                best_ev["capture_rate"],
                best_ev["precision"],
                -best_ev["triggers"],
                -best,
            ):
                best, best_ev = t, ev
    info = {"feasible": best is not None, "max_train_precision": max_prec}
    if best is None:
        return fallback, fb_ev, info  # target unreachable -> best precision
    return best, best_ev, info


# ------------------------------------------------------------------------ driver
def mean_sd(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return {"mean": None, "sd": None}
    return {
        "mean": round(statistics.mean(vals), 6),
        "sd": round(statistics.pstdev(vals), 6) if len(vals) > 1 else 0.0,
    }


def _agg(fold_evals, key):
    return mean_sd([e[key] for e in fold_evals])


def run_model(label, rows, out):
    rng = random.Random(SEED)
    folds = stratified_folds(rows, K, rng)

    per_fold = []
    pct_test, abs_test, otheronly_test = [], [], []
    pool = {
        "pct": {"triggers": 0, "captured": 0, "errors": 0, "trig": 0, "n": 0},
        "abs": {"triggers": 0, "captured": 0, "errors": 0, "trig": 0, "n": 0},
    }
    thr_pct_all, thr_abs_all = [], []

    for i in range(K):
        test = folds[i]
        train = [r for j, f in enumerate(folds) if j != i for r in f]
        if not test or not train:
            continue

        # (A) 30th-percentile rule
        thr_pct = percentile(sorted(r["margin"] for r in train), PCTL)
        ev_pct = evaluate(test, thr_pct)

        # (B) absolute refit (precision-constrained capture maximiser)
        thr_abs, ev_abs_train, abs_info = search_threshold(train)
        ev_abs = evaluate(test, thr_abs)

        # (C) OR-other alone (diagnostic floor)
        ev_other = evaluate(test, -1.0)

        thr_pct_all.append(thr_pct)
        thr_abs_all.append(thr_abs)
        pct_test.append(ev_pct)
        abs_test.append(ev_abs)
        otheronly_test.append(ev_other)
        for tag, ev in (("pct", ev_pct), ("abs", ev_abs)):
            pool[tag]["triggers"] += ev["triggers"]
            pool[tag]["captured"] += ev["captured_errors"]
            pool[tag]["errors"] += ev["errors"]
            pool[tag]["n"] += ev["n"]

        per_fold.append(
            {
                "fold": i + 1,
                "test_n": len(test),
                "train_n": len(train),
                "test_errors": ev_pct["errors"],
                "pct_threshold": round(thr_pct, 6),
                "pct_rule": ev_pct,
                "abs_threshold": round(thr_abs, 6),
                "abs_rule": ev_abs,
                "abs_train_choice": ev_abs_train,
                "abs_train_info": abs_info,
                "other_only": ev_other,
            }
        )

    summary = {
        "pct_rule": {
            "precision": _agg(pct_test, "precision"),
            "capture_rate": _agg(pct_test, "capture_rate"),
            "trigger_rate": _agg(pct_test, "trigger_rate"),
            "false_alarms": _agg(pct_test, "false_alarms"),
            "threshold_used": mean_sd(thr_pct_all),
            "pooled": {
                "trigger_rate": _ratio(pool["pct"]["triggers"], pool["pct"]["n"]),
                "capture_rate": _ratio(pool["pct"]["captured"], pool["pct"]["errors"]),
                "precision": _ratio(pool["pct"]["captured"], pool["pct"]["triggers"]),
            },
        },
        "abs_rule": {
            "precision": _agg(abs_test, "precision"),
            "capture_rate": _agg(abs_test, "capture_rate"),
            "trigger_rate": _agg(abs_test, "trigger_rate"),
            "false_alarms": _agg(abs_test, "false_alarms"),
            "threshold_used": mean_sd(thr_abs_all),
            "target_feasible_folds": sum(
                1 for f in per_fold if f["abs_train_info"]["feasible"]
            ),
            "folds": len(per_fold),
            "pooled": {
                "trigger_rate": _ratio(pool["abs"]["triggers"], pool["abs"]["n"]),
                "capture_rate": _ratio(pool["abs"]["captured"], pool["abs"]["errors"]),
                "precision": _ratio(pool["abs"]["captured"], pool["abs"]["triggers"]),
            },
        },
        "other_only_floor": {
            "precision": _agg(otheronly_test, "precision"),
            "capture_rate": _agg(otheronly_test, "capture_rate"),
            "trigger_rate": _agg(otheronly_test, "trigger_rate"),
        },
        # OR-other component contribution on the held-out folds under the abs rule
        "other_component": {
            "triggers": _agg(abs_test, "other_added_triggers"),
            "captures": _agg(abs_test, "other_added_captures"),
            "share_of_triggers": _agg(abs_test, "other_added_share_of_triggers"),
            "share_of_captures": _agg(abs_test, "other_added_share_of_captures"),
            "share_of_triggers_pct_rule": _agg(
                pct_test, "other_added_share_of_triggers"
            ),
            "share_of_captures_pct_rule": _agg(
                pct_test, "other_added_share_of_captures"
            ),
        },
    }

    # recommendations: fold-mean and a full-data refit for deployment
    full_thr_abs, full_ev, full_info = search_threshold(rows)
    full_thr_pct = percentile(sorted(r["margin"] for r in rows), PCTL)
    recommendation = {
        "fold_mean_pct_threshold": summary["pct_rule"]["threshold_used"]["mean"],
        "fold_mean_abs_threshold": summary["abs_rule"]["threshold_used"]["mean"],
        "full_data_abs_refit_threshold": round(full_thr_abs, 6),
        "full_data_abs_refit_train_eval": full_ev,
        "full_data_abs_refit_info": full_info,
        "full_data_pct30_threshold": round(full_thr_pct, 6),
        "target_precision": TARGET_PRECISION,
    }

    out[label] = {
        "n_real_joined": len(rows),
        "folds_detail": per_fold,
        "summary": summary,
        "recommendation": recommendation,
    }


def main():
    gold_rows = _load(GOLD)
    result = {
        "meta": {
            "k": K,
            "seed": SEED,
            "percentile": PCTL,
            "target_precision": TARGET_PRECISION,
            "gate_rule": "margin <= threshold OR model_selected_other",
        },
        "data": {},
        "models": {},
    }
    for label, path in SERVER.items():
        rows, rep = build_dataset(path, gold_rows)
        result["data"][label] = rep
        run_model(label, rows, result["models"])

    with OUT.open("w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------- console table
    def fmt(x):
        return "  n/a" if x is None else f"{x:.3f}"

    print("=" * 78)
    print("R2 threshold refit -- 5-fold stratified CV on real labelled turns")
    for label, rep in result["data"].items():
        print(
            f"  {label}: real={rep['real_rows']} joined={rep['joined']} "
            f"unmatched={rep['unmatched']} dup_dropped={rep['dropped_duplicate_txt']}"
        )
    print("=" * 78)
    for label in SERVER:
        m = result["models"][label]
        s = m["summary"]
        print(f"\n### model {label}  (n={m['n_real_joined']})")
        print(
            "  rule            precision(mean+-sd)   capture(mean+-sd)    "
            "trigRate   falseAlarms  thr(mean)"
        )
        for name, blk in (("pct30 OR other", s["pct_rule"]), ("abs OR other", s["abs_rule"])):
            p, c, tr, fa, th = (
                blk["precision"],
                blk["capture_rate"],
                blk["trigger_rate"],
                blk["false_alarms"],
                blk["threshold_used"],
            )
            print(
                f"  {name:<15} {fmt(p['mean'])}+-{fmt(p['sd'])}        "
                f"{fmt(c['mean'])}+-{fmt(c['sd'])}       {fmt(tr['mean'])}      "
                f"{fmt(fa['mean']):>6}      {fmt(th['mean'])}"
            )
        oo = s["other_only_floor"]
        print(
            f"  {'other-only':<15} {fmt(oo['precision']['mean'])}+-{fmt(oo['precision']['sd'])}"
            f"        {fmt(oo['capture_rate']['mean'])}+-{fmt(oo['capture_rate']['sd'])}"
            f"       {fmt(oo['trigger_rate']['mean'])}"
        )
        print("  pooled (micro) abs rule: " + json.dumps(
            s["abs_rule"]["pooled"], ensure_ascii=False))
        print("  pooled (micro) pct rule: " + json.dumps(
            s["pct_rule"]["pooled"], ensure_ascii=False))
        print(
            "  0.90 precision target reachable on train: "
            f"{s['abs_rule']['target_feasible_folds']}/{s['abs_rule']['folds']} folds"
        )
        oc = s["other_component"]
        print(
            f"  OR-other adds triggers={fmt(oc['triggers']['mean'])} "
            f"captures={fmt(oc['captures']['mean'])} "
            f"share_trig={fmt(oc['share_of_triggers']['mean'])} "
            f"share_cap={fmt(oc['share_of_captures']['mean'])}"
        )
        r = m["recommendation"]
        print(
            f"  RECOMMENDED abs threshold: full-data refit = "
            f"{r['full_data_abs_refit_threshold']} (in-sample precision "
            f"{r['full_data_abs_refit_train_eval']['precision']}, capture "
            f"{r['full_data_abs_refit_train_eval']['capture_rate']}) ; "
            f"fold-mean = {fmt(r['fold_mean_abs_threshold'])} ; p30(full) = "
            f"{r['full_data_pct30_threshold']}"
        )
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
