"""W2 刀2(2026-10-08):REPEAT 轮强制承应——风暴静听吞显式重复请求轮的豁免面。

病理(call-123d4a21):风暴静听 active 期间,客户连说 3 遍同句(「听唔清,你
讲多次」类)全哑到挂机——风暴分支在 verdict 判定**之前**消费轮,rounds 1/2/4
静默、3/5 让路语,REPEAT 复述车道永远接不到。豁免=REPEAT 判据命中 → 清风暴
账+直念三语短承应+落穿正常轮路径(复述指引→LLM 真复述)。BOK_REPEAT_ACK=0
回旧行为。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "agent"))

from agent_runtime.agent import _ack_anchor_texts, _is_ack_anchor_text, _repeat_ack_line  # noqa: E402
from agent_runtime.flow import REPEAT, decide_advance, is_repeat_request_text  # noqa: E402

# ---- 判据单源(flow.is_repeat_request_text)== decide_advance REPEAT 分支 ----


def test_repeat_request_explicit_family_any_length():
    """显式族出现即命中、不看句长(16 字长句同判)。"""
    assert is_repeat_request_text("再说一次")
    assert is_repeat_request_text("唔好意思頭先冇聽清，你講多次")
    assert is_repeat_request_text("听不清，能重复一遍吗")
    assert is_repeat_request_text("can you repeat that please")


def test_repeat_request_fuzzy_family_short_only():
    """模糊族(乜嘢/咩/what)只认 ≤12 字短句;长句=内容提问唔算。"""
    assert is_repeat_request_text("乜嘢话？")
    assert is_repeat_request_text("what?")
    assert not is_repeat_request_text("乜嘢係賠償方案嘅具体内容啊")  # 长句内容提问
    assert not is_repeat_request_text("you what?")


def test_repeat_request_single_source_with_decide_advance():
    """helper 与 decide_advance 的 REPEAT 分支同一判据(源级收口后逐字同源)。"""
    for text in ("再说一次", "冇聽清", "大声啲", "你好好的讲多次得唔得啊"):
        assert decide_advance(text) == REPEAT, text
        assert is_repeat_request_text(text), text


def test_repeat_request_empty_text():
    assert not is_repeat_request_text("")
    assert not is_repeat_request_text(None)  # type: ignore[arg-type]


# ---- 承应句:三语 + ack 锚豁免 ----


def test_repeat_ack_line_trilingual_and_ack_exempt():
    for lang in ("zh", "cantonese", "en"):
        line = _repeat_ack_line(lang)
        assert line
        # zh/canto ≤16 字压 TTS 时长;en 按词计可放宽
        assert len(line) <= (32 if lang == "en" else 16)
        assert _is_ack_anchor_text(line), f"{lang} 承应句必须落 ack 锚豁免(唔进重复锚/摘要)"
    assert _repeat_ack_line("zh") in _ack_anchor_texts()


# ---- 源级 pin:agent.py 豁免块位置契约 ----


def test_storm_repeat_bypass_wiring_pinned():
    """源级钉:①BOK_REPEAT_ACK 总闸在;②豁免块在风暴静听分支之前;③风暴分支
    条件带 `not _storm_repeat_bypass`;④承应后不 raise(落穿复述车道);⑤判据
    单源 import is_repeat_request_text。"""
    src = (_ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    assert 'os.environ.get("BOK_REPEAT_ACK", "1") == "1"' in src
    i_bypass = src.index("_storm_repeat_bypass = False")
    i_storm_branch = src.index(
        'os.environ.get("BOK_INTERRUPT_STORM_BACKOFF", "1") == "1"\n'
        '                and _storm.get("active_until", 0.0) > 0.0'
    )
    assert i_bypass < i_storm_branch, "REPEAT 豁免必须在风暴静听消费分支之前判定"
    assert "and not _storm_repeat_bypass" in src
    assert "from .flow import is_repeat_request_text as _is_repeat_req" in src
    # 豁免块内唔准出现 StopResponse(落穿=LLM 复述接手)
    block = src[i_bypass:i_storm_branch]
    assert "StopResponse" not in block, "repeat-ack 豁免块必须落穿(不 StopResponse)"
    assert 'lane="repeat-ack"' in block
    # 承应句进 chokepoint 车道注册表
    assert '"garbled-reask", "wa-confirm", "repeat-ack",' in src


def test_storm_ack_env_gate_zero_is_old_behavior():
    """BOK_REPEAT_ACK=0 → 豁免判据整块不激活(源级:env 读面在 bypass 判定内,
    =0 时连 flow 判据都不跑=旧行为逐字节)。"""
    src = (_ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    i_gate = src.index('os.environ.get("BOK_REPEAT_ACK", "1") == "1"')
    i_flag = src.index("_storm_repeat_bypass = False")
    assert i_flag < i_gate
    assert "_storm_repeat_bypass = _is_repeat_req(user_text)" in src
