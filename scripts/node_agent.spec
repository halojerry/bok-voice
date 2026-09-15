# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：Bok node-agent 单文件二进制（onefile）。

范围（spec §4.2/§4.3 P0）：注册/心跳上报 + UI 配置注入（runtime-config.js）。
完整节点栈（LiveKit server / agent·interp workers / 模型 sidecar / bok.py
全栈编排）仍走 venv，按设计不进本包 —— 本二进制的受支持模式是
--heartbeat-only（含 --ui-dir 配置注入，两项都在全栈拉起之前完成）。

入口：tools/node_agent.py（main 分支 abb163b）。
产物：dist/node-agent（dist/ 已 gitignore，不入库）。
"""

import os

# SPECPATH = 本 spec 所在目录（scripts/）；仓库根向上一级。
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(ROOT, "tools", "node_agent.py")],
    # node_agent.py 运行时把 tools/ 插进 sys.path（为懒加载 bok），
    # pathex 同步含仓库根 + tools/，保证模块解析口径与源码运行一致。
    pathex=[ROOT, os.path.join(ROOT, "tools")],
    binaries=[],
    datas=[],
    hiddenimports=[
        # node_agent.py 实际 import 面（模块级）全部是 stdlib：
        #   argparse / json / sys / threading / time / urllib.request /
        #   dataclasses / pathlib
        # PyInstaller 静态分析自动覆盖上述模块，无需显式条目。
        # 唯一的非 stdlib import 是 main() 内懒加载的 `import bok`
        # （tools/bok.py，全栈编排器，会连带 torch/hf_transfer/certifi 等
        # 运行时依赖）—— 按「本轮只二进制化 node-agent 本体」的边界，
        # 它在 excludes 中被刻意排除，不进 hiddenimports。
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # tools/bok.py（cmd_up/cmd_down 全栈编排）不在打包范围：
        # 二进制上不带 --heartbeat-only 运行会得到明确的
        # ModuleNotFoundError: bok，而非半残的全栈行为。
        "bok",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="node-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # CLI 守护进程：心跳打点 / REFUSE_JOBS 日志走 stdout，必须保留控制台。
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    # 本轮产物不带签名（升级验签是后续轮，见 docs/NODE_PACKAGING.md）。
    codesign_identity=None,
    entitlements_file=None,
)
