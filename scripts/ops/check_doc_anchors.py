#!/usr/bin/env python3
"""活文档 `.py:行号` 锚计数棘轮检查（G3b 治理，2026-10-05）。

为什么存在:文档里的 `.py:123` 行号锚随合流/重构必然漂移——行号是锚不是契约,
`file:line` 只作辅助,权威=符号名。治理计划定案:活文档锚**总量只准降不准涨**
（棘轮）;新增行号锚必须显式 `--update` 重录基线（人工动作,留痕于 git）,
否则 CI 红——把「随手写行号」的成本显性化。

扫描面:与 check_doc_paths.py **完全同一活文档集**（直接复用其 iter_live_docs,
不另立第二份定义——一致性即正确性）;读取纪律同源:markdown 整文件,
workflow yml 只查 run: 行/块。历史档白名单不查:docs/superpowers/**、
docs/archive/**、reports/**（iter_live_docs 已排除）。

计数:正则 `\\.py:\\d+`（`.py` 文件名紧跟 `:数字`,如 agent.py:8913）在活文档
全集中的出现次数。

基线:scripts/ops/doc_anchor_baseline.json（形状 {"count","as_of","note"}）,
路径相对**被扫根**解析（真仓=本仓;--root 指向 tmp 夹具时夹具自备基线,供测试
演练增长路径）。基线缺失=门未武装,退出 1 并提示播种。

用法:
    .venv312/bin/python scripts/ops/check_doc_anchors.py            # 人类可读
    .venv312/bin/python scripts/ops/check_doc_anchors.py --json     # 机读 JSON
    .venv312/bin/python scripts/ops/check_doc_anchors.py --update   # 棘轮重录
    .venv312/bin/python scripts/ops/check_doc_anchors.py --root DIR # 换根(测试)

退出码:0=计数 ≤ 基线;1=超基线（增长）或基线缺失。--update 恒 0（重录即认账）。
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
_OPS = _pathlib.Path(__file__).resolve().parent  # 同桶兄弟(check_doc_paths)平铺 import 面
if str(_OPS) not in _sys.path:
    _sys.path.insert(0, str(_OPS))

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from check_doc_paths import WORKFLOW_EXTS, iter_live_docs, iter_yml_run_lines  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

BASELINE_RELPATH = Path("scripts") / "ops" / "doc_anchor_baseline.json"
ANCHOR_RE = re.compile(r"\.py:\d+")
_BASELINE_NOTE = "活文档 .py:行号 锚总量棘轮基线（G3b）;降基线/放行新增锚须显式 --update 重录"


def count_anchors_in_text(text: str) -> int:
    """单文本内 `.py:数字` 锚出现次数。"""
    return len(ANCHOR_RE.findall(text))


def scan_anchor_counts(root: Path | str) -> dict[str, int]:
    """活文档集逐件锚计数（仓库相对 posix 路径 → 次数;0 次文件不进表）。

    文件集与读取纪律与 check_doc_paths 同源（见模块 docstring）。
    """
    root = Path(root).resolve()
    counts: dict[str, int] = {}
    for doc in iter_live_docs(root):
        try:
            rel = doc.relative_to(root).as_posix()
        except ValueError:
            continue
        if doc.suffix in WORKFLOW_EXTS:
            text = "\n".join(t for _, t in iter_yml_run_lines(doc))
        else:
            try:
                text = doc.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        n = count_anchors_in_text(text)
        if n:
            counts[rel] = n
    return counts


def baseline_path(root: Path | str) -> Path:
    return Path(root).resolve() / BASELINE_RELPATH


def load_baseline(root: Path | str) -> dict | None:
    """读基线;缺失/坏 JSON/形状不对一律 None（=门未武装,由调用方裁决）。"""
    try:
        data = json.loads(baseline_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("count"), int):
        return None
    return data


def write_baseline(root: Path | str, count: int, as_of: str, note: str = _BASELINE_NOTE) -> None:
    p = baseline_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"count": count, "as_of": as_of, "note": note}, ensure_ascii=False, indent=2)
    p.write_text(payload + "\n", encoding="utf-8")


def _git_head(root: Path) -> str:
    """基线 as_of 戳;git 缺席（tmp 夹具/打包运行）回退 'unknown'。"""
    try:
        return subprocess.run(  # noqa: S603 - 固定 argv,无用户输入
            ["git", "-C", str(root), "rev-parse", "--short=7", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 - git 缺席不炸
        return "unknown"


def check_anchors(root: Path | str = ROOT) -> dict:
    """棘轮判定结果:{count, baseline, delta, per_file, ok, ...}。

    baseline/delta 为 None 表示基线缺失（ok=False,单独 error 字段说明）。
    per_file 按次数降序（同数次数字典序）。
    """
    root = Path(root).resolve()
    per_file = scan_anchor_counts(root)
    total = sum(per_file.values())
    result: dict = {
        "count": total,
        "per_file": dict(sorted(per_file.items(), key=lambda kv: (-kv[1], kv[0]))),
        "baseline_path": baseline_path(root).as_posix(),
    }
    base = load_baseline(root)
    if base is None:
        result.update(ok=False, error="baseline-missing", baseline=None, delta=None)
        return result
    result.update(
        ok=total <= base["count"],
        baseline=base["count"],
        delta=total - base["count"],
    )
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="活文档 .py:行号 锚计数棘轮检查（G3b 治理）")
    ap.add_argument("--json", action="store_true", help="输出机读 JSON")
    ap.add_argument("--root", default=str(ROOT), help="被扫仓库根（默认本仓;测试可指 tmp 夹具）")
    ap.add_argument("--update", action="store_true", help="以当前树重录基线（棘轮降基线的显式动作）")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()

    if args.update:
        new_total = sum(scan_anchor_counts(root).values())
        old = load_baseline(root)
        old_n = old.get("count") if isinstance(old, dict) else None
        write_baseline(root, new_total, _git_head(root))
        print(f"[check_doc_anchors] baseline updated: {old_n} -> {new_total} (as_of={_git_head(root)})")
        return 0

    result = check_anchors(root)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for rel, n in result["per_file"].items():
            print(f"{rel}: {n}")
        if result.get("error") == "baseline-missing":
            print(
                f"[check_doc_anchors] baseline missing: {result['baseline_path']}（先跑 --update 播种）",
                file=sys.stderr,
            )
        elif result["ok"]:
            print(
                f"[check_doc_anchors] OK: {result['count']} anchor(s) <= baseline {result['baseline']}",
                file=sys.stderr,
            )
        else:
            print(
                f"[check_doc_anchors] OVER: {result['count']} > baseline {result['baseline']}"
                f" (delta +{result['delta']})——新增行号锚须显式 --update 重录基线",
                file=sys.stderr,
            )
    if result.get("error") == "baseline-missing":
        return 1
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
