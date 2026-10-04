"""scripts/README.md 索引覆盖 lint（仓库治理 G1a）。

规范：docs/superpowers/plans/2026-10-04-repo-governance-plan.md §3.1/§3.3。

规则：
- 实际文件集 = ``scripts/**/*.py``（排除 ``cuda/``、``artifacts/``、``__pycache__/``；
  含 ``archive/`` 与后续迁移出的桶目录）；
- 索引集 = ``scripts/README.md`` 表格第一列的反引号路径；
- 两集必须相等：新增孤儿脚本未进表 = 红；表里指向已不存在的文件 = 红；
- 三个产品运行时 exec 脚本（``pregen_tts`` / ``mock_callee`` / ``mine_qa``，
  CP / tools.bok.py 直接起进程）必须在表中且消费者列带 ★ 标星。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
README = SCRIPTS / "README.md"
EXCLUDE_PARTS = frozenset({"cuda", "artifacts", "__pycache__"})
RUNTIME_EXEC = ("runtime/pregen_tts", "runtime/mock_callee", "runtime/mine_qa")
TABLE_HEADER = "| 脚本 | 用途 | 消费者 | 真栈 | 最近证据 |"
ROW_RE = re.compile(r"^\|\s*`(scripts/[A-Za-z0-9_./-]+\.py)`\s*\|")


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def managed_script_files() -> set[str]:
    """scripts/ 下受管 .py（排除 cuda/artifacts/__pycache__）。"""
    return {
        _relative(p)
        for p in SCRIPTS.rglob("*.py")
        if not (EXCLUDE_PARTS & set(p.relative_to(SCRIPTS).parts))
    }


def indexed_scripts() -> set[str]:
    """scripts/README.md 表格第一列的脚本路径。"""
    return {
        m.group(1)
        for line in README.read_text(encoding="utf-8").splitlines()
        if (m := ROW_RE.match(line))
    }


def _rows() -> list[tuple[str, str]]:
    """(脚本路径, 整行文本) 列表，保持表格顺序。"""
    return [
        (m.group(1), line)
        for line in README.read_text(encoding="utf-8").splitlines()
        if (m := ROW_RE.match(line))
    ]


def test_readme_exists_with_expected_header():
    assert README.is_file(), f"缺少 {README}：scripts/ 索引是本仓 G1a 契约（治理计划 §3.1）"
    text = README.read_text(encoding="utf-8")
    assert TABLE_HEADER in text, (
        f"scripts/README.md 表格表头必须逐字为：{TABLE_HEADER}\n"
        "（tests/test_scripts_index.py 依赖第一列做覆盖比对）"
    )


def test_index_covers_every_managed_script():
    """实际文件集与索引集必须相等——这是「目录即索引」的收口。"""
    actual = managed_script_files()
    indexed = indexed_scripts()
    missing = sorted(actual - indexed)
    stale = sorted(indexed - actual)
    assert not missing and not stale, (
        "scripts/README.md 索引与 scripts/ 实际 .py 不一致（增删/改名脚本必须同步本表）：\n"
        f"  未进表（{len(missing)}）: {missing}\n"
        f"  表里死链（{len(stale)}）: {stale}\n"
        "修复：更新 scripts/README.md 表格（一文件一行；格式 `| `scripts/x.py` | … |`）。"
    )


def test_index_has_no_duplicate_rows():
    paths = [p for p, _ in _rows()]
    dupes = sorted({p for p in paths if paths.count(p) > 1})
    assert not dupes, f"scripts/README.md 存在重复行：{dupes}"


def test_index_scope_heuristic_is_not_empty():
    """防止 rglob 被改坏后 lint 空转（sanity）。"""
    assert len(managed_script_files()) >= 100, (
        "受管脚本集合异常小——检查 tests/test_scripts_index.py 的排除规则是否误伤"
    )


def test_runtime_exec_scripts_are_starred():
    """产品运行时 exec 的三个脚本：消费者列必须含 ★ 且点名 CP/bok.py。"""
    rows = dict(_rows())
    for stem in RUNTIME_EXEC:
        path = f"scripts/{stem}.py"
        assert path in rows, (
            f"{path} 是产品运行时 exec 脚本（CP / tools.bok.py 直接起进程），必须进 scripts/README.md"
        )
        row = rows[path]
        assert "★" in row, (
            f"{path} 的表格行缺 ★ 标星：产品运行时脚本要在消费者列显著标注"
        )
        assert "CP" in row or "bok" in row, (
            f"{path} 的消费者列必须点名 CP 或 bok.py 的调用点（见治理计划 §1.1 运行时 exec 表）"
        )
