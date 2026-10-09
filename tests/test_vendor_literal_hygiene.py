"""W④（2026-10-09）web 客户面厂商字面量卫生门禁。

契约：客户构建（npm run build=customer 换桩档）的源码面零厂商字面量
（minimax/deepseek/doubao/豆包/火山/volcano/bytedance/qwen——客户不得从
UI/请求体/抓包面得知我们用哪家模型）。注释不进 bundle（minify 剥离），
扫描前先剥注释；平台换装面（settings/disaster 真身 + 其专属 lib/组件）
allowlist——build-variants.mjs 在客户构建时以桩页替换，源不进客户 bundle
（真身无入口=DCE 不达）。CI 另有 customer-out/ 产物级 grep 双保险。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB = REPO_ROOT / "apps" / "web"

# 平台换装面（build-variants.mjs 客户构建换桩；真身只进 platform-out）。
_SWAP_ALLOWLIST = {
    "app/(app)/settings/page.tsx",
    "app/(app)/disaster/page.tsx",
    "lib/settings-meta.ts",
    "components/settings-model-routing.tsx",
}

VENDOR_LITERALS = ("minimax", "deepseek", "doubao", "豆包", "火山", "volcano", "bytedance", "qwen")

# 剥注释（块注释 + 行注释；字符串里的 "//" 误伤可接受——本测试只求保守）。
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)


def _strip_comments(src: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", src))


def test_web_customer_source_zero_vendor_literals():
    tracked = subprocess.run(
        ["git", "ls-files", "--", "apps/web"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    offenders: list[str] = []
    for rel in tracked:
        if not rel.endswith((".ts", ".tsx")):
            continue
        rel_web = str((REPO_ROOT / rel).relative_to(WEB))
        if rel_web in _SWAP_ALLOWLIST:
            continue
        if rel_web.startswith(("node_modules/", "customer-out/", "platform-out/", "out/")):
            continue
        body = _strip_comments((REPO_ROOT / rel).read_text(encoding="utf-8")).lower()
        hits = [v for v in VENDOR_LITERALS if v in body]
        if hits:
            offenders.append(f"{rel} → {hits}")
    assert not offenders, (
        "客户面源码出现厂商字面量（SaaS 姿态=客户不可知厂商；平台面请移入"
        " platform-stubs 换装面或 platform-console，或改中性词）:\n" + "\n".join(offenders)
    )


def test_swap_allowlist_files_still_exist():
    """allowlist 防漂移：换装面文件必须真实存在（改名/删除要同步表）。"""
    for rel in _SWAP_ALLOWLIST:
        assert (WEB / rel).exists(), f"allowlist 文件缺失（已改名？同步本表）: {rel}"


def test_platform_stubs_clean_and_present():
    """桩页在位且自身零厂商字面量（客户构建的换装源）。"""
    for stub in ("platform-stubs/settings-page.tsx", "platform-stubs/disaster-page.tsx"):
        p = WEB / stub
        assert p.exists(), f"桩页缺失: {stub}"
        body = _strip_comments(p.read_text(encoding="utf-8")).lower()
        assert not [v for v in VENDOR_LITERALS if v in body], stub


def test_audition_dir_neutral():
    """试听物化目录名中性（URL 面零厂商词）；旧目录不得复活。"""
    assert (WEB / "public" / "voice-auditions").exists()
    assert not (WEB / "public" / "minimax-auditions").exists()
