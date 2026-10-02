"""B 线译文播放背压（_PlaybackBacklog）与播报时长估算（纯函数直喂）。

背景（2026-09-16 P2）：句级提交 + 打断默认关之后，源语语速 > 译文播报速度时，
译文会在框架 speech 队列里无限堆积（体感 lag 雪球）。策略=追最新：估时超限从
最旧弃未开播的句，队头与最新一条永不弃；interrupt 必须 force=True（会话打断
默认关，非 force 会 RuntimeError）。v1 PlaybackScheduler（services/
realtime-translation/src/playback-scheduler.js）的 maxBacklogMs 门的移植。
"""

from __future__ import annotations

import os

import pytest

from agent_runtime.interpret import (
    _PlaybackBacklog,
    _estimate_speech_seconds,
)


class _FakeHandle:
    """SpeechHandle 替身：done()/chat_items/interrupt 三点足矣。"""

    def __init__(self, text: str = "", done: bool = False):
        self._items = [_FakeItem(text)] if text else []
        self._done = done
        self.interrupt_calls: list[bool] = []

    def done(self) -> bool:
        return self._done

    @property
    def chat_items(self):
        return self._items

    def interrupt(self, *, force: bool = False):
        self.interrupt_calls.append(force)
        self._done = True


class _FakeItem:
    def __init__(self, text: str):
        self.text_content = text


# ---- 估时 ----


def test_estimate_zh_by_chars():
    # 「你好我想了解一下你们的产品」= 13 字（标点剥掉）→ 13/5 = 2.6s
    assert _estimate_speech_seconds("你好，我想了解一下你们的产品。", "zh") == pytest.approx(2.6)


def test_estimate_en_by_words():
    assert _estimate_speech_seconds("hello there my good friend", "en") == pytest.approx(5 / 2.6)


def test_estimate_empty_and_floor():
    assert _estimate_speech_seconds("", "zh") == 0.0
    assert _estimate_speech_seconds("好", "zh") == pytest.approx(0.8)  # 短句地板
    assert _estimate_speech_seconds("hi", "en") == pytest.approx(0.8)


# ---- 背压门槛 ----


def _backlog(max_s: float, monkeypatch, target_lang: str = "zh") -> _PlaybackBacklog:
    monkeypatch.setenv("BOK_INTERP_MAX_BACKLOG_S", str(max_s))
    monkeypatch.delenv("BOK_INTERP_BACKLOG", raising=False)
    return _PlaybackBacklog(target_lang)


def test_gate_drops_oldest_queued_keeps_head_and_newest(monkeypatch):
    bl = _backlog(2.0, monkeypatch)
    h1 = _FakeHandle("一二三四五六七八九十")  # 10 字 → 2.0s
    h2 = _FakeHandle("一二三四五六七八九十")
    h3 = _FakeHandle("一二三四五六七八九十")

    assert bl.on_speech_created(h1) == (1, pytest.approx(0.0), 0)  # 队头在播,等待积压=0
    assert bl.on_speech_created(h2) == (2, pytest.approx(2.0), 0)  # 等待=2.0 ≤门:宁积不弃
    depth, est, dropped = bl.on_speech_created(h3)
    assert (depth, dropped) == (2, 1)
    assert est == pytest.approx(2.0)  # 等待积压(弃后)
    assert not h1.interrupt_calls  # 队头=当前播报永不弃
    assert h2.interrupt_calls == [True]  # 未开播的最旧句 force 中断
    assert not h3.interrupt_calls  # 最新一条永不弃


