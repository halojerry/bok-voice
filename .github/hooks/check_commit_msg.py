#!/usr/bin/env python3
"""Conventional Commits 校验(pre-commit commit-msg 钩子,规范十条 R1 2026-10-04)。

只校验**手写提交**的格式 `type(scope)!: 主题`;git 自动消息(Merge/Revert/
fixup/squash)与初始化/发布类直接放行——校验格式,不校验品味。

用法(commit-msg 钩子由 pre-commit 以文件路径调用):
    python3 check_commit_msg.py .git/COMMIT_EDITMSG
退出码 0=放行,1=拒绝(拒绝消息给修复示例)。

路径闸:参数须是仓库 .git/ 下 git 自产的四个消息文件之一(规范化后 containment
校验,拒绝 '../' 越界——本文件是钩子入口,收的是 git 传的路径,不是任意用户输入)。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# type 集对齐仓库既有习惯(feat/fix/refactor/perf/test/docs/chore/build/ci/
# security/ops/revert);scope 可选、小写/数字/`-_.//`(可嵌 apps/agent 这类路径)。
_CONVENTIONAL_RE = re.compile(
    r"^(feat|fix|refactor|perf|test|docs|chore|build|ci|security|ops|revert)"
    r"(\([a-zA-Z0-9_\-./\u4e00-\u9fff]+\))?!?: \S.+"
)
# git 自动消息前缀:放行(不是人写的)。
_AUTO_PREFIXES = ("Merge ", "Revert ", "fixup! ", "squash! ", "Initial commit")
# git 自产消息文件名(钩子契约面;别的文件名一律拒)。
_MSG_BASENAMES = {"COMMIT_EDITMSG", "MERGE_MSG", "SQUASH_MSG", "TAG_EDITMSG"}


def _repo_git_dir() -> Path:
    # 钩子 cwd=仓库根(pre-commit 约定);.github/hooks/ 的上两级=仓库根。
    root = Path(__file__).resolve().parents[2]
    git_dir = root / ".git"
    return git_dir if git_dir.is_dir() else Path.cwd() / ".git"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: check_commit_msg.py <commit-msg-file>", file=sys.stderr)
        return 1
    raw = Path(argv[1])
    # 路径闸:名字在 git 自产集合 + 规范化后落在 .git 目录内(拒 '../' 越界)。
    if raw.name not in _MSG_BASENAMES:
        print(f"[commit-msg] 非法消息文件名(仅认 git 自产四件): {raw.name!r}", file=sys.stderr)
        return 1
    git_dir = _repo_git_dir().resolve()
    resolved = raw.resolve()
    if not resolved.is_relative_to(git_dir):
        print(f"[commit-msg] 消息文件越界({resolved} 不在 {git_dir} 内)", file=sys.stderr)
        return 1
    try:
        first = (resolved.read_text(encoding="utf-8", errors="replace").splitlines() or [""])[0].strip()
    except OSError as exc:
        print(f"[commit-msg] cannot read {resolved}: {exc}", file=sys.stderr)
        return 1
    if not first or any(first.startswith(p) for p in _AUTO_PREFIXES):
        return 0
    if _CONVENTIONAL_RE.match(first):
        return 0
    print(
        "[commit-msg] 提交主题不符合 Conventional Commits(规范十条 R1):\n"
        f"  收到: {first!r}\n"
        "  形如: feat(agent): 主题  |  fix(ci): 主题  |  docs: 主题\n"
        "  type ∈ feat|fix|refactor|perf|test|docs|chore|build|ci|security|ops|revert\n"
        "  绕过(仅应急,勿常态化): git commit --no-verify",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
