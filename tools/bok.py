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
  mac -> MLX sidecars + mlx_lm server on :1235
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
        "llm": "huihui-ai/Huihui-Qwen3.5-9B-abliterated-mlx-4bit",
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
        "ports": {"control_plane": 8000, "web": 3000, "asr": 8787, "tts": 8788, "llm": 1235, "b_line": 8790, "livekit": 7880},
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
        entry: dict = {"name": name, "repo": repo, "present": present, "required": True}
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
    return 0


def cmd_status() -> int:
    print(f"app-data: {app_data_dir()}")
    services = [
        ("control-plane", 8000),
        ("web", 3000),
        ("asr", 8787),
        ("tts", 8788),
        ("llm", 1235),
        ("b-line", 8790),
        ("livekit", 7880),
    ]
    for name, port in services:
        print(f"  {name:<13} :{port:<6} {'UP' if healthy(port) else 'DOWN'}")
    return 0


def _start_proc(args: list[str], pidfile: Path, logfile: Path, env: dict | None = None, cwd: str | Path | None = None) -> int:
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    merged = dict(os.environ)
    if env:
        merged.update(env)
    with logfile.open("ab") as log:
        proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, env=merged, start_new_session=True, cwd=str(cwd) if cwd else None)
    pidfile.write_text(str(proc.pid))
    return proc.pid


def _bline_config_path() -> Path:
    return app_data_dir() / "bline.json"


