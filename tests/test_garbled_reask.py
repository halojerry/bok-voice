"""garbled-reask 车道 + 空轮短接(EX-2,2026-09-28):离线门单测 + 源级 pin。

碎片轮(数字碎片/热词回声残渣)错语境落到垫话/胡答——本车道 canned 重问
(零 TTFT,替掉一个 LLM 轮)。判据见 agent.garbled_reask_gate(docstring 即规格);
本文件钉死门行为、ack 表、四/五闸 env 与植入位置。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.agent import (  # noqa: E402
    _garbled_reask_line,
    _is_ack_anchor_text,
    garbled_reask_gate,
)

# scripts/e2e_barge_in.py 的真客户插话激励(第一句触发回复/第二句播放中插入打断):
# 打断轮是**合法话头**,碎片车道绝不可以把它截成重问。
BARGEIN_FIRST = "我件貨爛咗想投訴。"
BARGEIN_SECOND = "我想先問下賠幾多。"


def _gate(**kw):
    base = dict(
        user_text="",
        band="unknown",
        excluded=False,
        closing=False,
        paused=False,
        wa_pending=False,
        digit_pending=False,
        digit_run=False,
        wa_numberish=False,
        hotword_terms=(),
        min_content_chars=2,
        consec=0,
        max_consec=2,
    )
    base.update(kw)
    return garbled_reask_gate(**base)


# ---- 门行为 ----

def test_lane_fires_band_low_even_on_content_text():
    # band=low:STT 自己都唔确定 → 开,唔理文本判据
    assert _gate(user_text=BARGEIN_FIRST, band="low") == "reask"


def test_lane_fires_band_unknown_only_when_garbled():
    assert _gate(user_text="64311133", band="unknown") == "reask"
    assert _gate(user_text="六四三一一三三", band="unknown") == "reask"
    # 有内容 → 唔开
    assert _gate(user_text=BARGEIN_FIRST, band="unknown") == ""


def test_lane_band_ok_never_fires():
    assert _gate(user_text="64311133", band="ok") == ""


def test_digit_run_and_wa_numberish_bypass():
    assert _gate(user_text="64311133", band="low", digit_run=True) == ""
    assert _gate(user_text="我的WhatsApp係", band="low", wa_numberish=True) == ""


def test_pending_and_state_bypasses():
    for kw in (
        {"excluded": True},
        {"closing": True},
        {"paused": True},
        {"wa_pending": True},
        {"digit_pending": True},
    ):
        assert _gate(user_text="64311133", band="low", **kw) == "", kw


def test_consecutive_cap_at_two_then_silent():
    # consec 0 → reask,1 → reask,2 → cap(静默丢弃)
    assert _gate(user_text="64311133", band="low", consec=0, max_consec=2) == "reask"
    assert _gate(user_text="64311133", band="low", consec=1, max_consec=2) == "reask"
    assert _gate(user_text="64311133", band="low", consec=2, max_consec=2) == "cap"
    assert _gate(user_text="64311133", band="low", consec=9, max_consec=2) == "cap"


def test_bargein_stimulus_never_gates():
    # 打断/合法话头(真 E2E 激励)在默认 unknown 带下绝不开车道
    assert _gate(user_text=BARGEIN_FIRST, band="unknown") == ""
    assert _gate(user_text=BARGEIN_SECOND, band="unknown") == ""
    # 即便误判成 low 之外嘅 unknown,有内容也唔开
    assert _gate(user_text=BARGEIN_SECOND, band="unknown", hotword_terms=("投訴",)) == ""


def test_min_content_chars_env_shape():
    # 单字碎片:默认 2 字门=开;门调 1=唔开
    assert _gate(user_text="哦", band="unknown", min_content_chars=2) == "reask"
    assert _gate(user_text="哦", band="unknown", min_content_chars=1) == ""


# ---- 三条重问台词 ----

def test_garbled_reask_line_three_langs():
    assert _garbled_reask_line("cantonese") == "唔好意思，頭先聽唔清楚，可以再講一次嗎？"
    assert _garbled_reask_line("zh") == "不好意思，刚才没听清楚，可以再说一次吗？"
    assert _garbled_reask_line("en") == "Sorry, I didn't quite catch that. Could you say it again?"


def test_garbled_reask_in_ack_anchor_table():
    # 6×3 表:三语重问行都进 ack 锚豁免(唔进【你上一句】/摘要)
    for lang in ("zh", "cantonese", "en"):
        assert _is_ack_anchor_text(_garbled_reask_line(lang)), lang


# ---- 源级 pin ----

def _agent_src() -> str:
    return (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")


def test_lane_sits_after_qa_fastpath_and_before_filler_arm():
    src = _agent_src()
    qa = src.index("QA_FASTPATH hit=0 reason=no_audio")
    reask = src.index("GARBLED_REASK lane=1")
    # rindex:取真调用点(车道注释里也提到 _filler.arm(),那处唔算)
    filler = src.rindex("_filler.arm()")
    assert qa < reask < filler
    # 车道登记形状:chokepoint+gen=script+anchor=False
    assert 'lane="garbled-reask", gen="script",' in src
    assert "text=_reask_line, anchor=False," in src


def test_killswitch_and_cap_markers_in_source():
    src = _agent_src()
    assert 'os.environ.get("BOK_GARBLED_REASK", "1") != "0"' in src
    assert 'print("GARBLED_REASK cap=1", flush=True)' in src
    assert "from bok_voice_core.turn_quality import band_from_confidence, looks_garbled" in src


def test_reset_on_real_reply_source_pinned():
    # 真回复(gen ∈ llm/qa_fastpath)交付时归零——与 stall relieve 同钩子
    src = _agent_src()
    assert "agent._reask_state = 0" in src
    assert 'if gen in ("llm", "qa_fastpath"):' in src


def test_empty_turn_short_circuit_source_pinned():
    src = _agent_src()
    assert 'print("EMPTY_TURN_DROPPED", flush=True)' in src
    # 记账镜像既有丢弃路径:拆看门狗 + raise StopResponse
    idx = src.index('print("EMPTY_TURN_DROPPED", flush=True)')
    window = src[idx - 200 : idx + 200]
    assert "_cancel_response_watchdog()" in window
    assert "raise StopResponse()" in window


def test_env_keys_registered_in_forward_env():
    bok = (ROOT / "tools" / "bok.py").read_text(encoding="utf-8")
    for key in (
        "BOK_GARBLED_REASK",
        "BOK_REASK_CONF_MEAN",
        "BOK_REASK_LOW_RATIO",
        "BOK_REASK_MIN_CONTENT_CHARS",
        "BOK_REASK_MAX_CONSEC",
    ):
        assert f'"{key}"' in bok, key


# ---- P1-SV 置信度缺口修正(2026-10-01 FLOW20 轮11 哑根因) ----


def _sv_gate(text, band="unknown", **kw):
    return garbled_reask_gate(
        user_text=text, band=band, excluded=False, closing=False,
        paused=False, wa_pending=False, digit_pending=False,
        digit_run=False, wa_numberish=False, **kw,
    )


def test_sv_no_conf_short_ack_not_garbled():
    """conf_available=False(SV):「哦。」2 字应承轮不再烧 reask 预算。"""
    assert _sv_gate("哦。", conf_available=False, prev_user_text="你係邊個平台買㗎?") == ""


def test_sv_no_conf_identical_repeat_still_fires():
    """逐字复读上一轮(真卡壳)照开。"""
    assert _sv_gate("你講多次", conf_available=False, prev_user_text="你講多次") == "reask"


def test_sv_no_conf_single_char_fires():
    """≤1 字碎片照开(极短=转写崩)。"""
    assert _sv_gate("呃", conf_available=False, prev_user_text="之前嗰句") == "reask"


def test_qwen3_path_unchanged_with_conf():
    """conf_available=True(Qwen3 默认):旧判据不变(unknown+looks_garbled 照开)。"""
    assert _sv_gate("哦。", conf_available=True) == "reask"
