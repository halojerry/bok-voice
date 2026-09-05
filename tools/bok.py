#!/usr/bin/env python
"""bokctl — cross-platform (no-Docker) launcher for the Bok voice stack.

Subcommands:
  catalog    List per-platform models + sizes.
  download   Download missing models into the app-data dir (resume + progress).
  status     Summarize service health + model readiness.
  up         Ensure models + runtimes, then start ASR/TTS/LLM/B-line.
  serve      Full desktop stack: control-plane + LiveKit + up + agent worker.
  down       Stop services started by bokctl (pidfiles).
  doctor     Preflight diagnostics (structure/deps/hardware; strict when packaged).

Platform split (MLX is Apple-only):
  mac -> MLX sidecars + mlx_lm server on :1235 + optional MT server on :1236
  win -> transformers sidecars (CUDA torch) + llama.cpp CUDA server on :1235
Zero-Ollama: there is no Ollama anywhere in the distribution path.
"""
from __future__ import annotations

import argparse
import json
import os
import platform as _platform
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


_BOK_ROOT_ENV = os.environ.get("BOK_ROOT", "")
ROOT = Path(_BOK_ROOT_ENV).resolve() if _BOK_ROOT_ENV else Path(__file__).resolve().parents[1]


def is_packaged() -> bool:
    """True when running from the desktop bundle (Tauri resources)."""
    return os.environ.get("BOK_PACKAGED") == "1"


def is_mac() -> bool:
    return _platform.system() == "Darwin"


def app_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path(os.environ.get("HOME", ".")) / "Library" / "Application Support"
    return base / "BokVoice"


