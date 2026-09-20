"""D4 止血:ASR chunk POST「成功才清」(2026-09-20,A_LINE_LOGIC §7 D4)。

旧版 `_maybe_partial` 先 `_pending.clear()` 再 POST sidecar `/api/chunk`,
异常静默 return——sidecar 瞬时不可用(连接拒/超时)时该窗 PCM 永久丢失且
零日志。修复后:失败保留(随下一窗/finish 自然带上)+ `QWEN3_ASR_CHUNK_POST_ERR`
打点 + 有界上限(丢最旧) + kill-switch(`QWEN3_ASR_CHUNK_KEEP=0` 回旧档)。

离线纯函数面:`_chunk_keep_plan`(处置计划)/`_chunk_keep_enabled`(总门)/
`_pcm_window_ms`(打点换算)全部模块级,livekit_plugins 顶层 import 不拖
livekit 依赖(本文件只触这三个名字)。异步 POST 行为面属第三方 stream 类,
不在本测——线上验收走 probe_fast_speech 既有链。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.providers import livekit_plugins as lkp  # noqa: E402


def test_keep_enabled_default_on(monkeypatch):
    """缺省=新档(成功才清);显式 0=回退旧档;其它非 1 值一律视为关。"""
    monkeypatch.delenv("QWEN3_ASR_CHUNK_KEEP", raising=False)
    assert lkp._chunk_keep_enabled() is True
    monkeypatch.setenv("QWEN3_ASR_CHUNK_KEEP", "0")
    assert lkp._chunk_keep_enabled() is False
    monkeypatch.setenv("QWEN3_ASR_CHUNK_KEEP", "1")
    assert lkp._chunk_keep_enabled() is True
    monkeypatch.setenv("QWEN3_ASR_CHUNK_KEEP", "yes")
    assert lkp._chunk_keep_enabled() is False


def test_keep_plan_under_limit_keeps_all():
    """失败窗在上限内 → 整窗保留,零丢弃。"""
    action, drop = lkp._chunk_keep_plan(16000 * 2 * 5)  # 5s < 12s 上限
    assert action == "keep"
    assert drop == 0


def test_keep_plan_at_exact_limit_keeps_all():
    """恰等于上限 → 保留(边界含端点,唔多丢一窗)。"""
    action, drop = lkp._chunk_keep_plan(lkp._ASR_CHUNK_KEEP_MAX_BYTES)
    assert action == "keep"
    assert drop == 0


def test_keep_plan_over_limit_trims_oldest():
    """超上限 → trim,丢弃量=超出部分(从头部丢最旧),保留面恒等于上限。"""
    limit = lkp._ASR_CHUNK_KEEP_MAX_BYTES
    over = limit + 16000 * 2 * 3  # 上限 + 3s 新音频
    action, drop = lkp._chunk_keep_plan(over)
    assert action == "trim"
    assert drop == over - limit
    # 保留面 = over - drop == limit,有界不变量
    assert over - drop == limit


def test_keep_plan_custom_limit():
    """上限由参数注入(调用方以后调档不用改函数);0 上限=全丢的极端档仍返回 trim。"""
    assert lkp._chunk_keep_plan(100, keep_max_bytes=100) == ("keep", 0)
    assert lkp._chunk_keep_plan(101, keep_max_bytes=100) == ("trim", 1)
    assert lkp._chunk_keep_plan(50, keep_max_bytes=0) == ("trim", 50)


def test_pcm_window_ms_conversion():
    """PCM16 单声道 16k:32000 字节=1s=1000ms;0 字节=0ms(打点可读面)。"""
    assert lkp._pcm_window_ms(32000) == 1000
    assert lkp._pcm_window_ms(0) == 0
    assert lkp._pcm_window_ms(16000) == 500


def test_no_pending_clear_in_success_path_source():
    """源码 pin:`_maybe_partial` 不再有无条件 clear;成功路径只 del 已发前缀。

    旧 bug 的结构特征是 POST 前 `self._pending.clear()`——钉死它不得回来
    (回退档的 clear 必须在 `_chunk_keep_enabled()` 的 False 分支内)。
    """
    src = (
        Path(__file__).resolve().parents[1]
        / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py"
    ).read_text(encoding="utf-8")
    body = src[src.index("async def _maybe_partial") : src.index("async def _finish_session")]
    # 旧 bug 签名=快照后紧跟无条件 clear——不得回归(逐字相邻形态)
    assert "pcm = bytes(self._pending)\n        self._pending.clear()" not in body
    # clear 只允许出现在 kill-switch 旧档分支里(位于 `if not _chunk_keep_enabled():` 之后)
    guard = body.find("if not _chunk_keep_enabled():")
    clr = body.find("self._pending.clear()")
    assert guard != -1 and clr != -1 and 0 < clr - guard < 200
    assert "del self._pending[: len(pcm)]" in body  # 成功才清
    assert "QWEN3_ASR_CHUNK_POST_ERR" in body  # 失败可见
    assert "_chunk_keep_plan(" in body  # 有界保留


def test_sidecar_chunk_is_append_semantics():
    """重复提交结论的源码证据钉住:sidecar /api/chunk 追加式 + finish 整段解码。

    若 sidecar 改成整段式(每 chunk 独立解码),`_chunk_keep_plan` 注释里的
    「不主动重发」取舍要重评——本测试先红提醒。
    """
    sidecar = (
        Path(__file__).resolve().parents[1]
        / "services" / "qwen3-asr-sidecar" / "app.py"
    ).read_text(encoding="utf-8")
    assert 'session["chunks"].extend(pcm)' in sidecar  # 追加式
    assert 'pcm = bytes(session["chunks"])' in sidecar  # finish 解码整段累积
