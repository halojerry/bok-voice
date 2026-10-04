#!/usr/bin/env python
"""paths 域(平台/路径锚:ROOT 根锚、app-data/runtime 目录解析、平台三闸
is_mac/is_linux/is_packaged、模型表键 platform_key、解释器定位 repo_python/
sidecar_python、打包内嵌二进制 bundled_node/bundled_llama/_embedded_livekit、
LiveKit 配置补丁路径 _livekit_config_path、MLX_SERVER_WRAPPER;G2 W② 从 core
搬出,搬运纪律=穿模块对象调用)。

- 本模块**零 bokctl 内部依赖**(stdlib-only:os/sys/platform/pathlib)——ROOT
  是全包的路径根锚,而 core 的域 import 行先于 core.ROOT 定义,域模块 import
  期取不到 core.ROOT。此前的边界判例(models 波:「MLX_SERVER_WRAPPER 是 ROOT
  派生路径常量,因 import 序必须留 core」)随本波解除:ROOT/_BOK_ROOT_ENV/
  MLX_SERVER_WRAPPER 全部落本模块,谁要根锚谁 `from bokctl import paths`。
- 本域自有名件(_BOK_ROOT_ENV/ROOT/is_packaged/is_mac/is_linux/app_data_dir/
  runtime_root/platform_key/sidecar_python/sidecar_venv_python/repo_python/
  _repo_pythonpath/bundled_node/bundled_llama/_embedded_livekit/
  _livekit_config_path/MLX_SERVER_WRAPPER)域内裸名互调(同模块全局=call-time
  可 patch)。
- 留守 core 的近邻(边界记录,2026-10-04;env 波更新):_certifi_bundle/
  _bake_ssl_cert_file **W②-env 波(最后一批)已随 env 组装面搬入 bokctl.env**
  (certifi 束定位是 TLS 凭据面不是路径解析);
  _virtual_audio_present 是 doctor 报告性探测(subprocess 探设备非路径);
  shutil_which/_cuda 是工具探测;_PROVIDER_HEALTH_MODULE 虽是 ROOT 派生常量,
  但与 _provider_health_summary 被 status/doctor 两面吃(health 波边界判例,
  随 provider-health 族留 core,读 paths.ROOT)。
- 测试面:patch 一律走 tests/_bokpatch.py(patch_bok;PATCH_TARGETS 已把
  ROOT/app_data_dir/platform_key/is_mac/is_linux/is_packaged/runtime_root/
  sidecar_python/repo_python/bundled_node/bundled_llama/_embedded_livekit/
  _livekit_config_path 改道 bokctl.paths);facade 读用 bok.paths.X。
  is_mac/is_linux 打桩monkeypatch.setattr(bok.paths._platform,"system",…)不受影响
  ——bok/core/paths 的 `_platform` 是同一个 stdlib platform 模块对象。
"""
from __future__ import annotations

import os
import platform as _platform
import sys
from pathlib import Path

_BOK_ROOT_ENV = os.environ.get("BOK_ROOT", "")
# G2 W①:core.py 比 bok.py 深一层,repo 根=parents[2](paths.py 与 core.py 同层,同锚)
ROOT = Path(_BOK_ROOT_ENV).resolve() if _BOK_ROOT_ENV else Path(__file__).resolve().parents[2]


def is_packaged() -> bool:
    """True when running from the desktop bundle (Tauri resources)."""
    return os.environ.get("BOK_PACKAGED") == "1"


def is_mac() -> bool:
    return _platform.system() == "Darwin"


def is_linux() -> bool:
    """Linux 档判定（Ubuntu 节点形态，2026-09-20）。按真实 OS 判定而非
    `not is_mac() and os.name != "nt"`——Windows 单测以 is_mac=False+os.name
    打桩模拟 Windows，过度宽松的判定会把桩吃掉（test_prod_windows 实证）。"""
    return _platform.system() == "Linux"


def app_data_dir() -> Path:
    """app-data 根（…/BokVoice）：SQLite/vault/logs/units/models 全落这里。

    平台分档（2026-09-20 Ubuntu 节点形态补齐）：nt=LOCALAPPDATA；Darwin=
    ~/Library/Application Support；Linux=XDG_DATA_HOME 或 ~/.local/share——
    旧版非 nt 恒落 mac 路径，Ubuntu 上会把数据写到不存在的 Library 目录树。
    """
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    elif is_mac():
        base = Path(os.environ.get("HOME", ".")) / "Library" / "Application Support"
    else:
        base = Path(
            os.environ.get("XDG_DATA_HOME")
            or str(Path(os.environ.get("HOME", ".")) / ".local" / "share")
        )
    return base / "BokVoice"


