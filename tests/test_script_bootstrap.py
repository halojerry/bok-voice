"""G1a 引导头 marker lint（仓库治理计划 §3.1）。

``scripts/**/*.py``（排除 ``cuda/``、``artifacts/``、``__pycache__/``）每个文件必须含：

1. marker 行 ``# --- scripts import bootstrap (G1) ---``（独立注释行）；
2. ``parents[1]`` 的 sys.path 引导语句（搬家后裸 import 兄弟模块仍可解析）。

缺失即红；修复方式 = 按 ``docs/superpowers/plans/2026-10-04-repo-governance-plan.md``
§3.1 的 8 行引导头模板补（插在模块 docstring / ``__future__`` 之后、业务 import 之前）。

红线：``scripts/cuda/`` 是独立交付包（CUDA 一键部署），任何文件不得含该 marker。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MARKER = "# --- scripts import bootstrap (G1) ---"
EXCLUDE_PARTS = frozenset({"cuda", "artifacts", "__pycache__"})
FIX_HINT = (
    "按 docs/superpowers/plans/2026-10-04-repo-governance-plan.md §3.1 的 8 行引导头模板补"
    "（marker + `_S = Path(__file__).resolve().parents[1]` 的 sys.path 引导）"
)
TEXT_SUFFIXES_FOR_CUDA = {".sh", ".md", ".example", ".env", ".unit", ".txt", ".yml", ".yaml", ".py"}


def managed_scripts() -> list[Path]:
    return sorted(
        p
        for p in SCRIPTS.rglob("*.py")
        if not (EXCLUDE_PARTS & set(p.relative_to(SCRIPTS).parts))
    )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def test_managed_scripts_is_not_empty():
    """防止 rglob 被改坏后本 lint 空转（sanity）。"""
    assert len(managed_scripts()) >= 100, (
        "受管脚本集合异常小——检查 tests/test_script_bootstrap.py 的排除规则是否误伤"
    )


def test_managed_scripts_have_bootstrap_marker():
    offenders = []
    for path in managed_scripts():
        text = _read(path)
        if not any(line.strip() == MARKER for line in text.splitlines()):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, (
        f"{len(offenders)} 个受管脚本缺 G1 引导头 marker（{FIX_HINT}）：\n  "
        + "\n  ".join(offenders)
    )


def test_managed_scripts_have_parents_path_guide():
    offenders = []
    for path in managed_scripts():
        if "parents[1]" not in _read(path):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, (
        f"{len(offenders)} 个受管脚本缺 parents[1] 引导语句（{FIX_HINT}）：\n  "
        + "\n  ".join(offenders)
    )


def test_cuda_package_is_not_bootstrapped():
    """红线：scripts/cuda/ 是独立交付包，引导头不得渗入。"""
    cuda = SCRIPTS / "cuda"
    if not cuda.is_dir():
        return
    offenders = []
    for path in sorted(cuda.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES_FOR_CUDA:
            continue
        if MARKER in _read(path):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, (
        "scripts/cuda/（独立 CUDA 部署包）出现 G1 引导头 marker，红线被破：\n  "
        + "\n  ".join(offenders)
    )
