"""bok patch 夹具(G2 W①,2026-10-04)。

为什么存在:bok.py 已退为门面 launcher(实现在 bokctl.core)——直接
``monkeypatch.setattr(bok, "X", v)`` 只改门面命名空间,core 内函数读自己的
模块全局,补丁静默失效(假绿)。本夹具把「属性名 → 权威模块」收成一张表:
W① 全部指向 bokctl.core;W② 起域模块逐个搬出,搬哪个域就改哪几行——
测试面零再改(治理计划 §G2 纪律:动的是映射表,不是 212 处调用点)。
"""
from __future__ import annotations

# 权威定义所在模块映射。W② 搬运时逐行改道,例如:
#   "cmd_prod": "bokctl.prod",
#   "_control_plane_env": "bokctl.env",
PATCH_TARGETS: dict[str, str] = {
    # prod 域(W② 搬出)
    "cmd_prod": "bokctl.prod",
    # paths 域(W② 搬出:路径/平台锚;行集=实际被 patch 的名件,未列名件
    # (MLX_SERVER_WRAPPER/_repo_pythonpath/sidecar_venv_python 等)测试面用
    # bok.paths.X 读)
    "ROOT": "bokctl.paths",
    "app_data_dir": "bokctl.paths",
    "platform_key": "bokctl.paths",
    "is_mac": "bokctl.paths",
    "is_linux": "bokctl.paths",
    "is_packaged": "bokctl.paths",
    "runtime_root": "bokctl.paths",
    "sidecar_python": "bokctl.paths",
    "repo_python": "bokctl.paths",
    "bundled_node": "bokctl.paths",
    "bundled_llama": "bokctl.paths",
    "_embedded_livekit": "bokctl.paths",
    "_livekit_config_path": "bokctl.paths",
    # doctor 域(W② 搬出)
    "_nvidia_gate": "bokctl.doctor",
    "_model_present": "bokctl.doctor",
    "_warn_memory_posture": "bokctl.doctor",
    "_doctor_gpu_gate": "bokctl.doctor",
    "_doctor_draft_warning": "bokctl.doctor",
    "_doctor_minimax_tts": "bokctl.doctor",
    # proc 域(W② 搬出)
    "_ps_field": "bokctl.proc",
    "_sweep_orphan_workers": "bokctl.proc",
    "_sweep_orphan_listeners": "bokctl.proc",
    "_kill_proc_tree": "bokctl.proc",
    "_ensure_monitor": "bokctl.proc",
    # health 域(W② 搬出)
    "_http_ok": "bokctl.health",
    "_llm_http_ready": "bokctl.health",
    "_ports_down_after_grace": "bokctl.health",
    # servers 域(W② 搬出:serve/up 服务面+spawn 原语)
    "cmd_up": "bokctl.servers",
    "_cmd_up_services": "bokctl.servers",
    "_start_call_plane": "bokctl.servers",
    "_start_llm": "bokctl.servers",
    "_start_mt_llm": "bokctl.servers",
    "_start_settle_llm": "bokctl.servers",
    "_start_laya": "bokctl.servers",
    "_apply_mlx_template_fix": "bokctl.servers",
    "_worker_specs": "bokctl.servers",
    "_local_tts_needed": "bokctl.servers",
    "_start_proc": "bokctl.servers",
    "_realtime_demo_enabled": "bokctl.servers",
    "_physical_mem_gib": "bokctl.servers",
    # models 域(W② 搬出:模型目录/路径/下载/选型;行集=实际被 patch 的名件,
    # 未列名件(MODELS/resolve_llm_repo/_llm_draft_* 等)测试面用 bok.models.X 读)
    "model_dir": "bokctl.models",
    "_lmstudio_models_dir": "bokctl.models",
    "model_path": "bokctl.models",
    "_settings_llm_local_model": "bokctl.models",
    "_settle_llm_model": "bokctl.models",
    "_enable_hf_transfer": "bokctl.models",
    "cmd_download": "bokctl.models",
    # env 域(W② 搬出,最后一批:worker/CP env 组装+_FORWARD_ENV 立法单点表;
    # 行集=实际被 patch 的名件,未列名件(_FORWARD_ENV/_apply_* 等)测试面用
    # bok.env.X 读)
    "_agent_worker_env": "bokctl.env",
    "_agent_prod_env": "bokctl.env",
    "_interp_env": "bokctl.env",
    "_control_plane_env": "bokctl.env",
}

_DEFAULT_TARGET = "bokctl.core"


def bok_target(name: str) -> str:
    """属性名 → '模块.属性' 定位串。"""
    return f"{PATCH_TARGETS.get(name, _DEFAULT_TARGET)}.{name}"


def patch_bok(monkeypatch, name: str, value, *, raising: bool = True) -> None:
    """按权威模块打补丁(默认 bokctl.core;PATCH_TARGETS 可改道)。"""
    monkeypatch.setattr(bok_target(name), value, raising=raising)
