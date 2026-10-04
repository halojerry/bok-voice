#!/usr/bin/env python3
"""活文档仓内路径断链检查（G1a 治理，2026-10-04）。

为什么存在:REPO_MAP/README/DELIVERY-MANUAL/AGENTS/CI workflow 里的仓内路径
（`scripts/...`、`docs/...`、`tools/...`、`apps/...`）会随搬迁/删除悄悄死掉,
活文档指向虚无 = 新人第一时间被带到坑里（治理计划 §3.3 定案:断链只增不减）。

扫描面（活文档）:
  * 仓库根 README.md / AGENTS.md / DELIVERY-MANUAL.md / REPO_MAP.md（存在才查）;
  * docs/*.md + docs/ 一级子目录的非归档 .md;
  * .github/workflows/*.yml（只查 run: 行/块内的路径）;
  * .agents/skills/call-diagnosis/**/*.md。
**历史档白名单不改也不查**:docs/superpowers/**、docs/archive/**、reports/**。

提取:markdown 链接 [x](path)、反引号 `path/to/file`、yml run: 行内路径 token;
只认「含 / 或带已知扩展名」的形态——http(s)://、纯单词、env 变量 $XXX、带通配符/
占位符（* ? { } < >）的模板一律跳过。

在scope 判定（避免把外域/运行时引用误报成仓内断链）:
  * 路径型引用（含 /）:首段必须是本仓当前顶层条目,或 RETIRED_ROOT_AREAS 记录的
    已退役顶层目录（退役引用必须浮出,清理后应归零）;
  * 裸文件名:仅当同一行还有**同扩展名**的引用确实解析到仓内文件时才算仓内断言
    （成组列举里的死名字,如 REPO_MAP 构建脚本清单）;单独出现的运行时/上游
    文件名（node-state.json、dataset.py 等）不当作仓内路径;
  * 局部工作台（.superpowers/** 等 gitignored 工作台）、构建装配产物（runtime/**,
    由 build_runtime.sh 在 CI 期生成）与 IGNORED_REFS（上游仓同形路径/计划态提案）
    显式豁免。

解析:相对引用文件所在目录 → 仓库根 → 全仓路径后缀匹配（容忍文档里的省略写法,
如 `components/x.tsx` 实际在 apps/web/ 下）;`/` 开头按仓库根,但首段不在仓库顶层
目录的（/api/... 等 HTTP 路由、/tmp/... 等系统路径）不视为仓内路径。

用法:
    .venv312/bin/python scripts/ops/check_doc_paths.py           # 人类可读清单
    .venv312/bin/python scripts/ops/check_doc_paths.py --json    # 机读 JSON 数组

退出码:0=无断链;1=有断链（清单到 stdout:引用文件:行号 → 断链路径）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# --- scripts import bootstrap (G1) ---
# sys.path 引导(G1 迁移解耦,见 docs/superpowers/plans/2026-10-04-repo-governance-plan.md §3.1):
# 同层时是 no-op;文件挪进任何桶后裸 import 兄弟模块继续解析。
import sys as _sys, pathlib as _pathlib
_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))

ROOT = Path(__file__).resolve().parents[2]

# --- 扫描面 ---------------------------------------------------------------

ROOT_DOCS = ("README.md", "AGENTS.md", "DELIVERY-MANUAL.md", "REPO_MAP.md")
DOCS_DIR = "docs"
DOCS_SUBDIR_EXCLUDES = frozenset({"archive", "superpowers"})
WORKFLOW_DIR = Path(".github") / "workflows"
WORKFLOW_EXTS = frozenset({".yml", ".yaml"})
SKILL_DIR = Path(".agents") / "skills" / "call-diagnosis"

# 历史档:治理计划定案不改引用,自然也不该被本检查器报（防御性排除）。
HISTORICAL_PREFIXES = ("docs/superpowers/", "docs/archive/", "reports/")

# 已退役顶层目录:治理计划已知删除、但活文档若仍指路必须浮出（清干净后为空引用）。
RETIRED_ROOT_AREAS = frozenset({"desktop"})

# 构建/装配产物顶层目录（gitignored,CI 期由 build_runtime.sh 生成）:
# 对这些路径的引用不是仓内文件断言（release.yml 的 runtime staging 校验属此类）。
GENERATED_ROOT_AREAS = frozenset({"runtime"})

# 局部工作台/工具目录（gitignored,不随克隆分发）:提及它们不是仓内路径断言。
LOCAL_ONLY_PREFIXES = (".superpowers/", ".zcode/", ".mimosa/")

# 已人工确认的非仓内引用（上游仓同形路径 / 计划态提案）:豁免,但每条都要写理由。
IGNORED_REFS = frozenset({
    # livekit/components-js 上游仓的目录（AGENTS.md「官方 Agents UI 接入」注明来源）
    "packages/shadcn/",
    # docs/CI_CD_PLAN.md 未落地提案（◻ 项;落地后应从此表移除）
    "apps/web/eslint.config.mjs",
    ".github/PULL_REQUEST_TEMPLATE.md",
})

# 已知文件扩展名（含 / 的引用始终是候选;不带 / 的引用要有这些扩展名才查）。
KNOWN_EXTS = frozenset({
    "py", "pyi", "sh", "bash", "zsh", "ps1", "bat", "cmd",
    "md", "markdown", "rst", "txt", "json", "jsonc", "yml", "yaml", "toml",
    "ini", "cfg", "conf", "spec", "service", "plist", "lock", "example", "env",
    "ts", "tsx", "js", "jsx", "mjs", "cjs", "vue", "html", "css", "scss",
    "sql", "csv", "tsv", "xml", "proto", "go", "rs", "java", "kt", "swift",
    "c", "h", "cc", "cpp", "hpp", "wav", "mp3", "png", "jpg", "jpeg", "svg",
    "gif", "webp", "pdf", "zip", "tar", "gz", "whl", "dmg", "exe",
})

# 索引/后缀匹配时跳过的重目录（构建产物/依赖/缓存,不是文档引用目标）。
INDEX_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".next",
    ".venv", ".venv312", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".turbo", ".parcel-cache", ".cache", "dist", "artifacts",
})

# --- 提取 -----------------------------------------------------------------

_MD_LINK_RE = re.compile(r"\]\(\s*<?([^)>\s]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_YML_RUN_RE = re.compile(r"^(\s*(?:-\s+)?)run:\s*(.*)$")
_YML_BLOCK_MARKERS = ("|", ">", "|-", ">-", "|+", ">+")
_LINE_SUFFIX_RE = re.compile(r":\d+(?:-\d+)?$")
_TOKEN_SAFE_RE = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.+\-]*(?:/[A-Za-z0-9_.+\-]+)*/?")
_ABS_REF_RE = re.compile(r"^/([A-Za-z0-9_.\-]+)(?:/|$)")


def normalize_candidate(raw: str) -> str | None:
    """把一行里抽出的原始 token 归一成候选仓内路径;不是路径形态返回 None。"""
    t = raw.strip()
    if not t:
        return None
    if t.startswith("<") and t.endswith(">"):
        t = t[1:-1].strip()
    # markdown 链接的 #fragment / ?query 不是路径本体
    t = re.split(r"[#?]", t, 1)[0]
    # 末尾 :行号（file.py:8913 形态）——行号锚由 check_doc_anchors 管,这里只查文件
    t = _LINE_SUFFIX_RE.sub("", t)
    # 中文/英文标点收尾（例如 "…见 `docs/x.md`。" 的句号已由反引号界掉,这里兜底逗号等）
    t = t.rstrip(",;、。，；：)）]】\"'")
    if not t:
        return None
    if t.startswith("./"):
        t = t[2:]
    if not is_path_candidate(t):
        return None
    return t


def is_path_candidate(t: str) -> bool:
    """token 是否值得当仓内路径解析:含 / 或带已知扩展名,且不含通配/占位/变量。"""
    if not t or "://" in t:
        return False
    if t.startswith(("$", "~", "-")):
        return False
    if not _TOKEN_SAFE_RE.fullmatch(t):
        return False  # 含 * ? { } < > \ : = & 空格等 → 模板/命令片段/URL,跳过
    if t.endswith("/"):
        return True  # 目录引用（如 `desktop/`、`scripts/`）
    name = t.rsplit("/", 1)[-1]
    if "." not in name:
        return False
    ext = name.rsplit(".", 1)[-1].lower()
    return ext in KNOWN_EXTS


def extract_refs_from_text(text: str) -> list[str]:
    """从普通 markdown 行提取引用:markdown 链接目标 + 反引号内容里的路径 token。"""
    refs: list[str] = []
    for m in _MD_LINK_RE.finditer(text):
        ref = normalize_candidate(m.group(1))
        if ref:
            refs.append(ref)
    for m in _BACKTICK_RE.finditer(text):
        content = m.group(1).strip()
        if not content:
            continue
        # 反引号里可能是命令/代码:逐 token 取路径形态的（python tools/bok.py serve → tools/bok.py）
        refs.extend(extract_command_refs(content))
    return refs


def extract_command_refs(text: str) -> list[str]:
    """从命令行文本（yml run: 行/块）提取路径 token;shell 变量/选项自然被过滤。"""
    refs: list[str] = []
    for token in text.split():
        ref = normalize_candidate(token)
        if ref:
            refs.append(ref)
    return refs


def iter_yml_run_lines(path: Path):
    """产出 workflow 里 run: 行/块内的 (行号, 文本)。只查 run,不查 uses/paths。"""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _YML_RUN_RE.match(line)
        if not m:
            i += 1
            continue
        rest = m.group(2).strip()
        if rest and rest not in _YML_BLOCK_MARKERS:
            yield i + 1, rest
            i += 1
            continue
        # run: | 块:收集比 run: 缩进更深的内容行
        base_indent = len(line) - len(line.lstrip())
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= base_indent:
                break
            if nxt.strip():
                yield j + 1, nxt
            j += 1
        i = j


# --- 解析 -----------------------------------------------------------------


def iter_live_docs(root: Path) -> list[Path]:
    """活文档面清单（历史档排除;文件不存在自然不进列表）。"""
    docs: list[Path] = []
    for name in ROOT_DOCS:
        p = root / name
        if p.is_file():
            docs.append(p)
    docs_root = root / DOCS_DIR
    if docs_root.is_dir():
        docs.extend(sorted(p for p in docs_root.glob("*.md") if p.is_file()))
        for sub in sorted(docs_root.iterdir()):
            if sub.is_dir() and sub.name not in DOCS_SUBDIR_EXCLUDES:
                docs.extend(sorted(p for p in sub.glob("*.md") if p.is_file()))
    wf = root / WORKFLOW_DIR
    if wf.is_dir():
        docs.extend(sorted(p for p in wf.iterdir() if p.is_file() and p.suffix in WORKFLOW_EXTS))
    skill = root / SKILL_DIR
    if skill.is_dir():
        docs.extend(sorted(skill.rglob("*.md")))
    # 白名单防御:永不检查历史档
    live: list[Path] = []
    for p in docs:
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            continue
        if rel.startswith(HISTORICAL_PREFIXES):
            continue
        live.append(p)
    return live


def _git_tracked(root: Path) -> frozenset[str] | None:
    """git ls-files 视图(tracked 文件集);git 不可用回退 None(调用方走工作树)。"""
    try:
        out = subprocess.run(  # noqa: S603 - 固定 argv,无用户输入
            ["git", "-C", str(root), "ls-files"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
    except Exception:  # noqa: BLE001 - git 缺席(打包运行)不炸,退工作树
        return None
    return frozenset(ln for ln in out.splitlines() if ln)


def _git_ignored(root: Path, rel_posix: str) -> bool:
    """路径是否被 .gitignore 盖住(运行时工件:本机盘上有、克隆里没有——
    如 deploy/cloud/.env、scripts/.probe_reply_quality.json;引用它们是
    合法的运行时路径文档,不是断链)。"""
    try:
        return subprocess.run(  # noqa: S603 - 固定 argv
            ["git", "-C", str(root), "check-ignore", "-q", rel_posix],
            capture_output=True, timeout=10,
        ).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def build_repo_index(root: Path, tracked: frozenset[str] | None = None) -> dict[str, list[str]]:
    """basename → 仓内相对路径列表（**仅文件**;目录判定走 _tracked_dirs）。

    基准=**git tracked 视图**(2026-10-04 CI 实弹教训:本机盘上的 gitignored
    运行时工件会让工作树解析本地绿、CI 红——克隆里没有那些文件)。git 不可用
    才退回 os.walk 工作树。
    """
    index: dict[str, list[str]] = {}
    entries: list[str]
    if tracked is not None:
        entries = list(tracked)
    else:
        entries = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in INDEX_SKIP_DIRS)
            for name in list(dirnames) + list(filenames):
                entries.append((Path(dirpath) / name).relative_to(root).as_posix())
    for rel in entries:
        name = rel.rstrip("/").rsplit("/", 1)[-1]
        index.setdefault(name, []).append(rel)
    return index


def _ref_ext(ref: str) -> str:
    """文件引用的扩展名（小写,不含点）;目录引用/无扩展名返回空串。"""
    if ref.endswith("/"):
        return ""
    name = ref.rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def ref_in_scope(ref: str, root_top: frozenset[str]) -> bool:
    """引用是否是对本仓路径的断言（见模块 docstring 的 scope 判定）。"""
    if ref.startswith(("..", "~")):
        return False
    if ref.startswith("/"):
        m = _ABS_REF_RE.match(ref)
        return bool(m) and m.group(1) in root_top
    if ref.startswith(LOCAL_ONLY_PREFIXES):
        return False
    if ref in IGNORED_REFS:
        return False
    if "/" in ref:
        first = ref.lstrip("./").split("/", 1)[0]
        if first in GENERATED_ROOT_AREAS:
            return False
        return first in root_top or first in RETIRED_ROOT_AREAS
    # 裸文件名:由调用方（同扩展名兄弟）二次判定
    return bool(_ref_ext(ref))


def _tracked_dirs(tracked: frozenset[str]) -> frozenset[str]:
    """tracked 文件的全父目录集(目录引用判定;git ls-files 只出文件)。"""
    dirs: set[str] = set()
    for rel in tracked:
        parts = rel.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    return frozenset(dirs)


def ref_exists(
    ref: str,
    doc_dir_rel: Path,
    root: Path,
    root_top: frozenset[str],
    index: dict[str, list[str]],
    tracked: frozenset[str] | None = None,
    tracked_dirs: frozenset[str] = frozenset(),
) -> bool:
    """两段显式解析 + 全仓后缀匹配;容错文档里常见的省略目录写法。

    tracked 在场时按 git 视图判存在(与 CI 克隆一致):文件∈tracked/目录∈
    tracked_dirs;候选全 miss 但路径被 .gitignore 盖住=运行时工件引用,
    豁免(见 _git_ignored——本机盘上有、克隆里没有的产物)。
    """

    def _hit(rel_posix: str) -> bool:
        cand = rel_posix.rstrip("/")
        if not cand:
            return True
        if tracked is not None:
            if cand in tracked or cand in tracked_dirs:
                return True
            if _git_ignored(root, cand):
                return True  # 运行时工件(本机有/克隆无):合法运行时路径引用
            return False
        return (root / cand).exists()

    if ref.startswith("/"):
        m = _ABS_REF_RE.match(ref)
        if not m or m.group(1) not in root_top:
            return True  # /api/... 路由、/tmp/... 系统路径等:不属仓内,视为外域
        return _hit(ref.lstrip("/"))
    rel = ref[2:] if ref.startswith("./") else ref
    # ① 相对引用文件所在目录
    if doc_dir_rel != Path("."):
        if _hit((doc_dir_rel / rel).as_posix()):
            return True
    # ② 仓库根
    if _hit(rel):
        return True
    # ③ 全仓后缀匹配:`components/x.tsx` 可对上 apps/web/components/x.tsx;
    #    目录省略写法同权:`assets/fillers/` 可对上 apps/agent/agent_runtime/assets/fillers
    clean = rel.rstrip("/")
    if not clean:
        return True
    base = clean.rsplit("/", 1)[-1]
    for candidate in index.get(base, ()):
        if candidate == clean or candidate.endswith("/" + clean):
            return True
    for d in tracked_dirs:
        if d == clean or d.endswith("/" + clean):
            return True
    return False


def check_doc_paths(root: Path | str = ROOT) -> list[dict]:
    """返回断链清单:[{file, line, ref}, ...]（file 为仓库相对 posix 路径）。"""
    root = Path(root).resolve()
    tracked = _git_tracked(root)
    index = build_repo_index(root, tracked)
    root_top = frozenset(p.name for p in root.iterdir())
    tdirs = _tracked_dirs(tracked) if tracked is not None else frozenset()
    broken: list[dict] = []
    seen: set[tuple[str, int, str]] = set()
    for doc in iter_live_docs(root):
        try:
            rel_doc = doc.relative_to(root).as_posix()
        except ValueError:
            continue
        doc_dir_rel = doc.parent.relative_to(root)
        is_yml = doc.suffix in WORKFLOW_EXTS
        if is_yml:
            entries = list(iter_yml_run_lines(doc))
        else:
            try:
                lines = doc.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            entries = list(enumerate(lines, start=1))
        for lineno, text in entries:
            refs = extract_command_refs(text) if is_yml else extract_refs_from_text(text)
            if not refs:
                continue
            resolved = {
                ref: ref_exists(
                    ref, doc_dir_rel, root, root_top, index, tracked, tdirs
                )
                for ref in refs
            }
            for ref in refs:
                if not ref_in_scope(ref, root_top):
                    continue
                if resolved[ref]:
                    continue
                if "/" not in ref:
                    # 裸文件名只在「同扩展名兄弟确实解析到仓内」时才算仓内断言
                    ext = _ref_ext(ref)
                    if not any(
                        other != ref and resolved[other] and _ref_ext(other) == ext
                        for other in refs
                    ):
                        continue
                key = (rel_doc, lineno, ref)
                if key in seen:
                    continue
                seen.add(key)
                broken.append({"file": rel_doc, "line": lineno, "ref": ref})
    return broken


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="活文档仓内路径断链检查（G1a 治理）")
    ap.add_argument("--json", action="store_true", help="输出机读 JSON 数组")
    args = ap.parse_args(argv)
    broken = check_doc_paths(ROOT)
    if args.json:
        print(json.dumps(broken, ensure_ascii=False, indent=2))
    else:
        for item in broken:
            print(f"{item['file']}:{item['line']} → {item['ref']}")
    if broken:
        print(f"[check_doc_paths] {len(broken)} broken path(s)", file=sys.stderr)
        return 1
    print("[check_doc_paths] OK: no broken repo paths in live docs", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
