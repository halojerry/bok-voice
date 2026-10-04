"""桶内脚本 repo 根锚 lint（G1c 收口）。

为什么存在:G1 分桶把 84 个脚本从 scripts/ 顶层挪进桶目录——顶层时代
``Path(__file__).resolve().parents[1]`` == 仓库根;入桶后 parents[1] == scripts/,
**运行期**才炸(探针写报告进 scripts/reports/、CP 包路径解析空转、工具脚本
找不到仓内资产)。G1c 修了 83 处(77 parents[1] + 6 parent.parent),本 lint
钉死:桶内文件的 ``__file__`` 锚只准 parents[0](本桶)/parents[2](repo 根),
``parent.parent`` 同禁——防新增/回拷文件再把旧习惯带回来。

白名单:
- G1 引导头的 ``_S = _pathlib.Path(__file__).resolve().parents[1]``——
  头的语义就是 scripts/ 根,by design(见 §3.1);
- **scripts 根锚**:变量名为 ``*_SCRIPTS``/``SCRIPTS_DIR``/``HERE``/``_HERE``
  (尾段)时 ``parents[1]`` == scripts/ 根,是**正确**语义(随后常
  ``.parent`` 推 repo 根);这类锚的坑在「单 .parent」形态,由第二个
  测试钉;
- ``probe_8khz_asr._load_e2e`` 的 ``parents[1] / "e2e"``——显式拼 scripts/e2e
  桶路径,by design;
- scripts/cuda/**(独立交付包)与 scripts/archive/**(退役件)不检查。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
EXCLUDE_PARTS = frozenset({"cuda", "artifacts", "__pycache__", "archive"})
HEADER_OK = re.compile(r"_S = _pathlib\.Path\(__file__\)\.resolve\(\)\.parents\[1\]")
ALLOWLIST: dict[str, re.Pattern[str]] = {
    # 显式白名单:文件 → 允许的 parents[1] 用行(须带 by-design 注释)
    "probes/probe_8khz_asr.py": re.compile(
        r'parents\[1\] / "e2e" / "e2e_campaign\.py".*G1c'
    ),
}


def managed_bucket_scripts() -> list[Path]:
    """scripts/ 一级桶内(非顶层、非排除目录)的受管脚本。"""
    return sorted(
        p
        for p in SCRIPTS.rglob("*.py")
        if not (EXCLUDE_PARTS & set(p.relative_to(SCRIPTS).parts))
        and len(p.relative_to(SCRIPTS).parts) >= 2  # 桶内,非顶层
    )


def test_bucket_scripts_have_no_broken_repo_root_anchors():
    """ROOT/REPO 命名的 parents[1]/parent.parent 锚 = repo 根语义错位,禁。"""
    offenders: list[str] = []
    scripts_name = re.compile(r"^_?[A-Za-z]*_?(?:SCRIPTS|HERE)$|^(?:SCRIPTS_DIR)$")
    for p in managed_bucket_scripts():
        rel = p.relative_to(SCRIPTS).as_posix()
        allow = ALLOWLIST.get(rel)
        for i, line in enumerate(
            p.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        ):
            if HEADER_OK.search(line):
                continue
            if "parents[1]" in line and "Path(__file__)" in line:
                if allow and allow.search(line):
                    continue
                var = line.split("=")[0].strip()
                if scripts_name.match(var):
                    continue  # scripts 根语义,合法
                offenders.append(f"{rel}:{i}: parents[1] 锚(入桶后=scripts/,repo 根应 parents[2]): {line.strip()[:90]}")
            if re.search(r"\.parent\.parent\b", line) and "Path(__file__)" in line:
                var = line.split("=")[0].strip()
                if scripts_name.match(var):
                    continue
                offenders.append(f"{rel}:{i}: parent.parent 锚(同上): {line.strip()[:90]}")
    assert not offenders, (
        "桶内脚本出现 parents[1]/parent.parent 型 repo 根锚——入桶后这些值=scripts/ 而非"
        "仓库根,运行期才炸(见 G1c 战报)。要么 parents[2],要么把锚收进引导头。违例:\n"
        + "\n".join(offenders)
    )


def test_bucket_scripts_no_scriptsdir_name_from_single_parent():
    """名为 *SCRIPTS* 的锚变量不得来自单 .parent——顶层习惯(=scripts/)入桶后=本桶。

    (check_schema_drift 的 `SCRIPTS_DIR` 断锚曾把 schema 门禁打红在 CI:
    产物路径拼出 scripts/scripts/artifacts/...——正是这一族。)
    """
    offenders: list[str] = []
    for p in managed_bucket_scripts():
        rel = p.relative_to(SCRIPTS).as_posix()
        for i, line in enumerate(
            p.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        ):
            if (
                "SCRIPTS" in line.split("=")[0]
                and "Path(__file__)" in line
                and re.search(r"resolve\(\)\.parent$", line.strip().rstrip(","))
            ):
                offenders.append(f"{rel}:{i}: {line.strip()[:90]}")
    assert not offenders, (
        "*SCRIPTS* 命名的锚用了单 .parent——入桶后那是本桶目录不是 scripts/ 根,"
        "应 parents[1]。违例:\n" + "\n".join(offenders)
    )


def test_allowlist_entries_still_exist_and_commented():
    """白名单条目若被删/改形,提示更新本测试而不是静默放宽。"""
    src = (SCRIPTS / "probes" / "probe_8khz_asr.py").read_text(encoding="utf-8")
    assert 'parents[1] / "e2e" / "e2e_campaign.py"' in src, (
        "probe_8khz_asr 的 e2e 桶拼接改形了——同步更新 ALLOWLIST 或让它走 parents[2] 拼法"
    )
