"""bokctl.commands.up —— `bok up`（G2 W③ 自 bokctl.servers 搬入）。

服务面（_cmd_up_services）与通话面（_start_call_plane）单点仍住
bokctl.servers，穿 ``servers.X`` call-time 取（patch 缝=模块属性）；
模型 ensure 由 servers._cmd_up_services 经 ``commands.download.cmd_download``
取（owner 随 W③ 搬入 commands.download）。"""
from __future__ import annotations

from bokctl import paths, servers


def cmd_up(models_only: bool = False) -> int:
    """全栈拉起（node_agent 与 serve 共用）：服务面 + 通话面。

    Ubuntu 节点形态修复（2026-09-20）：旧 cmd_up 只起服务面（sidecar/LLM/b-line），
    LiveKit 与三个 agent worker 只在 dev `serve` 里起——节点装完打不了电话。
    通话面现已提取为 _start_call_plane（与 serve 同源）。返回码沿用服务面语义：
    通话面未齐只打 stderr 不篡改服务面结果（由调用方就绪等待如实失败，健康面
    doctor 呈现 degrad）。

    ``models_only=True``（`bok up --models-only`，2026-10-02 审计）：只拉模型面
    （服务面：asr/llm/mt/settle/tts+proxy），跳过通话面（livekit/worker/monitor）
    ——重启后模型面由可选常驻单元 `bok-model-plane` 补拉（`prod install
    --with-model-plane`，默认 OFF），通话面归既有单元，人工零介入。
    """
    # 缺省档零参调用（既有 stub/调用方逐字节兼容）；仅显式 --models-only 传参。
    rc = servers._cmd_up_services(models_only=True) if models_only else servers._cmd_up_services()
    if rc:
        return rc
    if models_only:
        print("[bok] models-only: 模型面就绪——通话面（livekit/worker/monitor）跳过")
        return 0
    servers._start_call_plane(paths.repo_python())
    return 0


def run(args) -> int:
    return cmd_up(models_only=bool(getattr(args, "models_only", False)))
