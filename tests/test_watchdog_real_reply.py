"""watchdog 不掐真回复（真机第一批 B 组，2026-09-30 call-fde11c52 实证）。

病理：真机链路比探针慢 30-50%（WebRTC/8k 窄带），回复首帧叠 filler hold 落
5-6s；watchdog 6s 线→synth extend 仅 +2s 一次性→真回复（LLM 已完整生成
gen=24、TTS 攒句中）差 1-4s 被 force-interrupt 掐死，用户听到的全是
filler/ack/nudge 兜底层——「打断后不结合上下文」的真身。

修复契约：
- B2：synth 顺延一次性→**至多两次**（synth_extended bool→int 计数；真死火
  最多多等两窗，兜底不变）；
- B1：filler 盖耳顺延窗按**垫话时间轴剩余**撑高（hold_if_playing()+缓冲，
  与 env 下限取大）——垫话 3s 音频盖耳期 watchdog 只延 2s 是结构性短窗。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.agent import _watchdog_synth_extend_reason  # noqa: E402

AGENT_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")


class _FakeSession:
    def __init__(self, state: str):
        self.agent_state = state


def _pending(v: float):
    return lambda: v


# ---- B2：synth 顺延两次封顶 ----


def test_synth_extend_allows_second_window():
    """第一次顺延后（已延 1 次）再判在途 → 仍顺延（第二窗）；两次后才封顶。"""
    assert _watchdog_synth_extend_reason(_pending(100.0), 50.0, 1, _FakeSession("")) == "tts_pending"
    assert _watchdog_synth_extend_reason(_pending(100.0), 50.0, 2, _FakeSession("")) == ""


def test_synth_extend_fire_uses_int_counter():
    """fire 路计数为 int 累加（+1 不是置 True），arm 复位为 0——源级 pin。"""
    assert '_watchdog["synth_extended"] = int(_watchdog.get("synth_extended", 0) or 0) + 1' in AGENT_SRC
    assert '"synth_extended": 0' in AGENT_SRC  # arm 复位（int 零初始化）
    assert 'bool(_watchdog.get("synth_extended", False))' not in AGENT_SRC  # 旧 bool 判定退役


# ---- B1：filler 顺延窗按垫话时间轴剩余撑高 ----


def test_filler_extend_uses_hold_timeline():
    """源级 pin：filler on_fired 顺延窗 = max(env 下限, hold_if_playing()+缓冲)。"""
    assert "hold_if_playing()" in AGENT_SRC
    # 顺延窗计算处含 hold 时间轴（grep 锚：_extend_response_watchdog 体内）
    body_start = AGENT_SRC.index("def _extend_response_watchdog")
    body_end = AGENT_SRC.index("\n    def ", body_start + 10)
    body = AGENT_SRC[body_start:body_end]
    assert "hold_if_playing()" in body, "filler 顺延窗必须吃垫话时间轴剩余（B1）"
    assert "+ 1.5" in body, "hold 之上加 1.5s 出声缓冲"
