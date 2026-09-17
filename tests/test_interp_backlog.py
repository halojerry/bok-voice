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

    assert bl.on_speech_created(h1) == (1, pytest.approx(2.0), 0)
    assert bl.on_speech_created(h2) == (2, pytest.approx(4.0), 0)  # len==2：宁积不弃
    depth, est, dropped = bl.on_speech_created(h3)
    assert (depth, dropped) == (2, 1)
    assert est == pytest.approx(4.0)
    assert not h1.interrupt_calls  # 队头=当前播报永不弃
    assert h2.interrupt_calls == [True]  # 未开播的最旧句 force 中断
    assert not h3.interrupt_calls  # 最新一条永不弃


def test_gate_purges_done_and_re_estimates(monkeypatch):
    bl = _backlog(2.0, monkeypatch)
    h1 = _FakeHandle("一二三四五六七八九十")
    h2 = _FakeHandle("", done=False)  # 新句暂无 chat item → 地板 0.8
    h3 = _FakeHandle("", done=False)

    bl.on_speech_created(h1)
    bl.on_speech_created(h2)
    depth, est, dropped = bl.on_speech_created(h3)
    # h1(2.0)+h2(0.8)+h3(0.8)=3.6 > 2 → 弃 index1(h2)
    assert dropped == 1
    assert est == pytest.approx(2.8)
    assert h2.interrupt_calls == [True]

    # 播完/已弃的句下一轮清账（被 force 中断的 h2 同样 done）
    h1._done = True
    h4 = _FakeHandle("")
    depth, est, dropped = bl.on_speech_created(h4)
    # h1、h2 已 done 清账 → 队列只剩 h3+h4
    assert (depth, dropped) == (2, 0)
    assert est == pytest.approx(1.6)
    assert not h3.interrupt_calls  # 清账后 h3 变队头，永不弃
    h5 = _FakeHandle("")
    depth, est, dropped = bl.on_speech_created(h5)
    assert h4.interrupt_calls == [True]  # h4 现在是队头之后最旧的未播句
    assert (depth, dropped) == (2, 1)
    assert est == pytest.approx(1.6)


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
