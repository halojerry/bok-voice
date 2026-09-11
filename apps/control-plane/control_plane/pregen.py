"""人设保存点自动罐头物化(2026-09-10 用户拍板 mandate)。

「如果上线新人设没有 QA 罐头/垫话应该提醒,并自动触发全部生成」:
人设 create/update 命中音色变化 → 后台 detached 子进程跑
`scripts/pregen_tts.py --greetings --fillers --qa --persona <id>`
(幂等:已在缓存的 key 跳过,重跑零云调用)。响应带 `tts_pregen` 状态字段
=提醒面;运行时逐轮提醒仍是 agent.log 的 `BOK_FILLER voice_fallback`。

fail-dead:任何 spawn 失败只回状态绝不阻人设落库;杀开关
`BOK_PERSONA_AUTO_PREGEN=0` 全关。单飞:同一人设上一发未跑完不再叠发
(MiniMax 60 RPM 限流,叠发只会双双失败)。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _has_voice(raw: str) -> bool:
    """音色三态判空:单音色串非空 / JSON 映射任一值非空 = 有音色。"""
    text = str(raw or "").strip()
    if not text:
        return False
    if text.startswith("{"):
        try:
            mapping = json.loads(text)
        except ValueError:
            return True  # 解析失败当单串处理(交给 pregen 同款逻辑判)
        if isinstance(mapping, dict):
            return any(str(v or "").strip() for v in mapping.values())
        return True
    return True


def _voice_relevant_changed(existing: dict | None, persona: dict) -> bool:
    """create(无 existing)=触发;update 只在音色相关字段变化时触发。

    reference_audio/tts_provider/language 任一变化都会改变物化计划
    (音色 id / 模式 / 垫话语言池),其余字段(名字/语气)与罐头无关。
    """
    if not existing:
        return True
    keys = ("reference_audio", "tts_provider", "language")
    return any(str(existing.get(k) or "") != str(persona.get(k) or "") for k in keys)


def _bake_ssl_cert_file(env: dict[str, str]) -> None:
    """与 bok.py 同款:venv 无系统 CA,MiniMax WSS 无 SSL_CERT_FILE 必炸。"""
    if env.get("SSL_CERT_FILE"):
        return
    try:
        import certifi

        env["SSL_CERT_FILE"] = certifi.where()
    except Exception:  # noqa: BLE001 - 无 certifi 则交由子进程自身报缺
        pass


def _log_path() -> Path:
    """app-data/BokVoice/logs/tts-pregen.log(与 tts-cache 同一基目录布局)。"""
    if sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local"))
    else:
        base = Path.home() / ".local/share"
    return base / "BokVoice" / "logs" / "tts-pregen.log"


# 同进程单飞账本:persona_id → Popen(跑完留着,poll() 非 None 即可覆盖)。
# FastAPI sync handler 跑线程池——check-and-spawn 必须整段持锁,否则并发
# 保存同一人设会双双通过 poll() 检查双发子进程(重复云合成,60 RPM 压力)。
_PREGEN_PROCS: dict[str, subprocess.Popen] = {}
_SPAWN_LOCK = threading.Lock()


def _spawn_detached(cmd: list[str], env: dict[str, str], log: Path) -> subprocess.Popen:
    """起子进程:新会话(栈 Ctrl-C/组杀不断它)+日志落盘,失败退 DEVNULL。

    daemon reaper 线程回收退出码——不挂的话子进程退出后留僵尸直到该人设
    下一次保存才被 poll() 顺手收尸。
    """
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as lf:
            proc = subprocess.Popen(  # noqa: S603 - 固定脚本+参数,无 shell
                cmd,
                cwd=str(_repo_root()),
                env=env,
                stdout=lf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except OSError:
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            cwd=str(_repo_root()),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    threading.Thread(target=proc.wait, daemon=True, name=f"pregen-reap-{proc.pid}").start()
    return proc


def persona_pregen_status(
    persona: dict,
    *,
    base_url: str,
    existing: dict | None = None,
) -> dict:
    """决定并(可能)触发自动物化;永不抛错,返回值直接进响应 `tts_pregen` 键。"""
    try:
        if os.environ.get("BOK_PERSONA_AUTO_PREGEN", "1") != "1":
            return {"status": "disabled"}
        pid = str(persona.get("id") or "")
        if not pid:
            return {"status": "no_persona_id"}
        if not _has_voice(str(persona.get("reference_audio") or "")):
            return {
                "status": "no_voice",
                "hint": "人设未配置音色(reference_audio 为空),垫话/QA 罐头不会自动物化;填好音色再保存一次即触发",
            }
        if not _voice_relevant_changed(existing, persona):
            return {"status": "unchanged"}
        script = _repo_root() / "scripts" / "pregen_tts.py"
        if not script.exists():
            return {
                "status": "script_missing",
                "hint": "运行目录无 scripts/pregen_tts.py(打包部署),请手动执行 bok.py tts-pregen --greetings --fillers --qa --persona " + pid,
            }
        with _SPAWN_LOCK:
            running = _PREGEN_PROCS.get(pid)
            if running is not None and running.poll() is None:
                return {"status": "already_running", "persona_id": pid}
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "BOK_CP_URL": base_url}
            _bake_ssl_cert_file(env)
            cmd = [
                sys.executable,
                str(script),
                "--greetings",
                "--fillers",
                "--qa",
                "--persona",
                pid,
            ]
            proc = _spawn_detached(cmd, env, _log_path())
            _PREGEN_PROCS[pid] = proc
        log = _log_path()
        print(
            f"BOK_PERSONA_PREGEN queued persona={pid} pid={proc.pid} log={log}",
            flush=True,
        )
        return {"status": "queued", "persona_id": pid, "log": str(log)}
    except Exception as exc:  # noqa: BLE001 - 提醒面永不阻人设保存
        return {"status": "failed", "error": repr(exc)[:200]}