def write_bline_config() -> Path:
    """Write a fully-resolved B-line config into app-data (bundle stays read-only)."""
    cfg = {
        "asr": {"provider": "qwen3_asr", "base_url": "http://127.0.0.1:8787", "sample_rate": 16000},
        "translator": {
            "provider": "local_openai",
            "base_url": "http://127.0.0.1:1235/v1",
            "model": "local",
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


def _start_llm(current: dict[str, str], run_dir: Path, log_dir: Path) -> None:
    if healthy(1235):
        return
    llm_model = model_path(current, "llm")
    if is_mac():
        llm_py = sidecar_python("llm-mlx")
        _start_proc(
            [str(llm_py), "-m", "mlx_lm", "server",
             "--model", llm_model, "--host", "127.0.0.1", "--port", "1235",
             "--chat-template-args", '{"enable_thinking":false}', "--log-level", "WARNING"],
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

    if not healthy(8787):
        _start_proc(
            [str(asr_py), "-m", "uvicorn", "app:app", "--app-dir", "services/qwen3-asr-sidecar",
             "--host", "127.0.0.1", "--port", "8787"],
            run_dir / "asr.pid", log_dir / "asr.log",
            env={"QWEN3_ASR_MODEL": asr_model, "QWEN3_ASR_BACKEND": asr_backend,
                 "QWEN3_ASR_DEVICE": "cuda" if (_cuda() and not is_mac()) else "cpu"},
        )
    if not healthy(8788):
        _start_proc(
            [str(tts_py), "-m", "uvicorn", "app:app", "--app-dir", "services/qwen3-tts-sidecar",
             "--host", "127.0.0.1", "--port", "8788"],
            run_dir / "tts.pid", log_dir / "tts.log",
            env={"QWEN3_TTS_PRESET_MODEL": tts_preset, "QWEN3_TTS_CLONE_MODEL": tts_clone,
                 "QWEN3_TTS_BACKEND": tts_backend},
        )

    _start_llm(current, run_dir, log_dir)

    # B-line worker (Node, OpenAI-compatible translator on :1235).
    bline_cfg = write_bline_config()
    if not healthy(8790):
        _start_proc(
            [node(), str(ROOT / "services" / "realtime-translation" / "server.mjs")],
            run_dir / "bline.pid", log_dir / "bline.log",
            env={**node_env(), "BOK_BLINE_CONFIG": str(bline_cfg)},
        )

    print("[bok] waiting for services…")
    for _ in range(60):
        if all(healthy(p) for p in (8787, 8788, 8790, 1235)):
            print("[bok] ready: asr=8787 tts=8788 llm=1235 b-line=8790")
            return 0
        time.sleep(1)
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
    # control-plane
    # Dev 与打包统一：业务数据 SQLite 落盘、知识 vault 在 app-data（bundle 只读）。
    db = (app_data_dir() / "bok_voice.db").as_posix()
    cp_env: dict[str, str] = {
        "PYTHONPATH": _repo_pythonpath(),
        "BOK_SERVICE": "control-plane",
        "DATABASE_URL": f"sqlite:///{db}",
        "VAULT_ROOT": str(app_data_dir() / "vault"),
    }
    if not healthy(8000):
        _start_proc(
            [str(py), "-m", "uvicorn", "control_plane.main:app", "--host", "127.0.0.1", "--port", "8000"],
            run_dir / "control-plane.pid",
            log_dir / "control-plane.log",
            env=cp_env,
        )
    # Dev mode: Next server on :3000 (packaged serves static UI from Tauri).
    if not is_packaged() and not healthy(3000):
        _start_proc(
            [node(), str(_repo_web_modules() / "next" / "dist" / "bin" / "next"), "start", "-H", "127.0.0.1", "-p", "3000"],
            run_dir / "web.pid",
            log_dir / "web.log",
            env={"NEXT_PUBLIC_CONTROL_PLANE_URL": os.environ.get("CONTROL_PLANE_URL", "http://localhost:8000")},
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
        agent_env: dict[str, str] = {
            "PYTHONPATH": _repo_pythonpath(),
            "BOK_SERVICE": "agent",
            "LIVEKIT_URL": os.environ.get("LIVEKIT_URL", "ws://localhost:7880"),
            "LIVEKIT_API_KEY": os.environ.get("LIVEKIT_API_KEY", "devkey"),
            "LIVEKIT_API_SECRET": os.environ.get("LIVEKIT_API_SECRET", "devsecret"),
            "CONTROL_PLANE_URL": os.environ.get("CONTROL_PLANE_URL", "http://localhost:8000"),
            "MLX_LLM_BASE_URL": os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1"),
        }
        _start_proc([str(py), "-m", "agent_runtime.main"], run_dir / "agent.pid", log_dir / "agent.log", env=agent_env)

    print("[bok] waiting for desktop stack…")
    targets = [8000, 8787, 8788, 8790, 1235]
    if not is_packaged():
        targets.append(3000)
    if healthy(7880):
        targets.append(7880)
    for _ in range(120):
        if all(healthy(p) for p in targets):
            print("[bok] desktop ready: control-plane=8000 asr=8787 tts=8788 llm=1235 b-line=8790")
            return 0
        time.sleep(1)
    print("[bok] timeout waiting for desktop stack (see app-data/logs)", file=sys.stderr)
    return 1


def cmd_down() -> int:
    run_dir = app_data_dir() / "run"
    for pidfile in run_dir.glob("*.pid"):
        try:
            pid = int(pidfile.read_text().strip())
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

    for name, port in (("control-plane", 8000), ("asr", 8787), ("tts", 8788), ("llm", 1235), ("b-line", 8790), ("livekit", 7880)):
        print(f"  port {port:<5} ({name}): {'UP' if healthy(port) else 'DOWN'}")

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


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="bok", description="Bok voice stack launcher (no Docker)")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("catalog", "manifest", "download", "status", "up", "serve", "down", "doctor"):
        sub.add_parser(name)
    p_setup = sub.add_parser("setup", help="First-run model readiness / download")
    p_setup.add_argument("action", nargs="?", default="status", choices=["status", "download"])
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.cmd == "setup":
        return cmd_setup(args.action)
    return {"catalog": cmd_catalog, "manifest": cmd_manifest, "download": cmd_download, "status": cmd_status,
            "up": cmd_up, "serve": cmd_serve, "down": cmd_down, "doctor": cmd_doctor}[args.cmd]()


if __name__ == "__main__":
    raise SystemExit(main())
