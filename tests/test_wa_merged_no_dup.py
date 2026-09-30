"""WA 收号 merged 轮不重复落账（真机第一批 A 组，2026-09-30 call-4392c7bb 实证）。

病理：客户一句话完整报完号码（数字数 <累积门槛被 stash）→ 5s 后 flush 的
merged 文本与 stash **一模一样**（无增量）→ turns 表出现两行完全相同的
「呃，我的WhatsApp是一二三三四四五。」——看板/挖掘双计，Ethan 体感「ASR
重复识别」。

契约：flush 落 merged 前与「本通最后一条 wa-stash 已落文本」比对——相同
（单段无增量）= 跳过；不同（多段拼接有增量）= 照落（C3a 分析侧对账语义
不变）。stash 轮照落（报号碎片必须在案）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")


def test_flush_skips_merged_when_no_increment():
    """源级 pin：flush 落 merged 前与最后 stash 文本比对，相同即跳过（A 组）。"""
    assert '_wa_stash_last["text"]' in AGENT_SRC, "stash 落账点须记账本最后文本"
    assert '_wa_stash_last["text"] != stashed' in AGENT_SRC, (
        "flush 落 merged 须 gated 在「与最后 stash 不同（有增量）」上——单段无增量跳过"
    )


def test_stash_ledger_writes_and_resets():
    """源级 pin：stash 落账点写 _wa_stash_last；flush 消费后清（防跨轮误抑制）。"""
    body_start = AGENT_SRC.index('provider="wa-stash"')
    assert body_start > 0
    window = AGENT_SRC[body_start - 1500 : body_start + 200]
    assert '_wa_stash_last["text"] =' in window, "stash add_turn 附近须同步更新账本最后文本"
