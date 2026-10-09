"""W⑦/W⑧（2026-10-09）红队残余卫生：agent 系统 prompt 零厂商字面量 + web
markdown 渲染面纯净。

1. **prompt 注入边界双钉**：A 线 system prompt 组装件（稳定前缀/语言规则/
   步骤纪律/judge 契约）的字符串常量零厂商词——prompt 注入跨租户不存在，
   但「模型面遮蔽」要求连 prompt 都不泄露我们用哪家（客户可经话术模板诱导
   agent 复述系统指令）。AST 扫字符串常量（注释天然不进）。
2. **markdown 渲染卫生**：转录渲染（Streamdown）禁 raw HTML 链路——断言
   agent-chat-transcript 恒 disallowedElements 含 "a"、web 依赖无 rehype-raw
   （XSS→localStorage token 偷取残余面的结构闸）。
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_LITERALS = ("minimax", "deepseek", "doubao", "豆包", "火山", "volcano", "bytedance", "qwen")


def _string_constants(py: Path, symbols: set[str] | None = None) -> list[str]:
    """AST 提取字符串常量；symbols 给定时只收这些顶层函数/赋值名下的。"""
    tree = ast.parse(py.read_text(encoding="utf-8"))
    out: list[str] = []
    if symbols is None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.append(node.value)
        return out

    def _collect(node: ast.AST) -> None:
        """顶层 + 类体内一层匹配符号（方法/类常量）。"""
        for sub in getattr(node, "body", []):
            hit = None
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and sub.name in symbols:
                hit = sub
            elif isinstance(sub, ast.Assign):
                for t in sub.targets:
                    if isinstance(t, ast.Name) and t.id in symbols:
                        hit = sub
            if hit is None:
                if isinstance(sub, ast.ClassDef):
                    _collect(sub)
                continue
            for inner in ast.walk(hit):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    out.append(inner.value)

    _collect(tree)
    return out


def test_flow_prompt_constants_zero_vendor():
    """flow.py 判据/纪律 prompt 常量零厂商词。"""
    flow = REPO_ROOT / "apps" / "agent" / "agent_runtime" / "flow.py"
    strings = _string_constants(flow)
    hits = []
    for s in strings:
        low = s.lower()
        found = [v for v in VENDOR_LITERALS if v in low]
        if found:
            hits.append(f"{found} ← {s[:80]!r}")
    assert not hits, "flow.py prompt 字符串含厂商字面量:\n" + "\n".join(hits)


def test_prefix_builder_zero_vendor():
    """livekit_plugins 前缀组装（render_instruction_prefix/_zh_rule/_cantonese_rule）
    零厂商词。"""
    plugins = REPO_ROOT / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py"
    strings = _string_constants(
        plugins, {"render_instruction_prefix", "_zh_rule", "_cantonese_rule"}
    )
    assert strings, "前缀组装符号未找到（改名？同步本测试符号表）"
    hits = [f"{[v for v in VENDOR_LITERALS if v in s.lower()]} ← {s[:80]!r}" for s in strings
            if any(v in s.lower() for v in VENDOR_LITERALS)]
    assert not hits, "前缀组装字符串含厂商字面量:\n" + "\n".join(hits)


def test_transcript_renderer_structure_guards():
    """转录渲染闸：Streamdown 恒 disallowedElements 含 "a"；依赖树无 rehype-raw。"""
    web = REPO_ROOT / "apps" / "web"
    transcript = (web / "components" / "agents-ui" / "agent-chat-transcript.tsx").read_text(encoding="utf-8")
    assert "disallowedElements" in transcript, "agent-chat-transcript 丢失 disallowedElements"
    assert '["a"]' in transcript or '"a"' in transcript, "disallowedElements 必须含 a（禁链接注入）"
    pkg = (web / "package.json").read_text(encoding="utf-8")
    assert "rehype-raw" not in pkg, "引入 rehype-raw=raw HTML 渲染（XSS 面），禁止"


def test_web_no_dangerous_html_sink():
    """全 web 树零 dangerouslySetInnerHTML（React 逃逸口；红队评审基线钉死）。"""
    tracked = subprocess.run(
        ["git", "ls-files", "--", "apps/web"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    offenders = []
    for rel in tracked:
        if not rel.endswith(".tsx"):
            continue
        if "dangerouslySetInnerHTML" in (REPO_ROOT / rel).read_text(encoding="utf-8"):
            offenders.append(rel)
    assert not offenders, (
        "出现 dangerouslySetInnerHTML（如确需，进 hygiene allowlist 并评审）:\n"
        + "\n".join(offenders)
    )
