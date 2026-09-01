#!/usr/bin/env python
"""bokctl — cross-platform (no-Docker) launcher for the Bok voice stack.

Subcommands:
  catalog    List per-platform models + sizes.
  download   Download missing models into the app-data dir (resume + progress).
  status     Summarize service health + model readiness.
  up         Ensure models + venvs, then start ASR/TTS/LLM/B-line and wait for ready.
  down       Stop services started by bokctl(pidfiles).
  doctor     Quick preflight diagnostics.

Platform split (MLX is Apple-only):
  mac -> MLX sidecars (QWEN3_*_BACKEND=mlx) + mlx_lm server on :1235 (via start_sidecars.sh)
  win -> transformers sidecars (QWEN3_*_BACKEND=transformers, CUDA torch) + Ollama LLM (:11434)
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


def runtime_root() -> Path:
    """Locate the bundled runtime dir (venv/node/livekit).

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
        ):
            return cand
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return ROOT / "runtime"


def app_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path(os.environ.get("HOME", ".")) / "Library" / "Application Support"
    return base / "BokVoice"


def is_mac() -> bool:
    return _platform.system() == "Darwin"


MODELS: dict[str, dict[str, str]] = {
    "mac": {
        "asr": "aufklarer/Qwen3-ASR-1.7B-MLX-8bit",
        "tts_preset": "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit",
        "tts_clone": "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit",
        "llm": "huihui-ai/Huihui-Qwen3.5-9B-abliterated-mlx-4bit",
        "llm_ollama": "",
    },
    "windows": {
        "asr": "Qwen/Qwen3-ASR-1.7B",
        "tts_preset": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "tts_clone": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        "llm": "",
        "llm_ollama": "huihui-ai/abliterated:qwen3.5-9b",
    },
}


def model_dir(repo_id: str) -> Path:
    return app_data_dir() / "models" / repo_id.replace("/", "--")


def sidecar_python(name: str) -> Path:
    """Bundled venv for a sidecar, else the repo venv, else the generic runtime.

    Packaged app ships the sidecar venv under ``runtime/.venv`` and the sidecar
    source under ``services/<name>``; dev builds use ``services/<name>/.venv``.
    """
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
    """Return a bundled Node binary if present (packaged app), else None."""
    if os.name == "nt":
        cand = runtime_root() / "node" / "node.exe"
        cand2 = runtime_root() / "node.exe"
    else:
        cand = runtime_root() / "node" / "bin" / "node"
        cand2 = runtime_root() / "bin" / "node"
    for c in (cand, cand2):
        if c.exists():
            return str(c)
    return None


def bundled_node_modules() -> Path | None:
    """Return the bundled B-line node_modules dir, if present."""
    for c in (runtime_root() / "bline-node_modules", ROOT / "services" / "realtime-translation" / "node_modules"):
        if c.exists():
            return c
    return None


def node() -> str:
    return bundled_node() or "node"


def node_env() -> dict[str, str]:
    """Env var overrides for the B-line worker (Node resolution)."""
    mods = bundled_node_modules()
    if mods:
        return {"NODE_PATH": str(mods)}
    return {}


