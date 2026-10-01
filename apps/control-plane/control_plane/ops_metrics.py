"""CP 容灾可观测管道（DR 容灾+可观测波契约 §1-§5，B 路，2026-10-02）。

一条管道四个消费面：worker 指标上报（``POST /api/metrics/agent-report``，A 路
``apps/agent/agent_runtime/metrics_report.py``）→ CP 进程内滚动窗口（本模块
``MetricsStore`` 单例）→ {Provider 卡 ``GET /api/metrics/providers``；饥荒监视器
（状态机 + settings 热读 a_reply overlay + 建单准入闸）；Root 容灾面板
``GET /api/ops/disaster-status`` / ``POST /api/ops/disaster-override``；实时日志
``GET /api/calls/{id}/logs``}。响应形状以 ``docs/DR-WAVE-CONTRACT.md`` 为准
（契约冻结；改契约=主线拍板）。

## env 键（本模块读面；**需主线登记**）

- ``BOK_LLM_FAMINE_TTFT_S``：饥荒阈值**秒**（默认 4.0=4000ms）。与 agent worker
  第十五波本地兜底同键（worker 走 ``_FORWARD_ENV`` 已登记）；CP 进程消费面同键。
- ``BOK_LLM_FAMINE_HOLD_S``：EMA≥阈值持续 ≥ 该值（默认 10s）→ downgraded。
- ``BOK_LLM_FAMINE_RELEASE_S``：EMA<阈值持续 ≥ 该值（默认 30s）→ healthy。
- ``BOK_AGENT_LOG``（可选）：agent.log 绝对路径覆盖（默认按平台 app-data 约定
  ``<BokVoice>/logs/agent.log``，与 tools/bok.py app_data_dir 同布局）。
- ``BOK_SWAP_THRESHOLD_GB``（可选）：容灾面板 swap 阈值标注（默认 8；CP 只报数，
  红绿判定在 web 同键语义）。

**登记状态（主线维护 ``tools/bok.py``，B 路未碰）**：前三键已由主线在
``_control_plane_env`` 透传（2026-10-02，本 worktree 未提交改动，见该函数「容灾波」
块）；``BOK_AGENT_LOG``/``BOK_SWAP_THRESHOLD_GB`` **仍待主线登记**——不登记则
prod launchd 封闭 env 面收不到（BOK_FLOW_GRAPH 同款教训；dev 靠 ``_start_proc``
merge ``os.environ`` 照常可调）。

非正值/配错一律回落默认（``BOK_LLM_FAMINE_TTFT_S=0`` 若按字面消费=恒饥荒，
属误配陷阱，本模块显式回落 4s）。

## 饥荒状态机（§3，CP 单一真源）

EMA α=0.4 消费 ``llm_ttft``（ms 口径；**≥2 样本才有效**，与 worker 端第十五波
逐字同款），迟滞：::

    healthy --(EMA≥阈值)--> famine --(持续≥hold_s)--> downgraded
    famine/downgraded --(EMA<阈值持续≥release_s)--> healthy

- 评估点=**惰性**：每次样本写入与每次读面（providers/disaster/建单闸/settings
  overlay 判定）都跑一次 ``evaluate()``——只有样本没有读也能收敛（3s 轮询面
  足够密），且时间型转换（hold 到点、release 到点）在没有任何新样本时也能推进。
- 状态转换审计 ``ops.famine``（detail ``from/to/ema_ms``），钩子由 main 绑定到
  CP ``_audit``（``set_audit_hook``；测试可绑自己的收集器）。
- 手动覆盖（root，``ops.famine_override``）优先于自动：
  - ``force_downgrade``：钉住 downgraded（自动 release 不覆盖），直到
    ``force_healthy`` 解除。
  - ``force_healthy``：解除覆盖 + **自动证据清零**（EMA/样本数/迟滞时钟）交还
    状态机——避免陈旧 EMA 让操作员按了「恢复自动」10s 后又自动降档。
  - ``pause_dialing`` / ``resume_dialing``：只动外呼闸（``dialing_paused``），
    不碰 level/override。
- 无杀开关（契约未给）；监视器任何路径不抛（读面永不 500），无样本=恒 healthy。

## settings overlay（§3，零 packages/core 改动）

downgraded 时 CP 在**给 agent 的** ``GET /api/settings?internal=1`` 响应体里把
``model_routing_json`` 串的 ``a_reply`` 车道替换为 4B 本地档（``:1235/v1`` 队列
代理有 reply 优先权），**绝不落库**——用户原文/掩码面/磁盘照旧。overlay 后的串
经既有热切换通道（agent 每通装配读 ``settings["model_routing_json"]`` →
``resolve_route``）下一通生效。优先序天然正确（读 ``model_routes.resolve_route``
核实）：``BOK_MODEL_ROUTING=0`` kill-switch > overlay（在路由表内替换用户档）>
用户配置 > env 缺省链——kill-switch 开启时整表被忽略，overlay 自然失效。

## 实时日志（§5）

``read_call_log`` 按字节游标尾读 ``agent.log``：命中行=行内含 call_id 字样，
原始 print 行（无 call_id）按「最近一次结构行归属」跟随——回看窗
（``lookback_bytes``）只用于建立跨游标的归属上下文，不会重复下发已读行。
"""
from __future__ import annotations

