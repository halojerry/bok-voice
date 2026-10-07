"""PrefillSpeculator 单测（2026-09-10 抢跑防抖替代件）。

契约核心=严格前缀：投机 messages 必须等于「上一条真实请求 messages +
assistant 回复历史原文 + user(稳定前缀+尾部)」——真请求在 user 文本分叉前
逐字节一致,mlx 前缀缓存才命中。门控：busy 不开火/每轮限次/间隔/前缀增长。
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from agent_runtime.prefill_speculator import (
    PrefillSpeculator,
    cloud_budget_verdict,
    prefill_lane_for,
)


class _FakeCtx:
    def __init__(self, tail: str = "【第1/5步】"):
        self._tail = tail

    def render_context_tail(self) -> str:
        return self._tail


class _FakePrewarm:
    def __init__(self):
        self.calls: list[list[dict]] = []

    async def __call__(self, messages: list[dict]) -> None:
        self.calls.append(messages)


def _spec(tail="【第1/5步】") -> tuple[PrefillSpeculator, _FakePrewarm]:
    prewarm = _FakePrewarm()
    return PrefillSpeculator(prewarm, _FakeCtx(tail)), prewarm


_REQ = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "你好。\n\n【尾部】"},
    {"role": "assistant", "content": "<expr>开场白"},
]


def _arm(spec: PrefillSpeculator) -> None:
    """喂快照+回复+空闲,并清掉时间戳令间隔门/静默窗全开。"""
    spec.on_request_messages([dict(m) for m in _REQ])
    spec.on_reply_history_text("<expr>好的客户")
    spec.set_busy(False)
    spec._last_fire_ts = 0.0
    spec._last_final_ts = 0.0


def test_fire_message_shape_is_strict_prefix():
    """投机 prompt = 快照 + assistant 历史原文 + user(前缀+尾部)。"""

    async def run():
        spec, prewarm = _spec()
        _arm(spec)
        spec.on_stable_prefix("你好我想查下我個")  # create_task 需在 loop 内
        assert spec._task is not None
        await spec._task
        return prewarm

    prewarm = asyncio.run(run())
    assert len(prewarm.calls) == 1
    msgs = prewarm.calls[0]
    assert msgs[:3] == _REQ, "快照段必须逐字节等于上一条真实请求"
    assert msgs[3] == {"role": "assistant", "content": "<expr>好的客户"}
    assert msgs[4]["role"] == "user"
    assert msgs[4]["content"] == "你好我想查下我個\n\n【第1/5步】"


def test_busy_gate():
    async def run():
        spec, prewarm = _spec()
        spec.on_request_messages(list(_REQ))
        spec.on_reply_history_text("回复")
        spec.set_busy(True)
        spec._last_fire_ts = 0.0
        spec.on_stable_prefix("你好我想查下我個")
        await asyncio.sleep(0)
        return prewarm, spec

    prewarm, spec = asyncio.run(run())
    assert spec._task is None and not prewarm.calls, "busy 不开火"


def test_dedupe_budget_and_new_turn_reset():
    async def run():
        spec, prewarm = _spec()
        _arm(spec)
        spec.on_stable_prefix("你好我想查下我個")
        await spec._task
        assert len(prewarm.calls) == 1

        # 同长度/更短前缀:去重不开火
        spec._last_fire_ts = 0.0
        spec.on_stable_prefix("你好我想查下")
        assert spec._task is None

        # 增长前缀:第二轮开火后烧穿预算(默认 2)
        spec._last_fire_ts = 0.0
        spec.on_stable_prefix("你好我想查下我個集運件")
        await spec._task
        spec._last_fire_ts = 0.0
        spec.on_stable_prefix("你好我想查下我個集運件而家去咗")
        assert spec._task is None, "预算烧穿(默认 2)不再开火"

        # new_turn 归还预算（并记 FINAL 时刻——测试里手动归零重开静默窗）
        spec.new_turn()
        spec._last_fire_ts = 0.0
        spec._last_final_ts = 0.0
        spec.on_stable_prefix("你好我想查下我個集運件而家去咗邊")
        await spec._task
        return prewarm

    prewarm = asyncio.run(run())
    assert len(prewarm.calls) == 3


def test_gap_gate_blocks_rapid_refire():
    async def run():
        spec, prewarm = _spec()
        _arm(spec)
        spec.on_stable_prefix("你好我想查下我個")
        await spec._task
        # 刚开火完(时间戳=now),增长前缀被间隔门拦
        spec.on_stable_prefix("你好我想查下我個集運件而家")
        assert spec._task is None
        return prewarm

    asyncio.run(run())


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("BOK_PREFILL_SPEC", "0")

    async def run():
        spec, prewarm = _spec()
        _arm(spec)
        spec.on_stable_prefix("你好我想查下我個")
        await asyncio.sleep(0)
        return prewarm, spec

    prewarm, spec = asyncio.run(run())
    assert spec._task is None and not prewarm.calls


def test_no_snapshot_no_fire():
    async def run():
        spec, prewarm = _spec()
        spec.set_busy(False)
        spec._last_fire_ts = 0.0
        spec.on_stable_prefix("你好我想查下我個")
        await asyncio.sleep(0)
        return prewarm, spec

    prewarm, spec = asyncio.run(run())
    assert spec._task is None and not prewarm.calls


def test_short_prefix_no_fire():
    """与 PREFLIGHT 稳定前缀门槛(≥6 字)一致,太短不值得一次请求。"""

    async def run():
        spec, prewarm = _spec()
        _arm(spec)
        spec.on_stable_prefix("你好我想")
        await asyncio.sleep(0)
        return prewarm, spec

    prewarm, spec = asyncio.run(run())
    assert spec._task is None and not prewarm.calls


def test_final_quiet_gate_blocks_then_passes(monkeypatch):
    """FINAL 后静默窗(默认 1000ms)内不开火——真回复即将进场,投机预热让路;
    窗过后照常开火(quiet=1ms 档)。"""

    async def run():
        spec, prewarm = _spec()
        _arm(spec)
        spec.new_turn()  # FINAL 提交:记时刻 + 预算重置
        spec.on_stable_prefix("你好我想查下我個")
        await asyncio.sleep(0)
        assert spec._task is None and not prewarm.calls, "FINAL 后 1s 内不开火"
        monkeypatch.setenv("BOK_PREFILL_SPEC_FINAL_QUIET_MS", "1")
        await asyncio.sleep(0.02)  # 越过 1ms 窗
        spec.on_stable_prefix("你好我想查下我個集運件")
        assert spec._task is not None, "静默窗过后应照常开火"
        await spec._task
        return prewarm

    prewarm = asyncio.run(run())
    assert len(prewarm.calls) == 1


def test_revision_advanced_since_snapshot_skips_fire():
    """F6 稳定性门:快照后 context revision 已前进(换步/事实沉淀)→ 投机组的
    user 段必与真请求分叉=白烧 GPU,直接跳过开火。"""

    class _RevCtx:
        def __init__(self):
            self.revision = 3
            self._tail = "【第1/5步】"

        def render_context_tail(self) -> str:
            return self._tail

    async def run():
        ctx = _RevCtx()
        prewarm = _FakePrewarm()
        spec = PrefillSpeculator(prewarm, ctx)
        spec.on_request_messages([dict(m) for m in _REQ])
        spec.on_reply_history_text("回复")
        spec.set_busy(False)
        spec._last_fire_ts = 0.0
        spec._last_final_ts = 0.0
        ctx.revision = 4  # 快照之后尾部 revision 前进
        spec.on_stable_prefix("你好我想查下我個")
        await asyncio.sleep(0)
        return prewarm, spec

    prewarm, spec = asyncio.run(run())
    assert spec._task is None and not prewarm.calls, "revision 前进=投机尾部必分叉,不开火"


def test_revision_unchanged_fires():
    """F6 反向:revision 与快照一致 → 照常开火(ctx 暴露 revision 时门只拦变化轮)。"""

    class _RevCtx:
        def __init__(self):
            self.revision = 3
            self._tail = "【第1/5步】"

        def render_context_tail(self) -> str:
            return self._tail

    async def run():
        ctx = _RevCtx()
        prewarm = _FakePrewarm()
        spec = PrefillSpeculator(prewarm, ctx)
        spec.on_request_messages([dict(m) for m in _REQ])
        spec.on_reply_history_text("回复")
        spec.set_busy(False)
        spec._last_fire_ts = 0.0
        spec._last_final_ts = 0.0
        spec.on_stable_prefix("你好我想查下我個")
        assert spec._task is not None
        await spec._task
        return prewarm

    prewarm = asyncio.run(run())
    assert len(prewarm.calls) == 1


def test_new_turn_aborts_inflight():
    """FINAL 即断:在飞投机预热被 new_turn 取消——真回复要进 reply 车道,
    在飞预热(httpx 连接)即刻关闭,残余解码尾巴最小化。"""

    started = asyncio.Event()

    class _SlowPrewarm:
        async def __call__(self, messages: list[dict]) -> None:
            started.set()
            await asyncio.sleep(5)

    async def run():
        spec = PrefillSpeculator(_SlowPrewarm(), _FakeCtx())
        _arm(spec)
        spec.on_stable_prefix("你好我想查下我個")
        await started.wait()
        task = spec._task
        assert task is not None
        spec.new_turn()
        assert task.cancelled() is False, "cancel 是异步的,此刻尚未终结"
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("在飞任务应被取消")
        return spec

    spec = asyncio.run(run())
    assert spec._task is None, "finally 清槽,后续可再开火"


# ---- 云车道（2026-10-06 A 线对偶件）------------------------------------------
# 车道判定 prefill_lane_for / lane_for_llm_provider 的装配点行为在
# tests/test_prewarm_host_gate.py（host 门立法档案同处）;本段钉 lane="cloud"
# 臂的护栏与打点行为,及 lane="local" 逐字节不变。


def test_prefill_lane_local_default_and_deepseek_cloud(monkeypatch):
    """车道判定:本机恒 local(不看云开关);DeepSeek 缺省 cloud、=0 回旧门;
    非 DeepSeek 云端点不放。"""
    assert prefill_lane_for("http://127.0.0.1:1235/v1") == "local"
    assert prefill_lane_for("http://localhost:1238/v1") == "local"
    monkeypatch.setenv("BOK_PREFILL_SPEC_CLOUD", "0")
    assert prefill_lane_for("http://127.0.0.1:1235/v1") == "local", "本机不看云开关"

    monkeypatch.delenv("BOK_PREFILL_SPEC_CLOUD", raising=False)
    assert prefill_lane_for("https://api.deepseek.com/v1") == "cloud", "缺省=云档放行"
    assert prefill_lane_for("https://api.deepseek.com") == "cloud", "裸域(无 /v1)同判"
    monkeypatch.setenv("BOK_PREFILL_SPEC_CLOUD", "0")
    assert prefill_lane_for("https://api.deepseek.com/v1") == "", "=0 回旧 host 门"

    monkeypatch.delenv("BOK_PREFILL_SPEC_CLOUD", raising=False)
    assert prefill_lane_for("https://api.openai.com/v1") == "", "非 DeepSeek 云端点不放"
    assert prefill_lane_for("") == "local", "空 base 保守 local(旧门 True 同款)"
    assert prefill_lane_for("garbage") == "", "非本地非 DeepSeek=不放(旧门 False 同款)"


def test_lane_for_llm_provider_unreadable_is_conservative_local():
    from agent_runtime.prefill_speculator import lane_for_llm_provider

    assert lane_for_llm_provider(SimpleNamespace()) == "local"
    assert lane_for_llm_provider(object()) == "local"
    cloud = SimpleNamespace(_client=SimpleNamespace(base_url="https://api.deepseek.com/v1"))
    assert lane_for_llm_provider(cloud) == "cloud"


def test_cloud_budget_length_guardrail():
    env = {"BOK_PREFILL_SPEC_CLOUD_MAX_CHARS": "100"}
    ok, why = cloud_budget_verdict(101, 0, env)
    assert not ok and why == "max_chars"
    assert cloud_budget_verdict(100, 0, env) == (True, ""), "压线放行"
    assert cloud_budget_verdict(10**9, 0, {"BOK_PREFILL_SPEC_CLOUD_MAX_CHARS": "0"}) == (
        True,
        "",
    ), "0=关(不限长)"
    # 非整数回缺省(16000)
    bad = {"BOK_PREFILL_SPEC_CLOUD_MAX_CHARS": "abc"}
    assert cloud_budget_verdict(16001, 0, bad) == (False, "max_chars")
    assert cloud_budget_verdict(16000, 0, bad) == (True, "")


def test_cloud_budget_per_call_cap():
    env = {"BOK_PREFILL_SPEC_CLOUD_MAX_PER_CALL": "2"}
    assert cloud_budget_verdict(10, 1, env) == (True, "")
    ok, why = cloud_budget_verdict(10, 2, env)
    assert not ok and why == "max_per_call"
    assert cloud_budget_verdict(10, 999, {"BOK_PREFILL_SPEC_CLOUD_MAX_PER_CALL": "0"}) == (
        True,
        "",
    ), "0=关(不限次)"


def test_cloud_lane_fires_and_logs_lane_tag(capsys):
    """云臂照常开火(缺省护栏内),打点带 lane=cloud 后缀。"""

    async def run():
        prewarm = _FakePrewarm()
        spec = PrefillSpeculator(prewarm, _FakeCtx(), lane="cloud")
        _arm(spec)
        spec.on_stable_prefix("你好我想查下我個")
        assert spec._task is not None
        await spec._task
        return prewarm, spec

    prewarm, spec = asyncio.run(run())
    assert len(prewarm.calls) == 1
    assert spec._cloud_fires == 1
    out = capsys.readouterr().out
    assert "BOK_PREFILL_SPEC fire lane=cloud chars=" in out


def test_local_lane_fire_line_unchanged(capsys):
    """local 行打点逐字节不变(无 lane= 后缀)——RUNBOOK 日志口径不动。"""

    async def run():
        spec, prewarm = _spec()  # lane 缺省 "local"
        _arm(spec)
        spec.on_stable_prefix("你好我想查下我個")
        await spec._task
        return prewarm

    asyncio.run(run())
    out = capsys.readouterr().out
    assert "BOK_PREFILL_SPEC fire chars=" in out
    assert "lane=" not in out


def test_cloud_length_guardrail_skips_without_burning_state(monkeypatch):
    """长度护栏拦截:不开火且不烧轮内预算/去重态/每通计数。"""
    monkeypatch.setenv("BOK_PREFILL_SPEC_CLOUD_MAX_CHARS", "10")

    async def run():
        prewarm = _FakePrewarm()
        spec = PrefillSpeculator(prewarm, _FakeCtx(), lane="cloud")
        _arm(spec)
        spec.on_stable_prefix("你好我想查下我個")  # 快照+尾部+前缀合计远超 10 字
        await asyncio.sleep(0)
        return prewarm, spec

    prewarm, spec = asyncio.run(run())
    assert spec._task is None and not prewarm.calls, "超长 prompt 云档跳过"
    assert spec._turn_fires == 0 and spec._last_prefix == "", "拦截不烧轮内预算/去重态"
    assert spec._cloud_fires == 0


def test_cloud_per_call_cap_across_turns(monkeypatch):
    """每通上限跨轮生效:new_turn 归还轮内预算但不归还每通计数。"""
    monkeypatch.setenv("BOK_PREFILL_SPEC_CLOUD_MAX_PER_CALL", "2")

    async def run():
        prewarm = _FakePrewarm()
        spec = PrefillSpeculator(prewarm, _FakeCtx(), lane="cloud")
        _arm(spec)
        spec.on_stable_prefix("你好我想查下我個")
        await spec._task
        spec._last_fire_ts = 0.0
        spec.on_stable_prefix("你好我想查下我個集運件")
        await spec._task
        assert spec._cloud_fires == 2
        spec.new_turn()
        spec._last_fire_ts = 0.0
        spec._last_final_ts = 0.0
        spec.on_stable_prefix("你好我想查下我個集運件而家")
        await asyncio.sleep(0)
        return prewarm, spec

    prewarm, spec = asyncio.run(run())
    assert spec._task is None and len(prewarm.calls) == 2, "每通上限 2 拦第三发"


def test_cloud_lane_busy_and_gap_gates_unchanged(monkeypatch):
    """云臂不放松既有门控:busy/间隔(600ms)照旧拦;护栏 0=关隔离变量。"""
    monkeypatch.setenv("BOK_PREFILL_SPEC_CLOUD_MAX_CHARS", "0")
    monkeypatch.setenv("BOK_PREFILL_SPEC_CLOUD_MAX_PER_CALL", "0")

    async def run():
        prewarm = _FakePrewarm()
        spec = PrefillSpeculator(prewarm, _FakeCtx(), lane="cloud")
        spec.on_request_messages([dict(m) for m in _REQ])
        spec.on_reply_history_text("回复")
        spec.set_busy(True)
        spec._last_fire_ts = 0.0
        spec._last_final_ts = 0.0
        spec.on_stable_prefix("你好我想查下我個")
        await asyncio.sleep(0)
        assert spec._task is None and not prewarm.calls, "busy 不开火(云臂同)"
        spec.set_busy(False)
        spec.on_stable_prefix("你好我想查下我個")
        await spec._task
        assert len(prewarm.calls) == 1
        spec.on_stable_prefix("你好我想查下我個集運件而家")
        assert spec._task is None, "间隔门(600ms)照旧拦(云臂同)"
        return prewarm

    asyncio.run(run())


def test_local_lane_ignores_cloud_guardrails(monkeypatch):
    """护栏是云臂专属:local 车道在护栏收紧档下照常开火(逐字节旧行为)。"""
    monkeypatch.setenv("BOK_PREFILL_SPEC_CLOUD_MAX_CHARS", "10")
    monkeypatch.setenv("BOK_PREFILL_SPEC_CLOUD_MAX_PER_CALL", "1")

    async def run():
        spec, prewarm = _spec()  # lane 缺省 "local"
        _arm(spec)
        spec.on_stable_prefix("你好我想查下我個")
        await spec._task
        return prewarm, spec

    prewarm, spec = asyncio.run(run())
    assert len(prewarm.calls) == 1 and spec._cloud_fires == 0, "local 不经云护栏"
