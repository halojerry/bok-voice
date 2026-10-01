"""F7 PERCEIVED 真墙钟（2026-09-28 尾部手术③）：commit→首音频墙钟毫秒落 turns。

背景：assistant 账本行原先 started_ms=now_ms-latency（近似）、ended_ms=落库时刻，
与「用户讲完→AI 出声」真墙钟不同源。F7 在提交点(on_user_turn_completed)记
commit_ms、首音频监听记 first_audio_ms（绝对毫秒，与 _t0 同基准），assistant
分支成对 pop 后作 started_ms/ended_ms；缺任一则回退旧近似（开场白/脚本直念等）。

零新列（started_ms/ended_ms CP 侧已plumbed）、零 CP/模型改动。本文件用源级 pin
钉三个站点（同仓 test_asr_polish_wiring/test_late_answer_anchor_strip 风格），
摘掉任一即红。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")


def _fn_body(src: str, header: str) -> str:
    """抓一个 def/async def 函数体（直到下一个同级 def 或类级赋值）。"""
    m = re.search(
        re.escape(header) + r".*?(?=\n    (?:async def |def )|\n    [A-Za-z_]+ =)",
        src,
        re.S,
    )
    assert m, f"未找到函数: {header}"
    return m.group(0)


def test_commit_site_stamps_commit_ms():
    """站点①提交点：_turn_timing['commit_ms'] = 提交绝对毫秒（与 _t0 同基准）。"""
    assert '_turn_timing["commit_ms"] = int((time.monotonic() - _t0) * 1000)' in AGENT_SRC


def test_first_audio_listener_stamps_first_audio_ms_without_popping_commit():
    """站点②首音频：记 first_audio_ms；commit 只读不 pop（BOK_TURN_TIMING 仍要读）。"""
    body = _fn_body(AGENT_SRC, "def _on_reply_first_audio_timing")
    assert '_turn_timing["first_audio_ms"] = int((time.monotonic() - _t0) * 1000)' in body
    assert '_turn_timing.get("commit")' in body
    # 不 pop commit：否则后续 assistant 行拿不到 commit_ms / BOK_TURN_TIMING 断。
    assert '_turn_timing.pop("commit"' not in body


def test_assistant_branch_uses_wallclock_pair_with_fallback():
    """站点③assistant 分支：成对 pop commit_ms/first_audio_ms 作真墙钟；缺则回退。"""
    assert '_commit_ms = _turn_timing.pop("commit_ms", None)' in AGENT_SRC
    assert '_first_audio_ms = _turn_timing.pop("first_audio_ms", None)' in AGENT_SRC
    assert "started_ms, _ended_ms = _commit_ms, _first_audio_ms" in AGENT_SRC
    # 回退仍在（无 commit 戳的出声/旁路轮走旧近似）。
    assert "started_ms = max(0, now_ms - latency)" in AGENT_SRC
    # ended_ms 真实注入账本生产者（started_ms/ended_ms 对）。
    assert "ended_ms=_ended_ms" in AGENT_SRC


def test_report_assistant_turn_accepts_and_forwards_ended_ms():
    """账本生产者收 ended_ms 覆盖；缺省回退落库时刻（旧行为）。"""
    assert "ended_ms: int | None = None," in AGENT_SRC
    assert "ended_ms=ended_ms if ended_ms is not None else int((time.monotonic() - _t0) * 1000)," in AGENT_SRC
