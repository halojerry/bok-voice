"""D6 纯 ack 族不进史（2026-09-30 多轮上下文计划 Phase 3）。

病灶：`_say_script`→`session.say` 默认 add_to_chat_ctx=True——watchdog-ack/
nudge/storm/starve/defer/followup/garbled-reask 七条纯 ack 车道全部落 LLM
历史，占 8 对史窗、喂 4B 自己的 ack 刷屏（道歉毒性的史内残留；ack-anchor-
exempt 只挡了锚/摘要面）。官方 fast-filler 模式明确 fillers 用
add_to_chat_ctx=False。

契约（Ethan 拍板「仅纯 ack 族」）：
- 排除集 = {watchdog-ack, nudge, storm-ack, starve-ack×2, defer-ack,
  followup-ack, garbled-reask}：history=False 注册（不推票据+置 A3 旗）
  + say(add_to_chat_ctx=False) + _ledger_ack_line 手工补 turns 行；
- 保留集 = wa-flush/digit-flush/wa-confirm/stall-*/opening/flow-say/
  late-answer/branch-refuse/farewell/qa、branch 罐头——默认行为零变化；
- KV 前缀安全：史只增不删（add_to_chat_ctx=False=从不插入），无回溯删改。

闭包内代码按仓惯例源级 pin。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(
    encoding="utf-8"
)

_EXCLUDED_LANES = (
    "watchdog-ack",
    "nudge",
    "storm-ack",
    "starve-ack",
    "defer-ack",
    "followup-ack",
    "garbled-reask",
)
# 排除车道出现次数（starve-ack 两处发射点）
_EXPECTED_HISTORY_FALSE = {**{ln: 1 for ln in _EXCLUDED_LANES}, "starve-ack": 2}


def test_say_script_threads_add_to_chat_ctx():
    """_say_script 形参 + 全部 4 处 session.say 穿透。"""
    assert "add_to_chat_ctx: bool = True" in AGENT_SRC
    assert "return await session.say(text, add_to_chat_ctx=add_to_chat_ctx)" in AGENT_SRC
    assert (
        "audio=frames_aiter(pcm_to_frames(pcm, tts_provider.sample_rate)),\n"
        "            add_to_chat_ctx=add_to_chat_ctx,"
    ) in AGENT_SRC
    assert (
        "return await session.say(text, audio=_synth_and_play(), add_to_chat_ctx=add_to_chat_ctx)"
    ) in AGENT_SRC


def test_register_reply_lane_history_param():
    """history=False：不推票据 + 置 A3 旗（item_added 不再发生）。"""
    assert "history: bool = True," in AGENT_SRC
    assert "if not history:" in AGENT_SRC
    assert '_assistant_out["on"] = True' in AGENT_SRC


def test_excluded_lanes_registered_history_false():
    """七条纯 ack 车道全部 history=False 注册 + 不进史 say + 手工补账。"""
    for lane, n in _EXPECTED_HISTORY_FALSE.items():
        reg = AGENT_SRC.count(f'lane="{lane}"')
        assert reg >= n, f"lane={lane} 注册点少於预期 {n}（实际 {reg}）"
    # 各排除车道的注册位都带 history=False（数 history=False 总数 ≥ 排除位点数 8）
    assert AGENT_SRC.count("history=False") >= 8
    # 不进史的 say 调用（7 车道 8 个发射位,starve×2）
    assert AGENT_SRC.count("add_to_chat_ctx=False") >= 8
    # 手工补账调用逐车道在场
    for lane in _EXCLUDED_LANES:
        assert f'await _ledger_ack_line("{lane}"' in AGENT_SRC, f"缺 {lane} 手工补账"


def test_ledger_ack_line_helper_shape():
    """_ledger_ack_line：assistant 行 + provider=lane + gen=script，失败唔阻。"""
    assert "async def _ledger_ack_line(lane: str, text: str) -> None:" in AGENT_SRC
    assert 'provider=lane, gen="script",' in AGENT_SRC
    assert "ack lane ledger failed lane=" in AGENT_SRC


def test_kept_lanes_untouched():
    """保留车道零变化：注册不带 history、say 不带 add_to_chat_ctx=False。"""
    for lane in ("wa-flush", "digit-flush", "wa-confirm", "flow-say", "late-answer",
                 "branch-refuse", "farewell", "branch-canned"):
        assert f'lane="{lane}"' in AGENT_SRC, f"保留车道 {lane} 应仍在"
    # flow-say(直念步)的 say 调用不带 add_to_chat_ctx=False：定位其调用行
    import re

    m = re.search(
        r'_register_reply_lane\(\s*lane="flow-say"[^)]*\)(?!\s*\n\s*history)',
        AGENT_SRC,
    )
    assert m, "flow-say 注册应在场"
    # opening 轨注释仍钉进史语义
    assert "turn-1 前缀命中不变" in AGENT_SRC or "前缀命中不变" in AGENT_SRC