def test_gate_purges_done_and_re_estimates(monkeypatch):
    bl = _backlog(1.0, monkeypatch)
    h1 = _FakeHandle("一二三四五六七八九十")
    h2 = _FakeHandle("", done=False)  # 新句暂无 chat item → 地板 0.8
    h3 = _FakeHandle("", done=False)

    bl.on_speech_created(h1)
    bl.on_speech_created(h2)
    depth, est, dropped = bl.on_speech_created(h3)
    # 等待积压 h2(0.8)+h3(0.8)=1.6 > 1 → 弃 index1(h2);队头 h1 不计入门槛
    assert dropped == 1
    assert est == pytest.approx(0.8)
    assert h2.interrupt_calls == [True]

    # 播完/已弃的句下一轮清账（被 force 中断的 h2 同样 done）
    h1._done = True
    h4 = _FakeHandle("")
    depth, est, dropped = bl.on_speech_created(h4)
    # h1、h2 已 done 清账 → 队列只剩 h3+h4
    assert (depth, dropped) == (2, 0)
    assert est == pytest.approx(0.8)
    assert not h3.interrupt_calls  # 清账后 h3 变队头，永不弃
    h5 = _FakeHandle("")
    depth, est, dropped = bl.on_speech_created(h5)
    assert h4.interrupt_calls == [True]  # h4 现在是队头之后最旧的未播句
    assert (depth, dropped) == (2, 1)
    assert est == pytest.approx(0.8)


def test_head_playing_alone_never_triggers_drops(monkeypatch):
    """门槛语义 v2（2026-09-23 修复波#2,task-9 §14 实证）：队头=正在播报的沉没
    成本,不计入门槛——旧算法把队头全额估时计入,单句译文即可「超门」但 depth<3
    结构性弃不了,门槛日志恒饱和假警（est_ms=1600 drop=0）。"""
    bl = _backlog(1.0, monkeypatch)
    head = _FakeHandle("一" * 50)  # 长译文在播(10s 估时)
    depth, est, dropped = bl.on_speech_created(head)
    assert (depth, est, dropped) == (1, pytest.approx(0.0), 0)
    # 队头之后来一句短译文:等待积压 0.8 ≤ 1 → 不弃、不假警
    depth, est, dropped = bl.on_speech_created(_FakeHandle(""))
    assert (depth, est, dropped) == (2, pytest.approx(0.8), 0)


def test_source_queue_counts_into_gate_and_becomes_drop_candidate(monkeypatch):
    """源句队列计入门槛:摘译候选=最旧的待译源句(译文未生成,零音频浪费;
    原文行已落库=摘译保文,与 _src_q 溢出摘译同语义)。「估算时长超门槛即入
    弃选,不只看 depth」(task-9 §14:depth<3 结构性不触发→现网积压门失效)。"""
    bl = _backlog(1.0, monkeypatch)
    head = _FakeHandle("")  # 队头在播
    bl.on_speech_created(head)
    # 源队列积 2 句(各 ~1.6s):即使 say 队只有队头+最新(depth 2 无弃句候选),
    # 等待+源队列=3.2 > 1 → 摘译最旧源句
    bl.set_source_backlog(3.2)
    depth, est, dropped = bl.on_speech_created(_FakeHandle(""))  # 最新译文(0.8)
    assert dropped == 0  # say 队无候选(最新永不弃)
    assert bl.take_source_drops() == 1  # 摘译指令被 MT worker 消费
    assert bl.take_source_drops() == 0  # 一次性


def test_source_queue_drop_only_when_over_gate(monkeypatch):
    bl = _backlog(6.0, monkeypatch)
    bl.on_speech_created(_FakeHandle(""))
    bl.set_source_backlog(1.6)
    depth, est, dropped = bl.on_speech_created(_FakeHandle(""))
    assert dropped == 0
    assert bl.take_source_drops() == 0  # 0.8+1.6 ≤ 6:默认门槛零行为变化


def test_source_queue_say_candidates_exhausted_before_source_drops(monkeypatch):
    """弃句次序:先弃最旧未播译文(弃音保字),仍超门才轮到摘译源句。"""
    bl = _backlog(1.0, monkeypatch)
    bl.on_speech_created(_FakeHandle("一二三四五六七八九十"))  # 队头(在播,不计门槛)
    bl.on_speech_created(_FakeHandle("一二三四五六七八九十"))  # 等待 2.0
    bl.set_source_backlog(2.0)
    depth, est, dropped = bl.on_speech_created(
        _FakeHandle("一二三四五六七八九十")  # 最新 2.0:弃 h2 后等待仍 2.0 > 1
    )
    assert dropped == 1  # 先弃 say 队最旧(h2)
    assert bl.take_source_drops() == 1  # 仍超门且 say 队无候选(最新永弃保护)→ 摘译一句


