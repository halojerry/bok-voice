#!/usr/bin/env python
"""health 域(就绪真话探针/serve 等待环/可选线闸;G2 W② 从 core 搬出,
搬运纪律=穿模块对象调用)。

- 本模块只 `from bokctl import core` 拿模块对象:凡仍住在 core 的名字
  (_http_call/healthy/_relaxed_healthy 等)一律 `core.X` 调用时取——patch
  与后续域搬运在 core 侧保持可见(patch 缝=模块属性)。
- 本域自有函数(_http_ok/_llm_http_ready/_ports_down_after_grace/
  _only_optional_ports/_serve_ready_probe/_serve_ready_probe_relaxed/
  _wait_desktop_ready)域内裸名互调(同模块全局=call-time 可 patch)。
- 留守 core 的近邻(边界记录,2026-10-04;servers 波更新):健康面**共享面**不搬——
  healthy/_relaxed_healthy/_http_call/_probe_worker 被 status/doctor/prod/
  proc/serve 多面吃(doctor 波同判);CORE_PORTS/WORKER_PORTS/PROD_HTTP_CHECKS/
  _SWEEP_HTTP_PATHS/_agent_worker_port/_worker_ports/_desktop_stack_targets
  是端口拓扑单点表(status/doctor/prod/proc 共用);_provider_health_summary+
  _PROVIDER_HEALTH_MODULE 被 cmd_status+cmd_doctor 两面吃;
  _llm_raw_expected(doctor)/_llm_raw_status_check_expected(prod)同属跨域。
  本域只承接「就绪真话+等待环+可选线闸」这一自洽 web;消费点
  (_warn_llm_not_http_ready/_cmd_up_services/cmd_serve)W②-servers 波已随
  服务面搬入 bokctl.servers,照旧穿 health.X 取件。
- 测试面:patch 一律走 tests/_bokpatch.py(patch_bok;PATCH_TARGETS 已把
  _http_ok/_llm_http_ready/_ports_down_after_grace 改道 bokctl.health);
  facade 读用 bok.health.X。
"""
from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from bokctl import core


# ── 就绪真话（2026-10-02 编排审计第二波 · PR-A）──────────────────────────────
# 端口绑定先于权重可用：mlx_lm 先 listen 再装权重、sidecar 的 uvicorn socket
# 先于模型装载就绪，两者都让 1s TCP 探活变成谎（「绿着坏」）——等待环等到的是
# 半死进程、健康面全绿而通话全灭。下列两个探针只认 HTTP 200，永不抛。
def _http_ok(port: int, path: str, timeout_s: float = 1.5) -> bool:
    """HTTP 真话探针：**仅 HTTP 200 为 True**（404/426/5xx/超时/拒连全 False，
    与 _relaxed_healthy「任何应答=活」语义刻意相反——那个答的是「进程在」，
    这个答的是「能干活」）。永不抛。"""
    try:
        status, _ = core._http_call(f"http://127.0.0.1:{port}{path}", timeout_s=timeout_s)
        return status == 200
    except Exception:  # noqa: BLE001 - 探针只判定，不抛
        return False


def _llm_http_ready(port: int, timeout_s: float = 1.5) -> bool:
    """LLM 端口就绪真话：/v1/models 必须 HTTP 200。queue proxy 拓扑下 :1235 是
    代理（/v1/models 直通上游 mlx），代理活而上游 mlx 死亡/装载中同样 False。"""
    return _http_ok(port, "/v1/models", timeout_s)


def _ports_down_after_grace(
    targets: Sequence[int], probe: Callable[[int], bool] | None = None
) -> list[int]:
    """等待环超时前的宽松终检（纯函数便于单测）：对每个 target 用放宽超时逐口
    复检一次，返回仍探不活的端口列表（空=其实全部健康，别急着宣判超时）。"""
    if probe is None:
        probe = core._relaxed_healthy
    return [p for p in targets if not probe(p)]


# mt/settle/settle-proxy/llm-raw/embed/laya:模型缺失即跳过,缺它们不拖垮整栈
# (1238=9B 前门闸,queue 关的栈结构性没有,同享可选豁免)
_OPTIONAL_LLM_PORTS = (1236, 1237, 1238, 1239, 8789, 8791)


def _only_optional_ports(down: list[int]) -> bool:
    """宽松终检缺口全落在可选线(MT :1236/settle :1237/llm-raw :1239)→ True(整栈照常放行)。"""
    return bool(down) and all(p in _OPTIONAL_LLM_PORTS for p in down)


# serve 就绪等待环的逐口判据（2026-10-02 readiness 真话）：这三个口有真实
# HTTP 就绪面，必须 200 才算就绪（端口绑定先于权重可用，TCP=谎）；其余
# （8000/7880/worker）维持 TCP——worker 的 /worker 真端点由 prod status /
# monitor 面负责，serve 等待环不改语义。
_SERVE_HTTP_READY_PORTS: dict[int, str] = {8787: "/health", 8788: "/health"}


def _serve_ready_probe(port: int) -> bool:
    """serve 等待环 1s 快档判据：LLM :1235 走 /v1/models HTTP-200；ASR/TTS
    sidecar :8787/:8788 走 /health HTTP-200（模型装载中=503，等它）；其余 TCP。"""
    if port == 1235:
        return _llm_http_ready(port)
    path = _SERVE_HTTP_READY_PORTS.get(port)
    if path:
        return _http_ok(port, path)
    return core.healthy(port)


def _serve_ready_probe_relaxed(port: int) -> bool:
    """serve 宽松终检档判据：严格口维持 HTTP-200 真话（5s 窗吸收宿主 CPU
    风暴的调度延迟），其余端口退回 _relaxed_healthy 旧语义——互杀事故收编
    （2026-09-19）不得因本轮收窄。"""
    if port == 1235:
        return _llm_http_ready(port, timeout_s=5.0)
    path = _SERVE_HTTP_READY_PORTS.get(port)
    if path:
        return _http_ok(port, path, timeout_s=5.0)
    return core._relaxed_healthy(port)


def _wait_desktop_ready(targets: Sequence[int], tries: int = 120) -> bool:
    """serve 就绪等待环（120×1s 形状保留）：全部 target 按 _serve_ready_probe
    逐口判就绪才 True；超时 False（由调用方做宽松终检/宣判）。"""
    for _ in range(tries):
        if all(_serve_ready_probe(p) for p in targets):
            return True
        time.sleep(1)
    return False
