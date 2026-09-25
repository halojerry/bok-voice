"""model_routes 共享契约单测（阶段 0）——四路并行实现共同锚定的行为面。

全部 key 值均为测试夹具假值（fixture），不对应任何真实端点/凭据。
"""
from __future__ import annotations

import json

import pytest

from bok_voice_core.model_routes import (
    KILL_SWITCH_ENV,
    LANES,
    PROVIDER_LOCAL,
    PROVIDER_OPENAI,
    parse_routing,
    resolve_route,
    routing_kill_switch_active,
    validate_routing,
)

# 夹具假值（勿当真实凭据使用）
JUDGE_KEY_FIXTURE = "judge-fixture-key"
CLOUD_KEY_FIXTURE = "cloud-fixture-key"

CLEAN_ENV = {
    "MLX_LLM_BASE_URL": "http://127.0.0.1:1235/v1",
    "FLOW_JUDGE_LLM_BASE_URL": "http://127.0.0.1:1237/v1",
    "MT_LLM_BASE_URL": "http://127.0.0.1:1236/v1",
    "BOK_SETTLE_LLM_BASE_URL": "http://127.0.0.1:1237/v1",
}


def test_lanes_fixed_five() -> None:
    assert LANES == ("a_reply", "judge", "mt", "settle", "mining")


def test_parse_tolerance_bad_json() -> None:
    assert parse_routing("{not json") == {"lanes": {}, "presets": {}}
    assert parse_routing("") == {"lanes": {}, "presets": {}}
    assert parse_routing(None) == {"lanes": {}, "presets": {}}
    # 未知车道丢弃、缺字段补默认
    out = parse_routing(json.dumps({"lanes": {"ghost": {"provider": "openai"}, "mt": {}}}))
    assert set(out["lanes"]) == {"mt"}
    assert out["lanes"]["mt"]["provider"] == PROVIDER_LOCAL


def test_parse_presets_roundtrip_no_secrets() -> None:
    raw = json.dumps(
        {
            "lanes": {"judge": {"provider": "openai", "base_url": "https://fixture.example/v1",
                                 "model": "m", "api_key": CLOUD_KEY_FIXTURE}},
            "presets": {"演示档": {"judge": {"provider": "local", "base_url": ""}}},
        }
    )
    out = parse_routing(raw)
    assert out["presets"]["演示档"]["judge"]["provider"] == PROVIDER_LOCAL
    # 预置条目本身不携带密钥语义：api_key 规范化为空
    assert out["presets"]["演示档"]["judge"]["api_key"] == ""


def test_validate_openai_requires_fields() -> None:
    raw = json.dumps({"lanes": {"judge": {"provider": "openai"}}})
    errs = validate_routing(raw)
    assert any("base_url" in e for e in errs) and any("model" in e for e in errs)
    ok = json.dumps({"lanes": {"judge": {"provider": "openai",
                                          "base_url": "https://fixture.example/v1",
                                          "model": "m", "api_key": ""}}})
    assert validate_routing(ok) == []  # api_key 空=保留旧值，放行
    assert validate_routing("") == []


def test_kill_switch_ignores_table() -> None:
    env = {**CLEAN_ENV, KILL_SWITCH_ENV: "0"}
    routing = json.dumps({"lanes": {"judge": {"provider": "openai",
                                               "base_url": "https://fixture.example/v1",
                                               "model": "m",
                                               "api_key": CLOUD_KEY_FIXTURE}}})
    route = resolve_route("judge", env, routing)
    assert route.provider == PROVIDER_LOCAL
    assert route.base_url == "http://127.0.0.1:1237/v1"
    assert route.source == "env"
    assert routing_kill_switch_active(env)


def test_openai_routing_wins() -> None:
    routing = json.dumps({"lanes": {"judge": {"provider": "openai",
                                               "base_url": "https://fixture.example/v1",
                                               "model": "gpt-x",
                                               "api_key": CLOUD_KEY_FIXTURE,
                                               "extra": {"enable_thinking": True}}}})
    route = resolve_route("judge", CLEAN_ENV, routing)
    assert (route.provider, route.source) == (PROVIDER_OPENAI, "routing")
    assert route.base_url == "https://fixture.example/v1"
    assert route.model == "gpt-x"
    assert route.api_key == CLOUD_KEY_FIXTURE
    assert route.enable_thinking is True


def test_local_explicit_base_url_override() -> None:
    routing = json.dumps({"lanes": {"a_reply": {"provider": "local",
                                                  "base_url": "http://127.0.0.1:1234/v1"}}})
    route = resolve_route("a_reply", CLEAN_ENV, routing)
    assert route.provider == PROVIDER_LOCAL
    assert route.base_url == "http://127.0.0.1:1234/v1"
    assert route.source == "routing"
    assert route.model == ""  # 模型发现照旧


def test_env_chain_matches_legacy_per_lane() -> None:
    # a_reply / mining：主键 MLX
    assert resolve_route("a_reply", CLEAN_ENV).base_url.endswith(":1235/v1")
    assert resolve_route("mining", CLEAN_ENV).base_url.endswith(":1235/v1")
    # judge 回退链：主键空 → MLX（agent.py 旧行为同款）
    env = {k: v for k, v in CLEAN_ENV.items() if k != "FLOW_JUDGE_LLM_BASE_URL"}
    assert resolve_route("judge", env).base_url.endswith(":1235/v1")
    # settle 回退链同构
    env = {k: v for k, v in CLEAN_ENV.items() if k != "BOK_SETTLE_LLM_BASE_URL"}
    assert resolve_route("settle", env).base_url.endswith(":1235/v1")
    # mt：主键空 = ""（interpret 旧回退路径信号），消费方见空串走旧路
    env = {k: v for k, v in CLEAN_ENV.items() if k != "MT_LLM_BASE_URL"}
    mt = resolve_route("mt", env)
    assert mt.base_url == "" and mt.source == "env"


def test_env_local_api_key_sentinel() -> None:
    route = resolve_route("a_reply", CLEAN_ENV)
    assert route.api_key == "mlx"  # 本地档哨兵；消费方只在 openai 档真正使用
    env = dict(CLEAN_ENV)
    env["FLOW_JUDGE_LLM_API_KEY"] = JUDGE_KEY_FIXTURE
    assert resolve_route("judge", env).api_key == JUDGE_KEY_FIXTURE


def test_unknown_lane_raises() -> None:
    with pytest.raises(ValueError):
        resolve_route("nope", CLEAN_ENV)


def test_absent_routing_entry_falls_to_env() -> None:
    routing = json.dumps({"lanes": {"mt": {"provider": "openai",
                                            "base_url": "https://fixture.example/v1",
                                            "model": "m", "api_key": "k"}}})
    # 表里只有 mt，其余车道全部走 env 且与无表逐字节同值
    assert resolve_route("a_reply", CLEAN_ENV, routing) == resolve_route("a_reply", CLEAN_ENV)
    assert resolve_route("settle", CLEAN_ENV, routing) == resolve_route("settle", CLEAN_ENV)