def runtime_root() -> Path:
    """Locate the bundled runtime dir (python/node/llama/livekit).

    Tauri v2 collapses `../` resource paths into `_up_` directories. `tools/`,
    `services/`, `packages/` live at ``<res>/_up_/_up_/`` (2 levels of `..`),
    while `runtime/` sits at ``<res>/_up_/runtime`` (1 level of `..`). So the
    runtime is one level above the code root; search ROOT and its ancestors.
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


MODELS: dict[str, dict[str, str]] = {
    "mac": {
        "asr": "aufklarer/Qwen3-ASR-1.7B-MLX-8bit",
        "tts_preset": "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit",
        "tts_clone": "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit",
        # 客服 LLM 用 4B 关思考:话术化场景速度优先(一轮 ~1s,约为 9B 一半),
        # 港式粤语/夹英文/数字读法实测达标。更重任务(蒸馏/知识分析)另走大模型。
        "llm": "avan-ag/Qwen3.5-4B-Uncensored-MLX-4bit",
        # B 线同传专用翻译小模型(Hy-MT2,逐句无状态 MT):与主 LLM 分进程分端口,
        # prefill 互不挤占。可选(首启向导不门禁,缺失时 B 线回退主 LLM :1235)。
        "mt": "mlx-community/Hy-MT2-1.8B-Abliterated-8bit",
    },
    "windows": {
        "asr": "Qwen/Qwen3-ASR-1.7B",
        "tts_preset": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "tts_clone": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        # GGUF for llama.cpp; only the Q4_K_M file is downloaded (see patterns).
        "llm": "lukey03/Qwen3.5-9B-abliterated-GGUF",
    },
}

# Only pull the Q4_K_M GGUF (the repo also carries F16/vision variants).
WINDOWS_LLM_GGUF_PATTERNS = ["*Q4_K_M.gguf", "README.md"]

# 首启向导不门禁的模型(可选增强,缺失时对应功能自动回退:B 线 MT 回退主 LLM :1235)。
OPTIONAL_MODELS = {"mt"}


def platform_key() -> str:
    return "windows" if os.name == "nt" else "mac"


def model_dir(repo_id: str) -> Path:
    return app_data_dir() / "models" / repo_id.replace("/", "--")


def _lmstudio_models_dir() -> Path:
    return Path(os.environ.get("LMSTUDIO_MODELS_DIR", str(Path.home() / ".lmstudio" / "models")))


def model_path(current: dict[str, str], name: str) -> str:
    """Resolve a model to a path the running backend accepts.

    packaged -> app-data/models/<repo-with--->
    mac dev   -> ~/.lmstudio/models/<repo>  (LM Studio layout)
    win dev   -> repo id (transformers/hf cache)
    """
    repo = current.get(name, "")
    if not repo:
        return ""
    if is_packaged():
        return str(model_dir(repo))
    if is_mac():
        return str(_lmstudio_models_dir() / repo)
    return repo


def _settings_llm_local_model() -> str:
    """读设置页存的本地 LLM 模型(settings.llm.local_model);失败/空回退 ""。

    bok serve 决定 :1235 起哪个模型时优先用用户选定的模型;控制面/agent 注入的
    MLX_LLM_MODEL 与 Summarizer 用同一本机模型,必须保持一致。DB 不存在/损坏
    时静默回退默认,不让启动器因设置问题崩。
    """
    try:
        import sqlite3

        db_path = app_data_dir() / "bok_voice.db"
        if not db_path.exists():
            return ""
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute(
                "SELECT llm_json FROM global_settings WHERE id='global'"
            ).fetchone()
            if not row or not row[0]:
                return ""
            return str((json.loads(row[0]) or {}).get("local_model") or "").strip()
        finally:
            con.close()
    except Exception:
        return ""


def resolve_llm_repo(current: dict[str, str]) -> str:
    """选定要启动/注入的本地 LLM repo:设置页 local_model 优先,空用默认 current."""
    override = _settings_llm_local_model()
    return override or (current.get("llm") or "")


def _mt_llm_model(current: dict[str, str]) -> str:
    """B 线 MT 翻译小模型路径:MT_LLM_MODEL 显式覆盖 > MODELS 表 mt 条目;无则 ""。

    与主 LLM 的解析同一套路(路径须是后端真认的模型路径);解析不出(未下载/
    表里无 mt 条目)返回 "",启动侧跳过 :1236、interp env 侧不下发 MT_*
    (interpret 侧回退主 LLM :1235,唔会指去死端口)。
    """
    override = os.environ.get("MT_LLM_MODEL", "").strip()
    if override:
        return override
    return model_path(current, "mt")


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


def bundled_node_modules() -> Path | None:
    for c in (runtime_root() / "bline-node_modules", ROOT / "services" / "realtime-translation" / "node_modules"):
        if c.exists():
            return c
    return None


def node() -> str:
    return bundled_node() or "node"


def node_env() -> dict[str, str]:
    mods = bundled_node_modules()
    if mods:
        return {"NODE_PATH": str(mods)}
    return {}


def bundled_llama() -> Path | None:
    """Windows llama-server binary (CUDA build + cudart DLLs beside it)."""
    if os.name != "nt":
        return None
    for c in (runtime_root() / "llama" / "llama-server.exe", runtime_root() / "llama-server.exe"):
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


def healthy(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


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


def shutil_which(name: str):
    try:
        import shutil

        return shutil.which(name)
    except Exception:
        return None


def _cuda() -> bool:
    try:
        import torch

        return torch.cuda.is_available()
    except Exception:
        return False


def cmd_catalog() -> int:
    key = platform_key()
    print(f"platform: {key}")
    for name, repo in MODELS[key].items():
        if repo:
            print(f"  {name:<12} {repo}")
    print(f"  ~download into {app_data_dir() / 'models'}")
    return 0


def cmd_manifest() -> int:
    """Emit a JSON manifest for the desktop shell / CI release pipeline."""
    key = platform_key()
    data: dict = {
        "platform": key,
        "app_data_dir": str(app_data_dir()),
        "ports": {"control_plane": 8000, "web": 3000, "asr": 8787, "tts": 8788, "llm": 1235, "mt_llm": 1236, "b_line": 8790, "livekit": 7880},
        "models": {},
    }
    for name, repo in MODELS[key].items():
        if not repo:
            continue
        entry: dict = {"repo": repo}
        target = model_dir(repo)
        if target.exists():
            sizes = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
            entry["size_bytes"] = sizes
            entry["sha256"] = _dir_sha256(target)
        data["models"][name] = entry
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def setup_models() -> list[dict]:
    """Return per-model download status for the first-run wizard."""
    key = platform_key()
    out: list[dict] = []
    for name, repo in MODELS[key].items():
        if not repo:
            out.append({"name": name, "repo": "", "present": True, "required": False})
            continue
        target = model_dir(repo)
        present = target.exists() and any(target.iterdir())
        entry: dict = {"name": name, "repo": repo, "present": present, "required": name not in OPTIONAL_MODELS}
        if present:
            entry["size_bytes"] = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
        out.append(entry)
    return out


def _all_models_present() -> bool:
    return all(m["present"] for m in setup_models() if m["required"])


def cmd_setup(action: str = "status") -> int:
    if action == "download":
        cmd_download()
        print(json.dumps({"ready": _all_models_present()}, ensure_ascii=False))
        return 0
    data = {"ready": _all_models_present(), "models": setup_models()}
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def _dir_sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    for p in sorted(path.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(path).as_posix().encode())
            with p.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
    return h.hexdigest()[:16]


def _enable_hf_transfer() -> None:
    """Speed up model downloads with hf_transfer when available."""
    try:
        import hf_transfer  # noqa: F401

        os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")
    except Exception:
        pass


def cmd_download() -> int:
    key = platform_key()
    try:
        from huggingface_hub import snapshot_download
    except Exception as exc:  # pragma: no cover
        print(f"[download] huggingface_hub missing: {exc}", file=sys.stderr)
        return 2
    _enable_hf_transfer()
    for name, repo in MODELS[key].items():
        if not repo:
            continue
        target = model_dir(repo)
        if target.exists() and any(target.iterdir()):
            print(f"  [ok]   {name} present  {target}")
            continue
        print(f"  [down] {name}  {repo}")
        kwargs: dict = {}
        if key == "windows" and name == "llm":
            kwargs["allow_patterns"] = WINDOWS_LLM_GGUF_PATTERNS
        # hf_hub 1.x 自动断点续传，无需显式 resume_download。
        snapshot_download(repo_id=repo, local_dir=str(target), **kwargs)
        print(f"  [ok]   {name} downloaded")
    # hf_transfer 只用于下载加速；下载完成后摘掉，避免泄漏到 sidecar/LLM 进程，
    # 防止模型加载阶段偶发阻塞（观察：TTS 首启卡死与 HF_HUB_ENABLE_HF_TRANSFER 同现）。
    os.environ.pop("HF_HUB_ENABLE_HF_TRANSFER", None)
    return 0


def cmd_status() -> int:
    print(f"app-data: {app_data_dir()}")
    services = [
        ("control-plane", 8000),
        ("web", 3000),
        ("asr", 8787),
        ("tts", 8788),
        ("llm", 1235),
        ("mt-llm", 1236),
        ("b-line", 8790),
        ("livekit", 7880),
    ]
    for name, port in services:
        print(f"  {name:<13} :{port:<6} {'UP' if healthy(port) else 'DOWN'}")
    return 0


def _start_proc(args: list[str], pidfile: Path, logfile: Path, env: dict | None = None, cwd: str | Path | None = None) -> int:
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    merged = dict(os.environ)
    # 子进程日志实时可见（写到文件时 stdout 默认块缓冲，会吞掉关键启动日志）。
    merged.setdefault("PYTHONUNBUFFERED", "1")
    if env:
        merged.update(env)
    with logfile.open("ab") as log:
        proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, env=merged, start_new_session=True, cwd=str(cwd) if cwd else None)
    pidfile.write_text(str(proc.pid))
    return proc.pid


def _stop_pidfile(pidfile: Path) -> None:
    """Terminate the process group recorded in a run/*.pid file, if alive."""
    try:
        pid = int(pidfile.read_text().strip())
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


def _bline_config_path() -> Path:
    return app_data_dir() / "bline.json"


def write_bline_config(current: dict[str, str] | None = None) -> Path:
    """Write a fully-resolved B-line config into app-data (bundle stays read-only)."""
    if current is None:
        current = MODELS["mac"] if is_mac() else MODELS["windows"]
    cfg = {
        "asr": {"provider": "qwen3_asr", "base_url": "http://127.0.0.1:8787", "sample_rate": 16000},
        "translator": {
            "provider": "local_openai",
            "base_url": "http://127.0.0.1:1235/v1",
            # mlx_lm server 要求请求里的 model 是真实模型路径，不能用 "local"。
            "model": model_path({**current, "llm": resolve_llm_repo(current)}, "llm"),
        },
        "tts": {"provider": "qwen3_tts", "base_url": "http://127.0.0.1:8788", "sample_rate": 24000},
        "server": {
            "host": "127.0.0.1",
            "port": 8790,
            "metrics_file": str(app_data_dir() / "translation-metrics.jsonl"),
        },
    }
    p = _bline_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    return p


def _control_plane_env(db: Path | str) -> dict[str, str]:
    """Env for the control-plane child. MUST include LiveKit credentials so
    /api/token issues a real JWT instead of the old sha256 dev fallback."""
    # 结算摘要/蒸馏（Summarizer）用同一本机 MLX：settings 里的 llm 卡片可能是空 base_url /
    # 占位 model="local"，真实地址由这里注入（与 agent worker L667 同源）。
    _cur = MODELS["mac"] if is_mac() else MODELS["windows"]
    llm_model = model_path({**_cur, "llm": resolve_llm_repo(_cur)}, "llm")
    return {
        "PYTHONPATH": _repo_pythonpath(),
        "BOK_SERVICE": "control-plane",
        "DATABASE_URL": f"sqlite:///{Path(db).as_posix()}",
        "VAULT_ROOT": str(app_data_dir() / "vault"),
        "LIVEKIT_URL": os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880"),
        "LIVEKIT_API_KEY": os.environ.get("LIVEKIT_API_KEY", "devkey"),
        "LIVEKIT_API_SECRET": os.environ.get("LIVEKIT_API_SECRET", "devsecret"),
        "MLX_LLM_BASE_URL": os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1"),
        "MLX_LLM_MODEL": llm_model,
    }


def _start_llm(current: dict[str, str], run_dir: Path, log_dir: Path) -> None:
    if healthy(1235):
        return
    llm_model = model_path({**current, "llm": resolve_llm_repo(current)}, "llm")
    if is_mac():
        llm_py = sidecar_python("llm-mlx")
        # prompt-cache-size: 默认 10 槽会被 4-6 路并发会话打穿(每请求插入 system/对话/完成
        # 多条前缀键,LRU 轮换把共享前缀挤掉)。M4 48GB 下 4k 前缀 KV 仅 ~134MB,调大纯赚,
        # 让同人设/话术的跨会话前缀缓存命中(实测同前缀重放 1.67s→0.19s)。
        # 16GB 机型可下调,或用 --prompt-cache-bytes 限制缓存总字节。
        # prompt-cache-bytes 6GB:给 128 槽加总字节上限——长会话(几十轮×8k ctx)
        # 单槽可涨到几十 MB,不封顶会把统一内存吃穿触发 macOS 压缩/交换,TTFT 抖尖。
        # prefill-step-size 1024(官方默认 2048):prefill 分步喂 GPU,步子减半让
        # 并发请求在步间插得上队——单请求 prefill 略慢一点点,换并发 TTFT/打断
        # 响应不吃整步 2048 token 的长块。
        # log-level INFO(旧 WARNING):延迟调试要读 mlx 请求/prompt-cache 命中行
        # (llm.log);dev/mac serve 路径专用,生产 launchd 单元不从这里起 :1235,
        # 可用 BOK_LLM_LOG_LEVEL 回 WARNING。
        llm_log_level = os.environ.get("BOK_LLM_LOG_LEVEL", "INFO")
        _start_proc(
            [str(llm_py), "-m", "mlx_lm", "server",
             "--model", llm_model, "--host", "127.0.0.1", "--port", "1235",
             "--prompt-cache-size", "128",
             "--prompt-cache-bytes", "6GB",
             "--prefill-step-size", "1024",
             "--chat-template-args", '{"enable_thinking":false}', "--log-level", llm_log_level],
            run_dir / "llm.pid",
            log_dir / "llm.log",
        )
        return
    # Windows: llama.cpp CUDA (GPU 必选；无 GPU 由 doctor 门禁阻止).
    llama_bin = bundled_llama() or shutil_which("llama-server")
    if not llama_bin:
        print("[bok] llama-server 不可用（Windows 需 NVIDIA GPU 且打包内嵌 CUDA 版）", file=sys.stderr)
        return
    _start_proc(
        [str(llama_bin),
         "--jinja", "--chat-template-kwargs", '{"enable_thinking":false}',
         "--n-gpu-layers", "all",
         "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
         "--ctx-size", "8192",
         "--host", "127.0.0.1", "--port", "1235",
         "-m", llm_model],
        run_dir / "llm.pid",
        log_dir / "llm.log",
    )


def _start_mt_llm(current: dict[str, str], run_dir: Path, log_dir: Path) -> bool:
    """B 线同传翻译 LLM(:1236,Hy-MT2 小模型):与主 LLM 分进程,prefill 互不挤占。

    可选服务:模型缺失/未下载直接跳过并返回 False(等待方不收 1236,B 线
    interpret 回退主 LLM :1235);端口已健康(serve 重试/生产档拉起)不重复起。
    返回 True = 预期 :1236 会就绪。
    """
    if healthy(1236):
        return True
    mt_model = _mt_llm_model(current)
    if not mt_model or not Path(mt_model).exists():
        print(f"[bok] mt model not present, skip :1236 ({mt_model or 'unset'})", file=sys.stderr)
        return False
    llm_py = sidecar_python("llm-mlx")
    # 逐句无状态 MT:请求前缀只有模板头一条,32 槽 prompt cache 足够;Hy-MT2
    # 自带非思考对话模板,不传 --chat-template-args(主 LLM 的关思考参数不通用)。
    _start_proc(
        [str(llm_py), "-m", "mlx_lm", "server",
         "--model", mt_model, "--host", "127.0.0.1", "--port", "1236",
         "--prompt-cache-size", "32", "--log-level", "WARNING"],
        run_dir / "mt-llm.pid",
        log_dir / "mt-llm.log",
    )
    return True


def cmd_up() -> int:
    run_dir = app_data_dir() / "run"
    log_dir = app_data_dir() / "logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    print("[bok] ensuring models…")
    cmd_download()
    print("[bok] starting services…")

    current = MODELS["mac"] if is_mac() else MODELS["windows"]
    asr_py = sidecar_python("qwen3-asr-sidecar")
    tts_py = sidecar_python("qwen3-tts-sidecar")
    if not asr_py.exists() or not tts_py.exists():
        print("[bok] sidecar pythons missing — run setup (setup-macos.sh / setup-windows.ps1)", file=sys.stderr)
        return 2

    asr_model = model_path(current, "asr")
    tts_preset = model_path(current, "tts_preset")
    tts_clone = model_path(current, "tts_clone")
    asr_backend = "mlx" if is_mac() else "transformers"
    tts_backend = "mlx" if is_mac() else "transformers"

    asr_env = {"QWEN3_ASR_MODEL": asr_model, "QWEN3_ASR_BACKEND": asr_backend}
    if not is_mac():
        # Windows/transformers 后端才需要 device 指定;mac mlx 分支不读该 env(MLX 默认走 Metal)。
        asr_env["QWEN3_ASR_DEVICE"] = "cuda" if _cuda() else "cpu"
    if not healthy(8787):
        _start_proc(
            [str(asr_py), "-m", "uvicorn", "app:app", "--app-dir", "services/qwen3-asr-sidecar",
             "--host", "127.0.0.1", "--port", "8787"],
            run_dir / "asr.pid", log_dir / "asr.log",
            env=asr_env,
        )
    if not healthy(8788):
        # 清残留：serve 重试可能叠加多个卡死的 TTS 进程，先按 pidfile 收掉。
        _stop_pidfile(run_dir / "tts.pid")
        _start_proc(
            [str(tts_py), "-m", "uvicorn", "app:app", "--app-dir", "services/qwen3-tts-sidecar",
             "--host", "127.0.0.1", "--port", "8788"],
            run_dir / "tts.pid", log_dir / "tts.log",
            env={"QWEN3_TTS_PRESET_MODEL": tts_preset, "QWEN3_TTS_CLONE_MODEL": tts_clone,
                 "QWEN3_TTS_BACKEND": tts_backend,
                 # 语音克隆注册数据（voice_registry + 参考音频）落 app-data，bundle 只读/可升级。
                 "QWEN3_TTS_DATA_DIR": str(app_data_dir() / "tts-data"),
                 # 打包模式跳过 warmup：首启偶发卡死在参考音频读取/冷编译，
                 # 跳过只损失首包 1-2s，换取启动不被阻塞（开发模式保留 warmup）。
                 "QWEN3_TTS_WARMUP": "0" if is_packaged() else os.environ.get("QWEN3_TTS_WARMUP", "1")},
        )

    _start_llm(current, run_dir, log_dir)
    want_mt = _start_mt_llm(current, run_dir, log_dir)

    # B-line worker (Node, OpenAI-compatible translator on :1235).
    bline_cfg = write_bline_config(current)
    if not healthy(8790):
        _start_proc(
            [node(), str(ROOT / "services" / "realtime-translation" / "server.mjs")],
            run_dir / "bline.pid", log_dir / "bline.log",
            env={**node_env(), "BOK_BLINE_CONFIG": str(bline_cfg)},
        )

    print("[bok] waiting for services…")
    # mt(:1236)仅在确实拉起时纳入等待;主栈四端口照旧。
    core_ports = (8787, 8788, 8790, 1235)
    targets = core_ports + ((1236,) if want_mt else ())
    mt_ready_suffix = " mt=1236" if want_mt else ""
    for _ in range(180):
        if all(healthy(p) for p in targets):
            print(f"[bok] ready: asr=8787 tts=8788 llm=1235 b-line=8790{mt_ready_suffix}")
            return 0
        time.sleep(1)
    # TTS 首启偶发卡死在 MLX 模型加载/暖机（观察：与 LLM/ASR 同启时概率出现，
    # 单独重启几乎必然成功）。兜底：停掉后单独再拉起一次，再等 120s。
    if not healthy(8788):
        print("[bok] tts not healthy — restarting once (alone)", flush=True)
        _stop_pidfile(run_dir / "tts.pid")
        tts_py = sidecar_python("qwen3-tts-sidecar")
        tts_preset = model_path(MODELS["mac"] if is_mac() else MODELS["windows"], "tts_preset")
        tts_clone = model_path(MODELS["mac"] if is_mac() else MODELS["windows"], "tts_clone")
        tts_backend = "mlx" if is_mac() else "transformers"
        _start_proc(
            [str(tts_py), "-m", "uvicorn", "app:app", "--app-dir", "services/qwen3-tts-sidecar",
             "--host", "127.0.0.1", "--port", "8788"],
            run_dir / "tts.pid", log_dir / "tts.log",
            env={"QWEN3_TTS_PRESET_MODEL": tts_preset, "QWEN3_TTS_CLONE_MODEL": tts_clone,
                 "QWEN3_TTS_BACKEND": tts_backend,
                 "QWEN3_TTS_DATA_DIR": str(app_data_dir() / "tts-data"),
                 "QWEN3_TTS_WARMUP": "0" if is_packaged() else os.environ.get("QWEN3_TTS_WARMUP", "1")},
        )
        for _ in range(120):
            if healthy(8788):
                break
            time.sleep(1)
    if all(healthy(p) for p in targets):
        print(f"[bok] ready (after tts restart): asr=8787 tts=8788 llm=1235 b-line=8790{mt_ready_suffix}")
        return 0
    if want_mt and all(healthy(p) for p in core_ports) and not healthy(1236):
        # MT 是可选增强:主栈齐而独缺 mt 不拖垮整栈(B 线 interpret 回退主 LLM)。
        print("[bok] mt llm :1236 not ready — continue (B-line falls back to :1235)", file=sys.stderr)
        return 0
    print("[bok] timeout waiting for services (see app-data/logs)", file=sys.stderr)
    return 1


def _repo_web_modules() -> Path:
    return ROOT / "apps" / "web" / "node_modules"


def cmd_serve() -> int:
    """Bring up the full no-Docker desktop stack and wait until ready.

    Packaged mode (BOK_PACKAGED=1) serves the UI from the Tauri static bundle,
    so the Next server on :3000 is NOT started. All local services bind
    127.0.0.1. Business data goes to SQLite and the knowledge vault lives in
    app-data (never the read-only bundle).
    """
    run_dir = app_data_dir() / "run"
    log_dir = app_data_dir() / "logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    py = repo_python()
    # Dev 模式用系统 node 起 Next dev（打包模式 BOK_PACKAGED=1 跳过 web:3000）。
    node = bundled_node() or "node"
    # control-plane
    # Dev 与打包统一：业务数据 SQLite 落盘、知识 vault 在 app-data（bundle 只读）。
    db = (app_data_dir() / "bok_voice.db").as_posix()
    cp_env: dict[str, str] = _control_plane_env(db)
    if not healthy(8000):
        _start_proc(
            [str(py), "-m", "uvicorn", "control_plane.main:app", "--host", "127.0.0.1", "--port", "8000"],
            run_dir / "control-plane.pid",
            log_dir / "control-plane.log",
            env=cp_env,
        )
    # Dev mode: Next dev server on :3000 (packaged serves static UI from Tauri).
    # next.config.mjs 是 output:"export"，`next start` 无法服务 export 产物，
    # 必须用 `next dev`（export 只在 build 阶段生效）。
    if not is_packaged() and not healthy(3000):
        _start_proc(
            [str(node), str(_repo_web_modules() / "next" / "dist" / "bin" / "next"), "dev", "-H", "127.0.0.1", "-p", "3000"],
            run_dir / "web.pid",
            log_dir / "web.log",
            env={"NEXT_PUBLIC_CONTROL_PLANE_URL": os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")},
            cwd=str(ROOT / "apps" / "web"),
        )

    # LiveKit should come up before the agent worker.
    livekit_bin = _embedded_livekit() or shutil_which("livekit-server") or (ROOT / "services" / "livekit-server" / "livekit-server")
    if not healthy(7880) and livekit_bin and Path(livekit_bin).exists():
        _start_proc(
            [str(livekit_bin), "--config", str(ROOT / "services" / "livekit-server" / "livekit.yaml")],
            run_dir / "livekit.pid",
            log_dir / "livekit.log",
        )

    rc = cmd_up()
    if rc:
        return rc

    # Agent worker registers to the embedded/local LiveKit server.
    if healthy(7880):
        _cur = MODELS["mac"] if is_mac() else MODELS["windows"]
        agent_env: dict[str, str] = {
            "PYTHONPATH": _repo_pythonpath(),
            "BOK_SERVICE": "agent",
            "LIVEKIT_URL": os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880"),
            "LIVEKIT_API_KEY": os.environ.get("LIVEKIT_API_KEY", "devkey"),
            "LIVEKIT_API_SECRET": os.environ.get("LIVEKIT_API_SECRET", "devsecret"),
            "CONTROL_PLANE_URL": os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000"),
            "MLX_LLM_BASE_URL": os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1"),
            "MLX_LLM_MODEL": model_path({**_cur, "llm": resolve_llm_repo(_cur)}, "llm"),
        }
        _start_proc([str(py), "-m", "agent_runtime.main"], run_dir / "agent.pid", log_dir / "agent.log", env=agent_env)

        # B 线同传 interpreter:每个方向一个 worker(agent_name 显式分发,
        # 方向/语言对由 CP 在 me 端 token 的 RoomAgentDispatch metadata 下发)。
        # worker 常驻待命,没有同传房间时零占用(不加载模型,job 到达才拉管线)。
        for _dir, _pidname, _logname in (
            ("fwd", "interp-fwd.pid", "interp-fwd.log"),
            ("rev", "interp-rev.pid", "interp-rev.log"),
        ):
            interp_env = _interp_env(agent_env)
            interp_env["BOK_SERVICE"] = f"interp-{_dir}"
            interp_env["INTERP_DIRECTION"] = _dir
            _start_proc(
                [str(py), "-m", "agent_runtime.interpret"],
                run_dir / _pidname,
                log_dir / _logname,
                env=interp_env,
            )

    print("[bok] waiting for desktop stack…")
    targets = [8000, 8787, 8788, 8790, 1235]
    if healthy(1236):
        # MT 翻译小模型(:1236)可选:cmd_up 拉起了才纳入等待,缺模型不算失败。
        targets.append(1236)
    if not is_packaged():
        targets.append(3000)
    if healthy(7880):
        targets.append(7880)
    for _ in range(120):
        if all(healthy(p) for p in targets):
            ready = "[bok] desktop ready: control-plane=8000 asr=8787 tts=8788 llm=1235 b-line=8790"
            if 1236 in targets:
                ready += " mt=1236"
            print(ready)
            # 非打包模式自动打开浏览器页面(可用 BOK_NO_OPEN_BROWSER=1 关闭)。
            if not is_packaged() and os.environ.get("BOK_NO_OPEN_BROWSER", "0") != "1":
                try:
                    import webbrowser
                    webbrowser.open("http://127.0.0.1:3000")
                except Exception:  # pragma: no cover - 打开浏览器失败不影响启动
                    pass
            return 0
        time.sleep(1)
    print("[bok] timeout waiting for desktop stack (see app-data/logs)", file=sys.stderr)
    return 1


def cmd_down() -> int:
    run_dir = app_data_dir() / "run"
    for pidfile in run_dir.glob("*.pid"):
        try:
            pid = int(pidfile.read_text().strip())
            # _start_proc 以 start_new_session=True 启动（会话组长）；按进程组
            # 终止可连 livekit-agents worker 的 multiprocessing 子进程一起清掉，
            # 避免子进程残留占用 8081 导致下次 agent 启动失败。
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                os.kill(pid, signal.SIGTERM)
            print(f"[down] stopped {pidfile.stem} (pid {pid})")
        except Exception:
            continue
    # Legacy dev sidecars managed by old start_sidecars.sh (host pids in data/).
    data_dir = ROOT / "data"
    for pidfile in data_dir.glob("sidecar-*.pid"):
        try:
            pid = int(pidfile.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            print(f"[down] stopped {pidfile.stem} (pid {pid})")
        except Exception:
            continue
    return 0


def _nvidia_gate() -> tuple[bool, str]:
    """Windows LLM requires an NVIDIA GPU: driver >= 550, VRAM >= 8GB."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return False, "NVIDIA GPU 未检测到（nvidia-smi 无输出）"
        line = out.stdout.strip().splitlines()[0]
        driver, mem = [p.strip() for p in line.split(",")]
        driver_major = int(driver.split(".")[0])
        mem_mb = int(float(mem))
        if driver_major < 550:
            return False, f"NVIDIA 驱动 {driver} 过低，需要 >= 550"
        if mem_mb < 8192:
            return False, f"显存 {mem_mb}MB < 8192MB（建议 >= 8GB）"
        return True, f"NVIDIA OK driver={driver} vram={mem_mb}MB"
    except Exception as exc:
        return False, f"nvidia-smi 不可用: {exc}"


def _import_ok(py: Path, module: str) -> bool:
    try:
        r = subprocess.run([str(py), "-c", f"import {module}"], capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def _doctor_minimax_tts(data: Path, fails: list[str]) -> None:
    """MiniMax 音色校验探针：settings tts.provider=minimax 时，要求 api_key 非空
    且至少一个已配音色能在 MiniMax get_voice 下解析。

    - key 缺失 → 静默跳过（凭据永不入码，缺失本身唔算故障——用户可能纯用本地 TTS）。
    - 无任何已配音色 → 记 warning（设置页漏配，通话会 beep）。
    - get_voice 可达且列出了音色、但已配音色全部唔喺列表 → 硬 fail（确定性错配）。
    - 网络不可达/接口异常 → 只提示，唔 fail（doctor 唔依赖外网）。
    """
    db = data / "bok_voice.db"
    if not db.exists():
        print("minimax tts voice: skipped (no settings db)")
        return
    import sqlite3 as _sqlite3

    try:
        conn = _sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT tts_json FROM global_settings WHERE id='global'"
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        print(f"minimax tts voice: skipped (settings db unreadable: {exc})")
        return
    if not row:
        print("minimax tts voice: skipped (settings empty)")
        return
    try:
        tts = json.loads(row[0] or "{}")
    except Exception:
        tts = {}
    provider = str(tts.get("provider") or "").strip().lower()
    if provider not in ("minimax", "minimax_streaming"):
        return  # 非 MiniMax 供应商:静默返回
    api_key = str(tts.get("api_key") or "").strip() or os.environ.get("MINIMAX_API_KEY", "").strip()
    if not api_key:
        # 静默跳过:未配 key 唔係结构故障。
        print("minimax tts voice: skipped (provider=minimax but api_key empty)")
        return
    voices = [str(tts.get(k) or "").strip() for k in ("speaker", "speaker_zh", "speaker_cantonese", "speaker_en")]
    configured = [v for v in voices if v]
    if not configured:
        msg = "minimax tts: provider=minimax 且已配 key,但 speaker_zh/cantonese/en 全空(通话会静音)"
        print(f"minimax tts voice: FAIL ({msg})")
        fails.append(msg)
        return
    region = os.environ.get("MINIMAX_REGION", "cn").strip().lower()
    base = os.environ.get("MINIMAX_BASE_URL", "").strip().rstrip("/")
    if not base:
        base = "https://api.minimax.chat/v1" if region in {"intl", "global", "chat"} else "https://api.minimax.cn/v1"
    root = base.split("/v1/")[0]
    try:
        req = urllib.request.Request(
            f"{root}/v1/get_voice",
            data=json.dumps({"voice_type": "all"}).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read().decode())
    except Exception as exc:
        # 探针够唔到外网(离线 doctor/网络抖动)→ 只提示,唔当 fail。
        print(f"minimax tts voice: warning (probe unreachable: {exc})")
        return
    if str((body.get("base_resp") or {}).get("status_code")) not in ("0", "None"):
        print(f"minimax tts voice: warning (get_voice base_resp={body.get('base_resp')})")
        return
    found: set[str] = set()

    def _collect(node) -> None:
        if isinstance(node, dict):
            vid = node.get("voice_id")
            if isinstance(vid, str):
                found.add(vid)
            for v in node.values():
                _collect(v)
        elif isinstance(node, list):
            for v in node:
                _collect(v)

    _collect(body.get("data") or {})
    if not found:
        print("minimax tts voice: warning (get_voice returned no voices)")
        return
    resolved = [v for v in configured if v in found]
    if resolved:
        print(f"minimax tts voice: ok ({', '.join(resolved)}; {len(found)} voices on account)")
    else:
        msg = f"minimax tts: 已配音色全部唔喺账号音色列表: {configured}"
        print(f"minimax tts voice: FAIL ({msg})")
        fails.append(msg)


def cmd_doctor() -> int:
    """Preflight diagnostics. In packaged mode every check is a hard gate."""
    key = platform_key()
    packaged = is_packaged()
    fails: list[str] = []
    print(f"platform: {_platform.system()} ({key})")
    print(f"packaged: {packaged}")
    print(f"app-data: {app_data_dir()}")

    data = app_data_dir()
    try:
        data.mkdir(parents=True, exist_ok=True)
        probe = data / ".doctor-write"
        probe.write_text("ok")
        probe.unlink()
        print("app-data writable: ok")
    except Exception as exc:
        fails.append(f"app-data 不可写: {exc}")

    py = sidecar_python("qwen3-asr-sidecar")
    print(f"runtime python: {py} {'ok' if py.exists() else 'MISSING'}")
    if not py.exists():
        fails.append(f"runtime python missing: {py}")
    else:
        if is_mac():
            for mod, p in (("mlx_audio", py), ("mlx_lm", sidecar_python("llm-mlx"))):
                ok = _import_ok(p, mod)
                print(f"  import {mod}: {'ok' if ok else 'FAIL'}")
                if packaged and not ok:
                    fails.append(f"import {mod} failed")
        else:
            for mod in ("qwen_asr", "torch"):
                ok = _import_ok(py, mod)
                print(f"  import {mod}: {'ok' if ok else 'FAIL'}")
                if packaged and not ok:
                    fails.append(f"import {mod} failed")

    # 本地语义端点判定(LiveKit turn-detector v1-mini):缺失时端点判定退回纯 VAD,
    # 客户思考停顿更容易被提前截断。粤(cantonese)语无官方校准,阈值回退英文档。
    eot_ok = _import_ok(py, "livekit_local_inference") if py.exists() else False
    print(f"local EOT inference (turn-detector v1-mini): {'ok' if eot_ok else 'MISSING(退回纯 VAD 端点)'}")
    if packaged and not eot_ok:
        fails.append("livekit-local-inference missing (EOT 退回纯 VAD)")

    livekit = _embedded_livekit()
    print(f"livekit-server: {livekit if livekit else 'MISSING'}")
    if packaged and livekit is None:
        fails.append("livekit-server missing")

    node = bundled_node()
    print(f"node: {node if node else 'MISSING'}")
    if packaged and node is None:
        fails.append("node missing")

    if not is_mac():
        llama = bundled_llama()
        print(f"llama-server: {llama if llama else 'MISSING'}")
        if packaged and llama is None:
            fails.append("llama-server missing (Windows 需要 CUDA 版)")
        ok, msg = _nvidia_gate()
        print(f"nvidia gate: {msg}")
        if packaged and not ok:
            fails.append(msg)

    current = MODELS["mac"] if is_mac() else MODELS["windows"]
    for name, repo in current.items():
        if not repo:
            continue
        present = model_dir(repo).exists() and any(model_dir(repo).iterdir())
        print(f"model {name}: {'ok' if present else 'MISSING'} ({repo})")
        if not present:
            # 模型在 CI/首启前允许缺失：由 setup status 门禁管理，不阻塞 bundle 校验。
            print("  (模型权重不随包，首启向导下载；doctor 不将其视为结构失败)")

    for name, port in (("control-plane", 8000), ("asr", 8787), ("tts", 8788), ("llm", 1235), ("mt-llm", 1236), ("b-line", 8790), ("livekit", 7880)):
        print(f"  port {port:<5} ({name}): {'UP' if healthy(port) else 'DOWN'}")

    # /api/token 必须是真 JWT（三段式）；否则 A 线 UI 永远“接通失败”。
    if healthy(8000):
        try:
            body = json.dumps({"account_id": "acc-001", "room_name": "doctor-probe"}).encode()
            req = urllib.request.Request(
                "http://127.0.0.1:8000/api/token",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode())
            tok = str(payload.get("participantToken") or "")
            if tok.count(".") == 2:
                print("token endpoint: ok (real JWT)")
            else:
                msg = "token endpoint 返回的不是三段式 JWT（疑似旧 sha256 兜底）"
                fails.append(msg)
                print(f"token endpoint: FAIL ({msg})")
        except urllib.error.HTTPError as exc:
            msg = f"token endpoint HTTP {exc.code}（LiveKit 凭据缺失或服务异常）"
            fails.append(msg)
            print(f"token endpoint: FAIL ({msg})")
        except Exception as exc:
            msg = f"token endpoint 不可达: {exc}"
            fails.append(msg)
            print(f"token endpoint: FAIL ({msg})")
    else:
        print("token endpoint: skipped (control-plane down)")

    # MiniMax 云 TTS 探针:provider=minimax 时校验 api_key 非空 + 至少一个已配音色
    # 能喺账号音色列表解析(key 缺失静默跳过,凭据永不入码)。
    _doctor_minimax_tts(data, fails)

    if packaged and fails:
        print("\nPACKAGED DOCTOR FAILED:")
        for f in fails:
            print(f"  - {f}")
        return 1
    if fails:
        print("\nwarnings (dev mode, non-blocking):")
        for f in fails:
            print(f"  - {f}")
    print("doctor: OK" if not fails else "doctor: warnings")
    return 0


def _agent_prod_env() -> dict[str, str]:
    """agent/interp worker 生产环境（与 cmd_serve 同源）。"""
    _cur = MODELS["mac"] if is_mac() else MODELS["windows"]
    return {
        "PYTHONPATH": _repo_pythonpath(),
        "BOK_SERVICE": "agent",
        "LIVEKIT_URL": os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880"),
        "LIVEKIT_API_KEY": os.environ.get("LIVEKIT_API_KEY", "devkey"),
        "LIVEKIT_API_SECRET": os.environ.get("LIVEKIT_API_SECRET", "devsecret"),
        "CONTROL_PLANE_URL": os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000"),
        "MLX_LLM_BASE_URL": os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1"),
        "MLX_LLM_MODEL": model_path({**_cur, "llm": resolve_llm_repo(_cur)}, "llm"),
    }


def _interp_env(agent_env: dict[str, str]) -> dict[str, str]:
    """B 线 interpreter 追加 env:MT 翻译小模型(:1236,可选增强)。

    MT_LLM_MODEL 必须是后端真认的模型路径(与主 LLM 同一解析);MT_* 只在
    模型目录在盘或 :1236 已健康(与 _start_mt_llm 同判)时下发——模型缺失/
    Windows 无 mt 条目时完全不注入,interpret 侧按 env 有无回退主 LLM
    :1235/DeepSeek,避免 worker 指去死端口整场无声。MINIMAX_* 不在这里注入
    ——TTS 供应商/音色由 interpret.py 按设置与方向解析。
    """
    env = dict(agent_env)
    mt_model = _mt_llm_model(MODELS["mac"] if is_mac() else MODELS["windows"])
    if (mt_model and Path(mt_model).exists()) or healthy(1236):
        env["MT_LLM_BASE_URL"] = os.environ.get("MT_LLM_BASE_URL", "http://127.0.0.1:1236/v1")
        if mt_model:
            env["MT_LLM_MODEL"] = mt_model
    return env


def cmd_prod_install() -> int:
    """生成生产常驻单元（mac launchd plist / Windows 服务脚本），不启动。

    dev 栈用 `bok.py serve`（前台 + run/*.pid）；生产档把常驻进程交给 OS 守护
    （launchd KeepAlive=崩溃自动拉起），补上桌面形态天然缺的 watchdog。
    首次需配 livekit.yaml 生产键（services/livekit-server/livekit.yaml）。
    """
    agent_env = _agent_prod_env()
    unit_dir = app_data_dir() / "units"
    unit_dir.mkdir(parents=True, exist_ok=True)
    log_dir = app_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    if is_mac():
        livekit_bin = str(_embedded_livekit() or "livekit-server")
        py = repo_python()
        # unit 定义:name → (args, 附加 env)。agent/interp 共用 agent_env。
        units = [
            ("bok-control-plane", [str(py), "-m", "uvicorn", "control_plane.main:app", "--host", "127.0.0.1", "--port", "8000"], _control_plane_env((app_data_dir() / "bok_voice.db").as_posix()), "Bok 控制面 API"),
            ("bok-livekit", [livekit_bin, "--config", str(ROOT / "services" / "livekit-server" / "livekit.yaml")], {}, "LiveKit 信令/媒体"),
            ("bok-agent", [str(py), "-m", "agent_runtime.main"], agent_env, "A 线客服 agent worker"),
            ("bok-interp-fwd", [str(py), "-m", "agent_runtime.interpret"], {**_interp_env(agent_env), "BOK_SERVICE": "interp-fwd", "INTERP_DIRECTION": "fwd"}, "B 线同传 fwd"),
            ("bok-interp-rev", [str(py), "-m", "agent_runtime.interpret"], {**_interp_env(agent_env), "BOK_SERVICE": "interp-rev", "INTERP_DIRECTION": "rev"}, "B 线同传 rev"),
        ]
        for name, args, env, comment in units:
            label = f"com.bokvoice.{name}"
            arg_xml = "\n".join(f"    <string>{a}</string>" for a in args)
            env_xml = "\n".join(f"      <key>{k}</key>\n      <string>{v}</string>" for k, v in sorted(env.items()))
            plist = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0">\n<dict>\n'
                f"  <key>Label</key><string>{label}</string>\n"
                "  <key>ProgramArguments</key>\n  <array>\n" + arg_xml + "\n  </array>\n"
                f"  <key>WorkingDirectory</key><string>{ROOT}</string>\n"
                "  <key>EnvironmentVariables</key>\n  <dict>\n" + env_xml + "\n  </dict>\n"
                "  <key>RunAtLoad</key><true/>\n"
                "  <key>KeepAlive</key><true/>\n"
                f"  <key>StandardOutPath</key><string>{log_dir / (name + '.log')}</string>\n"
                f"  <key>StandardErrorPath</key><string>{log_dir / (name + '.err.log')}</string>\n"
                "</dict>\n</plist>\n"
            )
            out = unit_dir / f"{label}.plist"
            out.write_text(plist)
            print(f"generated {out.relative_to(app_data_dir())}  ({comment})")
    else:
        print("Windows 生产档：请用 NSSM 将以下进程注册为服务（本项目开发期用 `bok.py serve`）：")
        for name, args, _env, comment in []:
            pass
        for name, args in (
            ("bok-control-plane", [str(repo_python()), "-m", "uvicorn", "control_plane.main:app", "--port", "8000"]),
            ("bok-agent", [str(repo_python()), "-m", "agent_runtime.main"]),
        ):
            print(f"  nssm install {name} {args[0]} {' '.join(args[1:])}")
    print(f"\nunits 目录: {unit_dir}")
    print("mac 装载(KeepAlive 自动拉起):  launchctl bootstrap gui/$(id -u) " + str(unit_dir) + "/*.plist")
    print("mac 卸载:                      launchctl bootout gui/$(id -u)/com.bokvoice.bok-control-plane 等")
    print("livekit 生产键/端口见 services/livekit-server/livekit.yaml")
    return 0


def cmd_prod_status() -> int:
    """生产健康面汇总:官方健康端点(livekit GET /、worker GET :8081/worker)+ sidecar /health。"""
    print("bok prod status:")
    checks = [
        ("control-plane", 8000, "/health"),
        ("asr", 8787, "/health"),
        ("tts", 8788, "/health"),
        ("llm", 1235, "/v1/models"),
        ("b-line", 8790, "/health"),
        ("livekit", 7880, "/"),
    ]
    # MT 翻译小模型是可选增强:起了才纳入健康面(缺模型的环境不算 DEGRADED)。
    if healthy(1236):
        checks.append(("mt-llm", 1236, "/v1/models"))
    all_ok = True
    for name, port, path in checks:
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3)
            body = r.read(200).decode()[:200]
            ok = r.status == 200 and "Not Ready" not in body
            print(f"  {name:<13} :{port}  {'ok' if ok else 'NON-200'}")
            all_ok = all_ok and ok
        except Exception as exc:
            print(f"  {name:<13} :{port}  DOWN ({exc})")
            all_ok = False
    try:
        r = urllib.request.urlopen("http://127.0.0.1:8081/worker", timeout=3)
        data = json.loads(r.read().decode())
        print(f"  {'agent worker':<13} ok  agent_name={data.get('agent_name')} active_jobs={data.get('active_jobs')} load={data.get('worker_load')}")
    except Exception as exc:
        print(f"  {'agent worker':<13} DOWN ({exc})")
        all_ok = False
    print("prod: OK" if all_ok else "prod: DEGRADED")
    return 0 if all_ok else 1


def cmd_prod(cmd: str) -> int:
    if cmd == "install":
        return cmd_prod_install()
    return cmd_prod_status()


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="bok", description="Bok voice stack launcher (no Docker)")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("catalog", "manifest", "download", "status", "up", "serve", "down", "doctor"):
        sub.add_parser(name)
    p_prod = sub.add_parser("prod", help="生产常驻单元与健康面")
    p_prod.add_argument("action", nargs="?", default="status", choices=["install", "status"])
    p_setup = sub.add_parser("setup", help="First-run model readiness / download")
    p_setup.add_argument("action", nargs="?", default="status", choices=["status", "download"])
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.cmd == "setup":
        return cmd_setup(args.action)
    if args.cmd == "prod":
        return cmd_prod(args.action)
    return {"catalog": cmd_catalog, "manifest": cmd_manifest, "download": cmd_download, "status": cmd_status,
            "up": cmd_up, "serve": cmd_serve, "down": cmd_down, "doctor": cmd_doctor}[args.cmd]()


if __name__ == "__main__":
    raise SystemExit(main())
