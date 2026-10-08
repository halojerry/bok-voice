"""W2 刀3(2026-10-08):复读守卫残片清洗——被打断轮的「前缀同源片」不进重复锚。

病理(架构体检 2026-10-08):10-07 的 interrupted-item corpus skip 走
``item.interrupted`` 旗;旗没送到的事件形态下,被打断的前缀残片仍进重复锚/
账本,风暴后重生成同答案的首句被误判复读(REPEAT_GUARD_CANCEL_DROP
first_sent=1 家族,10-05 以来 38 次)。修法=watcher 打断补账点采集残片
(partial+guard buffer,有界 4 条),item 侧豁免出口补「前缀同源」判定——
判据单源=``_is_repeat_fragment_pollution``(纯函数),kill-switch 复用
``BOK_REPEAT_CROSS_TURN``(=0 恒 False=旧行为)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "agent"))

from agent_runtime.agent import _is_repeat_fragment_pollution  # noqa: E402

_FRAG = "好的我哋而家就幫你查下張單"  # 假想被打断半截回复(13 字)


@pytest.fixture(autouse=True)
def _cross_turn_on(monkeypatch):
    monkeypatch.setenv("BOK_REPEAT_CROSS_TURN", "1")


def test_equal_normalized_text_hits():
    assert _is_repeat_fragment_pollution("好的，我哋而家就幫你查下張單。", [_FRAG])
    # 标点/空白归一后相等
    assert _is_repeat_fragment_pollution("好的我哋而家就幫你查下張單", [_FRAG])


def test_prefix_both_directions_hit():
    # item 是残片的前缀(守卫早发片段形态)
    assert _is_repeat_fragment_pollution("好的我哋而家就", [_FRAG])
    # item 比残片长(整段到达,残片是其前缀)
    assert _is_repeat_fragment_pollution(_FRAG + "即刻同你核實", [_FRAG])


def test_high_similarity_hits():
    # 近同形(一语气词之差,相似 0.92):同源残片的近重复形态
    variant = "好的我哋宜家就幫你查下張單"
    assert _is_repeat_fragment_pollution(variant, [_FRAG])


def test_unrelated_text_passes():
    assert not _is_repeat_fragment_pollution("张单号係七八九零一", [_FRAG])
    assert not _is_repeat_fragment_pollution("多谢你嘅耐心等待", [_FRAG])


def test_short_texts_never_hit():
    # 双方过短(归一 <6 字)不过滤——防误杀短应承/短句
    assert not _is_repeat_fragment_pollution("好的", ["好的我"])
    assert not _is_repeat_fragment_pollution("好的呀", [])


def test_empty_frag_ledger_or_text():
    assert not _is_repeat_fragment_pollution(_FRAG, [])
    assert not _is_repeat_fragment_pollution("", [_FRAG])
    assert not _is_repeat_fragment_pollution(None, [_FRAG])  # type: ignore[arg-type]


def test_kill_switch_zero_disables(monkeypatch):
    """BOK_REPEAT_CROSS_TURN=0 → 判定恒 False=旧行为逐字节(残片清洗随跨轮
    复读防线同闸)。"""
    monkeypatch.setenv("BOK_REPEAT_CROSS_TURN", "0")
    assert not _is_repeat_fragment_pollution(_FRAG, [_FRAG])


def test_multiple_frags_any_hit():
    assert _is_repeat_fragment_pollution("唔好意思頭先聽唔清楚", ["另外一句残片", "唔好意思頭先聽唔清楚啲"])


# ---- 源级 pin:wiring 契约 ----


def test_fragment_clean_wiring_pinned():
    """源级钉:①item 侧豁免出口判据扩展(flag or fragment);②watcher 打断
    补账点采集残片(有界 4);③豁免出口判定序 ack→interrupted→fragment。"""
    src = (_ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    # 纯函数在场(判据单源)
    assert "def _is_repeat_fragment_pollution(" in src
    assert 'os.environ.get("BOK_REPEAT_CROSS_TURN", "1") != "1"' in src
    # item 侧豁免出口:flag 或 fragment 同走豁免通道(复用 10-07 出口)
    assert "(_item_interrupted or _item_frag_hit) and not _assistant_ack" in src
    assert "_item_frag_hit = (not _item_interrupted) and _is_interrupted_fragment_text(text)" in src
    # 判定序:ack 判定在前、interrupted/fragment 在后、豁免出口最后
    i_ack = src.index("_assistant_ack = _is_ack_anchor_text(text)")
    i_int = src.index('_item_interrupted = bool(getattr(item, "interrupted", False))')
    i_frag = src.index("_item_frag_hit = (not _item_interrupted)")
    i_exit = src.index('if (_item_interrupted or _item_frag_hit) and not _assistant_ack:')
    assert i_ack < i_int < i_frag < i_exit
    # watcher 补账点:partial 非空才入账、有界 4
    assert "if partial.strip():" in src
    assert "_interrupted_frags.append(partial)" in src
    assert "del _interrupted_frags[:-4]" in src
    # 残片账声明在 entrypoint(watcher 与 item 侧同源闭包)
    assert "_interrupted_frags: list = []" in src
    # 观测行带 reason=flag|fragment(可归因)
    assert "reason={'flag' if _item_interrupted else 'fragment'}" in src


def test_existing_10_07_skip_pin_still_holds():
    """10-07 豁免通道回归:原判定与观测行不被本刀破坏(判定序 pin 原样)。"""
    src = (_ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    assert '_item_interrupted = bool(getattr(item, "interrupted", False))' in src
    assert "interrupted-item corpus skip" in src
