"""人设保存点自动罐头物化(2026-09-10 用户拍板 mandate)。

「如果上线新人设没有 QA 罐头/垫话应该提醒,并自动触发全部生成」:
人设 create/update 命中音色变化 → 后台 detached 子进程跑
`scripts/pregen_tts.py --greetings --fillers --qa --branches --persona <id>`
(幂等:已在缓存的 key 跳过,重跑零云调用)。响应带 `tts_pregen` 状态字段
=提醒面;运行时逐轮提醒仍是 agent.log 的 `BOK_FILLER voice_fallback`。

fail-dead:任何 spawn 失败只回状态绝不阻人设落库;杀开关
`BOK_PERSONA_AUTO_PREGEN=0` 全关。单飞:同一人设上一发未跑完不再叠发
(MiniMax 60 RPM 限流,叠发只会双双失败)。

罐头状态面(2026-09-17 qa-canvas Phase 1 Task 3):`qa_status_json` spawn
`pregen_tts.py --qa-status` 取逐条目物化状态(子进程=与物化同一条代码路径,
零重复);`qa_canned_status` 做 TTL 缓存、spawn 失败降级 available=False;
`cache_root` 供 canned-audio 回放 {key}.pcm;`qa_pregen_spawn` 手动触发
--qa 物化(单飞锁与 persona 自动物化共用)。

分支罐头状态面/一键补录(2026-09-20 流程画布答法抽屉):`branch_status_json`
spawn `--branch-status`、`branch_canned_status` TTL 缓存**按账号分键**
(QA 全局单份跨账号串状态是已知问题,不再复制)、`branch_pregen_spawn`
触发 --branches(可限 --texts-file,单飞锁键 __branches__)。
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
                # 分支罐头快路(2026-09-20 路线 A-①):分支应答随人设音色一并物化
                "--branches",
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


# ---- 罐头状态面(qa-canvas Phase 1 Task 3) ----

_STATUS_TTL_S = 60.0
_status_cache: tuple[float, dict | None] = (0.0, None)


def _repo_root_script() -> Path:
    return _repo_root() / "scripts" / "pregen_tts.py"


def qa_status_json(base_url: str) -> dict:
    """spawn pregen_tts.py --qa-status 取状态(子进程=与物化同一条代码路径,零重复)。"""
    script = _repo_root_script()
    if not script.exists():
        return {"available": False}
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "BOK_CP_URL": base_url}
    _bake_ssl_cert_file(env)
    proc = subprocess.run(  # noqa: S603 - 固定脚本+参数,无 shell
        [sys.executable, str(script), "--qa-status", "--cp", base_url],
        cwd=str(_repo_root()), env=env, capture_output=True, text=True, timeout=120,
    )
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            parsed = json.loads(line)
            if "qa_status" in parsed:
                out: dict = {"available": True, "qa_status": parsed["qa_status"]}
                # F1(2026-09-20)信息位:逐语言音色来源(旧版脚本无此键=缺省空表)。
                if "voice_source" in parsed:
                    out["voice_source"] = parsed["voice_source"]
                return out
    return {"available": False}


def qa_canned_status(base_url: str, *, force: bool = False) -> dict:
    """TTL 缓存的罐头状态;spawn 失败降级 available=False(spec §8)。"""
    import time as _time

    now = _time.monotonic()
    if not force and _status_cache[1] is not None and now - _status_cache[0] < _STATUS_TTL_S:
        return _status_cache[1]  # type: ignore[return-value]
    try:
        data = qa_status_json(base_url)
        out = {
            "available": bool(data.get("available")),
            "statuses": dict(data.get("qa_status") or {}),
            "generated_at": int(_time.time()),
            # F1(2026-09-20)信息位:旧脚本无键=空表(端点侧再兜一次)。
            "voice_source": dict(data.get("voice_source") or {}),
        }
    except Exception:  # noqa: BLE001 - 状态面永不炸端点
        out = {"available": False, "statuses": {}, "generated_at": int(_time.time()), "voice_source": {}}
    globals()["_status_cache"] = (now, out)
    return out


def cache_root() -> Path:
    """tts-cache 根目录(canned-audio 读 {key}.pcm 用)。

    与 agent_runtime.tts_cache.default_cache_dir 同布局——依赖方向禁止 CP
    import agent 包,目录推导照抄;布局是稳定契约,改动须双侧同步。
    """
    explicit = os.environ.get("BOK_TTS_CACHE_DIR", "").strip()
    if explicit:
        return Path(explicit) / "tts-cache"
    if sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local"))
    else:
        base = Path.home() / ".local/share"
    return base / "BokVoice" / "tts-cache"


def qa_pregen_spawn(base_url: str, entry_ids: list[str]) -> dict:
    """触发 --qa 物化(可限 ids);与 persona 自动物化共用单飞锁语义。"""
    script = _repo_root_script()
    if not script.exists():
        return {"status": "script_missing"}
    with _SPAWN_LOCK:
        running = _PREGEN_PROCS.get("__qa__")
        if running is not None and running.poll() is None:
            return {"status": "already_running"}
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "BOK_CP_URL": base_url}
        _bake_ssl_cert_file(env)
        cmd = [sys.executable, str(script), "--qa", "--cp", base_url]
        for eid in entry_ids or []:
            cmd += ["--entry-id", str(eid)]
        proc = _spawn_detached(cmd, env, _log_path())
        _PREGEN_PROCS["__qa__"] = proc
    return {"status": "queued", "pid": proc.pid, "log": str(_log_path())}


# ---- 分支罐头状态面/一键补录(2026-09-20 流程画布答法抽屉注线) ----

# 按 (base_url, account_id) 分键的 TTL 缓存——QA 那份 _status_cache 是全局单份
# (跨账号串状态是已知问题),分支这份不再复制该写法。
_branch_status_cache: dict[tuple[str, str], tuple[float, dict]] = {}


def branch_status_json(base_url: str, account_id: str = "") -> dict:
    """spawn pregen_tts.py --branch-status 取分支物化状态(子进程=与物化同一条代码路径)。

    stdout 倒找 JSON 行取 branch_status 键,与 qa_status_json 同姿态。
    """
    script = _repo_root_script()
    if not script.exists():
        return {"available": False}
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "BOK_CP_URL": base_url}
    _bake_ssl_cert_file(env)
    cmd = [sys.executable, str(script), "--branch-status", "--cp", base_url]
    if account_id:
        cmd += ["--account-id", str(account_id)]
    proc = subprocess.run(  # noqa: S603 - 固定脚本+参数,无 shell
        cmd, cwd=str(_repo_root()), env=env, capture_output=True, text=True, timeout=120,
    )
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            parsed = json.loads(line)
            if "branch_status" in parsed:
                out: dict = {"available": True, "branch_status": parsed["branch_status"]}
                # F1(2026-09-20)信息位:逐语言音色来源(旧版脚本无此键=缺省空表)。
                if "voice_source" in parsed:
                    out["voice_source"] = parsed["voice_source"]
                return out
    return {"available": False}


def branch_canned_status(base_url: str, *, account_id: str = "", force: bool = False) -> dict:
    """TTL 缓存的分支罐头状态;缓存键含账号(不沿用 QA 全局单份的跨账号串状态
    写法);spawn 失败降级 available=False(与 qa_canned_status 同姿态)。"""
    import time as _time

    key = (str(base_url), str(account_id or ""))
    now = _time.monotonic()
    cached = _branch_status_cache.get(key)
    if not force and cached is not None and now - cached[0] < _STATUS_TTL_S:
        return cached[1]
    try:
        data = branch_status_json(base_url, key[1])
        out = {
            "available": bool(data.get("available")),
            "statuses": dict(data.get("branch_status") or {}),
            "generated_at": int(_time.time()),
            # F1(2026-09-20)信息位:旧脚本无键=空表(端点侧再兜一次)。
            "voice_source": dict(data.get("voice_source") or {}),
        }
    except Exception:  # noqa: BLE001 - 状态面永不炸端点
        out = {"available": False, "statuses": {}, "generated_at": int(_time.time()), "voice_source": {}}
    _branch_status_cache[key] = (now, out)
    return out


def branch_pregen_spawn(base_url: str, account_id: str, texts: list[str] | None = None) -> dict:
    """触发 --branches 分支物化(可限 --texts-file);单飞锁键 __branches__,
    与 QA/persona 各持一把——分支批量跑得慢,不互相挤占也不阻塞 QA 补录。"""
    script = _repo_root_script()
    if not script.exists():
        return {"status": "script_missing"}
    with _SPAWN_LOCK:
        running = _PREGEN_PROCS.get("__branches__")
        if running is not None and running.poll() is None:
            return {"status": "already_running"}
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "BOK_CP_URL": base_url}
        _bake_ssl_cert_file(env)
        cmd = [sys.executable, str(script), "--branches", "--cp", base_url]
        if account_id:
            cmd += ["--account-id", str(account_id)]
        if texts:
            # 文本经临时文件传子进程(一行一条=resp 原文含动作标记)——CLI 参数面
            # 免转义/免长度上限;文件留在系统临时目录(子进程启动即读,OS 定期清理)。
            import tempfile

            tf = tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", prefix="bok-branch-texts-",
                delete=False, encoding="utf-8",
            )
            try:
                tf.write("\n".join(str(t) for t in texts))
            finally:
                tf.close()
            cmd += ["--texts-file", tf.name]
        proc = _spawn_detached(cmd, env, _log_path())
        _PREGEN_PROCS["__branches__"] = proc
    return {"status": "queued", "pid": proc.pid, "log": str(_log_path())}