def healthy(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def cmd_catalog() -> int:
    key = "windows" if os.name == "nt" else "mac"
    print(f"platform: {key}")
    for name, repo in MODELS[key].items():
        if repo:
            print(f"  {name:<12} {repo}")
    print(f"  ~download (~13GB) into {app_data_dir() / 'models'}")
    return 0


def cmd_manifest() -> int:
    """Emit a JSON manifest for the desktop shell / CI release pipeline.

    Covers: platform, app-data location, every model (repo + size hint +
    checksum once present), the fixed local ports, and the build version so a
    packaged app can show an OTA-compatible "what is installed" panel and CI
    can attach a parity manifest to a release.
    """
    key = "windows" if os.name == "nt" else "mac"
    data: dict = {
        "platform": key,
        "app_data_dir": str(app_data_dir()),
        "ports": {"control_plane": 8000, "web": 3000, "asr": 8787, "tts": 8788, "llm": 1235 if is_mac() else 11434, "b_line": 8790},
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
    """Return per-model download status for the first-run wizard.

    Each entry: name, repo, present(bool), size_bytes(if present), required(bool).
    """
    key = "windows" if os.name == "nt" else "mac"
    out: list[dict] = []
    for name, repo in MODELS[key].items():
        # A platform may leave a slot empty (e.g. mac uses mlx_lm, no Ollama).
        # Empty repo => not required, so it never blocks readiness.
        if not repo:
            out.append({"name": name, "repo": "", "present": True, "required": False})
            continue
        if name == "llm_ollama":
            # Ollama is pulled via the ollama CLI, not snapshot_download.
            out.append({"name": name, "repo": repo, "present": _ollama_present(repo), "required": True})
            continue
        target = model_dir(repo)
        present = target.exists() and any(target.iterdir())
        entry: dict = {"name": name, "repo": repo, "present": present, "required": True}
        if present:
            entry["size_bytes"] = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
        out.append(entry)
    return out


def cmd_setup(action: str = "status") -> int:
    """First-run setup helper. ``status`` reports model readiness; ``download``
    downloads missing models then reports readiness."""
    if action == "download":
        cmd_download()
        print(json.dumps({"ready": _all_models_present()}, ensure_ascii=False))
        return 0
    data = {"ready": _all_models_present(), "models": setup_models()}
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def _ollama_present(repo: str) -> bool:
    try:
        subprocess.run(["ollama", "list"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return bool(subprocess.run(["ollama", "list"], check=False, stdout=subprocess.PIPE, text=True).stdout)
    except Exception:
        return False


def _all_models_present() -> bool:
    return all(m["present"] for m in setup_models() if m["required"])


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


def cmd_download() -> int:
    key = "windows" if os.name == "nt" else "mac"
    try:
        from huggingface_hub import snapshot_download
    except Exception as exc:  # pragma: no cover
        print(f"[download] huggingface_hub missing: {exc}", file=sys.stderr)
        return 2
    for name, repo in MODELS[key].items():
        if not repo or name == "llm_ollama":
            continue
        target = model_dir(repo)
        if target.exists() and any(target.iterdir()):
            print(f"  [ok]   {name} present  {target}")
            continue
        print(f"  [down] {name}  {repo}")
        snapshot_download(repo_id=repo, local_dir=str(target), resume_download=True)
        print(f"  [ok]   {name} downloaded")
    return 0


def cmd_status() -> int:
    llm_port = 1235 if is_mac() else 11434
    print(f"app-data: {app_data_dir()}")
    services = [
        ("control-plane", 8000),
        ("web", 3000),
        ("asr", 8787),
        ("tts", 8788),
        ("llm", llm_port),
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


def cmd_up() -> int:
    run_dir = app_data_dir() / "run"
    log_dir = app_data_dir() / "logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    print("[bok] ensuring models…")
    cmd_download()
    print("[bok] starting services…")

    if is_mac() and not is_packaged():
        subprocess.run(["bash", str(ROOT / "scripts" / "start_sidecars.sh")], cwd=str(ROOT))
    else:
        m = MODELS["windows"]
        current = MODELS["mac"] if is_mac() else m
        asr_py = sidecar_python("qwen3-asr-sidecar")
        tts_py = sidecar_python("qwen3-tts-sidecar")
        if not asr_py.exists() or not tts_py.exists():
            print("[bok] sidecar venvs missing — run setup-windows.ps1 first", file=sys.stderr)
            return 2
        asr_model = str(model_dir(current["asr"])) if is_packaged() else current["asr"]
        tts_preset = str(model_dir(current["tts_preset"])) if is_packaged() else current["tts_preset"]
        tts_clone = str(model_dir(current["tts_clone"])) if is_packaged() else current["tts_clone"]
        asr_backend = "mlx" if is_mac() else "transformers"
        tts_backend = "mlx" if is_mac() else "transformers"
        if not healthy(8787):
            _start_proc([str(asr_py), "-m", "uvicorn", "app:app", "--app-dir", "services/qwen3-asr-sidecar",
                         "--host", "0.0.0.0", "--port", "8787"], run_dir / "asr.pid", log_dir / "asr.log",
                       env={"QWEN3_ASR_MODEL": asr_model, "QWEN3_ASR_BACKEND": asr_backend,
                            "QWEN3_ASR_DEVICE": "cuda" if (_cuda() and not is_mac()) else "cpu"})
        if not healthy(8788):
            _start_proc([str(tts_py), "-m", "uvicorn", "app:app", "--app-dir", "services/qwen3-tts-sidecar",
                         "--host", "0.0.0.0", "--port", "8788"], run_dir / "tts.pid", log_dir / "tts.log",
                       env={"QWEN3_TTS_PRESET_MODEL": tts_preset, "QWEN3_TTS_CLONE_MODEL": tts_clone,
                            "QWEN3_TTS_BACKEND": tts_backend})
        if not is_mac():
            if not healthy(11434):
                _start_proc(["ollama", "serve"], run_dir / "ollama.pid", log_dir / "ollama.log")
            subprocess.run(["ollama", "pull", m["llm_ollama"]], check=False)
        else:
            # Packaged Mac: start the bundled mlx_lm server directly.
            llm_venv_py = sidecar_python("llm-mlx")
            llm_model = str(model_dir(current["llm"])) if is_packaged() else current["llm"]
            if not healthy(1235):
                _start_proc(
                    [str(llm_venv_py), "-m", "mlx_lm", "server",
                     "--model", llm_model, "--port", "1235",
                     "--chat-template-args", '{"enable_thinking":false}', "--log-level", "WARNING"],
                    run_dir / "llm.pid",
                    log_dir / "llm.log",
                )

    if not healthy(8790):
        _start_proc([node(), str(ROOT / "services" / "realtime-translation" / "server.mjs")],
                   run_dir / "bline.pid", log_dir / "bline.log",
                   env=node_env())

    llm_port = 1235 if is_mac() else 11434
    print("[bok] waiting for services…")
    for _ in range(60):
        if all(healthy(p) for p in (8787, 8788, 8790, llm_port)):
            print(f"[bok] ready: asr=8787 tts=8788 llm={llm_port} b-line=8790")
            return 0
        time.sleep(1)
    print("[bok] timeout waiting for services (see app-data/logs)", file=sys.stderr)
    return 1


def _repo_web_modules() -> Path:
    return ROOT / "apps" / "web" / "node_modules"


def cmd_serve() -> int:
    """Bring up the full no-Docker desktop stack and wait until ready.

    Packaged mode (BOK_PACKAGED=1) serves the UI from the Tauri static bundle,
    so the Next server on :3000 is NOT started. LiveKit (:7880) is launched from
    the embedded `runtime/livekit-server` when present; the agent worker is
    started against it. Dev mode keeps the previous behaviour.
    """
    run_dir = app_data_dir() / "run"
    log_dir = app_data_dir() / "logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    py = repo_python()
    # control-plane
    if not healthy(8000):
        _start_proc(
            [str(py), "-m", "uvicorn", "control_plane.main:app", "--host", "0.0.0.0", "--port", "8000"],
            run_dir / "control-plane.pid",
            log_dir / "control-plane.log",
            env={"PYTHONPATH": _repo_pythonpath(), "BOK_SERVICE": "control-plane"},
        )
    # Packaged mode: UI is served by the Tauri static bundle, so no Next server.
    if not is_packaged() and not healthy(3000):
        _start_proc(
            [node(), str(_repo_web_modules() / "next" / "dist" / "bin" / "next"), "start", "-p", "3000"],
            run_dir / "web.pid",
            log_dir / "web.log",
            env={"NEXT_PUBLIC_CONTROL_PLANE_URL": os.environ.get("CONTROL_PLANE_URL", "http://localhost:8000")},
            cwd=str(ROOT / "apps" / "web"),
        )

    # LiveKit should come up before the sidecars/agent worker.
    livekit_bin = _embedded_livekit() or shutil_which("livekit-server") or (ROOT / "services" / "livekit-server" / "livekit-server")
    if not healthy(7880) and livekit_bin and Path(livekit_bin).exists():
        _start_proc(
            [str(livekit_bin), "--config", str(ROOT / "services" / "livekit-server" / "livekit.yaml")],
            run_dir / "livekit.pid",
            log_dir / "livekit.log",
        )

    # Sidecars + LLM (mac via start_sidecars.sh, win via transformers) + b-line.
    rc = cmd_up()
    if rc:
        return rc

    # Agent worker registers to the embedded/local LiveKit server.
    if not healthy(7880):
        # LiveKit not present; agent has nothing to join.
        pass
    else:
        _start_proc(
            [str(py), "-m", "agent_runtime.main"],
            run_dir / "agent.pid",
            log_dir / "agent.log",
            env={
                "PYTHONPATH": _repo_pythonpath(),
                "BOK_SERVICE": "agent",
                "LIVEKIT_URL": os.environ.get("LIVEKIT_URL", "ws://localhost:7880"),
                "LIVEKIT_API_KEY": os.environ.get("LIVEKIT_API_KEY", "devkey"),
                "LIVEKIT_API_SECRET": os.environ.get("LIVEKIT_API_SECRET", "devsecret"),
                "CONTROL_PLANE_URL": os.environ.get("CONTROL_PLANE_URL", "http://localhost:8000"),
            },
        )

    print("[bok] waiting for desktop stack…")
    targets = [8000, 8787, 8788, 8790, 1235 if is_mac() else 11434]
    if not is_packaged():
        targets.append(3000)
    if healthy(7880):
        targets.append(7880)
    for _ in range(120):
        if all(healthy(p) for p in targets):
            print(f"[bok] desktop ready: control-plane=8000 asr=8787 tts=8788 b-line=8790")
            return 0
        time.sleep(1)
    print("[bok] timeout waiting for desktop stack (see app-data/logs)", file=sys.stderr)
    return 1


def _embedded_livekit() -> Path | None:
    """Return the embedded LiveKit server binary in a packaged app, if any."""
    if os.name == "nt":
        cand = runtime_root() / "livekit-server.exe"
    else:
        cand = runtime_root() / "livekit-server"
    return cand if cand.exists() else None


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


def cmd_down() -> int:
    run_dir = app_data_dir() / "run"
    for pidfile in run_dir.glob("*.pid"):
        try:
            pid = int(pidfile.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            print(f"[down] stopped {pidfile.stem} (pid {pid})")
        except Exception:
            continue
    # Also stop the mac sidecars managed by start_sidecars.sh (host pids in data/).
    data_dir = ROOT / "data"
    for pidfile in data_dir.glob("sidecar-*.pid"):
        try:
            pid = int(pidfile.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            print(f"[down] stopped {pidfile.stem} (pid {pid})")
        except Exception:
            continue
    return 0


def cmd_doctor() -> int:
    print(f"platform: {_platform.system()}")
    print(f"python: {sys.version.split()[0]}")
    print(f"app-data: {app_data_dir()}")
    for name in ("qwen3-asr-sidecar", "qwen3-tts-sidecar"):
        py = sidecar_venv_python(name)
        print(f"  venv {name}: {'ok' if py.exists() else 'MISSING'} ({py})")
    try:
        import torch
        print(f"  torch: {torch.__version__} cuda={torch.cuda.is_available()}")
    except Exception:
        print("  torch: not installed")
    return 0


def _cuda() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


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
