"""M-30 turns 上报本地暂存重放测试（2026-09-23 生产就绪修复波#2）。

背景（task-9 腿 9.6 实证）：CP 通话中被 kill -9，通话续命但断窗 ~14 轮 turns
上报永久丢——`ControlPlaneClient.add_turn` 单发 POST、零重试零队列（失败即弃，
只剩 REPORT_TASK_ERR），观测/学习账本（turns、QA 挖掘、gap mining 原料）出现
静默洞。

修复契约：
- `add_turn` 失败（连接错误 / 5xx / 429）→ 本地有界暂存（deque，drop-oldest）+
  背景重放任务；CP 恢复后按序补齐（成功轮也踢追平，不必等下次失败）；
- 4xx（404 已删单 / 401 鉴权）不重放（重放无益），与旧行为同弃；
- 杀开关 `BOK_TURNS_REPLAY`（默认开；"0"=回旧行为：失败即抛，调用方 REPORT_TASK_ERR）；
- 重放周期有界（连续失败 N 次即收工），下次 add_turn 失败/成功再踢新周期——自愈不跑飞；
- aclose 有界 drain：会话收尾尽力补交。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("BOK_TURNS_REPLAY", "1")

import agent_runtime.control_plane as cp_mod  # noqa: E402
from agent_runtime.control_plane import ControlPlaneClient  # noqa: E402


class _FlagCP:
    """MockTransport 处理器：down 旗标断联；恢复后按 codes（transcript→状态码）
    回状态（缺省 200）——按转键控而非 FIFO，重试噪声不消费脚本。

    只记 2xx 成功投递的 transcript（seen）——失败投递不进账本，断言不受重试噪声扰。
    """

    def __init__(self, codes: dict[str, int] | None = None):
        self.down = True
        self.codes = dict(codes or {})
        self.seen: list[str] = []
        self.attempts = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        if self.down:
            raise httpx.ConnectError("cp down")
        transcript = str(dict(request.url.params).get("transcript") or "")
        code = self.codes.get(transcript, 200)
        if code >= 400:
            return httpx.Response(code)
        self.seen.append(transcript)
        return httpx.Response(200)


def _client(codes: dict[str, int] | None = None, monkeypatch=None) -> tuple[ControlPlaneClient, _FlagCP]:
    flaky = _FlagCP(codes)
    cp = ControlPlaneClient("http://cp.test", call_id="call-t")
    cp._client = httpx.AsyncClient(
        base_url="http://cp.test", timeout=5, transport=httpx.MockTransport(flaky.handler)
    )
    if monkeypatch is not None:
        # 测试节奏：毫秒级退避（生产默认 2s 不进测试）
        monkeypatch.setattr(cp_mod, "_TURN_REPLAY_BACKOFF_S", 0.01)
    return cp, flaky


async def _settle(cp: ControlPlaneClient, timeout: float = 5.0) -> None:
    """等当前重放任务落定（含超时兜底，测试不悬挂）。"""
    task = cp._replay_task
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except Exception:
            pass


def test_add_turn_success_posts_once_no_spool(monkeypatch):
    """正常路径零变化：一转一 POST，暂存恒空。"""
    cp, flaky = _client(monkeypatch=monkeypatch)
    flaky.down = False

    async def _run():
        await cp.add_turn("call-t", "customer", "你好")
        await _settle(cp)
        assert len(cp._turn_spool) == 0

    asyncio.run(_run())
    assert flaky.seen == ["你好"]
    asyncio.run(cp.aclose())


def test_add_turn_spools_on_connect_error_then_drains_in_order(monkeypatch):
    """核心场景：CP 断窗 3 轮全部暂存；恢复后按原序补齐（成功轮踢追平）。"""
    cp, flaky = _client(monkeypatch=monkeypatch)

    async def _run():
        for text in ("第一轮", "第二轮", "第三轮"):
            await cp.add_turn("call-t", "customer", text)  # 断窗失败→暂存，不抛
        assert [p["transcript"] for _, p in cp._turn_spool] == ["第一轮", "第二轮", "第三轮"]
        flaky.down = False  # CP 恢复
        await cp.add_turn("call-t", "customer", "第四轮")  # 成功轮踢追平
        await _settle(cp)
        assert len(cp._turn_spool) == 0

    asyncio.run(_run())
    # 断窗三轮由重放按原序补齐（最旧先投）；恢复轮直投与重放组的交错不钉序
    replay_group = [t for t in flaky.seen if t in {"第一轮", "第二轮", "第三轮"}]
    assert replay_group == ["第一轮", "第二轮", "第三轮"]
    assert sorted(flaky.seen) == sorted(["第一轮", "第二轮", "第三轮", "第四轮"])
    asyncio.run(cp.aclose())


def test_add_turn_5xx_spooled_4xx_dropped(monkeypatch):
    """500 暂存待补；404（已删单）不重放——与旧行为同弃。"""
    cp, flaky = _client(codes={"五佰轮": 500, "四零四轮": 404}, monkeypatch=monkeypatch)
    flaky.down = False  # 状态码由 codes 驱动（down 旗标只管断联）

    async def _run():
        await cp.add_turn("call-t", "customer", "五佰轮")   # 500 → 暂存
        await cp.add_turn("call-t", "customer", "四零四轮")  # 404 → 弃
        assert [p["transcript"] for _, p in cp._turn_spool] == ["五佰轮"]
        flaky.codes["五佰轮"] = 200  # 恢复：重试腿放行
        await cp.add_turn("call-t", "customer", "恢复轮")  # 成功踢追平
        await _settle(cp)
        assert len(cp._turn_spool) == 0

    asyncio.run(_run())
    # 404 永不成功投递（不重放）；500 轮由重放补投一次；恢复轮直投一次
    assert "四零四轮" not in flaky.seen
    assert sorted(flaky.seen) == sorted(["五佰轮", "恢复轮"])
    asyncio.run(cp.aclose())


def test_spool_bounded_overflow_drops_oldest(monkeypatch):
    """有界暂存：超上限丢最旧（防长断窗无界增长），新轮保命。"""
    cp, _flaky = _client(monkeypatch=monkeypatch)
    monkeypatch.setattr(cp_mod, "_TURN_SPOOL_MAX", 3)

    async def _run():
        for i in range(5):
            await cp.add_turn("call-t", "customer", f"轮{i}")
            await _settle(cp)

    asyncio.run(_run())
    assert len(cp._turn_spool) == 3
    assert [p["transcript"] for _, p in cp._turn_spool] == ["轮2", "轮3", "轮4"]
    asyncio.run(cp.aclose())


def test_kill_switch_off_raises_like_before(monkeypatch):
    """BOK_TURNS_REPLAY=0 → 旧行为：失败上抛（REPORT_TASK_ERR 面），零暂存。"""
    monkeypatch.setenv("BOK_TURNS_REPLAY", "0")
    cp, _flaky = _client(monkeypatch=monkeypatch)

    async def _run():
        with pytest.raises(httpx.ConnectError):
            await cp.add_turn("call-t", "customer", "开关关闭轮")

    asyncio.run(_run())
    assert len(cp._turn_spool) == 0
    asyncio.run(cp.aclose())


def test_replay_cycle_bounded_then_rekicked_by_next_turn(monkeypatch):
    """重放周期有界：连续失败 N 次收工（暂存保留）；下次 add_turn 失败再踢新周期。"""
    cp, _flaky = _client(monkeypatch=monkeypatch)
    monkeypatch.setattr(cp_mod, "_TURN_REPLAY_MAX_ATTEMPTS", 2)

    async def _run():
        await cp.add_turn("call-t", "customer", "第一轮")
        task1 = cp._replay_task
        assert task1 is not None
        await _settle(cp)
        assert task1.done()  # 周期收工（连续失败 2 次）
        assert len(cp._turn_spool) == 1  # 暂存保留
        await cp.add_turn("call-t", "customer", "第二轮")  # 失败→再踢新周期
        task2 = cp._replay_task
        assert task2 is not None and task2 is not task1 and not task2.done()
        await _settle(cp)
        assert len(cp._turn_spool) == 2

    asyncio.run(_run())
    asyncio.run(cp.aclose())


def test_aclose_bounded_drain_flushes_pending(monkeypatch):
    """会话收尾：aclose 再踢重放并有界等待，断窗轮次尽力补交（不永久挂住）。"""
    cp, flaky = _client(monkeypatch=monkeypatch)

    async def _run():
        await cp.add_turn("call-t", "customer", "收尾前一轮")  # 失败→暂存（周期很快收工）
        assert len(cp._turn_spool) == 1
        flaky.down = False
        await cp.aclose()  # 有界 drain：补交

    asyncio.run(_run())
    assert flaky.seen == ["收尾前一轮"]
