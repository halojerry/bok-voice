"""模型路由统一契约（2026-09-25 阶段 0 定案）.

五个 LLM 车道（a_reply / judge / mt / settle / mining）的本地↔云端解析单点：
CP 设置面（``global_settings.model_routing_json``）与既有 env 缺省链在这里合流，
agent worker / CP 进程内消费者 / CLI 三面共用（``intent_rules.py`` 共享契约先例）。

铁律——**local 档逐字节同旧**：路由表条目缺省（空表/kill-switch）时，解析结果必须令
消费方走与改造前完全一致的 env 行为；``LaneRoute.base_url == ""`` 仅在 mt 车道
（``MT_LLM_BASE_URL`` 未设 = interpret 旧回退路径）出现，消费方见空串走旧路。
``api_key`` 本地档恒 "mlx"（mlx server 忽略），消费方只在 provider=openai 时才真正
使用它——不得因本模块把 "mlx" 塞进原本不带 key 的请求。

解析顺序（计划 §2.3）：``BOK_MODEL_ROUTING=0`` kill-switch（忽略全表）> 路由表
openai 档 / local 档显式 base_url > env 缺省链（judge 云钩子本来就是 env 链的一部分，
不单列）。规格出处：docs/superpowers/plans/2026-09-25-root-cause-fixes-and-model-routing.md。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "LANES",
    "PROVIDER_LOCAL",
    "PROVIDER_OPENAI",
    "KILL_SWITCH_ENV",
    "LaneRoute",
    "parse_routing",
    "validate_routing",
    "routing_kill_switch_active",
    "resolve_route",
]

LANES: tuple[str, ...] = ("a_reply", "judge", "mt", "settle", "mining")
PROVIDER_LOCAL = "local"
PROVIDER_OPENAI = "openai"

# kill-switch：="0" 忽略路由表、字节同旧（经 bok.py _FORWARD_ENV 进 dev/prod 双面）。
KILL_SWITCH_ENV = "BOK_MODEL_ROUTING"

_MLX_DEFAULT = "http://127.0.0.1:1235/v1"

# env 缺省链：lane -> (主 env 键, 回退 env 键或 None, 两者皆空的缺省值)。
# 与改造前各消费点逐点核对过：judge 回退 MLX（agent.py 旧 4018 行同款）、settle 回退
# MLX（summarize.py 72 行同款）、mt 未设 = "" （interpret 旧回退路径信号）。
_LANE_ENV_CHAIN: dict[str, tuple[str, str | None, str]] = {
    "a_reply": ("MLX_LLM_BASE_URL", None, _MLX_DEFAULT),
    "judge": ("FLOW_JUDGE_LLM_BASE_URL", "MLX_LLM_BASE_URL", _MLX_DEFAULT),
    "mt": ("MT_LLM_BASE_URL", None, ""),
    "settle": ("BOK_SETTLE_LLM_BASE_URL", "MLX_LLM_BASE_URL", _MLX_DEFAULT),
    "mining": ("MLX_LLM_BASE_URL", None, _MLX_DEFAULT),
}


@dataclass(frozen=True)
class LaneRoute:
    """一次车道解析的终值。"""

    lane: str
    provider: str  # PROVIDER_LOCAL | PROVIDER_OPENAI
    base_url: str  # "" 仅 mt 旧回退语义；其余恒具体值
    model: str  # "" = 消费方沿用现有模型发现/发现逻辑
    api_key: str  # 本地档 "mlx"；云端档真实 key（routing source 时可为 "" = 保留旧值，CP 面负责合流）
    enable_thinking: bool  # 云端档随请求下发（Qwen3.5 系思考陷阱，LANE-AB 实证）
    source: str  # "routing" | "env"


def _normalize_lane(obj: Mapping[str, Any] | None) -> dict[str, Any]:
    o = dict(obj or {})
    provider = str(o.get("provider") or PROVIDER_LOCAL).strip() or PROVIDER_LOCAL
    if provider not in (PROVIDER_LOCAL, PROVIDER_OPENAI):
        provider = PROVIDER_LOCAL
    extra = o.get("extra") if isinstance(o.get("extra"), Mapping) else {}
    return {
        "provider": provider,
        "base_url": str(o.get("base_url") or "").strip(),
        "model": str(o.get("model") or "").strip(),
        "api_key": str(o.get("api_key") or ""),
        "extra": {"enable_thinking": bool(extra.get("enable_thinking", False))},
    }


def parse_routing(raw: str | Mapping[str, Any] | None) -> dict[str, Any]:
    """宽容解析（运行时面）：坏 JSON/未知键/缺字段 → 缺省，永不 raise。

    返回 ``{"lanes": {lane: 规范化配置}, "presets": {名字: {lane: 规范化配置}}}``；
    预置条目永不携带 api_key（保存面负责剥除）。
    """
    if isinstance(raw, Mapping):
        data: Any = dict(raw)
    else:
        text = str(raw or "").strip()
        if not text:
            return {"lanes": {}, "presets": {}}
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return {"lanes": {}, "presets": {}}
    if not isinstance(data, Mapping):
        return {"lanes": {}, "presets": {}}
    lanes_raw = data.get("lanes")
    lanes = {
        lane: _normalize_lane(v)
        for lane, v in (lanes_raw.items() if isinstance(lanes_raw, Mapping) else [])
        if lane in LANES and isinstance(v, Mapping)
    }
    presets_raw = data.get("presets")
    presets: dict[str, dict[str, Any]] = {}
    if isinstance(presets_raw, Mapping):
        for name, p in presets_raw.items():
            key = str(name).strip()
            if key and isinstance(p, Mapping):
                presets[key] = {
                    lane: _normalize_lane(v)
                    for lane, v in p.items()
                    if lane in LANES and isinstance(v, Mapping)
                }
    return {"lanes": lanes, "presets": presets}


def validate_routing(raw: str | Mapping[str, Any] | None) -> list[str]:
    """严格校验（CP 保存面）：openai 车道必填 base_url + model；api_key 允许空=保留旧值。

    返回人话错误清单（空清单 = 可保存）；CP 端点拿去拼 400。
    """
    parsed = parse_routing(raw)
    errors: list[str] = []
    for lane, cfg in parsed["lanes"].items():
        if cfg["provider"] != PROVIDER_OPENAI:
            continue
        if not cfg["base_url"]:
            errors.append(f"{lane}: 云端档缺 base_url")
        if not cfg["model"]:
            errors.append(f"{lane}: 云端档缺 model")
    return errors


def routing_kill_switch_active(env: Mapping[str, str]) -> bool:
    """kill-switch 读法与全仓同款（== "0" 才关，未设/其它值 = 路由生效）。"""
    return str(env.get(KILL_SWITCH_ENV, "")).strip() == "0"


def resolve_route(
    lane: str,
    env: Mapping[str, str],
    routing: str | Mapping[str, Any] | None = None,
) -> LaneRoute:
    """解析单点。顺序：kill-switch > 路由表（openai 档 / local 档显式 base_url）> env 缺省链。"""
    if lane not in LANES:
        raise ValueError(f"unknown model lane: {lane}")
    if not routing_kill_switch_active(env):
        cfg = parse_routing(routing)["lanes"].get(lane)
        if cfg is not None:
            if cfg["provider"] == PROVIDER_OPENAI:
                return LaneRoute(
                    lane=lane,
                    provider=PROVIDER_OPENAI,
                    base_url=cfg["base_url"],
                    model=cfg["model"],
                    api_key=cfg["api_key"],
                    enable_thinking=cfg["extra"]["enable_thinking"],
                    source="routing",
                )
            if cfg["base_url"]:
                # local 档显式改端点（如 LM Studio :1234）——同卡适用，模型发现照旧。
                return LaneRoute(
                    lane=lane,
                    provider=PROVIDER_LOCAL,
                    base_url=cfg["base_url"],
                    model=cfg["model"],
                    api_key=cfg["api_key"] or "mlx",
                    enable_thinking=cfg["extra"]["enable_thinking"],
                    source="routing",
                )
    primary_env, fallback_env, empty_default = _LANE_ENV_CHAIN[lane]
    base_url = str(env.get(primary_env, "") or "").strip()
    if not base_url and fallback_env:
        base_url = str(env.get(fallback_env, "") or "").strip()
    if not base_url:
        base_url = empty_default
    api_key = str(env.get("FLOW_JUDGE_LLM_API_KEY", "") or "").strip() or "mlx"
    return LaneRoute(
        lane=lane,
        provider=PROVIDER_LOCAL,
        base_url=base_url,
        model="",
        api_key=api_key,
        enable_thinking=False,
        source="env",
    )