def runtime_root() -> Path:
    """Locate the bundled runtime dir (python/node/llama/livekit).

    规范位置 = 仓库根 ``<root>/runtime``（2026-09-17 迁出 desktop/，Tauri 退役
    前置）。仍向上走祖先目录兜底：兼容历史布局与未来打包形态把代码根埋深一层
    的场景（runtime 与代码根同级或在其上方）。
    """
    cur = ROOT
    for _ in range(5):
        cand = cur / "runtime"
        if (
            (cand / "python" / "bin" / "python3").exists()
            or (cand / "python" / "python.exe").exists()
            or (cand / ".venv").exists()
            or (cand / "livekit-server").exists()
            or (cand / "livekit-server.exe").exists()
            or (cand / "llama").exists()
        ):
            return cand
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return ROOT / "runtime"


def platform_key() -> str:
    """模型表键：Darwin=mac（mlx 栈）；其余（Windows/Linux）=windows（llama.cpp
    GGUF + transformers ASR/TTS）。旧版非 nt 恒回 "mac"，Linux 会去下 mlx 模型
    并在 :1235 起 mlx_lm——Ubuntu 节点形态修复（2026-09-20）。"""
    return "mac" if is_mac() else "windows"


def sidecar_python(name: str) -> Path:
    """Bundled runtime python, else the repo venv for that service."""
    if os.name == "nt":
        cands = [
            runtime_root() / "python" / "python.exe",
            ROOT / "services" / name / ".venv" / "Scripts" / "python.exe",
        ]
    else:
        cands = [
            runtime_root() / "python" / "bin" / "python3",
            ROOT / "services" / name / ".venv" / "bin" / "python",
        ]
    for c in cands:
        if c.exists():
            return c
    return cands[-1]


def sidecar_venv_python(name: str) -> Path:
    if os.name == "nt":
        return ROOT / "services" / name / ".venv" / "Scripts" / "python.exe"
    return ROOT / "services" / name / ".venv" / "bin" / "python"


def repo_python() -> Path:
    """Pick a Python interpreter that can import control_plane + obs packages."""
    if os.name == "nt":
        candidates = [
            runtime_root() / "python" / "python.exe",
            ROOT / ".venv312" / "Scripts" / "python.exe",
            ROOT / ".venv" / "Scripts" / "python.exe",
            Path(sys.executable),
        ]
    else:
        candidates = [
            runtime_root() / "python" / "bin" / "python3",
            ROOT / ".venv312" / "bin" / "python",
            ROOT / ".venv" / "bin" / "python",
            Path(sys.executable),
        ]
    for py in candidates:
        if py.exists():
            return py
    return candidates[-1]


def bundled_node() -> str | None:
    """Bundled Node binary (externalBin: Resources or Contents/MacOS; runtime dir)."""
    res = os.environ.get("BOK_RESOURCE_DIR", "")
    if os.name == "nt":
        cands = [
            Path(res) / "node.exe" if res else None,
            runtime_root() / "node" / "node.exe",
            runtime_root() / "node.exe",
        ]
    else:
        macos = Path(res).parent / "MacOS" / "node" if res else None
        cands = [
            Path(res) / "node" if res else None,
            macos,
            runtime_root() / "node" / "bin" / "node",
            runtime_root() / "bin" / "node",
        ]
    for c in cands:
        if c and c.exists():
            return str(c)
    return None


def bundled_llama() -> Path | None:
    """打包内嵌 llama-server：Windows=llama-server.exe；Linux=llama-server
    （2026-09-20 Ubuntu 节点：runtime/llama/ 或 runtime/llama/linux/ 放置；
    找不到时 _start_llm 回退 PATH 的 llama-server）。"""
    if os.name == "nt":
        for c in (runtime_root() / "llama" / "llama-server.exe", runtime_root() / "llama-server.exe"):
            if c.exists():
                return c
        return None
    if is_mac():
        return None
    for c in (
        runtime_root() / "llama" / "llama-server",
        runtime_root() / "llama" / "linux" / "llama-server",
        runtime_root() / "llama-server",
    ):
        if c.exists():
            return c
    return None