import json
import math
import os
import platform
import re
import socket
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from bok_voice_core.model_routes import parse_routing

__all__ = [
    "KINDS",
    "KIND_TO_PROVIDER",
    "WINDOW_S",
    "MetricsStore",
    "store",
    "reset_state",
    "percentile_ms",
    "famine_overlay_lane",
    "overlay_a_reply",
    "app_data_dir",
    "agent_log_path",
    "read_call_log",
    "server_registry",
    "probe_tcp",
    "probe_servers",
    "swap_used_gb",
    "swap_threshold_gb",
]

# ---- 契约常量（§1/§2/§3）----

# kind 枚举固定四种（§1）；kind→provider 映射（§2）。
KINDS: tuple[str, ...] = ("llm_ttft", "asr_transcribe", "tts_first_audio", "vad_infer")
KIND_TO_PROVIDER: dict[str, str] = {
    "asr_transcribe": "asr",
    "llm_ttft": "llm",
    "tts_first_audio": "tts",
    "vad_infer": "vad",
}  # 键序=§2 响应 providers 行序（asr/llm/tts/vad）

WINDOW_S = 300.0
SAMPLE_MAXLEN = 500
EMA_ALPHA = 0.4

DEFAULT_TTFT_S = 4.0
DEFAULT_HOLD_S = 10.0
DEFAULT_RELEASE_S = 30.0

LEVEL_HEALTHY = "healthy"
LEVEL_FAMINE = "famine"
LEVEL_DOWNGRADED = "downgraded"
LEVELS: tuple[str, ...] = (LEVEL_HEALTHY, LEVEL_FAMINE, LEVEL_DOWNGRADED)

OVERRIDE_ACTIONS: tuple[str, ...] = (
    "force_downgrade",
    "force_healthy",
    "pause_dialing",
    "resume_dialing",
)

# overlay 车道端点缺省（§3：:1235 走队列代理有 reply 优先权）。
_MLX_DEFAULT_BASE_URL = "http://127.0.0.1:1235/v1"

# 结构行判据（§5）：call id = `call-<uuid4 hex[:8]>`（_create_call_in 同款生成）。
_CALL_ID_RE = re.compile(r"call-[0-9a-f]{6,12}")


