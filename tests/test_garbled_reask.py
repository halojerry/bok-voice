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
    _band_round_is_garbled,
    _garbled_reask_line,
    _is_ack_anchor_text,
    garbled_reask_gate,
)
from agent_runtime.flow import FlowController, stall_ladder_level  # noqa: E402

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


def test_lane_sits_before_stall_ladder_and_before_filler_arm():
    # FIX-2(a)(D2-3,2026-10-01):旧序 lane 在 QA 快路之后、_filler.arm() 之前,
    # 烂转写轮先被 stall 阶梯 degrade 台词截走——求值上移到 stall 阶梯块之前
    # (ASR 病优先于模型病);仍在 _filler.arm() 之前 raise(碎片轮不起垫话)。
    src = _agent_src()
    reask = src.index("GARBLED_REASK lane=1")
    ladder = src.index("[stall-ladder] step=")
    # rindex:取真调用点(车道注释里也提到 _filler.arm(),那处唔算)
    filler = src.rindex("_filler.arm()")
    assert reask < ladder < filler
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


# ---- FIX-2(D2-3,2026-10-01):ASR 病优先于模型病 ----
# 病灶:连续烂转写 UNCLEAR 轮先被 stall 阶梯 degrade 台词截走,永远走不到重问;
# 且烂转写轮还在喂 step_streak/unclear_streak(把 ASR 病记模型头上=归因错误)。


def test_band_round_is_garbled_pure():
    """streak 守卫判据=band=="low" 或 band=="unknown"∧looks_garbled(与门同源)。"""
    # low:STT 自己都唔确定 → 烂轮,唔理文本
    assert _band_round_is_garbled("low", "我件貨爛咗想投訴。")
    # ok:置信度高 → 正常文本永唔算烂(窄带数字错听高置信实测,band 只是辅助)
    assert not _band_round_is_garbled("ok", "64311133")
    # unknown + 碎片/数字主导文本 → 烂
    assert _band_round_is_garbled("unknown", "64311133")
    assert _band_round_is_garbled("unknown", "六四三一一三三")
    assert _band_round_is_garbled("unknown", "哦。")  # ≤1 实质字
    # unknown + 有内容 → 唔算烂(正常 UNCLEAR 轮照常喂 streak)
    assert not _band_round_is_garbled("unknown", "我件貨爛咗想投訴。")
    # 热词词表命中剥除后无内容 → 烂(词表回声残渣形态)
    assert _band_round_is_garbled("unknown", "拼多多", ("拼多多",))


def test_garbled_rounds_never_feed_streak_contrast():
    """守卫对照:无守卫 3 轮烂转写当 UNCLEAR 喂=degrade;守卫后恒 0。"""
    garbage = "六四三一一三三"
    # 旧路(病灶形态):3 轮烂转写被记成 UNCLEAR → 阶梯 degure 门槛
    fc_old = FlowController(steps=[])
    for i in range(3):
        fc_old.note_turn_outcome("unclear", 0, f"g{i}")
    assert stall_ladder_level(fc_old.step_streak[0]) == "degrade"
    # FIX-2(b):同判据下 3 轮全部跳过写点 → 永不爬升
    assert _band_round_is_garbled("unknown", garbage)
    fc_new = FlowController(steps=[])
    for i in range(3):
        if not _band_round_is_garbled("unknown", garbage):
            fc_new.note_turn_outcome("unclear", 0, f"g{i}")
    assert fc_new.step_streak.get(0, 0) == 0
    assert stall_ladder_level(fc_new.step_streak.get(0, 0)) == ""
    # 正常 UNCLEAR 轮(可懂输入)旧语义保持:照常爬升
    fc_ok = FlowController(steps=[])
    for i in range(3):
        assert not _band_round_is_garbled("unknown", "我件貨爛咗想投訴。")
        fc_ok.note_turn_outcome("unclear", 0, f"n{i}")
    assert stall_ladder_level(fc_ok.step_streak[0]) == "degrade"


def test_consecutive_garbled_rounds_consumed_by_reask_lane():
    """连续 3 轮烂转写:门连发 reask/reask/cap——每轮都被车道消费(raise),
    到不了 stall 阶梯;cap 轮同属 garbled band(不喂 streak)。"""
    consec = 0
    verdicts = []
    for _ in range(3):
        v = _gate(user_text="六四三一一三三", band="unknown", consec=consec)
        verdicts.append(v)
        if v == "reask":
            consec += 1
    assert verdicts == ["reask", "reask", "cap"]
    assert _band_round_is_garbled("unknown", "六四三一一三三")  # cap 轮旗标面


def test_garbled_streak_guard_wiring_source_pinned():
    """源级 pin:旗标块在 streak 写点前;规则路/ judge 路三写点全守;judge 透传。"""
    src = _agent_src()
    # 旗标在 _flow_step_before 之后、规则推进 note_turn_outcome 之前
    flag = src.index("_garbled_band_round = False")
    write_first = src.index("if not _garbled_band_round:\n                        flow_ctrl.note_turn_outcome(")
    assert flag < write_first
    assert '"" if flow_ctrl.current != _flow_step_before else verdict,' in src
    assert "_band_round_is_garbled(" in src
    assert "_background_flow_judge(_step_at, user_text, turn_key=_turn_key, garbled=_garbled_band_round)" in src
    assert "async def _background_flow_judge(step_at: int, utt: str, turn_key: str = \"\", garbled: bool = False) -> None:" in src
    # judge 路三写点:note_turn_outcome/degrade_boost/unclear bump
    assert "if turn_key and flow_ctrl.current == step_at and not garbled:" in src
    assert "and flow_ctrl.current == step_at\n                and not garbled\n            ):" in src
    assert "if jv == UNCLEAR and not garbled:" in src
