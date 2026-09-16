"""瘦客户端静态探针：钉死「节点托管 UI 的 CP 地址必须在运行时可注入」。

背景:
  2026-09-16 站点交付 Task 3。node-agent 托管的 operator UI 与桌面包同源
  （apps/web 静态导出），靠 tools/node_agent.py write_ui_config 覆写
  runtime-config.js 注入 cpUrl/livekitUrl（layout.tsx 的 <script src="/runtime-config.js">
  先于业务脚本加载，apiBase() 运行时求值）。瘦客户端铁律 = 产物里绝不烤死
  localhost——本探针把 verify_bundle.sh --app 的「无 localhost:8000 烘焙」grep 门
  从打包 app 形态扩到节点打包形态，并连同注入链三件（index.html 引用、stub
  形态、注入模拟）一起钉成可跑的红绿基线。纯文件系统检查（无 HTTP——
  静态文件语义 filesystem 即可完全表达，fetch 不增益）。

用法:
  python scripts/probe_thin_client_static.py [--web-root apps/web]
  在仓库根执行即可；out/ 缺建时 exit 2 并提示构建命令。

env:
  探针自身无专用 env。

前置:
  apps/web/out 存在（cd apps/web && npm run build 先行）；缺建 exit 2
  （环境缺失，不算探针失败）。

步骤（每步打 PASS/FAIL，任一 FAIL → exit 1）:
  ①  out/index.html 引用 /runtime-config.js（注入链的加载前提）
  ②  out/runtime-config.js 存在且为可注入 stub 形态：含 __BOK_CONFIG__ 标记、
      未硬设非空 cpUrl（硬设即 stub 失去「可被覆写」语义）
  ③  临时副本上模拟 node_agent 注入（write_ui_config 同格式覆写）→
      副本含标记且 cpUrl/livekitUrl 可解析回读
  ④  out/ 全量递归文本扫描：无烤死 http://localhost:8000（verify_bundle 规则扩展）

纯 stdlib（pathlib/re/json/tempfile/argparse）零三方依赖。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BAKED_LITERAL = "http://localhost:8000"
MARKER = "__BOK_CONFIG__"
# 硬设非空 cpUrl（单双引号都算）——stub 形态绝不携带真地址。
_HARDCODED_CP_RE = re.compile(r"""["']cpUrl["']\s*:\s*["'][^"']+["']""")
# 注入格式回读：write_ui_config 用 json.dumps（恒双引号）。
_INJECTED_CP_RE = re.compile(r'"cpUrl"\s*:\s*"([^"]+)"')
_INJECTED_LK_RE = re.compile(r'"livekitUrl"\s*:\s*"([^"]+)"')

_RESULTS: list[tuple[bool, str]] = []


def _record(ok: bool, label: str, detail: str = "") -> None:
    line = f"[{'PASS' if ok else 'FAIL'}] {label}"
    if detail:
        line += f" —— {detail}"
    print(line, flush=True)
    _RESULTS.append((ok, label))


def _read_text(path: Path) -> str | None:
    """文本读取；含 NUL 或非 UTF-8 字节视为二进制返回 None（扫描跳过）。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bok 瘦客户端静态导出探针")
    parser.add_argument(
        "--web-root", default=str(REPO / "apps" / "web"),
        help="web 应用根目录（默认仓库内 apps/web）")
    args = parser.parse_args(argv)
    web_root = Path(args.web_root).resolve()
    out_dir = web_root / "out"

    # 预检：out 缺建是环境缺失（exit 2），不算探针失败。
    if not out_dir.is_dir():
        print(
            f"THIN-CLIENT PROBE：静态导出缺建（{out_dir}）——先 cd apps/web && npm run build",
            flush=True,
        )
        return 2

    # ① index.html 必须引用 /runtime-config.js（layout.tsx 的注入链加载前提；
    #    静态导出的 <script src> 会原样出现在 HTML 里）。
    index_html = out_dir / "index.html"
    index_text = _read_text(index_html) if index_html.is_file() else None
    refs = bool(index_text) and '"/runtime-config.js"' in index_text
    _record(
        refs,
        "① out/index.html 引用 /runtime-config.js",
        "" if refs else f"{index_html} 缺 script 引用（注入链断裂）",
    )

    # ② 注入点本体：out/runtime-config.js 存在且是 stub 形态（含标记、未硬设
    #    非空 cpUrl）。硬设即节点覆写被回退值遮蔽/暴露构建机地址，双输。
    stub = out_dir / "runtime-config.js"
    stub_text = _read_text(stub) if stub.is_file() else None
    stub_ok = (
        stub_text is not None
        and MARKER in stub_text
        and not _HARDCODED_CP_RE.search(stub_text)
    )
    if stub_text is None:
        detail = f"{stub} 缺失或非文本——public/runtime-config.js 未随静态导出"
    elif not (MARKER in stub_text):
        detail = "缺 __BOK_CONFIG__ 标记"
    elif _HARDCODED_CP_RE.search(stub_text):
        detail = "stub 硬设了非空 cpUrl（失去可注入语义）"
    else:
        detail = "stub 形态正确（含标记、无硬设 cpUrl）"
    _record(stub_ok, "② out/runtime-config.js 为可注入 stub 形态", detail)

    # ③ 注入模拟：临时副本上按 node_agent write_ui_config 的逐字节同格式覆写，
    #    再回读解析——钉住「写进去的地址读得回来」的往返契约（stdlib 无 JS
    #    引擎，以格式正则回读代执行，格式与 write_ui_config 同源锚定）。
    cp_url, lk_url = "http://192.168.8.9:8000", "ws://192.168.8.9:7880"
    inject_ok = False
    detail = ""
    with tempfile.TemporaryDirectory(prefix="bok-thinclient-") as td:
        copy = Path(td) / "runtime-config.js"
        if stub_text is not None:
            copy.write_text(stub_text, encoding="utf-8")  # 先落 stub 原样
        # 与 tools/node_agent.py write_ui_config 同构（window.__BOK_CONFIG__ = <json>;）。
        copy.write_text(
            "window." + MARKER + " = "
            + json.dumps({"cpUrl": cp_url, "livekitUrl": lk_url})
            + ";\n",
            encoding="utf-8",
        )
        injected = _read_text(copy)
        cp_hit = _INJECTED_CP_RE.search(injected) if injected else None
        lk_hit = _INJECTED_LK_RE.search(injected) if injected else None
        inject_ok = bool(
            injected and MARKER in injected
            and cp_hit and cp_hit.group(1) == cp_url
            and lk_hit and lk_hit.group(1) == lk_url
        )
        if not inject_ok:
            detail = f"副本={injected!r}（期望 cpUrl={cp_url} livekitUrl={lk_url}）"
    _record(
        inject_ok,
        "③ 注入模拟：node_agent 格式覆写副本可回读 cpUrl/livekitUrl",
        detail,
    )

    # ④ 全量烘焙扫描：out/ 递归文本文件（含 NUL/非 UTF-8 当二进制跳过）任一处
    #    出现 http://localhost:8000 即 FAIL——localhost 优先解析 ::1，节点/客户端
    #    fetch 恒 TypeError（verify_bundle --app 同规则，扩展到全文本文件）。
    scanned, baked = 0, []
    for p in sorted(out_dir.rglob("*")):
        if not p.is_file():
            continue
        text = _read_text(p)
        if text is None:
            continue
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            if BAKED_LITERAL in line:
                baked.append(f"{p.relative_to(out_dir)}:{lineno}")
    _record(
        not baked,
        "④ out/ 无烤死 http://localhost:8000",
        f"扫描文本文件 {scanned} 个" + ("" if not baked else f"；命中 {len(baked)} 处 -> {baked[:10]}"),
    )

    failed = [label for ok, label in _RESULTS if not ok]
    print(flush=True)
    if failed:
        print(
            f"THIN-CLIENT PROBE FAIL：{len(failed)}/{len(_RESULTS)} 步失败 -> {'; '.join(failed)}",
            flush=True,
        )
        return 1
    print(f"THIN-CLIENT PROBE PASS：{len(_RESULTS)}/{len(_RESULTS)} 步全过", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