def test_source_drop_armed_at_most_one_per_event(monkeypatch):
    """压力阀步进:每次评估至多摘译一句(下轮 speech_created 再评估),不连跳。"""
    bl = _backlog(1.0, monkeypatch)
    bl.on_speech_created(_FakeHandle(""))
    bl.set_source_backlog(10.0)
    bl.on_speech_created(_FakeHandle(""))
    assert bl.take_source_drops() == 1
    # 阈值回读:set_source_backlog 直接替换估值,不累积
    assert bl._source_backlog_s() == pytest.approx(10.0)


# ---- MT worker 消费侧(fix round 1,评审 Minor-6) ----


def test_mt_consume_skip_skips_one_then_translates(monkeypatch, capsys):
    """MT worker 取句即消费摘译指令:arm 后首个取到的源句跳过(True),后续照常
    译(False);消费一次性,不重复跳;零 arm 恒 False。"""
    from agent_runtime.interpret import _mt_consume_skip

    bl = _backlog(1.0, monkeypatch)
    assert _mt_consume_skip(bl, "正常句") is False  # 零 arm → 照常 MT
    bl.source_drops_pending = 1                     # 门槛 arm 一条摘译
    assert _mt_consume_skip(bl, "被摘译句") is True  # 真跳过（不译不播）
    assert _mt_consume_skip(bl, "下一句") is False  # 消费一次性，后续照常
    assert "source-skip x1" in capsys.readouterr().out


def test_gate_disabled_via_master_switch(monkeypatch):
    monkeypatch.setenv("BOK_INTERP_BACKLOG", "0")
    bl = _PlaybackBacklog("zh")
    assert bl.enabled is False
    monkeypatch.setenv("BOK_INTERP_BACKLOG", "1")
    bl2 = _PlaybackBacklog("zh")
    assert bl2.enabled is True
    monkeypatch.setenv("BOK_INTERP_MAX_BACKLOG_S", "0")
    assert _PlaybackBacklog("zh").enabled is False  # 0=关


def test_gate_max_s_env_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("BOK_INTERP_MAX_BACKLOG_S", "nonsense")
    monkeypatch.delenv("BOK_INTERP_BACKLOG", raising=False)
    assert _PlaybackBacklog("zh")._max_s == pytest.approx(6.0)


def test_gate_zero_max_s_never_drops(monkeypatch):
    bl = _backlog(0.0, monkeypatch)
    for _ in range(5):
        bl.on_speech_created(_FakeHandle("一二三四五六七八九十"))
    assert bl.dropped == 0  # enabled=False 时 entrypoint 钩子直接短路


def test_interrupt_never_non_force(monkeypatch):
    """打断默认关的会话里 SpeechHandle.interrupt() 非 force 会 RuntimeError——
    背压路径必须永远 force=True。"""
    bl = _backlog(2.0, monkeypatch)
    h1 = _FakeHandle("一二三四五六七八九十")
    h2 = _FakeHandle("一二三四五六七八九十")
    h3 = _FakeHandle("一二三四五六七八九十")
    bl.on_speech_created(h1)
    bl.on_speech_created(h2)
    bl.on_speech_created(h3)
    for h in (h1, h2, h3):
        for f in h.interrupt_calls:
            assert f is True


def test_default_env_unmodified(monkeypatch):
    """默认 env（无 BOK_INTERP_*）下门槛=6s、总闸开。"""
    monkeypatch.delenv("BOK_INTERP_BACKLOG", raising=False)
    monkeypatch.delenv("BOK_INTERP_MAX_BACKLOG_S", raising=False)
    bl = _PlaybackBacklog("zh")
    assert bl.enabled is True
    assert bl._max_s == pytest.approx(6.0)
    assert os.environ.get("BOK_INTERP_BACKLOG") is None
