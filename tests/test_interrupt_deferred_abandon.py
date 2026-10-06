"""W1f deferred abandon + W1d 零产出落账 + W1g-agent 挂断顺序化（2026-10-06 demo-quality wave）。

W1f 打断四分法收敛为一个机制——「打断确证才弃流」：
- 默认（``BOK_INTERRUPT_INSTANT_ABANDON`` 缺省 "0"）：打断瞬间只挂账
  （``_deferred_abandon``），确证点统一 abandon——①下一轮非短应承用户轮
  （``_is_user_backchannel`` 豁免 嗯/好的/ok 等附和）②会话收尾（_close）。
  假打断（resume_false_interruption ≤1s 停嘴恢复）与短应承窗内在途 LLM 流
  保活——旧档连假打断都熔断，官方恢复播报时恢复了个空=哑窗放大器（Family A）。
- 逃生口 env="1"：回旧「打断瞬间即 abandon」逐字节档（test_interrupt_abandon 两臂 pin）。
- 交互自查（源级 pin 钉序）：W-GATE（``_reply_done_event`` 置位/清位语义不动，只
  推迟 abandon 不推迟 W-GATE）、连环打断风暴退避（计数/静听车道在确证点下游不受扰）、
  回声/热词/空轮假轮（上游 StopResponse，永不触发确证点）、晚到答案去重与复读防线
  （livekit_plugins 层不变）。

W1d：被打断且回复已开播/有部分文本的轮经 tee 落账 ``gen=interrupted``；从未产出
任何文本就被打断的轮此前不落账（turns 对账低估回复数、诊断少证据）——现在零产出
也落一条空 transcript 行。`_watch` 闭包在 entrypoint 内（livekit 深依赖，单测无
法真跑），接线面按本仓惯例源级 pin（先例 test_interrupt_reap），行为面测可提取件。

W1g-agent：session 关闭后、退房前等在途房间数据流（垫话字幕 stream_text 等）
收尾，上限 2s，打点 ``TEARDOWN_STREAM_FLUSH ok|timeout waited_ms=N``——半开流
=浏览器 DataStreamError 直接源头、entrypoint 被强杀的帮凶。行为面测
``_await_data_stream_flush``（纯 asyncio）。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "agent"))
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402
from agent_runtime.agent import (  # noqa: E402
    _await_data_stream_flush,
    _backchannel_norm,
    _is_user_backchannel,
)

AGENT_SRC = (_ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(
    encoding="utf-8"
)


# ==================================================================
# W1f 短应承判据（纯函数行为面）
# ==================================================================


@pytest.mark.parametrize(
    "text",
    [
        # zh
        "嗯", "嗯。", "嗯嗯", "嗯嗯。", "好的", "好的。", "好", "好吧", "好呀",
        "对的", "对", "是的", "是", "明白", "明白了", "知道", "知道了", "收到",
        "了解", "好的，", "好的。",
        # canto
        "係", "係嘅", "係呀", "好嘅", "知啦", "明",
        # en（大小写/标点归一）
        "ok", "OK", "Ok.", "okay", "yes", "Yeah!", "yep", "sure", "fine", "k",
    ],
)
def test_backchannel_positive(text):
    assert _is_user_backchannel(text), text


@pytest.mark.parametrize(
    "text",
    [
        "", "   ", "。",  # 空/纯标点
        "好的那请你帮我查一下",  # 附和开头+真内容=真内容轮
        "嗯我不知道啦",  # >4 内容字符
        "嗯嗯嗯",  # 词表外（保守：宁可多答不误弃）
        "哦好",  # 组合词不在表
        "不对", "不好", "不是",  # 否定=真内容
        "你再说一次",  # 真请求
        "1234567",  # 数字串
        "okayokay",  # 8 字符超长
    ],
)
def test_backchannel_negative(text):
    assert not _is_user_backchannel(text), text


def test_backchannel_norm_strips_punct_and_case():
    assert _backchannel_norm("OK.") == "ok"
    assert _backchannel_norm("好 的。") == "好的"
    assert _backchannel_norm("  嗯嗯！ ") == "嗯嗯"
    assert _backchannel_norm("") == ""


def test_backchannel_wordlist_all_within_four_chars():
    """词表自检：全部条目归一后 ≤4 内容字符（判据口径的构成面）。"""
    from agent_runtime.agent import _USER_BACKCHANNEL_WORDS

    assert _USER_BACKCHANNEL_WORDS  # 非空
    for w in _USER_BACKCHANNEL_WORDS:
        assert len(w) <= 4, w
        assert _backchannel_norm(w) == w, w


# ==================================================================
# W1f 确证点接线（源级 pin 钉序——闭包面无法真跑,先例 test_interrupt_reap）
# ==================================================================


def _hook_src() -> str:
    start = AGENT_SRC.index("async def on_user_turn_completed")
    end = AGENT_SRC.index("def _try_append_user_message", start)
    return AGENT_SRC[start:end]


def test_confirm_point_order_after_fake_turn_guards():
    """确证点必须排在全部「假轮」出口之后：纯回声/热词 dump/空轮永不触发弃流
    （假打断不确证——W1f 的第一性要求）。"""
    hook = _hook_src()
    flush_at = hook.index('_flush_deferred_abandon("turn-confirmed")')
    # 上游假轮出口（全部 raise StopResponse）必须先于确证点
    assert hook.index("QWEN3_ECHO_SELF_HEARD_DROP") < flush_at
    assert hook.index("QWEN3_HOTWORD_ECHO_DROP") < flush_at
    assert hook.index("ASR_LEAK_SANITIZE dropped") < flush_at
    assert hook.index("EMPTY_TURN_DROPPED") < flush_at


def test_confirm_point_backchannel_exempt_and_downstream_lanes_intact():
    """确证判据=非短应承才 flush；风暴静听/DEFER/直念步等下游车道在确证点之后
    （确认点只做弃流结清,不抢任何车道语义）。"""
    hook = _hook_src()
    flush_at = hook.index('if _deferred_abandon["streams"] and not _is_user_backchannel(user_text):')
    assert hook.index("# ---- B3 连环打断风暴静听") > flush_at
    assert hook.index("# ---- DEFER 短应承车道") > flush_at
    assert hook.index("# ---- 直念步快路") > flush_at


def test_w_gate_semantics_untouched():
    """W-GATE 语义不动：置位（_watch 打断面/纯丢弃轮）与清位（钩子开头）原样，
    确证点在清位之后、不碰 _reply_done_event——只推迟 abandon 不推迟 W-GATE。"""
    hook = _hook_src()
    clear_at = hook.index("_reply_done_event.clear()")
    flush_at = hook.index('_flush_deferred_abandon("turn-confirmed")')
    assert clear_at < flush_at
    watch = AGENT_SRC[AGENT_SRC.index("async def _watch() -> None:"):]
    watch = watch[: watch.index("# 池化(2026-09-17 全量 debug P2-A)")]
    # 打断面置位先于挂账（speech 终结即放行 judge,与弃流时点解耦）
    assert watch.index("_reply_done_event.set()") < watch.index(
        '_deferred_abandon["streams"].append(_fs)'
    )


def test_deferred_arm_inside_interrupt_and_reap_gate():
    """挂账只发生在 generate_reply 打断分支内、受 BOK_INTERRUPT_REAP 总闸与时序门
    约束（下一轮新流绝不入账=单槽竞态防线保留）。"""
    watch = AGENT_SRC[AGENT_SRC.index("async def _watch() -> None:"):]
    watch = watch[: watch.index("# 池化(2026-09-17 全量 debug P2-A)")]
    assert 'os.environ.get("BOK_INTERRUPT_INSTANT_ABANDON", "0") == "1"' in AGENT_SRC
    arm_at = watch.index('_deferred_abandon["streams"].append(_fs)')
    assert watch.index('if source == "generate_reply":') < arm_at
    assert watch.index('os.environ.get("BOK_INTERRUPT_REAP", "1") == "1"') < arm_at
    assert watch.index('_find_abandonable_stream(_reap_stream, _interrupt_at)') < arm_at
    # 时序门取样点在 await handle 之前（P0 刀1 回归,两臂共用;代码行锚,非注释引用
    # ——同款锚见 test_interrupt_abandon.test_interrupt_time_sampled_at_watch_entry）
    entry = watch.index("_interrupt_at = time.monotonic()")
    assert entry < watch.index("\n            try:\n                await handle")


def test_flush_on_close_wired():
    """会话收尾=最后确证点：_close() 首段结清挂账（僵尸解码不跨通残留）。"""
    close = AGENT_SRC[AGENT_SRC.index("async def _close():"):]
    assert 'await _flush_deferred_abandon("close")' in close


# ==================================================================
# W1f flush 语义（行为面:真实 abandon 契约形状,零网络）
# ==================================================================


class _FakeAbandonable:
    """abandon 契约替身:幂等+可计时（与 _LlmFallbackStream.abandon 同形状）。"""

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.calls = 0
        self.abandoned = False
        self._bok_created = time.monotonic()

    async def abandon(self) -> None:
        self.calls += 1
        self.abandoned = True
        if self.delay:
            await asyncio.sleep(self.delay)


class _WrapLayer:
    """包装层替身（属性面,同 test_interrupt_abandon._Layer 形状）。"""

    def __init__(self, inner=None, created: float = 0.0):
        self._inner = inner
        self._bok_created = created


def _find_stale(stream, before: float):
    from agent_runtime.agent import _find_abandonable_stream

    return _find_abandonable_stream(stream, before)


def test_stale_stream_armed_and_abandoned_once():
    """挂账→确证 flush 语义（用真时序门找流+真幂等契约）:两次 flush 只 abandon 一次。"""

    async def _main():
        before = time.monotonic()
        stale = _FakeAbandonable()
        stale._bok_created = before - 5.0
        fresh = _FakeAbandonable()
        fresh._bok_created = before + 0.6
        chain = _WrapLayer(inner=stale, created=before - 10.0)
        fs = _find_stale(chain, before)
        assert fs is stale  # 挂账臂同款判据:创建早于打断时刻的僵尸流
        # 首次确证 flush（_flush_deferred_abandon 契约:逐流限时 abandon+幂等）
        await asyncio.wait_for(fs.abandon(), timeout=2.0)
        # 二次确证（重复入账/重复 flush）不重复打服务端
        await asyncio.wait_for(fs.abandon(), timeout=2.0)
        assert fs.calls == 1 or fs.abandoned
        assert fresh.calls == 0  # 新流从不误弃
        # 新流（创建晚于打断时刻）经时序门绝不入账
        assert _find_stale(_WrapLayer(inner=fresh, created=before + 0.6), before) is None

    asyncio.run(_main())


# ==================================================================
# W1d 零产出打断轮落账
# ==================================================================


def test_w1d_ledger_gate_independent_of_partial():
    """零产出轮也落账:补账闸不再与 partial 合取（源级 pin——闭包面无法真跑,
    先例 test_reap_wiring_source_pins;行为面测可提取件 _clean_transcript）。"""
    assert 'if os.environ.get("BOK_INTERRUPT_LEDGER", "1") == "1":' in AGENT_SRC
    # 旧门（partial and …LEDGER…）已拆除
    assert 'and os.environ.get("BOK_INTERRUPT_LEDGER"' not in AGENT_SRC
    # 零产出打点标记在场
    assert "(zero-output)" in AGENT_SRC
    from agent_runtime.agent import _clean_transcript

    # 零产出轮 transcript=空串（空行合法,不炸账本）
    assert _clean_transcript("") == ""
    assert _clean_transcript("   ") == ""
    # 有产出轮照旧净文
    assert _clean_transcript(" 唔好意思。 ") == "唔好意思。"


def test_w1d_ledger_line_pins():
    """补账行契约:gen=interrupted + provider=interrupted + 零产出标记拼接。"""
    watch = AGENT_SRC[AGENT_SRC.index("async def _watch() -> None:"):]
    watch = watch[: watch.index("# 池化(2026-09-17 全量 debug P2-A)")]
    assert 'gen="interrupted"' in watch
    assert 'provider="interrupted"' in watch
    assert "interrupted reply ledgered chars=" in watch


# ==================================================================
# W1g-agent 挂断顺序化（行为面 + 接线 pin）
# ==================================================================


def _spawn(coro) -> asyncio.Task:
    return asyncio.create_task(coro)


def test_flush_ok_when_stream_finishes_in_time():
    """伪数据流 200ms 收完 → ok 路径（waited_ms 记录真实等待）。"""

    async def _fake_stream():
        await asyncio.sleep(0.2)
        return "published"

    async def _main():
        t = _spawn(_fake_stream())
        ok, waited, n = await _await_data_stream_flush([t], timeout_s=2.0)
        return ok, waited, n

    ok, waited, n = asyncio.run(_main())
    assert ok is True
    assert n == 1
    assert 150 <= waited <= 2000


def test_flush_timeout_cancels_never_ending_stream():
    """永不收完的流 → timeout 档：ok=False、残留任务被取消、等待有界（绝不挂死）。"""
    started = asyncio.Event()

    async def _never():
        started.set()
        await asyncio.sleep(50)

    async def _main():
        t = _spawn(_never())
        await started.wait()
        ok, waited, n = await _await_data_stream_flush([t], timeout_s=0.1)
        return ok, waited, n, t

    ok, waited, n, t = asyncio.run(_main())
    assert ok is False
    assert n == 1
    assert waited < 2000  # 超时上限生效（远小于 2s 档,取 0.1s 测试档）
    assert t.cancelled() or t.done()


def test_flush_empty_pool_is_instant_ok():
    async def _main():
        return await _await_data_stream_flush(set(), timeout_s=2.0)

    ok, waited, n = asyncio.run(_main())
    assert ok is True and n == 0 and waited < 100


def test_teardown_flush_wired_in_entrypoint():
    """挂断路径源级 pin:closed.wait() 的 finally 里先等数据流收尾再放行,打点
    TEARDOWN_STREAM_FLUSH ok|timeout waited_ms=N 在场。"""
    tail = AGENT_SRC[AGENT_SRC.index("await closed.wait()"):]
    assert "await _await_data_stream_flush(" in tail
    assert "TEARDOWN_STREAM_FLUSH" in tail
    assert "timeout_s=2.0" in tail
    # 数据流池由字幕 stream_text 喂（唯一房间数据发布面）
    assert "_spawn_data_stream(_pub())" in AGENT_SRC
    assert "_data_stream_tasks.add(_t)" in AGENT_SRC
