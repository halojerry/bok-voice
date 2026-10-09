#!/usr/bin/env python
"""servers 域(serve/up 服务面:服务拉起编排、_start_* 服务拉起家族、
_worker_specs 装配、本地 TTS 门控、sidecar env 透传、LLM launch 配置件、
spawn 原语四件;G2 W② 从 core 搬出,搬运纪律=穿模块对象调用)。

G2 W③(2026-10-05):cmd_serve/cmd_up 两个命令入口随 CLI 分家搬入
bokctl/commands/{serve,up}.py——本域保留它们下面的全部服务面实现
(_cmd_up_services/_start_call_plane/_start_* 家族/_worker_specs 等)。

- 本模块 `from bokctl import core/env`(及 health/proc/models/paths 域)拿模块
  对象:凡仍住在 core 的名字(healthy/_desktop_stack_targets/_agent_worker_port/
  _cp_bind_host 等)一律 `core.X` 调用时取——patch 与域搬运在属主模块侧保持
  可见(patch 缝=模块属性);env 组装面(_FORWARD_ENV/_control_plane_env/
  _agent_prod_env/_agent_worker_env/_interp_env/_apply_interp_direction_env/
  _dev_9b_enabled/_llm_queue_proxy_on)W②-env 波(2026-10-04,最后一批)搬入
  bokctl.env,本域穿 `env.X` 取(env 波新例:域间消费=改穿所属域,core 不做值
  转发);health/proc 域件穿 health.X/proc.X,models 域件(models.MODELS/model_path/
  resolve_llm_repo/_mt_llm_model/_settle_llm_model/laya_model_path/
  _llm_draft_flags 等)穿 models.X 取;路径/平台锚(ROOT/
  app_data_dir/repo_python/sidecar_python/is_mac/is_packaged/bundled_node/
  bundled_llama/_embedded_livekit/_livekit_config_path/MLX_SERVER_WRAPPER)
  paths 波(2026-10-04)后穿 `paths.X` 取;模型 ensure 的 cmd_download W③ 起
  住 bokctl.commands.download,本域穿 `commands.download.cmd_download()` 取。
- 本域自有函数(_cmd_up_services/_start_call_plane/_start_llm/
  _start_mt_llm/_start_settle_proxy/_start_settle_llm/_start_laya/_worker_specs/
  _realtime_demo_enabled/_local_tts_needed/_cloud_posture/_ensure_only_for_
  posture(2026-10-05 demo-cloud wave:云姿势判定+ensure 收窄集,读设置库判
  asr/llm/settle/mt 各腿云本地,保守法+worker 真相优先,零 bok_voice_core
  import)/_qwen3_*_sidecar_env/_apply_mlx_
  template_fix/_mlx_hf_offline_env/_settle_cache_bytes/_default_prompt_cache_
  bytes/_physical_mem_gib/_mac_llm_server_argv/_warn_llm_not_http_ready/
  _repo_web_modules + spawn 原语 _start_proc/_spawn_kwargs/_rotate_log/
  _stop_pidfile)域内裸名互调(同模块全局=call-time 可 patch)。
- 留守 core 的近邻(边界记录,2026-10-04;env 波更新):env 组装面(_FORWARD_ENV/
  _control_plane_env/_agent_prod_env/_agent_worker_env/_interp_env/_apply_judge_env/
  _apply_interp_direction_env/_bake_ssl_cert_file/_settle_gate_url/_dev_9b_enabled)
  与拓扑闸 _llm_queue_proxy_on **W②-env 波(最后一批)已搬入 bokctl.env**——
  _llm_queue_proxy_on 属纯 env 旗标读(与 _dev_9b_enabled 同族),health 波
  「随共享面留 core」的旧判解除;core 侧 _llm_raw_expected 与本域四个消费点
  都改穿 env.X;_llm_draft_enabled/_llm_draft_flags/_llm_draft_model 属模型
  选型机制,**models 波(W②)已搬入 bokctl.models**——本域穿 `models.X` 取
  (servers 波新例);MLX_SERVER_WRAPPER 旧判例(「ROOT 派生常量因 import 序留
  core」)已随 paths 波解除——常量随 ROOT 住 bokctl.paths,消费者穿 paths. 取;
  健康面五件套+端口表+_desktop_stack_targets
  (health 波既定);_cp_bind_host(prod 消费)/_pid_alive(prod 消费)判留;
  cmd_monitor/cmd_down W③ 起住 bokctl/commands/{monitor,down}.py(原 proc 波
  判「留 core」随 CLI 分家解除——down 被 prod uninstall 与 node_agent 吃,
  monitor 是命令;两者已改穿 servers.X 取装配/spawn 件)。
- 测试面:patch 一律走 tests/_bokpatch.py(patch_bok;PATCH_TARGETS 已把 cmd_up/
  _cmd_up_services/_start_call_plane/_start_llm/_start_mt_llm/_start_settle_llm/
  _start_laya/_apply_mlx_template_fix/_worker_specs/_local_tts_needed/_start_proc/
  _realtime_demo_enabled/_physical_mem_gib 改道——cmd_up W③ 起改道
  bokctl.commands.up,其余仍 bokctl.servers);facade 读用 bok.servers.X。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from bokctl import commands, core, env, health, models, paths, proc


# spawn 原语四件(2026-10-04 servers 波随服务面搬出;消费者=本域 _start_* 家族
# + core.cmd_monitor + proc._ensure_monitor,后两者已改穿 servers.X)。
def _rotate_log(logfile: Path, max_bytes: int = 50 * 1024 * 1024, keep: int = 3) -> None:
    """stdout 日志大小轮转（>50MB 归档 .1/.2/.3,留 3 代）——dev 栈日志此前
    无限增长,审计回溯既要留痕也要可磁盘承载（2026-09-07 审计闭环）。"""
    try:
        if not logfile.exists() or logfile.stat().st_size < max_bytes:
            return
        for i in range(keep - 1, 0, -1):
            src = logfile.with_suffix(logfile.suffix + f".{i}")
            dst = logfile.with_suffix(logfile.suffix + f".{i + 1}")
            if src.exists():
                dst.write_bytes(src.read_bytes())
        logfile.with_suffix(logfile.suffix + ".1").write_bytes(logfile.read_bytes())
        logfile.write_bytes(b"")
    except Exception:
        pass


def _spawn_kwargs() -> dict:
    """平台 spawn 旗标（与 down 的停止语义成对：改这里必须同步 _kill_proc_tree）。

    POSIX：start_new_session=True 起会话组长，killpg 一组全清。
    Windows：CPython 对 start_new_session 是**静默忽略**（POSIX-only kwarg，
    见 scripts/probes/probe_windows_lifecycle.py docstring 记录的 CPython 事实），必须
    显式 CREATE_NEW_PROCESS_GROUP 建独立进程组——taskkill /PID <pid> /T /F 才有
    干净的树根可收割；组内子进程也不再收宿主控制台的 Ctrl 事件（服务形态更稳）。
    """
    if os.name == "nt":
        # 0x200=CREATE_NEW_PROCESS_GROUP（win32 常量，跨 SDK 版本稳定）；getattr
        # 守卫让 POSIX 解释器上模拟 nt 的单测/probe 也能走到这个分支（POSIX 的
        # subprocess 没有该属性，真机 nt 恒命中第一候选）。
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
    return {"start_new_session": True}


def _start_proc(
    args: list[str],
    pidfile: Path,
    logfile: Path,
    env: dict | None = None,
    cwd: str | Path | None = None,
) -> int:
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    _rotate_log(logfile)
    merged = dict(os.environ)
    # 子进程日志实时可见（写到文件时 stdout 默认块缓冲，会吞掉关键启动日志）。
    merged.setdefault("PYTHONUNBUFFERED", "1")
    if env:
        merged.update(env)
    # 来源 stamp（2026-09-19 provenance 完整版）：子代 env 钉本树 ROOT（Linux
    # /proc/<pid>/environ 可读回）；macOS ps 不吐环境，另落 pid 作用域标记文件
    # （root + 子代 lstart，清扫时精确比对防 pid 复用串号）。端口清扫据此区分
    # 本树子代与他树进程，他树永不误杀（跨树互杀根因/多会话纪律）。
    merged["BOK_SERVE_ROOT"] = str(paths.ROOT)
    spawn = subprocess.Popen
    # 本地名 child(原 proc)：G2 W② 起 proc 是 bokctl.proc 域模块,本地同名会
    # 遮蔽模块(行为零变化,纯防遮蔽改名)。
    with logfile.open("ab") as log:
        child = spawn(
            args,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=merged,
            cwd=str(cwd) if cwd else None,
            **_spawn_kwargs(),
        )
    proc._write_proc_stamps(pidfile, child.pid)
    return child.pid


def _stop_pidfile(pidfile: Path) -> None:
    """Terminate the process group recorded in a run/*.pid file, if alive.

    他树戳守卫（2026-09-22）：pidfile 指向的进程读得出「他树拉起」时拒绝杀
    ——run/*.pid 是 HOME 作用域单槽共享文件，worker 换装/多会话会互相覆写，
    旧版无脑 killpg 会把别人的 worker 收割掉（跨树互杀同款根因）。"""
    try:
        pid = int(pidfile.read_text().strip())
        foreign, root = proc._pid_origin_foreign(pid)
        if foreign:
            print(f"[stop] skip {pidfile.name}: pid {pid} 属另一代码树（{root}）"
                  "——他树进程永不收割", file=sys.stderr)
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


# 云端 TTS provider 集（serve 本地 TTS 门控判据；与 agent 装配分支同名值对齐——
# agent.py effective_tts_provider 消费 {"minimax","minimax_streaming","volcano",
# "volcano_streaming","fake","fake_tts"}，其余值（qwen3_tts/未知）都算本地车道）。
_CLOUD_TTS_PROVIDERS = frozenset(
    {"minimax", "minimax_streaming", "volcano", "volcano_streaming", "fake", "fake_tts"}
)


def _local_tts_needed() -> tuple[bool, str]:
    """读 CP 设置判定本地 TTS sidecar（:8788）要不要拉起（2026-09-27）。

    全云端形态（全局 tts.provider 指云端、且无人设覆盖回本地）跳过 :8788——
    MLX 权重常驻零消费还挤内存。A 线 minimax 分支与 B 线 interpret 云端档都
    不碰本地 TTS；云端失败回退是 hd→turbo 同云换档（FallbackAdapter），不落
    本地。判据与 agent 装配 `effective_tts_provider`（F-11）同一条规则：
    persona.tts_provider 覆盖 > 全局 tts.provider > 缺省 qwen3_tts（本地）。
    env：BOK_LOCAL_TTS=1 强制拉起（E2E 探针渲染客户话音直打 :8788）、=0 强制
    跳过；DB 缺失/损坏 → 拉起（保守=旧行为零变化）。返回 (needed, 原因)。
    """
    override = os.environ.get("BOK_LOCAL_TTS", "").strip()
    if override == "1":
        return True, "BOK_LOCAL_TTS=1 强制"
    if override == "0":
        return False, "BOK_LOCAL_TTS=0 强制跳过"
    try:
        import sqlite3

        db_path = paths.app_data_dir() / "bok_voice.db"
        if not db_path.exists():
            return True, "无设置库默认拉起"
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute(
                "SELECT tts_json FROM global_settings WHERE id='global'"
            ).fetchone()
            global_p = ""
            if row and row[0]:
                global_p = str(
                    (json.loads(row[0]) or {}).get("provider") or ""
                ).strip().lower()
            overrides = [
                str(r[0] or "").strip().lower()
                for r in con.execute(
                    "SELECT tts_provider FROM persona_profiles"
                ).fetchall()
            ]
        finally:
            con.close()
        cloud_global = (global_p or "qwen3_tts") in _CLOUD_TTS_PROVIDERS
        local_persona = [p for p in overrides if p and p not in _CLOUD_TTS_PROVIDERS]
        if local_persona:
            return True, f"人设 tts_provider={local_persona[0]!r} 用本地"
        if cloud_global:
            return False, f"全云端 tts.provider={global_p}"
        return True, f"tts.provider={global_p!r}(空/本地=默认拉起)"
    except Exception as exc:  # noqa: BLE001 - 启动器不因设置问题崩
        return True, f"读取失败({exc.__class__.__name__})默认拉起"


def _cloud_posture() -> dict:
    """云端演示档姿势判定（2026-10-05 demo-cloud wave）：读 CP 设置库判 serve
    侧哪些本地服务可以不拉、哪些模型不必 ensure（A 线全云豆包+DeepSeek+MiniMax、
    B 线豆包 ASR + MT 本地 :1236 的演示形态，本地盘只落真正需要的权重）。

    三条立法：
    - **保守法（旧行为零变化）**：DB 缺失/损坏/列缺/JSON 坏 → 各云腿一律判本地
      （=与改造前 serve 完全同形）。绝不因设置读不出而少拉服务。
    - **worker 行为真相优先**：serve 只在 worker 真会走云端时才跳过本地件——
      `BOK_DOUBAO_ASR=0`（worker 总闸回退本地 Qwen3-ASR）与
      `BOK_MODEL_ROUTING=0`（路由表忽略→env 缺省链=本地 :1235/:1237）恒判本地，
      即使设置面写了云端档。
    - **零 bok_voice_core import**：CLI 在裸环境（bootstrap 前/打包节点）跑，
      路由表语义在此按 packages/core/bok_voice_core/model_routes.py 同款规则
      轻量镜像（provider 规范化、未知值坍缩 local），不 import 共享契约包。

    判据（与 agent 装配分支同源）：
    - asr_cloud：``asr_json.provider ∈ {doubao, doubao_asr}`` 且凭据在场（新式
      单 ``api_key``，或旧式 ``app_id``+``access_token`` 两者齐——agent.py 装配
      点同判）；缺凭据=agent 会回退本地 → 判本地。
    - llm_cloud / settle_cloud：``model_routing_json.lanes`` 的 a_reply+judge+
      settle（/settle 单独）provider=="openai"。
    - mt_local：lanes.mt provider≠"openai"（缺省/缺席/local → True，:1236 照旧
      按模型在盘拉起）。

    env 闸（serve 侧旋钮；worker 不读这两个键、不入 _FORWARD_ENV）：
    ``BOK_LOCAL_ASR``=1 强制本地 / =0 强制跳过本地；``BOK_LOCAL_LLM`` 同镜像
    （作用于 :1235 与 :1237 两腿，mt 车道仍按路由表）。``BOK_DOUBAO_ASR=0``
    优先于 ``BOK_LOCAL_ASR=0``（worker 物理行为赢）。

    返回 dict：``asr_cloud/asr_why、llm_cloud/llm_why、settle_cloud/settle_why、
    mt_local/mt_why``。
    """
    asr_cloud, asr_why = False, ""
    llm_cloud, llm_why = False, ""
    settle_cloud, settle_why = False, ""
    mt_local, mt_why = True, ""

    def _route_all_local(why: str) -> None:
        nonlocal llm_cloud, llm_why, settle_cloud, settle_why, mt_local, mt_why
        llm_cloud, llm_why = False, why
        settle_cloud, settle_why = False, why
        mt_local, mt_why = True, why

    asr_cfg: dict = {}
    lanes: dict = {}
    db_note = ""
    routing_bad = False
    try:
        import sqlite3

        db_path = paths.app_data_dir() / "bok_voice.db"
        if not db_path.exists():
            db_note = "无设置库(保守=本地)"
        else:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            try:
                row = con.execute(
                    "SELECT asr_json, model_routing_json FROM global_settings"
                    " WHERE id='global'"
                ).fetchone()
            finally:
                con.close()
            if row:
                try:
                    asr_cfg = json.loads(row[0]) if row[0] else {}
                except (ValueError, TypeError):
                    asr_cfg = {}
                if not isinstance(asr_cfg, dict):
                    asr_cfg = {}
                try:
                    routing = json.loads(row[1]) if row[1] else {}
                except (ValueError, TypeError):
                    routing = {}
                    routing_bad = True
                if isinstance(routing, dict):
                    raw_lanes = routing.get("lanes")
                    if isinstance(raw_lanes, dict):
                        lanes = raw_lanes
    except Exception as exc:  # noqa: BLE001 - 启动器不因设置问题崩
        db_note = f"读取失败({exc.__class__.__name__})(保守=本地)"

    def _lane_provider(lane: str) -> str:
        cfg = lanes.get(lane)
        if not isinstance(cfg, dict):
            return "local"
        p = str(cfg.get("provider") or "local").strip() or "local"
        # 未知值按 model_routes._normalize_lane 同款坍缩 local。
        return p if p in ("openai", "local") else "local"

    # ---- ASR 腿（:8787）----
    if os.environ.get("BOK_DOUBAO_ASR", "").strip() == "0":
        asr_cloud, asr_why = False, "BOK_DOUBAO_ASR=0 worker 回退本地"
    else:
        override = os.environ.get("BOK_LOCAL_ASR", "").strip()
        if override == "1":
            asr_cloud, asr_why = False, "BOK_LOCAL_ASR=1 强制本地"
        elif override == "0":
            asr_cloud, asr_why = True, "BOK_LOCAL_ASR=0 强制跳过本地"
        elif db_note:
            asr_cloud, asr_why = False, db_note
        else:
            provider = str(asr_cfg.get("provider") or "").strip().lower()
            if provider in ("doubao", "doubao_asr"):
                new_style = bool(str(asr_cfg.get("api_key") or "").strip())
                old_style = bool(str(asr_cfg.get("app_id") or "").strip()) and bool(
                    str(asr_cfg.get("access_token") or "").strip()
                )
                if new_style or old_style:
                    asr_cloud, asr_why = True, "asr.provider=doubao(云凭据在场)"
                else:
                    asr_cloud, asr_why = False, "asr.provider=doubao 缺凭据(agent 回退本地)"
            else:
                asr_cloud, asr_why = False, f"asr.provider={provider or '未设'}(本地)"

    # ---- LLM 腿（:1235 主 / :1237 settle / mt 车道）----
    if os.environ.get("BOK_MODEL_ROUTING", "").strip() == "0":
        _route_all_local("BOK_MODEL_ROUTING=0 路由忽略(env 缺省链=本地)")
    elif os.environ.get("BOK_LOCAL_LLM", "").strip() == "1":
        _route_all_local("BOK_LOCAL_LLM=1 强制本地")
    elif os.environ.get("BOK_LOCAL_LLM", "").strip() == "0":
        llm_cloud, llm_why = True, "BOK_LOCAL_LLM=0 强制跳过本地"
        settle_cloud, settle_why = True, "BOK_LOCAL_LLM=0 强制跳过本地"
        mt_local = _lane_provider("mt") != "openai"
        mt_why = f"路由 mt={_lane_provider('mt')}"
    elif db_note:
        _route_all_local(db_note)
    elif routing_bad:
        _route_all_local("路由表损坏(保守=本地)")
    else:
        llm_cloud = (
            _lane_provider("a_reply") == "openai"
            and _lane_provider("judge") == "openai"
            and _lane_provider("settle") == "openai"
        )
        llm_why = (
            "路由 a_reply/judge/settle 全 openai"
            if llm_cloud
            else (
                "路由未全云(a_reply="
                f"{_lane_provider('a_reply')}/judge={_lane_provider('judge')}"
                f"/settle={_lane_provider('settle')})"
            )
        )
        settle_cloud = _lane_provider("settle") == "openai"
        settle_why = f"路由 settle={_lane_provider('settle')}"
        mt_local = _lane_provider("mt") != "openai"
        mt_why = f"路由 mt={_lane_provider('mt')}"

    return {
        "asr_cloud": asr_cloud,
        "asr_why": asr_why,
        "llm_cloud": llm_cloud,
        "llm_why": llm_why,
        "settle_cloud": settle_cloud,
        "settle_why": settle_why,
        "mt_local": mt_local,
        "mt_why": mt_why,
    }


def _ensure_only_for_posture(posture: dict, tts_needed: bool) -> set[str]:
    """按姿势计算 `download --only` 收窄集（2026-10-05 demo-cloud wave）。

    云腿对应的模型键不进 ensure（本地盘只落真正需要的权重）；mt_local 才含
    mt；embedding/laya/llm_draft 恒不进——它们是盘上可选件，服务侧「模型在盘
    才起」的既有门控自洽，收窄不改变其缺席语义（缺席=sidecar 跳过一行明示 +
    agent 回退）。键集与当前平台表求交（非 mac 表无 mt/settle 等键时不产生
    幽灵键；表内空 repo 条目 cmd_download 自身跳过）。
    """
    table = models.MODELS["mac"] if paths.is_mac() else models.MODELS["windows"]
    keys = set(table)
    only: set[str] = set()
    if not posture["asr_cloud"]:
        only |= {"asr", "sensevoice"} & keys
    if tts_needed:
        only |= {"tts_preset", "tts_clone"} & keys
    if not posture["llm_cloud"]:
        only |= {"llm", "llm_4b"} & keys
    if not posture["settle_cloud"]:
        only |= {"settle"} & keys
    if posture["mt_local"]:
        only |= {"mt"} & keys
    return only


def _qwen3_tts_sidecar_env(base: dict[str, str]) -> dict[str, str]:
    """TTS sidecar 启动 env：必填键 + `QWEN3_TTS_*` 前缀整族透传（2026-09-28）。

    sidecar 是独立进程，`_FORWARD_ENV`（agent worker 表）不覆盖它——此前两处
    启动点是硬编码最小集，调参键（STREAM_INTERVAL/SPLIT_MAX_CHARS/SILENCE_*
    等）dev 靠 `_start_proc` merge `os.environ` 才生效，prod 封闭 env 结构性
    不可调（`BOK_FLOW_GRAPH`/`_interp_env` 同款教训；xiaozhi 对标调研发现的
    当天新键 `QWEN3_TTS_SPLIT_MAX_CHARS` 正落在死区）。前缀整族透传：新增
    sidecar 调参键自动可达，不用回改本函数。空值不透传（必填键优先）。"""
    env = dict(base)
    # 双模型强制键(第十七波 2026-10-02):TTS sidecar 启动期条件加载的逃生键
    # (registry 空时 clone 不载省 2.9GB;"1"=无条件双载回旧行为)——无 QWEN3_TTS_
    # 前缀,单独透传。
    if os.environ.get("BOK_TTS_BOTH_MODELS", "") != "":
        env.setdefault("BOK_TTS_BOTH_MODELS", os.environ["BOK_TTS_BOTH_MODELS"])
    for key, val in os.environ.items():
        if key.startswith("QWEN3_TTS_") and val != "":
            env.setdefault(key, val)
    return env


def _qwen3_asr_sidecar_env(base: dict[str, str]) -> dict[str, str]:
    """ASR sidecar 启动 env：必填键 + `QWEN3_ASR_*` 前缀整族透传（2026-10-02，
    镜像 `_qwen3_tts_sidecar_env` 先例）。

    sidecar 是独立进程，`_FORWARD_ENV`（agent worker 表）不覆盖它——此前启动点
    是硬编码最小集（MODEL/BACKEND/DEVICE/CONTENTION_YIELD/SV_*），而 sidecar
    实际读 ~25 个 `QWEN3_ASR_*` 键（SAMPLE_RATE/INC_MIN_CPS/PARTIAL_*/
    FINISH_*/TRIM_*/CONFIDENCE…）：调参键 dev 靠 `_start_proc` merge
    `os.environ` 才活，prod 封闭 env 结构性不可调（TTS 先例同款死门）。
    前缀整族透传：新增 sidecar 调参键自动可达，不用回改本函数。空值不透传
    （必填键优先）。BOK_ASR_ENGINE / QWEN3_ASR_DEVICE 仍由启动点显式处理。"""
    env = dict(base)
    for key, val in os.environ.items():
        if key.startswith("QWEN3_ASR_") and val != "":
            env.setdefault(key, val)
    return env


def _apply_mlx_template_fix(llm_py: Path) -> None:
    """mlx_lm 模板生成提示边界归一(幂等,scripts/pipeline/mlx_lm_template_leak_fix.py)。

    模板 endfor 后注释块泄漏换行 → 生成/历史模式边界 token 不一致 → 上一轮
    请求永远不是下一轮缓存前缀,命中坍缩回 system 锚点(2026-09-06 token 级
    探针实证 25→48/78)。runtime site-packages 不入 git,重建后由这里重打;
    失败零阻塞(损失跨轮命中而已)。"""
    script = paths.ROOT / "scripts" / "pipeline" / "mlx_lm_template_leak_fix.py"
    if not script.exists():
        return
    try:
        subprocess.run(
            [str(llm_py), str(script)],
            check=False, capture_output=True, timeout=60,
        )
    except Exception as exc:  # noqa: BLE001 - 补丁失败零阻塞
        print(f"[bok] mlx template fix skipped: {exc!r}", file=sys.stderr)


def _physical_mem_gib() -> float:
    """物理内存 GiB(探测失败回 0.0=按小内存档处理,零风险)。"""
    try:
        # HW_MEMSIZE 名在此 Python os.sysconf 不认(ValueError);SC_PHYS_PAGES
        # ×SC_PAGE_SIZE 是 48GB 机实测可用的 POSIX 路,sysctl 做兜底。
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2**30
    except (ValueError, OSError, AttributeError):
        pass
    try:
        _out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5
        )
        return int(_out.stdout.strip()) / 2**30
    except Exception:  # noqa: BLE001 - 探测失败按小内存档
        return 0.0


def _default_prompt_cache_bytes(draft_on: bool = False) -> str:
    """:1235 prompt-cache-bytes 档位,三级优先级:① BOK_LLM_PROMPT_CACHE_BYTES 显式
    覆盖(专家直设,最高;draft 开时**不折**——用户显式值尊重原样);② draft_on=True
    (draft 模型在场)→ 3.5GB;③ 否则 4GB。

    档位沿革:12GB(2026-09-26 前,全栈 47/48G 占用)→ 6GB(2026-09-26 单通实弹
    无损下调)→ **4GB(2026-09-29 v2 生命周期治本 P1.d,spec §4)**:8 连打生命
    周期探针实测每通 cache 增量 ~0.35-0.45GB,6GB 上限在第 8-10 通打穿进 LRU
    换页(2.93→6.46GB 实测)——统一内存架构下换页期 GPU 可用内存被挤,生成段
    tps 崩至 2.6(call-ed6aa9b8 生成段 10.7s 实证)。4GB=日常 8-10 通工作集内
    零换页;更长连打的换页退化由 LLM_STALL_OBS 观测行盯住(P2.c;换页期 tps
    崩的根治=mlx 侧课题留档)。draft 折扣(2026-09-25):0.6B-4bit 权重+draft
    KV 同池计——开 draft 4GB→3.5GB 腾挪。要回 6GB:env 显式覆盖。"""
    override = os.environ.get("BOK_LLM_PROMPT_CACHE_BYTES", "").strip()
    if override:
        return override
    return "3.5GB" if draft_on else "4GB"


def _settle_cache_bytes() -> str:
    """:1237 prompt-cache-bytes 档位：``BOK_SETTLE_CACHE_BYTES`` 显式覆盖 >
    缺省 4GB（2026-10-02 编排审计第二波——该键此前只活在注释里，argv 恒硬编码
    4GB，注释承诺的「16GB 机型可显式下调」旋钮实际不存在）。与 :1235 的
    ``BOK_LLM_PROMPT_CACHE_BYTES`` 覆盖独立（两进程各自预算）。空/空白=未设。"""
    override = os.environ.get("BOK_SETTLE_CACHE_BYTES", "").strip()
    return override or "4GB"


def _mac_llm_server_argv(
    llm_py: Path,
    llm_model: str,
    mlx_port: str,
    current: dict[str, str],
    log_level: str = "INFO",
    draft_flags: list[str] | None = None,
) -> list[str]:
    """mac mlx_lm server 完整命令行组装(纯函数,离线可单测)。

    入口=同仓 wrapper ``services/llm-mlx/bok_mlx_server.py``（2026-10-01
    W-ABORT；argv 其余逐字节原样透传给 mlx server）。Windows/Linux 的
    llama.cpp 分支不涉及。

    draft 旗标(BOK_LLM_DRAFT=1 且模型在盘,见 _llm_draft_flags)**追加在 argv
    末尾**——关=逐字节同旧命令行(默认档零漂移);开=尾部多
    ``--draft-model <path> --num-draft-tokens 3``。prompt-cache-bytes 随 draft
    开关折档(_default_prompt_cache_bytes)。draft_flags 由调用方预算入参可免
    重复求值(跳过打印打两遍)。"""
    if draft_flags is None:
        draft_flags = models._llm_draft_flags(current)
    cache_bytes = _default_prompt_cache_bytes(draft_on=bool(draft_flags))
    return [
        str(llm_py), str(paths.MLX_SERVER_WRAPPER),
        "--model", llm_model, "--host", "127.0.0.1", "--port", mlx_port,
        "--prompt-cache-size", "128",
        "--prompt-cache-bytes", cache_bytes,
        "--prefill-step-size", "512",
        "--chat-template-args", '{"enable_thinking":false}',
        "--log-level", log_level,
        *draft_flags,
    ]


def _warn_llm_not_http_ready(ports: Sequence[int]) -> None:
    """TCP 健康跳过点的一次性 /v1/models 真话探针（2026-10-02 readiness 真话）：
    mlx 端口先绑后装权重（或代理活着而上游 mlx 半死）时 TCP 探活全绿，serve
    会把「绿着坏」的栈当已起跳过 → 下一通首轮全量冷 prefill 甚至哑火。只打
    警告、**不改跳过语义**（双起风险远大于告警价值；真修复走 down+serve）。"""
    not_ready = [p for p in ports if not health._llm_http_ready(p)]
    if not_ready:
        for p in not_ready:
            print(f"[bok] llm :{p} tcp-up but /v1/models not ready "
                  "(weights loading or half-dead)", file=sys.stderr)


def _start_llm(
    current: dict[str, str], run_dir: Path, log_dir: Path, posture: dict | None = None
) -> None:
    """主 LLM(:1235) 拉起。posture=调用方预算的 `_cloud_posture()` 结果
    （2026-10-05 demo-cloud wave；None 时现场求值）——llm_cloud（路由
    a_reply+judge+settle 全 openai）时整条本地链（mlx/queue proxy）不拉起。"""
    p = posture if posture is not None else _cloud_posture()
    if p["llm_cloud"]:
        print(f"[bok] llm :1235 skipped (cloud: {p['llm_why']}; BOK_LOCAL_LLM=1 强制拉起)")
        return
    # 队列代理拓扑下「健康」= 两级都在（:1235 代理 + :1239 mlx）——只探公网口会
    # 把「代理活着、mlx 死了」的半瘫当健康跳过（2026-09-26 新拓扑配套）。
    # readiness 真话（2026-10-02）：TCP 跳过前补探一次 /v1/models，不就绪大声
    # 告警（不改跳过语义，防双起）。
    if env._llm_queue_proxy_on() and paths.is_mac():
        if core.healthy(1235) and core.healthy(1239):
            _warn_llm_not_http_ready((1235, 1239))
            return
    elif core.healthy(1235):
        _warn_llm_not_http_ready((1235,))
        return
    llm_model = models.model_path({**current, "llm": models.resolve_llm_repo(current)}, "llm")
    if paths.is_mac():
        llm_py = paths.sidecar_python("llm-mlx")
        _apply_mlx_template_fix(llm_py)
    llm_model = models.model_path({**current, "llm": models.resolve_llm_repo(current)}, "llm")
    if paths.is_mac():
        llm_py = paths.sidecar_python("llm-mlx")
        # prompt-cache-size: 默认 10 槽会被 4-6 路并发会话打穿(每请求插入 system/对话/完成
        # 多条前缀键,LRU 轮换把共享前缀挤掉)。M4 48GB 下 4k 前缀 KV 仅 ~134MB,调大纯赚,
        # 让同人设/话术的跨会话前缀缓存命中(实测同前缀重放 1.67s→0.19s)。
        # 16GB 机型可下调,或用 --prompt-cache-bytes 限制缓存总字节。
        # prompt-cache-bytes:给 128 槽加总字节上限——长会话(几十轮×8k ctx)
        # 单槽可涨到几十 MB,不封顶会把统一内存吃穿触发 macOS 压缩/交换,TTFT 抖尖。
        # 档位见 _default_prompt_cache_bytes(env 覆盖+draft 折扣)。
        # draft 旗标先算:cache 档位与打印行都要感知它(开=draft=on 尾标)。
        _draft_flags = models._llm_draft_flags(current)
        _cache_bytes = _default_prompt_cache_bytes(draft_on=bool(_draft_flags))
        _cache_tier = ("explicit" if os.environ.get("BOK_LLM_PROMPT_CACHE_BYTES", "").strip()
                       else "demo_preset" if os.environ.get("BOK_DEMO_PRESET", "") == "1"
                       else "mem")
        if _draft_flags:
            print(f"[bok] llm prompt-cache {_cache_bytes} (tier={_cache_tier}, draft=on)")
        else:
            print(f"[bok] llm prompt-cache {_cache_bytes} (tier={_cache_tier})")
        # prefill-step-size 512(官方默认 2048,2026-09-08 二分实证从 1024 再降):
        # 暖缓存 TTFT 中位 913/917ms vs 1024 的 1066/1092ms(双轮反向 A/B,增量轮
        # 尾段一步喂完少等半步),并发交错打平——纯赚。
        # log-level INFO(旧 WARNING):延迟调试要读 mlx 请求/prompt-cache 命中行
        # (llm.log);dev/mac serve 路径专用,生产 launchd 单元不从这里起 :1235,
        # 可用 BOK_LLM_LOG_LEVEL 回 WARNING。
        llm_log_level = os.environ.get("BOK_LLM_LOG_LEVEL", "INFO")
        # 队列代理拓扑（2026-09-26 根治 mlx 解码争用,默认开）：mlx 挪内部 :1239、
        # queue_proxy 占公网口 :1235（生成单并发+reply 插队;BOK_LLM_QUEUE_PROXY=0
        # 回旧拓扑 mlx 直跑 :1235）。消费方（agent/CP/judge）env 一律 :1235 不动。
        # draft 兼容已核实（2026-09-25 读码）：queue_proxy 透传原始 body 仅换
        # content-type/x-bok-lane 头、不剥任何字段;draft 是 server 启动旗标
        # （mlx 启动时装载 draft 模型）而非 body 参数——代理零改动。
        _queue_on = env._llm_queue_proxy_on()
        _mlx_port = "1239" if _queue_on else "1235"
        _start_proc(
            _mac_llm_server_argv(llm_py, llm_model, _mlx_port, current,
                                 log_level=llm_log_level, draft_flags=_draft_flags),
            run_dir / "llm.pid",
            log_dir / "llm.log",
            env=_mlx_hf_offline_env(),
        )
        if _queue_on:
            print("[bok] llm queue proxy :1235 -> mlx :1239 (reply lane priority)")
            _start_proc(
                [str(paths.repo_python()), str(paths.ROOT / "services" / "llm-mlx" / "queue_proxy.py")],
                run_dir / "llm-proxy.pid",
                log_dir / "llm-proxy.log",
                env={"BOK_LLM_QUEUE_UPSTREAM": "http://127.0.0.1:1239",
                     "BOK_LLM_QUEUE_HOST": "127.0.0.1", "BOK_LLM_QUEUE_PORT": "1235"},
            )
        return
    # 非 mac（Windows/Linux）：llama.cpp 后端（GPU 必选；无 GPU 由 doctor 门禁阻止）。
    # Linux 档（2026-09-20 Ubuntu 节点）：runtime/llama/llama-server 或 PATH 提供。
    llama_bin = paths.bundled_llama() or core.shutil_which("llama-server")
    if not llama_bin:
        print("[bok] llama-server 不可用（需 GPU；Windows 打包内嵌 CUDA 版 / "
              "Linux 需 runtime/llama/llama-server 或 PATH 安装 llama.cpp）", file=sys.stderr)
        return
    # 旗标升级（2026-09-24 CUDA 实机标定）：+fa on（4090 实测必需）、+cache-reuse
    # 256（KV 前缀复用——ContextAwareLLM 严格前缀纪律的 llama.cpp 平移；实测
    # 后续轮 prefill 只算增量 3400tok/s）、-rea off（Qwen3.5 thinking 模型默认
    # 思考会把全部 token 烧在 reasoning_content——CUDA 首部署实证）。
    # **槽上下文铁律（2026-09-25 二轮实弹）**：llama.cpp -np 并发槽均分 -c 总量
    # ——旧 -c 8192 / -np 6 = 每槽 1365，真实通话第 2 轮即 exceed_context_size
    # 400→LLM_FALLBACK 直念兜底。改为 -c 30720 / -np 3 = 每槽 10240（补偿类
    # 长模板第 7 轮 prompt 实测 5131 token；KV q8_0 显存增量 ~GB 级可承受）。
    _start_proc(
        [str(llama_bin),
         "--jinja", "--chat-template-kwargs", '{"enable_thinking":false}',
         "--n-gpu-layers", "all",
         "-fa", "on",
         "--cache-reuse", "256",
         "-np", "3",
         "-rea", "off",
         "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
         "--ctx-size", "30720",
         "--host", "127.0.0.1", "--port", "1235",
         "-m", llm_model],
        run_dir / "llm.pid",
        log_dir / "llm.log",
    )


def _mlx_hf_offline_env() -> dict[str, str]:
    """mlx server 全家离线档（2026-10-01 第十三波）：模型恒本地绝对路径，hub 元数据
    探测纯属浪费——call-231aa92a 窗口 settle-llm.log INFO 实证重启后首请求先去
    huggingface.co 查 revision（401 匿名限流）再冷缓存，首请求 3.5-3.9s 的直接
    组分。本地缺件时离线档让它大声失败（INFO 日志可见）而非静默网络等待。
    `download` 车道不走本 env（bootstrap 照常联网拉模型）。残迹观察位：huihui-9B
    首请求仍见过一次 revision 查询——transformers 系 tokenizer 装载认
    TRANSFORMERS_OFFLINE 不认 HF_HUB_OFFLINE,两旗都给;INFO 日志盯下一次。"""
    return {
        "HF_HUB_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "TRANSFORMERS_NO_ADVISORY_WARNINGS": "1",
    }


def _start_mt_llm(current: dict[str, str], run_dir: Path, log_dir: Path, posture: dict | None = None) -> bool:
    """B 线同传翻译 LLM(:1236,Hy-MT2 小模型):与主 LLM 分进程,prefill 互不挤占。

    可选服务:模型缺失/未下载直接跳过并返回 False(等待方不收 1236,B 线
    interpret 回退主 LLM :1235);端口已健康(serve 重试/生产档拉起)不重复起。
    返回 True = 预期 :1236 会就绪。

    posture=调用方预算的 ``_cloud_posture()`` 结果（2026-10-08 补闸——mt 车道
    openai 云档却仍因「模型在盘」起 :1236:主树白白常驻一颗闲置本地模型违全云
    指令,worktree/干净树缺 sidecar venv 直接 FileNotFoundError 打死 serve）。
    mt_local=False 时整条跳过（BOK_LOCAL_LLM=1 强制回本地档,闸在 posture 里）。
    """
    p = posture if posture is not None else _cloud_posture()
    if not p["mt_local"]:
        print(f"[bok] mt :1236 skipped (cloud: {p['mt_why']}; BOK_LOCAL_LLM=1 强制拉起)")
        return False
    if core.healthy(1236):
        return True
    mt_model = models._mt_llm_model(current)
    if not mt_model or not Path(mt_model).exists():
        print(f"[bok] mt model not present, skip :1236 ({mt_model or 'unset'})", file=sys.stderr)
        return False
    llm_py = paths.sidecar_python("llm-mlx")
    _apply_mlx_template_fix(llm_py)
    # 逐句无状态 MT:请求前缀只有模板头一条,32 槽 prompt cache 足够;Hy-MT2
    # 自带非思考对话模板,不传 --chat-template-args(主 LLM 的关思考参数不通用)。
    # 入口=wrapper（W-ABORT；B 线取消/打断流同享 abort）。
    _start_proc(
        [str(llm_py), str(paths.MLX_SERVER_WRAPPER),
         "--model", mt_model, "--host", "127.0.0.1", "--port", "1236",
         "--prompt-cache-size", "32", "--log-level", "WARNING"],
        run_dir / "mt-llm.pid",
        log_dir / "mt-llm.log",
        env=_mlx_hf_offline_env(),
    )
    return True


def _start_settle_proxy(run_dir: Path, log_dir: Path) -> bool:
    """9B 前门闸 :1238→:1237（2026-10-03 I1,与 :1235 代理同码同姿势）。

    queue proxy 拓扑关 → 不起（旧形状裸 :1237）；:1238 已健康 → 幂等跳过
    （_start_settle_llm 的 healthy 早退路径也要捞一把,防「9B 在跑但闸没起
    →serve 等 1238 假死」）。返回 True=闸在位（新起或已健康）。
    """
    if not env._llm_queue_proxy_on():
        return False
    if core.healthy(1238):
        return True
    print("[bok] settle queue proxy :1238 -> 9B :1237 (reply lane priority)")
    _start_proc(
        [str(paths.repo_python()), str(paths.ROOT / "services" / "llm-mlx" / "queue_proxy.py")],
        run_dir / "settle-proxy.pid",
        log_dir / "settle-proxy.log",
        env={"BOK_LLM_QUEUE_UPSTREAM": "http://127.0.0.1:1237",
             "BOK_LLM_QUEUE_HOST": "127.0.0.1", "BOK_LLM_QUEUE_PORT": "1238"},
    )
    return True


def _start_settle_llm(
    current: dict[str, str], run_dir: Path, log_dir: Path, posture: dict | None = None
) -> bool:
    """后台重活专线 LLM(:1237,9B):settle 纪要/知识蒸馏与 flow judge 指到这颗。

    可选服务(同 _start_mt_llm 契约):模型缺失直接跳过返回 False——Summarizer
    与 judge 走各自 env 缺席链路回退 :1235;端口已健康不重复起。Qwen3.5 家族
    与主 LLM 同模板参数(关思考);log WARNING(后台作业,唔刷屏)。

    posture=调用方预算的 `_cloud_posture()` 结果（2026-10-05 demo-cloud wave；
    None 时现场求值）——settle_cloud（路由 settle=openai）时 :1237 连同 :1238
    前门闸整条不拉起（keep：BOK_DEV_9B 闸仍在其后）。

    9B 后端化(2026-09-25):默认不随栈常驻——9B 常驻=夜间崩速主犯之一
    (reports/latency-soak/LANE-AB-2026-09-25.md 附3:judge 9B 二号驻留与回复
    4B(:1235) 共挤统一内存,swap 满 + 闲置权重页换出,in-call tps 4-12 vs 隔离
    43-47)——该前提已被 P1(ASR 迁 CPU,MPS 只剩 LLM)拆除。2026-10-01 P2 翻档:
    :1237=Huihui-9B **a_reply 专线**(暖态 TTFT 175ms,soak p50 912ms/0
    fallback),默认随栈(模型在盘);BOK_DEV_9B=0 显式关=旧形状(judge/settle
    回退 :1235)。
    """
    p = posture if posture is not None else _cloud_posture()
    if p["settle_cloud"]:
        print(
            f"[bok] settle lane :1237 skipped (cloud: {p['settle_why']}; BOK_LOCAL_LLM=1 强制拉起)",
            file=sys.stderr,
        )
        return False
    if not env._dev_9b_enabled():
        print("[bok] 9B lane off (BOK_DEV_9B=0) — skip :1237 (a_reply 车道/judge/settle 回退 :1235)", file=sys.stderr)
        return False
    if core.healthy(1237):
        _start_settle_proxy(run_dir, log_dir)  # 幂等:9B 已跑也要捞闸(serve 会等 1238)
        return True
    settle_model = models._settle_llm_model(current)
    if not settle_model or not Path(settle_model).exists():
        print(f"[bok] settle model not present, skip :1237 ({settle_model or 'unset'})", file=sys.stderr)
        return False
    llm_py = paths.sidecar_python("llm-mlx")
    _apply_mlx_template_fix(llm_py)
    # log-level INFO(2026-10-01 第十二波,call-231aa92a 取证需求):9B 已是 a_reply
    # 主脑,槽位占用归因(排队的 35s TTFT 类事故)要读 mlx 请求/prompt-cache 命中行
    # (settle-llm.log);BOK_LLM_LOG_LEVEL=WARNING 回静默(与 :1235 同一旋钮)。
    _log_level = os.environ.get("BOK_LLM_LOG_LEVEL", "INFO")
    # 入口=wrapper（W-ABORT；被打断/取消的 9B 生成立即放槽=打断级联根治点）。
    # prompt-cache-size 128(2026-10-02 十八波,P2 角色翻档补课)::1237 已是 a_reply
    # 主脑,但 cache 槽停留在后台作业年代的 8——soak 实弹 13:00:16 cache 打满
    # (8 sequences)LRU 逐出回复链,R8 3312 tok 全量重 prefill 10.6s 独占单生成
    # 线,后续轮 1.1-1.3k tok 连环全量 miss 各 ~4s=PERCEIVED p95 5622 的全部来源
    # (:1235 同病灶同修,见其注释:每请求插 system/对话/完成 多条前缀键,LRU
    # 轮换把共享前缀挤掉)。
    # bytes 2GB→4GB(2026-10-02 实机验证波):FLOW20 连跑两通时 2GB 帽逐出断崖
    # (Prompt Cache 20 seq/2.70GB→15 seq/1.75GB 实录)——run2 的 R1 前缀被逐,
    # 2948 tok 全量 prefill 16s(与 28 tok 装配 prewarm 同线程交替)=首 token
    # 3s 超时→fallback→LATE_ANSWER 坏标记链。与 :1235 同档 4GB;十八波「2GB=
    # 内存预算真闸门」的判断基于云 TTS 前的全栈 27GB 常驻年代,现云姿态栈
    # ~12GB+9B 权重 5.3GB,4GB cache 总预算充裕。
    # BOK_SETTLE_CACHE_BYTES 做实（2026-10-02 审计）：档位=env 覆盖>缺省 4GB
    # （16GB 机型显式下调；与 :1235 的 BOK_LLM_PROMPT_CACHE_BYTES 独立），
    # tier 打点镜像 :1235 的 explicit/default 先例。
    _cache_bytes = _settle_cache_bytes()
    _cache_tier = ("explicit" if os.environ.get("BOK_SETTLE_CACHE_BYTES", "").strip()
                   else "default")
    print(f"[bok] settle prompt-cache {_cache_bytes} (tier={_cache_tier})")
    _start_proc(
        [str(llm_py), str(paths.MLX_SERVER_WRAPPER),
         "--model", settle_model, "--host", "127.0.0.1", "--port", "1237",
         "--prompt-cache-size", "128", "--prompt-cache-bytes", _cache_bytes,
         "--chat-template-args", '{"enable_thinking":false}', "--log-level", _log_level],
        run_dir / "settle-llm.pid",
        log_dir / "settle-llm.log",
        env=_mlx_hf_offline_env(),
    )
    _start_settle_proxy(run_dir, log_dir)
    return True


def _start_laya(current: dict[str, str], run_dir: Path, log_dir: Path) -> bool:
    """Laya 决策 sidecar(:8791,意图/流程判定 10ms 快路):独立 FastAPI sidecar,
    与 asr/tts/embed 同族(非 agent worker,down 走 run/*.pid 全局收割,prod 单元
    面不列——照 embed 的可选增强姿势)。

    双闸(2026-09-26,2026-09-27 默认翻启):``BOK_LAYA_JUDGE`` 默认 "1"=随栈拉起
    (模型在盘才起);显式 ="0" 才关。**2026-09-25 起两把闸任一打开即拉起**
    (BOK_LAYA_JUDGE=意图判定车道 / BOK_LAYA_QA=QA 验证车道——sidecar 是共享的,
    两车道独立开关节省一次 690MB 驻留)。sidecar 进程自身闸只拦显式
    BOK_LAYA_JUDGE=0(未设=开),503 姿势不变;agent 侧两闸各自独立三保险。
    模型缺失/venv 缺席跳过并留一行明示(agent 走原 9B judge 回落链)。
    端口注::8789 是 embed sidecar 既定端口,本服务用家族下一空位 8791。
    """
    if (
        os.environ.get("BOK_LAYA_JUDGE", "1") != "1"
        and os.environ.get("BOK_LAYA_QA", "0") != "1"
    ):
        print(
            "[bok] laya off (BOK_LAYA_JUDGE!=1 and BOK_LAYA_QA!=1) — skip :8791 "
            "(intent judges fall back to 9B lane; QA lane falls back to QA_SEM)",
            file=sys.stderr,
        )
        return False
    if core.healthy(8791):
        return True
    laya_py = paths.sidecar_python("laya-sidecar")
    laya_model = models.laya_model_path(current)
    if not laya_py.exists() or not laya_model:
        print(f"[bok] laya model/sidecar not present, skip :8791 ({laya_model or 'unset'})", file=sys.stderr)
        return False
    _start_proc(
        [str(laya_py), "-m", "uvicorn", "app:app", "--app-dir", "services/laya-sidecar",
         "--host", "127.0.0.1", "--port", "8791"],
        run_dir / "laya.pid",
        log_dir / "laya.log",
        env={"LAYA_MODEL_DIR": laya_model},
    )
    return True


def _start_call_plane(py) -> bool:
    """通话面拉起（LiveKit + 三个 agent worker + 常驻监控），serve/node 全栈共用。

    2026-09-20 Ubuntu 节点补齐：本块原只活在 dev `serve` 内联——node_agent 经
    cmd_up 拉全栈时 LiveKit/worker 缺位，节点装完打不了电话。提取为单点后
    serve 与 cmd_up 同源（幂等：health 门保证重复调用不叠进程）。
    返回 LiveKit 是否就绪；未就绪不拉 worker，由调用方就绪等待如实失败。
    """
    run_dir = paths.app_data_dir() / "run"
    log_dir = paths.app_data_dir() / "logs"
    livekit_cfg = paths._livekit_config_path()
    livekit_bin = (
        paths._embedded_livekit()
        or core.shutil_which("livekit-server")
        or (paths.ROOT / "services" / "livekit-server" / "livekit-server")
    )
    if not core.healthy(7880) and livekit_bin and Path(livekit_bin).exists():
        _start_proc(
            [str(livekit_bin), "--config", str(livekit_cfg)],
            run_dir / "livekit.pid",
            log_dir / "livekit.log",
        )
    _lk_respawned = False
    _lk_deadline = time.monotonic() + 30.0
    while not core.healthy(7880):
        if time.monotonic() >= _lk_deadline:
            if livekit_bin and Path(livekit_bin).exists() and not _lk_respawned:
                _start_proc(
                    [str(livekit_bin), "--config", str(livekit_cfg)],
                    run_dir / "livekit.pid",
                    log_dir / "livekit.log",
                )
                _lk_respawned = True
                _lk_deadline = time.monotonic() + 15.0
                print("[bok] livekit went down during startup — respawned a fresh instance")
                continue
            print("[bok] livekit :7880 not ready — agent workers NOT started", file=sys.stderr)
            return False
        time.sleep(0.5)
    for _spec in _worker_specs(py):
        if not core.healthy(_spec["port"]):
            _start_proc(_spec["argv"], _spec["pidfile"], _spec["logfile"], env=_spec["env"])
        else:
            print(
                f"[bok] {_spec['name']} worker already listening :{_spec['port']}"
                " (run bok.py down first to restart it)"
            )
    # 常驻监控环：LiveKit 重启后 worker 注册全丢（历史实证「no worker is available」
    # 连 4 通 0 轮），serve 一次性起完没人补拉——monitor 做 down→up 全量 respawn。
    proc._ensure_monitor(py)
    return True


def _cmd_up_services(models_only: bool = False) -> int:
    run_dir = paths.app_data_dir() / "run"
    log_dir = paths.app_data_dir() / "logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    # 云端演示档姿势（2026-10-05 demo-cloud wave）：TTS 门控与云姿势判定先于
    # ensure/拉起——模型只 ensure 真正需要的、云腿端口（:8787/:1235/:1237）
    # 不拉起、就绪等待随之收窄。全本地形状逐字节同旧（零漂移铁律）。
    tts_needed, tts_why = _local_tts_needed()
    posture = _cloud_posture()
    print("[bok] ensuring models…")
    # cmd_download W③ 起住 bokctl.commands.download(模型 ensure 单点不变)。
    if (
        not posture["asr_cloud"]
        and tts_needed
        and not posture["llm_cloud"]
        and not posture["settle_cloud"]
    ):
        # 全本地形状:整表 ensure 逐字节同旧(零漂移铁律)。
        commands.download.cmd_download()
    else:
        only = _ensure_only_for_posture(posture, tts_needed)
        print(
            "[bok] cloud posture: "
            f"asr={'cloud' if posture['asr_cloud'] else 'local'}"
            f" tts={'local' if tts_needed else 'cloud'}"
            f" llm={'cloud' if posture['llm_cloud'] else 'local'}"
            f" mt={'local' if posture['mt_local'] else 'cloud'}"
            f" settle={'cloud' if posture['settle_cloud'] else 'local'}"
            f" — ensure narrowed: {sorted(only)}"
            " (embed/laya/draft=盘上可选,不进 ensure;服务侧模型在盘才起)"
        )
        # 空收窄集（全云姿势且 mt 也走云）必须整支跳过：cmd_download(only=set())
        # 的 `if only else None` 语义会把空集当「整表」，反向全量下载。
        if only:
            commands.download.cmd_download(only=only)
    print("[bok] starting services…")

    current = models.MODELS["mac"] if paths.is_mac() else models.MODELS["windows"]
    asr_py = paths.sidecar_python("qwen3-asr-sidecar")
    tts_py = paths.sidecar_python("qwen3-tts-sidecar")
    # 云端演示档(2026-10-05 真栈实弹发现):sidecar venv 存在性检查只对「本姿势
    # 真要拉起」的 sidecar 生效——云 ASR 档不起 :8787 就不要求其 venv 在盘
    # (否则演示新机/干净 worktree 在全云档被一个用不上的 venv 卡 exit 2)。
    _missing = []
    if not posture["asr_cloud"] and not asr_py.exists():
        _missing.append(("asr", asr_py))
    if tts_needed and not tts_py.exists():
        _missing.append(("tts", tts_py))
    if _missing:
        for _which, _p in _missing:
            print(f"[bok] sidecar python missing ({_which}): {_p}", file=sys.stderr)
        print(
            "[bok] sidecar pythons missing — run setup"
            " (./scripts/bootstrap.sh; node 节点机=scripts/install-node.sh)",
            file=sys.stderr,
        )
        return 2

    asr_model = models.model_path(current, "asr")
    tts_preset = models.model_path(current, "tts_preset")
    tts_clone = models.model_path(current, "tts_clone")
    asr_backend = "mlx" if paths.is_mac() else "transformers"
    tts_backend = "mlx" if paths.is_mac() else "transformers"

    asr_env = {"QWEN3_ASR_MODEL": asr_model, "QWEN3_ASR_BACKEND": asr_backend}
    if not paths.is_mac():
        # Windows/transformers 后端才需要 device 指定;mac mlx 分支不读该 env(MLX 默认走 Metal)。
        asr_env["QWEN3_ASR_DEVICE"] = "cuda" if core._cuda() else "cpu"
    # ASR 并发竞态让位(2026-10-01 双通实弹):重活在别人在飞时降档——sidecar
    # 进程不吃全 env 面,prod 封闭面显式透传;缺省=sidecar 内默认开。
    # P1 SV 车道键同路透传(SV_LANGUAGE/SV_THREADS 调参口)。
    for _k in ("QWEN3_ASR_CONTENTION_YIELD", "QWEN3_ASR_SV_LANGUAGE", "QWEN3_ASR_SV_THREADS"):
        _v = os.environ.get(_k)
        if _v is not None:
            asr_env[_k] = _v
    # P1 引擎档进 sidecar（2026-10-01 任务 B）：sidecar 据此决定启动是否 eager
    # 加载 Qwen3-1.7B GPU 权重——sensevoice 档（含缺省/未设，与 agent
    # `_asr_engine_from_cfg` 缺省对齐）纯 CPU 车道不加载 GPU 权重（~1.9GB 纯占
    # 卡）；显式 BOK_ASR_ENGINE=qwen3 回滚档照旧 eager，sidecar 行为零变化。
    _asr_engine = os.environ.get("BOK_ASR_ENGINE", "").strip()
    if _asr_engine:
        asr_env["BOK_ASR_ENGINE"] = _asr_engine
    # P1 SV-CPU 引擎:模型目录下发。ONNX 布局无 config.json(model_path 判据
    # 不适用),专用解析:download 落位(app-data/models/<repo-->)有
    # model.int8.onnx+tokens.txt 即用;缺席不下发=sidecar 用自身缺省/fail-open。
    _sv_repo = current.get("sensevoice", "")
    if _sv_repo:
        _sv_dir = models.model_dir(_sv_repo)
        if (_sv_dir / "model.int8.onnx").is_file() and (_sv_dir / "tokens.txt").is_file():
            asr_env["QWEN3_ASR_SV_MODEL_DIR"] = str(_sv_dir)
    if not core.healthy(8787):
        if posture["asr_cloud"]:
            # 云端演示档（2026-10-05）：worker 装配吃豆包云 ASR，本地 Qwen3-ASR
            # sidecar 不拉起（1.9GB+ 权重零消费）；BOK_LOCAL_ASR=1 是显式回拉口。
            print(
                f"[bok] asr sidecar :8787 skipped (cloud: {posture['asr_why']};"
                " BOK_LOCAL_ASR=1 强制拉起)"
            )
        else:
            _start_proc(
                [str(asr_py), "-m", "uvicorn", "app:app", "--app-dir", "services/qwen3-asr-sidecar",
                 "--host", "127.0.0.1", "--port", "8787"],
                run_dir / "asr.pid", log_dir / "asr.log",
                # 前缀整族透传（2026-10-02）：sidecar 专属调参键 prod 封闭 env 可达
                # （TTS `_qwen3_tts_sidecar_env` 先例；BOK_ASR_ENGINE/DEVICE 已在上
                # 方显式进 asr_env）。
                env=_qwen3_asr_sidecar_env(asr_env),
            )
    if tts_needed and not core.healthy(8788):
        # 清残留：serve 重试可能叠加多个卡死的 TTS 进程，先按 pidfile 收掉。
        _stop_pidfile(run_dir / "tts.pid")
        _start_proc(
            [str(tts_py), "-m", "uvicorn", "app:app", "--app-dir", "services/qwen3-tts-sidecar",
             "--host", "127.0.0.1", "--port", "8788"],
            run_dir / "tts.pid", log_dir / "tts.log",
            env=_qwen3_tts_sidecar_env({
                "QWEN3_TTS_PRESET_MODEL": tts_preset, "QWEN3_TTS_CLONE_MODEL": tts_clone,
                "QWEN3_TTS_BACKEND": tts_backend,
                # 语音克隆注册数据（voice_registry + 参考音频）落 app-data，bundle 只读/可升级。
                "QWEN3_TTS_DATA_DIR": str(paths.app_data_dir() / "tts-data"),
                # 打包模式跳过 warmup：首启偶发卡死在参考音频读取/冷编译，
                # 跳过只损失首包 1-2s，换取启动不被阻塞（开发模式保留 warmup）。
                "QWEN3_TTS_WARMUP": "0" if paths.is_packaged() else os.environ.get("QWEN3_TTS_WARMUP", "1")}),
        )
    elif not tts_needed:
        print(f"[bok] tts sidecar :8788 skipped (cloud-only: {tts_why}; BOK_LOCAL_TTS=1 强制拉起)")

    _start_llm(current, run_dir, log_dir, posture)
    want_mt = _start_mt_llm(current, run_dir, log_dir, posture)
    want_settle = _start_settle_llm(current, run_dir, log_dir, posture)
    # W1b embedding sidecar(:8789,bge-m3 MLX):意图语义车道可选增强——镜像
    # mt/settle 的「模型在盘才起」姿势;venv/模型/端口三缺一即跳过,agent 装配
    # 面降级闩自动关语义车道。W1b 只做 mac-mlx 形态,windows 表无 embedding 键。
    want_embed = False
    if paths.is_mac() and current.get("embedding"):
        embed_py = paths.sidecar_python("bge-embed-sidecar")
        embed_model = models.model_path(current, "embedding")
        if embed_py.exists() and models._usable_model_dir(Path(embed_model)):
            if not core.healthy(8789):
                _start_proc(
                    [str(embed_py), "-m", "uvicorn", "app:app", "--app-dir", "services/bge-embed-sidecar",
                     "--host", "127.0.0.1", "--port", "8789"],
                    run_dir / "embed.pid", log_dir / "embed.log",
                    env={
                        "BGE_EMBED_MODEL": embed_model,
                        # 打包模式跳过暖机(TTS 同款):启动不被阻塞,代价=首个
                        # 请求 1-2s 冷加载;dev 保留暖机让 /health 就绪=真就绪。
                        "BGE_EMBED_WARMUP": "0" if paths.is_packaged() else os.environ.get("BGE_EMBED_WARMUP", "1"),
                    },
                )
            want_embed = True
        else:
            print(f"[bok] embed model/sidecar not present, skip :8789 ({embed_model or 'unset'})", file=sys.stderr)

    # Laya 决策 sidecar(:8791,2026-09-26;2026-09-27 默认翻启):BOK_LAYA_JUDGE
    # 默认 "1"、模型+venv 在盘才起(缺席一行明示跳过;=0 显式关;agent 侧
    # fail-open=缺 sidecar 走原 9B judge 回落链,行为零退化)。
    want_laya = _start_laya(current, run_dir, log_dir)

    # B-line v1 Node worker (:8790) 已退役（2026-10-02，B 线 v2 双 Python worker 为唯一翻译链）。

    print("[bok] waiting for services…")
    # mt(:1236)/settle(:1237)/embed(:8789)/laya(:8791)仅在确实拉起时纳入等待;主栈端口照旧。
    # :8788 同理（2026-09-27 全云端门控）——跳过时不等待也不进重启兜底。
    # 云腿（:8787/:1235，2026-10-05 demo-cloud）按姿势收窄——跳过时不等待也不进
    # 宽松终检缺口（镜像 tts_needed 模式）。
    asr_skip = posture["asr_cloud"]
    llm_skip = posture["llm_cloud"]
    core_ports = (
        ((8787,) if not asr_skip else ())
        + ((1235,) if not llm_skip else ())
        + ((8788,) if tts_needed else ())
    )
    asr_ready = "asr=skipped(cloud)" if asr_skip else "asr=8787"
    llm_ready = "llm=skipped(cloud)" if llm_skip else "llm=1235"
    tts_ready = "tts=8788" if tts_needed else "tts=skipped(cloud-only)"
    targets = (
        core_ports
        + ((1236,) if want_mt else ())
        + ((1237,) if want_settle else ())
        + ((1238,) if want_settle and env._llm_queue_proxy_on() else ())
        + ((8789,) if want_embed else ())
        + ((8791,) if want_laya else ())
    )
    mt_ready_suffix = " mt=1236" if want_mt else ""
    mo_suffix = " (models-only)" if models_only else ""
    for _ in range(180):
        if all(core.healthy(p) for p in targets):
            print(f"[bok] ready: {asr_ready} {tts_ready} {llm_ready}{mt_ready_suffix}{mo_suffix}")
            return 0
        time.sleep(1)
    # TTS 首启偶发卡死在 MLX 模型加载/暖机（观察：与 LLM/ASR 同启时概率出现，
    # 单独重启几乎必然成功）。兜底：停掉后单独再拉起一次，再等 120s。
    if tts_needed and not core.healthy(8788):
        print("[bok] tts not healthy — restarting once (alone)", flush=True)
        _stop_pidfile(run_dir / "tts.pid")
        tts_py = paths.sidecar_python("qwen3-tts-sidecar")
        tts_preset = models.model_path(
            models.MODELS["mac"] if paths.is_mac() else models.MODELS["windows"], "tts_preset")
        tts_clone = models.model_path(
            models.MODELS["mac"] if paths.is_mac() else models.MODELS["windows"], "tts_clone")
        tts_backend = "mlx" if paths.is_mac() else "transformers"
        _start_proc(
            [str(tts_py), "-m", "uvicorn", "app:app", "--app-dir", "services/qwen3-tts-sidecar",
             "--host", "127.0.0.1", "--port", "8788"],
            run_dir / "tts.pid", log_dir / "tts.log",
            env=_qwen3_tts_sidecar_env({
                "QWEN3_TTS_PRESET_MODEL": tts_preset, "QWEN3_TTS_CLONE_MODEL": tts_clone,
                "QWEN3_TTS_BACKEND": tts_backend,
                "QWEN3_TTS_DATA_DIR": str(paths.app_data_dir() / "tts-data"),
                "QWEN3_TTS_WARMUP": "0" if paths.is_packaged() else os.environ.get("QWEN3_TTS_WARMUP", "1")}),
        )
        for _ in range(120):
            if core.healthy(8788):
                break
            time.sleep(1)
    if all(core.healthy(p) for p in targets):
        print(f"[bok] ready (after tts restart): {asr_ready} {tts_ready} {llm_ready}{mt_ready_suffix}")
        return 0
    if want_mt and all(core.healthy(p) for p in core_ports) and not core.healthy(1236):
        # MT 是可选增强:主栈齐而独缺 mt 不拖垮整栈(B 线 interpret 回退主 LLM)。
        print("[bok] mt llm :1236 not ready — continue (B-line falls back to :1235)", file=sys.stderr)
        return 0
    # 宽松终检（2026-09-19 互杀事故收编）：宿主 CPU 风暴下 1s TCP 探测可整轮
    # 假死，180s 走完≠服务真死——宣判超时前逐口 5s 复检，全绿即 ready；仍有
    # 真死端口才退出并列出缺口（便于排障）。退出会留下加载中的子代给下一轮
    # serve 的孤儿清扫当孤儿杀（互杀循环根因），能不退就不退。
    still_down = health._ports_down_after_grace(targets)
    if not still_down:
        print(f"[bok] ready (relaxed recheck): {asr_ready} {tts_ready} {llm_ready}{mt_ready_suffix}")
        return 0
    if health._only_optional_ports(still_down):
        # 可选线豁免与上方 1s 档的 MT 语义对齐:宽松终检只剩可选缺口也放行
        # (B 线回退主 LLM/settle 回退 :1235),唔令 serve 在 agent worker 拉起前
        # 退出——评审:宽松路径原先漏掉这层豁免,会假超时退出留下加载中子代。
        print(f"[bok] optional llm ports still down: {still_down} — continue (falls back)", file=sys.stderr)
        return 0
    print(f"[bok] timeout waiting for services — still down: {still_down} (see app-data/logs)", file=sys.stderr)
    return 1


def _repo_web_modules() -> Path:
    return paths.ROOT / "apps" / "web" / "node_modules"


def _realtime_demo_enabled() -> bool:
    """演示档 worker 随栈开关（opt-in）：BOK_QWEN_REALTIME="1" 才拉起 :8084。

    默认关——演示档整通走云端 S2S（按分钟真金计费），不是每套栈都该常驻一个
    空转 worker；要试演示在启动环境设 BOK_QWEN_REALTIME=1（凭据 QWEN_REALTIME_KEY
    经 _FORWARD_ENV 透传）。worker 手工直起不受此门（realtime_demo.py 自身只在
    ="0" 时拒接 job）。"""
    return os.environ.get("BOK_QWEN_REALTIME", "") == "1"


def _interp_lite_enabled() -> bool:
    """B 线薄线随栈开关（opt-in，2026-10-09 interp-lite 试点）：BOK_INTERP_LITE="1"
    时 interp-fwd/rev 两 worker 改拉 ``agent_runtime.interp_lite.worker``。

    serve 侧开关（BOK_LOCAL_TTS 先例，不进 _FORWARD_ENV——worker 不读它，bokctl
    读）；端口/agent_name/健康面与旧线同槽位（8082/8083、bok-interp-fwd/rev），CP/
    前端零感知。切换=重启栈（试点期例外，蓝图 docs/superpowers/plans/
    2026-10-09-interp-lite.md §7）；默认 0=旧线逐字节。"""
    return os.environ.get("BOK_INTERP_LITE", "") == "1"


def _interp_worker_module() -> str:
    return (
        "agent_runtime.interp_lite.worker"
        if _interp_lite_enabled()
        else "agent_runtime.interpret"
    )


def interp_lite_commit_env(base: dict) -> dict:
    """薄线句档提交缺省（W6×interp-lite 合流刀1，2026-10-09；纯函数，单测直喂）。

    W6 立项档（docs/superpowers/plans/2026-10-09-bline-fluency.md）60 条 INTERP_LAG
    分布定案：病=碎片化串行（src_chars p50=10 字、段间天窗复利），不是 MT 腿——
    旧线 B 档「逗号 6 字/限速 1.0s」把语流切成 2 秒碎片。本函数给**薄线 worker**
    注入句档缺省（翻译单元=句末标点，出声即连续长段）：

    - ``QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS=999``：逗号（次级标点）档结构性关闭，
      只认句末标点（。！？；?!，强档另有 ≥6 字门不受此键影响）；
    - ``QWEN3_ASR_CLAUSE_LEN_CHARS=30``：连续无标点语流 30 内容字保险丝（实弹
      定档 2026-10-09：30 字档 seg_p50=4.9s/onset 10.1s vs 20 字档 3.4s/9.4s，
      大窗同源=提交节律非保险丝宽度；拉丁 2× 语义在 provider 内）；
    - ``QWEN3_ASR_COMMIT_MIN_INTERVAL_S=2.0``：句档限速。

    三键语义=**覆盖 B 档碎片缺省**（``_interp_env`` 已 setdefault 6/8/1.0 进 base，
    故本函数必须硬覆盖而非 setdefault），优先序=运营显式 env > 薄线句档 > B 档
    碎片缺省。键集全部既有（_FORWARD_ENV 已登记，零新键）；worker 进程 env 各自
    独立，旧线（开关关）零感知。回退=serve env 显式设旧值或 BOK_INTERP_LITE=0。"""
    profile = {
        "QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS": "999",
        "QWEN3_ASR_CLAUSE_LEN_CHARS": "30",
        "QWEN3_ASR_COMMIT_MIN_INTERVAL_S": "2.0",
    }
    out = dict(base)
    out.update(profile)
    for key in profile:
        if key in os.environ:  # 运营显式值最高优先
            out[key] = os.environ[key]
    return out


def _worker_specs(py) -> list[dict]:
    """agent worker spawn 描述(serve/monitor 同源)：A 线 main + B 线 fwd/rev
    + 演示档 realtime-demo（BOK_QWEN_REALTIME=1 才在列）。"""
    agent_env = env._agent_worker_env(py)
    run_dir = paths.app_data_dir() / "run"
    log_dir = paths.app_data_dir() / "logs"
    specs = [
        {
            "name": "agent",
            # BOK_WORKER_PORT 动态口（2026-10-02 审计）：spec 口必须与 worker
            # 进程实际 bind 的口一致（env 经 _FORWARD_ENV 透传），否则健康面
            # 探活/跳过判定全打缺省口。缺省 8081 零漂移。
            "port": core._agent_worker_port(),
            "pidfile": run_dir / "agent.pid",
            "logfile": log_dir / "agent.log",
            "argv": [str(py), "-m", "agent_runtime.main"],
            "env": agent_env,
        }
    ]
    for _dir, _port in (("fwd", 8082), ("rev", 8083)):
        interp_env = env._interp_env(agent_env)
        interp_env["BOK_SERVICE"] = f"interp-{_dir}"
        interp_env["INTERP_DIRECTION"] = _dir
        env._apply_interp_direction_env(interp_env, _dir)
        # 薄线句档提交缺省（W6×interp-lite 合流刀1）：翻译单元=句末标点。
        if _interp_lite_enabled():
            interp_env = interp_lite_commit_env(interp_env)
        specs.append(
            {
                "name": f"interp-{_dir}",
                "port": _port,
                "pidfile": run_dir / f"interp-{_dir}.pid",
                "logfile": log_dir / f"interp-{_dir}.log",
                # interp-lite 试点开关：BOK_INTERP_LITE=1 换薄线入口（同端口/同
                # agent_name/同健康面；蓝图 2026-10-09-interp-lite.md）。
                "argv": [str(py), "-m", _interp_worker_module()],
                "env": interp_env,
            }
        )
    if _realtime_demo_enabled():
        # 演示档 worker（云端 Realtime S2S）：env 基于 _agent_worker_env 全集
        # （BOK_CP_TOKEN/QWEN_REALTIME_KEY 等 _FORWARD_ENV 键经 passthrough 在内）。
        rt_env = dict(agent_env)
        rt_env["BOK_SERVICE"] = "realtime-demo"
        specs.append(
            {
                "name": "realtime-demo",
                "port": 8084,
                "pidfile": run_dir / "realtime-demo.pid",
                "logfile": log_dir / "realtime-demo.log",
                "argv": [str(py), "-m", "agent_runtime.realtime_demo"],
                "env": rt_env,
            }
        )
    return specs