def _now_iso() -> str:
    """UTC ISO8601 秒精度（契约样例 ``2026-10-01T15:00:00Z`` 同口径）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env_float(key: str, default: float, env: Mapping[str, str] | None = None) -> float:
    """读 float env（空/坏值回落默认）。"""
    raw = (env if env is not None else os.environ).get(key, "")
    try:
        return float(str(raw or "").strip())
    except (TypeError, ValueError):
        return float(default)


# ---- 读面 env 单点（测试直喂 env Mapping，不碰进程 env）----


def famine_threshold_ms(env: Mapping[str, str] | None = None) -> float:
    """饥荒阈值 ms（BOK_LLM_FAMINE_TTFT_S 秒→ms；非正回落 4s 防「0=恒饥荒」误配）。"""
    value = _env_float("BOK_LLM_FAMINE_TTFT_S", DEFAULT_TTFT_S, env)
    return (value if value > 0 else DEFAULT_TTFT_S) * 1000.0


def famine_hold_s(env: Mapping[str, str] | None = None) -> float:
    """famine → downgraded 的持续门（秒；非正回落默认 10s）。"""
    value = _env_float("BOK_LLM_FAMINE_HOLD_S", DEFAULT_HOLD_S, env)
    return value if value > 0 else DEFAULT_HOLD_S


def famine_release_s(env: Mapping[str, str] | None = None) -> float:
    """回落 healthy 的持续门（秒；非正回落默认 30s）。"""
    value = _env_float("BOK_LLM_FAMINE_RELEASE_S", DEFAULT_RELEASE_S, env)
    return value if value > 0 else DEFAULT_RELEASE_S


def swap_threshold_gb(env: Mapping[str, str] | None = None) -> float:
    """容灾面板 swap 阈值（GB，默认 8；非正回落默认）。"""
    value = _env_float("BOK_SWAP_THRESHOLD_GB", 8.0, env)
    return value if value > 0 else 8.0


def percentile_ms(values: Iterable[float], q: float) -> int:
    """分位纯函数（线性插值，numpy 缺省口径）；空输入=0，返回整数毫秒。"""
    vals = sorted(float(v) for v in values)
    if not vals:
        return 0
    if len(vals) == 1:
        return int(round(vals[0]))
    q = min(max(float(q), 0.0), 1.0)
    pos = q * (len(vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return int(round(vals[lo] + (vals[hi] - vals[lo]) * frac))


class MetricsStore:
    """进程内滚动窗口 + 饥荒状态机（CP 单一真源；线程锁——FastAPI 同步端点跑线程池）。

    ``clock``=单调时钟（测试直喂 ``now`` 参数可完全绕开）；``window_s``/``maxlen``
    可覆写（测试用小窗）。
    """

    def __init__(
        self,
        *,
        window_s: float = WINDOW_S,
        maxlen: int = SAMPLE_MAXLEN,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_s = float(window_s)
        self._samples: dict[str, deque] = {
            kind: deque(maxlen=max(int(maxlen), 1)) for kind in KINDS
        }
        self._clock = clock
        self._lock = threading.RLock()
        self._ema_ms = 0.0
        self._ema_n = 0
        self._level = LEVEL_HEALTHY
        self._since: str | None = None
        self._high_since: float | None = None
        self._low_since: float | None = None
        self._override: str | None = None
        self._dialing_paused = False
        self._audit: Callable[[str, dict], None] | None = None

    # ---- 审计钩子 / 测试隔离 ----

    def set_audit_hook(self, hook: Callable[[str, dict], None] | None) -> None:
        """绑定转换审计钩子（main 绑到 CP ``_audit``；None=只打 stdout 行）。"""
        self._audit = hook

    def reset(self) -> None:
        """状态回初始（测试隔离用；审计钩子与样本 deque 容量保留）。"""
        with self._lock:
            for bucket in self._samples.values():
                bucket.clear()
            self._ema_ms = 0.0
            self._ema_n = 0
            self._level = LEVEL_HEALTHY
            self._since = None
            self._high_since = None
            self._low_since = None
            self._override = None
            self._dialing_paused = False

    # ---- 内部：转换与评估（锁内；转换清单锁外 emit）----

    def _emit(self, transitions: list[dict]) -> None:
        for item in transitions:
            print(
                f"[ops-metrics] famine transition {item['from']} -> {item['to']} "
                f"ema_ms={item['ema_ms']}",
                flush=True,
            )
            hook = self._audit
            if hook is None:
                continue
            try:
                hook("ops.famine", dict(item))
            except Exception as exc:  # noqa: BLE001 - 审计失败不破状态机
                print(f"[ops-metrics] famine audit failed: {exc!r}", flush=True)

    def _transition_locked(self, to: str, ts_iso: str) -> dict:
        frm = self._level
        self._level = to
        # since=该 level 起点（healthy 无持续语义=契约样例 null）。
        self._since = None if to == LEVEL_HEALTHY else ts_iso
        return {"from": frm, "to": to, "ema_ms": round(self._ema_ms, 1)}

    def _evaluate_locked(self, now: float) -> list[dict]:
        """状态机评估（迟滞）；返回本帧产生的转换清单（0-1 条）。"""
        if self._override is not None:
            return []  # 手动覆盖优先：自动转换（含 release）全停
        if self._ema_n < 2:
            return []  # ≥2 样本才有有效 EMA（与 worker 端同款）
        out: list[dict] = []
        if self._ema_ms >= famine_threshold_ms():
            self._low_since = None
            if self._high_since is None:
                self._high_since = now
            if self._level == LEVEL_HEALTHY:
                out.append(self._transition_locked(LEVEL_FAMINE, _now_iso()))
            elif (
                self._level == LEVEL_FAMINE
                and (now - float(self._high_since)) >= famine_hold_s()
            ):
                out.append(self._transition_locked(LEVEL_DOWNGRADED, _now_iso()))
        else:
            self._high_since = None
            if self._low_since is None:
                self._low_since = now
            if (
                self._level != LEVEL_HEALTHY
                and (now - float(self._low_since)) >= famine_release_s()
            ):
                out.append(self._transition_locked(LEVEL_HEALTHY, _now_iso()))
        return out

    def _append_locked(self, kind: str, ms: Any, t: float, call_id: str) -> bool:
        if kind not in KINDS:
            return False
        try:
            value = float(ms)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(value) or value < 0:
            return False  # 脏样本不进统计（-1 取消哨兵/NaN 等）
        self._samples[kind].append((t, value, str(call_id or "")))
        if kind == "llm_ttft":
            self._ema_n += 1
            if self._ema_n == 1:
                self._ema_ms = value
            else:
                self._ema_ms = self._ema_ms * (1.0 - EMA_ALPHA) + value * EMA_ALPHA
        return True

    def _famine_view_locked(self, *, include_override: bool) -> dict:
        view: dict[str, Any] = {
            "level": self._level,
            "ema_s": round(self._ema_ms / 1000.0, 3),
            "since": self._since,
            "downgraded": self._level == LEVEL_DOWNGRADED,
            "dialing_paused": self._dialing_paused,
        }
        if include_override:
            view["manual_override"] = self._override
        return view

    # ---- 写入面 ----

    def record(
        self,
        kind: str,
        ms: float,
        *,
        call_id: str = "",
        now: float | None = None,
    ) -> bool:
        """入一个样本（非法 kind/NaN/负值=False 不入）。"""
        t = self._clock() if now is None else float(now)
        with self._lock:
            ok = self._append_locked(kind, ms, t, call_id)
            transitions = self._evaluate_locked(t) if ok else []
        self._emit(transitions)
        return ok

    def record_many(
        self,
        samples: Iterable[tuple[str, Any]],
        *,
        call_id: str = "",
        now: float | None = None,
    ) -> int:
        """整批入样（同批共用到达时刻，评估一次）；返回实际入样数。"""
        t = self._clock() if now is None else float(now)
        accepted = 0
        with self._lock:
            for kind, ms in samples:
                if self._append_locked(kind, ms, t, call_id):
                    accepted += 1
            transitions = self._evaluate_locked(t)
        self._emit(transitions)
        return accepted

    # ---- 读面 ----

    def evaluate(self, now: float | None = None) -> None:
        """显式跑一次时间型评估（读路径惰性调用同一实现；测试/后台可单点触发）。"""
        t = self._clock() if now is None else float(now)
        with self._lock:
            transitions = self._evaluate_locked(t)
        self._emit(transitions)

    def providers(self, *, call_id: str = "", now: float | None = None) -> dict[str, dict]:
        """§2 providers 段：四 provider 恒在（无样本=last/p50/p95 null + n 0）。"""
        t = self._clock() if now is None else float(now)
        with self._lock:
            transitions = self._evaluate_locked(t)
            out: dict[str, dict] = {}
            for kind, provider in KIND_TO_PROVIDER.items():
                vals = [
                    v
                    for (ts, v, cid) in self._samples[kind]
                    if (t - ts) <= self._window_s and (not call_id or cid == call_id)
                ]
                if vals:
                    out[provider] = {
                        "last_ms": int(round(vals[-1])),
                        "p50": percentile_ms(vals, 0.5),
                        "p95": percentile_ms(vals, 0.95),
                        "n": len(vals),
                    }
                else:
                    out[provider] = {"last_ms": None, "p50": None, "p95": None, "n": 0}
        self._emit(transitions)
        return out

    def famine_view(
        self, *, include_override: bool = True, now: float | None = None
    ) -> dict:
        """§3 状态视图（读时惰性评估）；``include_override``=§4 六键 / §2 五键。"""
        t = self._clock() if now is None else float(now)
        with self._lock:
            transitions = self._evaluate_locked(t)
            view = self._famine_view_locked(include_override=include_override)
        self._emit(transitions)
        return view

    def providers_view(self, *, call_id: str = "", now: float | None = None) -> dict:
        """§2 整响应：``{window_s, providers, famine}``（famine=五键，契约样例形状）。"""
        providers = self.providers(call_id=call_id, now=now)
        famine = self.famine_view(include_override=False, now=now)
        return {"window_s": int(self._window_s), "providers": providers, "famine": famine}

    def blocking_state(self, *, now: float | None = None) -> dict:
        """建单准入闸读面：``{blocked, reason, famine}``（downgraded/停拨任一=blocked）。"""
        view = self.famine_view(include_override=True, now=now)
        if view["downgraded"]:
            reason = "downgraded"
        elif view["dialing_paused"]:
            reason = "dialing_paused"
        else:
            reason = ""
        return {"blocked": bool(reason), "reason": reason, "famine": view}

    # ---- 控制面（手动覆盖，root 端点）----

    def override(self, action: str, *, now: float | None = None) -> dict:
        """执行一个覆盖动作，返回覆盖后的 famine 六键视图；未知 action=ValueError。"""
        if action not in OVERRIDE_ACTIONS:
            raise ValueError(f"unknown override action: {action}")
        t = self._clock() if now is None else float(now)
        ts_iso = _now_iso()
        with self._lock:
            transitions: list[dict] = []
            if action == "force_downgrade":
                # 钉住降档：自动 release 不再生效，直到 force_healthy 解除。
                self._override = action
                if self._level != LEVEL_DOWNGRADED:
                    transitions.append(self._transition_locked(LEVEL_DOWNGRADED, ts_iso))
            elif action == "force_healthy":
                # 解除覆盖 + 自动证据清零（EMA/样本/迟滞钟）交还状态机。
                self._override = None
                self._ema_ms = 0.0
                self._ema_n = 0
                self._high_since = None
                self._low_since = None
                if self._level != LEVEL_HEALTHY:
                    transitions.append(self._transition_locked(LEVEL_HEALTHY, ts_iso))
                else:
                    self._since = None
            elif action == "pause_dialing":
                self._dialing_paused = True
            else:  # resume_dialing
                self._dialing_paused = False
            view = self._famine_view_locked(include_override=True)
        self._emit(transitions)
        return view


# ---- 进程内单例（CP 单一真源）----

_STORE: MetricsStore | None = None


def store() -> MetricsStore:
    """进程内单例（首用即建；main 启动时绑审计钩子）。"""
    global _STORE
    if _STORE is None:
        _STORE = MetricsStore()
    return _STORE


def reset_state() -> None:
    """测试隔离：单例状态复位（钩子/容量保留）。"""
    store().reset()


# ---- settings overlay（§3；纯响应体改写，零落库）----


def famine_overlay_lane(*, model: str = "", base_url: str = "") -> dict:
    """overlay 车道 dict（provider=local 恒；base_url 缺省 :1235/v1 队列代理）。"""
    return {
        "provider": "local",
        "base_url": str(base_url or "").strip() or _MLX_DEFAULT_BASE_URL,
        "model": str(model or "").strip(),
        "api_key": "",
        "extra": {"enable_thinking": False},
    }


def overlay_a_reply(raw: str | Mapping[str, Any] | None, lane_cfg: dict) -> str:
    """把 ``a_reply`` 车道替换进用户路由串并重序列化（响应体 overlay，绝不落库）。

    宽容：坏 JSON/空串经共享契约 ``parse_routing`` 归一后仅含 overlay 车道——
    其余车道回落 env 缺省链（``resolve_route`` 的既有语义）。优先级由消费侧
    ``resolve_route`` 决定：``BOK_MODEL_ROUTING=0``（kill-switch）> 本 overlay
    （路由表内替换用户档）> 用户其余配置 > env 链。
    """
    doc = parse_routing(raw)
    lanes = dict(doc["lanes"])
    lanes["a_reply"] = dict(lane_cfg)
    return json.dumps({"lanes": lanes, "presets": doc["presets"]}, ensure_ascii=False)


# ---- 容灾面板辅助（§4）----


def server_registry(env: Mapping[str, str] | None = None) -> list[tuple[str, str, int]]:
    """本机服务注册表 [(name, host, port)]：已知端口缺省 + 既有 env URL 复用。

    端口来源=既有配置（MLX_LLM_BASE_URL/MT_LLM_BASE_URL/BOK_SETTLE_LLM_BASE_URL/
    QWEN3_*_BASE_URL/LIVEKIT_URL/BOK_CSC_URL/BOK_LAYA_URL）——与栈实际监听面同源，
    不新立配置键；未配置=已知缺省（无效端口 TCP 秒拒，面板照常）。
    """
    e = env if env is not None else os.environ

    def _split(url: str, default_port: int) -> tuple[str, int]:
        from urllib.parse import urlparse

        try:
            parsed = urlparse(str(url or ""))
            host = parsed.hostname or "127.0.0.1"
            return host, int(parsed.port or default_port)
        except Exception:  # noqa: BLE001 - 坏 URL 落缺省
            return "127.0.0.1", default_port

    specs: list[tuple[str, str, int]] = [
        ("llm", str(e.get("MLX_LLM_BASE_URL") or "http://127.0.0.1:1235/v1"), 1235),
        (
            "llm-9b",
            str(
                e.get("BOK_SETTLE_LLM_BASE_URL")
                or e.get("FLOW_JUDGE_LLM_BASE_URL")
                or "http://127.0.0.1:1237/v1"
            ),
            1237,
        ),
        ("mt-llm", str(e.get("MT_LLM_BASE_URL") or "http://127.0.0.1:1236/v1"), 1236),
        ("asr", str(e.get("QWEN3_ASR_BASE_URL") or "http://127.0.0.1:8787"), 8787),
        ("tts", str(e.get("QWEN3_TTS_BASE_URL") or "http://127.0.0.1:8788"), 8788),
        ("livekit", str(e.get("LIVEKIT_URL") or "ws://127.0.0.1:7880"), 7880),
        ("b-line", "http://127.0.0.1:8790", 8790),
        ("laya", str(e.get("BOK_LAYA_URL") or "http://127.0.0.1:8791"), 8791),
        ("csc", str(e.get("BOK_CSC_URL") or "http://127.0.0.1:8792"), 8792),
    ]
    out: list[tuple[str, str, int]] = []
    for name, url, default_port in specs:
        host, port = _split(url, default_port)
        out.append((name, host, port))
    return out


def probe_tcp(host: str, port: int, timeout: float = 0.25) -> bool:
    """TCP 连通探针（绝不抛；web ":port 亮灯" 只需进程在听）。"""
    try:
        with socket.create_connection((str(host), int(port)), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001 - 探针只报告
        return False


def probe_servers(
    servers: Iterable[tuple[str, str, int]] | None = None,
    *,
    timeout: float = 0.25,
    probe: Callable[[str, int, float], bool] = probe_tcp,
) -> list[dict]:
    """并行探活注册表 → §4 ``[{name, port, up}]``（顺序=注册表序）。"""
    specs = list(server_registry() if servers is None else servers)
    if not specs:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(specs))) as pool:
        results = list(pool.map(lambda spec: probe(spec[1], spec[2], timeout), specs))
    return [
        {"name": str(name), "port": int(port), "up": bool(up)}
        for (name, _host, port), up in zip(specs, results)
    ]


_SWAP_USAGE_RE = re.compile(r"used\s*=\s*([0-9.]+)\s*([MG])", re.IGNORECASE)


def _parse_swapusage(text: str) -> float | None:
    """``sysctl vm.swapusage`` 输出 → used GB（``total = 4096.00M used = 1234.00M ...``）。"""
    match = _SWAP_USAGE_RE.search(str(text or ""))
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).upper()
    return round(value / 1024.0, 2) if unit == "M" else round(value, 2)


def _parse_meminfo_swap(text: str) -> float | None:
    """Linux /proc/meminfo → swap used GB（kB 口径）。"""
    total = free = None
    for line in str(text or "").splitlines():
        if line.startswith("SwapTotal:"):
            total = float(line.split()[1])
        elif line.startswith("SwapFree:"):
            free = float(line.split()[1])
    if total is None or free is None:
        return None
    return round(max(total - free, 0.0) / (1024.0 * 1024.0), 2)


def swap_used_gb() -> float | None:
    """系统 swap 用量 GB；macOS=``sysctl -n vm.swapusage``，Linux=/proc/meminfo。

    不支持的平台/读失败=None（面板显示「无数据」灰灯，不编数字、不抛）。
    """
    try:
        system = platform.system()
        if system == "Darwin":
            out = subprocess.run(
                ["sysctl", "-n", "vm.swapusage"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            return _parse_swapusage(out.stdout)
        if system == "Linux":
            return _parse_meminfo_swap(Path("/proc/meminfo").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 面板读面绝不抛
        return None
    return None


# ---- 实时日志（§5）----


def app_data_dir(env: Mapping[str, str] | None = None) -> Path:
    """app-data 根（与 tools/bok.py ``app_data_dir`` 同布局；CP 不 import tools）。

    平台分档：nt=LOCALAPPDATA；Darwin=~/Library/Application Support；
    Linux=XDG_DATA_HOME 或 ~/.local/share。
    """
    e = env if env is not None else os.environ
    if os.name == "nt":
        base = Path(str(e.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")))
    elif platform.system() == "Darwin":
        base = Path(str(e.get("HOME") or ".")) / "Library" / "Application Support"
    else:
        base = Path(
            str(e.get("XDG_DATA_HOME") or (Path(str(e.get("HOME") or ".")) / ".local" / "share"))
        )
    return base / "BokVoice"


def agent_log_path(env: Mapping[str, str] | None = None) -> Path:
    """agent.log 路径：``BOK_AGENT_LOG`` 显式覆盖 > 平台 app-data ``logs/agent.log``。"""
    e = env if env is not None else os.environ
    override = str(e.get("BOK_AGENT_LOG") or "").strip()
    if override:
        return Path(override)
    return app_data_dir(e) / "logs" / "agent.log"


def read_call_log(
    call_id: str,
    *,
    after: int = 0,
    limit: int = 200,
    path: Path | None = None,
    max_scan_bytes: int = 1_000_000,
    lookback_bytes: int = 65_536,
) -> dict:
    """§5 尾读：``{lines, next_offset, eof}``（字节游标续读）。

    过滤=行内含 call_id；原始 print 行（无 call_id）按「最近一次结构行归属」跟随
    ——``lookback_bytes`` 回看窗只用于建立跨游标的归属上下文（不回发已读行）。
    ``next_offset``=已完整消费的字节位置（未命中行也推进，游标不卡）；命中数达
    ``limit`` 时停在该行起点，下一轮从这里续读（零丢行）。读取预算
    ``max_scan_bytes`` 有界，单轮永不整读大文件。
    """
    limit = max(1, min(int(limit or 200), 1000))
    target = str(call_id or "").strip()
    if not target:
        # 空 call_id=无过滤语义（会命中一切原始行），按「无」处理。
        return {"lines": [], "next_offset": max(int(after or 0), 0), "eof": False}
    log_path = Path(path) if path is not None else agent_log_path()
    start = max(int(after or 0), 0)
    try:
        size = log_path.stat().st_size
    except OSError:
        return {"lines": [], "next_offset": start, "eof": True}
    if start > size:
        start = size
    ctx_start = max(0, start - max(int(lookback_bytes), 0))
    try:
        with log_path.open("rb") as fh:
            fh.seek(ctx_start)
            raw = fh.read(min(size - ctx_start, (start - ctx_start) + max(int(max_scan_bytes), 1)))
    except OSError:
        return {"lines": [], "next_offset": start, "eof": True}
    complete = raw.endswith(b"\n")
    lines = raw.split(b"\n")
    if not complete:
        lines = lines[:-1]  # 尾部分行不完整：留待下次
    current = ""  # 最近一次结构行的 call id（跨游标由回看窗重建）
    out: list[str] = []
    next_offset = start
    off = ctx_start
    for line in lines:
        line_start = off
        off = line_start + len(line) + 1
        if not line and line_start >= size:
            break  # 文件尾空行（split 产物）
        text = line.decode("utf-8", "replace")
        match = _CALL_ID_RE.search(text)
        if match:
            current = match.group(0)
        if line_start < start:
            continue  # 回看窗：只建归属上下文
        if len(out) >= limit:
            return {"lines": out, "next_offset": next_offset, "eof": False}
        if (target and target in text) or (current == target):
            out.append(text)
        next_offset = min(off, size)
    return {"lines": out, "next_offset": next_offset, "eof": next_offset >= size}
