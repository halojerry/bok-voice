"""Mimosa 扫描 triage：findings.json vs 仓库审定清单 → 只打印未审定增量。

用法：
  python scripts/mimosa_triage.py [<findings.json 路径或扫描目录>]
  缺省取 ~/.mimosa/security-scans/ 下**最新** scan-*/findings.json。

退出码：0=全部命中审定清单；2=存在未审定 finding（CI/收官门用）；
3=找不到 findings.json（用法错误）。

审定清单=``security/mimosa/suppressions.json``：**家族级**规则（publicClass +
路径 glob → verdict/reason/evidence），不是逐 finding 一行——同族误阳一条
规则覆盖，新增同族 finding 自动命中。verdict ∈ {false-positive, accepted-risk,
fixed}。纪律：每条必须有 evidence（测试/文档/代码单点引用）；本脚本不判
「安全」，只判「审没审过」。
"""

from __future__ import annotations

import fnmatch
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "security" / "mimosa" / "suppressions.json"


def _latest_findings() -> Path | None:
    base = Path.home() / ".mimosa" / "security-scans"
    if not base.is_dir():
        return None
    cands = sorted(base.glob("*/scan-*/findings.json"))
    return cands[-1] if cands else None


def _load_findings(arg: str | None) -> list[dict]:
    target: Path | None = None
    if arg:
        p = Path(arg).expanduser()
        if p.is_dir():
            p = p / "findings.json"
        target = p
    else:
        target = _latest_findings()
    if target is None or not target.is_file():
        print(f"[triage] 找不到 findings.json（参数={arg!r}）", file=sys.stderr)
        raise SystemExit(3)
    data = json.loads(target.read_text())
    items = data if isinstance(data, list) else data.get("findings", [])
    print(f"[triage] findings: {len(items)} 条（{target}）")
    return items


def _rule_matches(rule: dict, finding: dict) -> bool:
    cls = str((finding.get("identity") or {}).get("publicClass") or "")
    if rule.get("publicClass") and rule["publicClass"] != cls:
        return False
    path = str((finding.get("location") or {}).get("path") or "")
    pat = rule.get("path_glob") or "*"
    if not fnmatch.fnmatch(path, pat):
        return False
    title_pat = rule.get("title_contains")
    if title_pat and title_pat not in str(finding.get("title") or ""):
        return False
    return True


def main() -> int:
    items = _load_findings(sys.argv[1] if len(sys.argv) > 1 else None)
    rules = json.loads(MANIFEST.read_text())["rules"]
    unadj: list[dict] = []
    counts: dict[str, int] = {}
    for f in items:
        hit = next((r for r in rules if _rule_matches(r, f)), None)
        if hit is None:
            unadj.append(f)
        else:
            counts[hit["id"]] = counts.get(hit["id"], 0) + 1
    print("[triage] 家族命中统计（规则 → 条数）:")
    for rid in sorted(counts):
        rule = next(r for r in rules if r["id"] == rid)
        print(f"  {rid:<28} {counts[rid]:>3}  [{rule['verdict']}] {rule['reason'][:60]}")
    if unadj:
        print(f"\n[triage] 未审定 finding {len(unadj)} 条（新增量，需人工逐条判定）:")
        for f in unadj[:40]:
            loc = f.get("location") or {}
            print(
                f"  - {(f.get('identity') or {}).get('publicClass')} "
                f"{loc.get('path')}:{loc.get('line')} {(f.get('title') or '')[:40]}"
            )
        if len(unadj) > 40:
            print(f"  …（另有 {len(unadj) - 40} 条）")
        return 2
    print("\n[triage] 全部 finding 命中审定清单（零未审增量）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
