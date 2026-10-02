"""模型路由统一配置——agent runtime 侧消费接线单测（2026-09-25 阶段 0）。

纯离线：只测 plumbing 纯函数（livekit_plugins.route_llm_kwargs / agent._judge_lane_target /
agent._llm_judge / interpret._build_llm_provider）与 MlxLlmLLM 请求体形状，不起 worker、
不碰网络。全部 key 值均为测试夹具假值（fixture），不对应任何真实端点/凭据。

零漂移锚：env 档（无路由/空表/kill-switch）下每个消费点的输出必须与改造前
逐字节同值——对照旧表达式现算（不硬编码期望串，防两边同错）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime import agent as agent_mod  # noqa: E402
from agent_runtime import interpret as interpret_mod  # noqa: E402
from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    MlxLlmLLM,
    StatelessMTLLM,
    route_llm_kwargs,
)
from bok_voice_core.model_routes import (  # noqa: E402
    KILL_SWITCH_ENV,
    resolve_route,
)

# 夹具假值（勿当真实凭据使用；刻意不用 sk- 前缀字面量）
CLOUD_KEY_FIXTURE = "fixture-key"
JUDGE_KEY_FIXTURE = "judge-fixture-key"
CLOUD_BASE_FIXTURE = "https://fixture.example/v1"

_LANE_ENV_KEYS = (
    "MLX_LLM_BASE_URL",
    "MLX_LLM_MODEL",
    "MT_LLM_BASE_URL",
    "MT_LLM_MODEL",
    "FLOW_JUDGE_LLM_BASE_URL",
    "FLOW_JUDGE_LLM_MODEL",
    "FLOW_JUDGE_LLM_API_KEY",
    KILL_SWITCH_ENV,
)


@pytest.fixture()
def clean_lane_env(monkeypatch):
    """隔离车道 env 面：不删的话宿主 export 会污染缺省链断言。"""
    for key in _LANE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _routing(lanes: dict) -> str:
    return json.dumps({"lanes": lanes})


def _base_url(p) -> str:
    """livekit openai 插件把端点挂在 openai client 上（_LLMOptions 不存 base_url）；
    httpx.URL 会补尾斜杠，统一剥掉再比对。"""
    return str(p._client.base_url).rstrip("/")


def _openai_lane(model: str, api_key: str = CLOUD_KEY_FIXTURE) -> dict:
    return {
        "provider": "openai",
        "base_url": CLOUD_BASE_FIXTURE,
        "model": model,
        "api_key": api_key,
    }


# ---- route_llm_kwargs：a_reply/mt → MlxLlmLLM 构造参数映射 ----


def test_route_llm_kwargs_env_lane_is_passthrough(clean_lane_env):
    """env 档＝原读法原样回传：键集合恰为 {base_url, model}，不带 key/思考旗
    ——构造调用与改造前逐字节同形（零漂移保证）。"""
    route = resolve_route("a_reply", {}, "")
    kwargs = route_llm_kwargs(route, env_base_url="http://env-passthrough/v1", cfg_model="cfg-m")
    assert kwargs == {"base_url": "http://env-passthrough/v1", "model": "cfg-m"}
    assert set(kwargs.keys()) == {"base_url", "model"}


def test_route_llm_kwargs_openai_lane_full_override(clean_lane_env):
    route = resolve_route(
        "a_reply",
        {},
        _routing({"a_reply": _openai_lane("fixture-reply-model")}),
    )
    kwargs = route_llm_kwargs(route, env_base_url="http://ignored/v1", cfg_model="also-ignored")
    assert kwargs == {
        "base_url": CLOUD_BASE_FIXTURE,
        "model": "fixture-reply-model",
        "api_key": CLOUD_KEY_FIXTURE,
        "enable_thinking": False,
    }


def test_route_llm_kwargs_openai_empty_key_falls_back_sentinel(clean_lane_env):
    """openai 档 key 空（routing「保留旧值」合流前形态）→ 回构造器缺省哨兵，
    等价不带 key 的既有请求，绝不发空 Bearer。"""
    route = resolve_route("mt", {}, _routing({"mt": _openai_lane("m", api_key="")}))
    kwargs = route_llm_kwargs(route, env_base_url="http://ignored/v1", cfg_model="cfg-m")
    assert kwargs["api_key"] == "mlx"


def test_route_llm_kwargs_local_routing_overrides_endpoint_only(clean_lane_env):
    """local routing 档＝只换端点；model 非空才覆盖；不带 key/思考旗。"""
    route = resolve_route(
        "a_reply",
        {},
        _routing({"a_reply": {"provider": "local", "base_url": "http://127.0.0.1:1234/v1", "model": ""}}),
    )
    kwargs = route_llm_kwargs(route, env_base_url="http://env-default/v1", cfg_model="cfg-m")
    assert kwargs == {"base_url": "http://127.0.0.1:1234/v1", "model": "cfg-m"}

    route2 = resolve_route(
        "a_reply",
        {},
        _routing({"a_reply": {"provider": "local", "base_url": "http://127.0.0.1:1234/v1", "model": "explicit-m"}}),
    )
    assert route_llm_kwargs(route2, env_base_url="http://env-default/v1", cfg_model="cfg-m") == {
        "base_url": "http://127.0.0.1:1234/v1",
        "model": "explicit-m",
    }


# ---- _judge_lane_target：judge 车道消费映射 ----


def _old_env_chain(env: dict, llm_base, llm_model):
    """改造前构造点表达式原样复刻（judge 两处同源），作零漂移对照基准。"""
    jbase = (
        env.get("FLOW_JUDGE_LLM_BASE_URL", "").strip()
        or (llm_base or "")
        or env.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1")
    ).rstrip("/")
    jmodel = (
        env.get("FLOW_JUDGE_LLM_MODEL", "").strip()
        or llm_model
        or env.get("MLX_LLM_MODEL", "")
    )
    return jbase, jmodel, env.get("FLOW_JUDGE_LLM_API_KEY", "mlx")


@pytest.mark.parametrize(
    "env,llm_base,llm_model",
    [
        ({}, "", ""),  # 全缺省：MLX 缺省链
        ({"FLOW_JUDGE_LLM_BASE_URL": "http://127.0.0.1:1237/v1/"}, "", ""),  # 专线优先+rstrip
        ({"MLX_LLM_BASE_URL": "http://127.0.0.1:1235/v1"}, "", ""),  # 回退层
        ({}, "http://llm-card/v1", ""),  # llm 卡中间层（env 档旧链独有）
        ({"FLOW_JUDGE_LLM_MODEL": "jm"}, "", "card-m"),  # 模型链：专线 > 卡
        ({}, "", "card-m"),  # 模型链：卡 > MLX
        ({"FLOW_JUDGE_LLM_API_KEY": JUDGE_KEY_FIXTURE}, "", ""),  # 云钩子 key 原样（不 strip）
    ],
)
def test_judge_lane_target_env_chain_byte_identical(clean_lane_env, env, llm_base, llm_model):
    route = resolve_route("judge", env, "")
    assert route.source == "env"
    got = agent_mod._judge_lane_target(route, env, llm_base_url=llm_base, llm_model=llm_model)
    assert got == (*_old_env_chain(env, llm_base, llm_model), None)
    assert got[3] is None  # env 档不带思考旗


def test_judge_lane_target_openai_lane(clean_lane_env):
    route = resolve_route(
        "judge",
        {},
        _routing({"judge": _openai_lane("fixture-judge-model", api_key=JUDGE_KEY_FIXTURE)}),
    )
    jbase, jmodel, jkey, jthinking = agent_mod._judge_lane_target(route, {})
    assert (jbase, jmodel, jkey, jthinking) == (
        CLOUD_BASE_FIXTURE,
        "fixture-judge-model",
        JUDGE_KEY_FIXTURE,
        False,
    )


def test_judge_lane_target_local_routing_keeps_env_key(clean_lane_env):
    """local routing 档＝换端点/模型，key 与思考旗保持既有 env 行为。"""
    routing = _routing({"judge": {"provider": "local", "base_url": "http://127.0.0.1:1234/v1", "model": "explicit-m"}})
    route = resolve_route("judge", {}, routing)
    jbase, jmodel, jkey, jthinking = agent_mod._judge_lane_target(
        route, {"FLOW_JUDGE_LLM_API_KEY": JUDGE_KEY_FIXTURE}, llm_model="card-m"
    )
    assert (jbase, jmodel, jkey, jthinking) == ("http://127.0.0.1:1234/v1", "explicit-m", JUDGE_KEY_FIXTURE, None)
    # model 空＝回落旧链（专线 env → 卡）
    route2 = resolve_route(
        "judge",
        {},
        _routing({"judge": {"provider": "local", "base_url": "http://127.0.0.1:1234/v1", "model": ""}}),
    )
    assert agent_mod._judge_lane_target(route2, {}, llm_model="card-m")[1] == "card-m"


def test_judge_lane_target_no_endpoint_short_circuit_preserved(clean_lane_env):
    """端点/模型双双缺省仍产出空值——调用点的 `if not jbase or not jmodel: return`
    防线语义不变（意图判据侧 judge_skipped 打点照走）。"""
    route = resolve_route("judge", {}, "")
    jbase, jmodel, _jkey, _jthinking = agent_mod._judge_lane_target(route, {})
    assert not jmodel  # MLX_LLM_MODEL 未设＝空（与旧链一致，jbase 恒有缺省值）


# ---- _llm_judge：enable_thinking 请求体形状 ----


class _FakeAsyncOpenAI:
    calls: list[dict] = []

    def __init__(self, **kwargs):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        type(self).calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="C"))])


@pytest.fixture()
def fake_openai(monkeypatch):
    _FakeAsyncOpenAI.calls = []
    monkeypatch.setattr("openai.AsyncOpenAI", _FakeAsyncOpenAI)
    return _FakeAsyncOpenAI


def _run_judge(**kwargs):
    return asyncio.run(
        agent_mod._llm_judge("http://127.0.0.1:1235/v1", "m", [{"role": "user", "content": "x"}], **kwargs)
    )


def test_llm_judge_without_flag_request_shape_unchanged(fake_openai):
    """缺省（env 档 judge）＝请求 kwargs 无 extra_body 键，逐字节同旧。"""
    _run_judge()
    assert "extra_body" not in _FakeAsyncOpenAI.calls[0]


@pytest.mark.parametrize("flag", [True, False])
def test_llm_judge_enable_thinking_plumbs_body(fake_openai, flag):
    """仅 openai 档传旗：布尔原样进 extra_body（=chat/completions 请求体顶层）。"""
    _run_judge(enable_thinking=flag)
    assert _FakeAsyncOpenAI.calls[0]["extra_body"] == {"enable_thinking": flag}


# ---- MlxLlmLLM：enable_thinking 构造参数 ----


def test_mlx_llm_extra_body_without_flag_unchanged(clean_lane_env):
    p = MlxLlmLLM(base_url="http://127.0.0.1:9999/v1", model="m1")
    assert "enable_thinking" not in p._opts.extra_body
    # 既有 extra_body 键不受影响
    assert p._opts.extra_body["max_tokens"] and p._opts.extra_body["stop"]


@pytest.mark.parametrize("flag", [True, False])
def test_mlx_llm_enable_thinking_lands_in_extra_body(clean_lane_env, flag):
    p = MlxLlmLLM(base_url="http://127.0.0.1:9999/v1", model="m1", enable_thinking=flag)
    assert p._opts.extra_body["enable_thinking"] is flag


# ---- interpret._build_llm_provider：mt / a_reply（B 线回复兜底）两车道 ----


def _valid_local_model(tmp_path: Path) -> str:
    p = tmp_path / "fixture-mt-model"
    p.write_text("fixture", encoding="utf-8")
    return str(p)


def test_build_llm_provider_mt_env_lane_unchanged(monkeypatch, tmp_path):
    """env 档回归：MT env 齐 → MT 分支（与改造前同 base/model）。"""
    model_path = _valid_local_model(tmp_path)
    monkeypatch.setenv("MT_LLM_BASE_URL", "http://127.0.0.1:1236/v1")
    monkeypatch.setenv("MT_LLM_MODEL", model_path)
    provider = interpret_mod._build_llm_provider({}, "cantonese")
    assert isinstance(provider, StatelessMTLLM)
    assert _base_url(provider._inner) == "http://127.0.0.1:1236/v1"
    assert provider._inner._opts.model == model_path
    assert "enable_thinking" not in provider._inner._opts.extra_body


def test_build_llm_provider_mt_unset_falls_back_env_lane(clean_lane_env):
    """MT 未设＝旧回退路径原样保留（mt 车道 route.base_url=="" 语义）。"""
    provider = interpret_mod._build_llm_provider({}, "cantonese")
    assert isinstance(provider, MlxLlmLLM)
    assert _base_url(provider) == "http://127.0.0.1:1235/v1"
    assert not isinstance(provider, StatelessMTLLM)


def test_build_llm_provider_mt_openai_route(monkeypatch, clean_lane_env, tmp_path):
    """mt 车道 openai 档＝云端整体接管（本地路径门禁不适用），思考旗进请求体。"""
    monkeypatch.setenv("MT_LLM_MODEL", str(tmp_path / "ghost"))  # 非法本地路径也拦不住 openai 档
    routing = _routing(
        {
            "mt": {
                "provider": "openai",
                "base_url": CLOUD_BASE_FIXTURE,
                "model": "fixture-mt-model",
                "api_key": CLOUD_KEY_FIXTURE,
                "extra": {"enable_thinking": False},
            }
        }
    )
    provider = interpret_mod._build_llm_provider({}, "cantonese", routing_raw=routing)
    assert isinstance(provider, StatelessMTLLM)
    assert _base_url(provider._inner) == CLOUD_BASE_FIXTURE
    assert provider._inner._opts.model == "fixture-mt-model"
    assert provider._inner._opts.extra_body["enable_thinking"] is False
    assert provider._inner._client.api_key == CLOUD_KEY_FIXTURE


def test_build_llm_provider_mt_local_routing_overrides_endpoint(monkeypatch, clean_lane_env, tmp_path):
    """mt 车道 local routing 档＝换端点/模型后走既有门禁（显式模型须过路径门）。"""
    model_path = _valid_local_model(tmp_path)
    routing = _routing({"mt": {"provider": "local", "base_url": "http://127.0.0.1:4321/v1", "model": model_path}})
    provider = interpret_mod._build_llm_provider({}, "cantonese", routing_raw=routing)
    assert isinstance(provider, StatelessMTLLM)
    assert _base_url(provider._inner) == "http://127.0.0.1:4321/v1"
    assert provider._inner._opts.model == model_path
    assert "enable_thinking" not in provider._inner._opts.extra_body  # local 档不带思考旗


def test_build_llm_provider_mt_local_routing_bad_model_still_guards(monkeypatch, clean_lane_env, tmp_path, capsys):
    """local routing 档挂死防线不绕：model 非本地路径 → 跳 MT 走回退链 + 日志留值。"""
    routing = _routing({"mt": {"provider": "local", "base_url": "http://127.0.0.1:4321/v1", "model": "repo-id-not-a-path"}})
    provider = interpret_mod._build_llm_provider({}, "cantonese", routing_raw=routing)
    assert isinstance(provider, MlxLlmLLM)
    assert not isinstance(provider, StatelessMTLLM)
    assert "mt model invalid" in capsys.readouterr().out


def test_build_llm_provider_reply_fallback_openai_route(clean_lane_env):
    """B 线回复兜底（a_reply）openai 档＝云端端点/模型/密钥+思考旗。"""
    routing = _routing({"a_reply": _openai_lane("fixture-reply-model")})
    provider = interpret_mod._build_llm_provider({}, "en", routing_raw=routing)
    assert isinstance(provider, MlxLlmLLM)
    assert _base_url(provider) == CLOUD_BASE_FIXTURE
    assert provider._opts.model == "fixture-reply-model"
    assert provider._opts.extra_body["enable_thinking"] is False
    assert provider._client.api_key == CLOUD_KEY_FIXTURE


def test_build_llm_provider_two_calls_different_routing_no_cross_talk(monkeypatch, clean_lane_env):
    """两通不同 routing 不串线：纯函数只吃参数，两次调用各按各的 routing 解析。"""
    routing_a = _routing({"mt": {"provider": "openai", "base_url": "https://a-fixture.example/v1",
                                 "model": "mt-a", "api_key": CLOUD_KEY_FIXTURE}})
    routing_b = _routing({"mt": {"provider": "openai", "base_url": "https://b-fixture.example/v1",
                                 "model": "mt-b", "api_key": CLOUD_KEY_FIXTURE}})
    pa = interpret_mod._build_llm_provider({}, "cantonese", routing_raw=routing_a)
    pb = interpret_mod._build_llm_provider({}, "cantonese", routing_raw=routing_b)
    assert _base_url(pa._inner) == "https://a-fixture.example/v1"
    assert pa._inner._opts.model == "mt-a"
    assert _base_url(pb._inner) == "https://b-fixture.example/v1"
    assert pb._inner._opts.model == "mt-b"


def test_build_llm_provider_kill_switch_ignores_table(monkeypatch, clean_lane_env):
    """kill-switch（经 resolve_route 生效）＝路由表被忽略，字节同 env 档旧行为。"""
    monkeypatch.setenv(KILL_SWITCH_ENV, "0")
    routing = _routing({"mt": _openai_lane("fixture-mt-model"), "a_reply": _openai_lane("fixture-reply-model")})
    provider = interpret_mod._build_llm_provider({}, "cantonese", routing_raw=routing)
    assert isinstance(provider, MlxLlmLLM) and not isinstance(provider, StatelessMTLLM)
    assert _base_url(provider) == "http://127.0.0.1:1235/v1"
    assert "enable_thinking" not in provider._opts.extra_body