def _embedded_livekit() -> Path | None:
    """Embedded LiveKit server binary (externalBin Resources/MacOS or runtime)."""
    res = os.environ.get("BOK_RESOURCE_DIR", "")
    if os.name == "nt":
        cands = [Path(res) / "livekit-server.exe" if res else None, runtime_root() / "livekit-server.exe"]
    else:
        macos = Path(res).parent / "MacOS" / "livekit-server" if res else None
        cands = [Path(res) / "livekit-server" if res else None, macos, runtime_root() / "livekit-server"]
    for c in cands:
        if c and c.exists():
            return c
    return None


def _livekit_config_path() -> Path:
    """LiveKit 生效配置路径（2026-09-20 Ubuntu 节点形态）：

    无 env 覆盖 → 原样返回仓内 services/livekit-server/livekit.yaml（dev 形态
    逐字节零变化）；有覆盖 → 生成补丁副本到 app-data/run/livekit.yaml：
      - `BOK_LIVEKIT_BIND`：bind_addresses（逗号分隔多址）——内网多话务员形态
        填本机内网 IP（默认 127.0.0.1 只有节点本机能连房）；
      - `BOK_LIVEKIT_WEBHOOK_URL`：webhook urls[0]——节点形态指向**云 CP**
        （仓内默认 http://127.0.0.1:8000/api/webhook/livekit 在节点上指向不存
        在的本地 CP，崩溃补位重派会断）；
      - `LIVEKIT_API_KEY/SECRET`：keys 段——生产键与 CP 签发 token 用的 env
        同源（旧版 keys 恒为 devkey/devsecret 而 CP 读 env，分布式部署必错配）。
    打补丁用行级替换（不引 yaml 依赖；仓内文件结构由本模块测试钉住）。
    """
    base = ROOT / "services" / "livekit-server" / "livekit.yaml"
    bind = (os.environ.get("BOK_LIVEKIT_BIND") or "").strip()
    webhook = (os.environ.get("BOK_LIVEKIT_WEBHOOK_URL") or "").strip()
    key = (os.environ.get("LIVEKIT_API_KEY") or "").strip()
    secret = (os.environ.get("LIVEKIT_API_SECRET") or "").strip()
    if not bind and not webhook and not (key and secret):
        return base
    lines = base.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("bind_addresses:") and bind:
            out.append("bind_addresses:")
            for addr in [a.strip() for a in bind.split(",") if a.strip()]:
                out.append(f"  - {addr}")
            i += 1
            while i < len(lines) and lines[i].startswith("  - "):
                i += 1
            continue
        if stripped == "urls:" and webhook:
            out.append(line)
            out.append(f"    - {webhook}")
            i += 1
            while i < len(lines) and lines[i].startswith("    - "):
                i += 1
            continue
        if stripped == "keys:" and key and secret:
            out.append(line)
            out.append(f"  {key}: {secret}")
            i += 1
            while i < len(lines) and lines[i].startswith("  ") and ":" in lines[i]:
                i += 1
            continue
        out.append(line)
        i += 1
    target = app_data_dir() / "run" / "livekit.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(out) + "\n", encoding="utf-8")
    return target


def _repo_pythonpath() -> str:
    parts = [
        ROOT / "packages" / "core",
        ROOT / "packages" / "business-db",
        ROOT / "packages" / "knowledge",
        ROOT / "packages" / "observability",
        ROOT / "apps" / "control-plane",
        ROOT / "apps" / "agent",
    ]
    return os.pathsep.join(str(p) for p in parts)


# mlx_lm server 入口 wrapper（2026-10-01 W-ABORT）：`from mlx_lm import server`
# 后做按请求身份的生成中止 patch（POST /v1/abort），argv 原样透传。三处 mlx
# 启动点（:1235/:1239 主 LLM、:1236 MT、:1237 settle/9B）统一走它；
# BOK_MLX_ABORT=0 时 wrapper 零 patch=逐字节旧行为。客户端 req_id 由 agent
# worker 侧 livekit_plugins.MlxLlmLLM 注入（X-Bok-Req-Id）。
MLX_SERVER_WRAPPER = ROOT / "services" / "llm-mlx" / "bok_mlx_server.py"
