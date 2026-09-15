from __future__ import annotations

import array
import asyncio
import importlib.util
import json
import math
import struct
import sys
import time
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mock_callee.py"
_spec = importlib.util.spec_from_file_location("mock_callee", _SCRIPT)
assert _spec and _spec.loader
mock_callee = importlib.util.module_from_spec(_spec)
sys.modules["mock_callee"] = mock_callee
_spec.loader.exec_module(mock_callee)


def test_plan_timeline_answer():
    tl = mock_callee.plan_timeline("answer", ring_delay_s=3.0, lines=2)
    assert tl[0] == ("join", 3.0)
    assert [e for e, _ in tl if e == "speak"] == ["speak", "speak"]
    assert tl[-1][0] == "leave"


def test_plan_timeline_no_answer():
    # no_answer:永不进房(只等响铃窗耗尽后退出)。
    tl = mock_callee.plan_timeline("no_answer", ring_delay_s=3.0, lines=2)
    assert [e for e, _ in tl] == ["exit"]


def test_plan_timeline_reject():
    # reject:进房即走。事件序列固定 join→leave;但 leave 的**计划时刻只是占位**——
    # 真正的离房锚在 run() 里改为「实际 connect 完成 + DWELL_AFTER_CONNECT_S」,
    # 否则 connect 耗时 ≥0.5s 时离房会抢在 agent 侧监听注册前到达 → 误判 answered。
    tl = mock_callee.plan_timeline("reject", ring_delay_s=3.0, lines=2)
    assert [e for e, _ in tl] == ["join", "leave"]
    assert tl[0][1] == 3.0
    assert "reject" in mock_callee.DWELL_AFTER_CONNECT_S
    dwell = mock_callee.DWELL_AFTER_CONNECT_S["reject"]
    # dwell 必须落在 agent 的 1.5s 离房监听窗内(>0 且 <1.5)。
    assert 0.0 < dwell < 1.5


def test_plan_timeline_hangup_mid():
    tl = mock_callee.plan_timeline("hangup_mid", ring_delay_s=3.0, lines=3)
    assert [e for e, _ in tl].count("speak") == 1
    assert tl[-1][0] == "leave"


def test_plan_timeline_answer_timing_contract():
    # 首句 ~0.8s 起(agent 进房后 1.5s 窗过了才出声,不会被判 reject)。
    tl = mock_callee.plan_timeline("answer", ring_delay_s=1.0, lines=3)
    speaks = [at for e, at in tl if e == "speak"]
    assert len(speaks) == 3
    assert speaks[0] == 1.8
    assert speaks == sorted(speaks)
    assert tl[-1][0] == "leave" and tl[-1][1] > speaks[-1]


def test_plan_timeline_unknown_scenario_defaults_to_answer():
    tl = mock_callee.plan_timeline("bogus", ring_delay_s=2.0, lines=1)
    assert tl[0][0] == "join" and tl[-1][0] == "leave"


def test_plan_timeline_lines_floor():
    # lines<=0 时 answer 至少说一句,避免零台词。
    tl = mock_callee.plan_timeline("answer", ring_delay_s=0.0, lines=0)
    assert [e for e, _ in tl].count("speak") == 1


def test_plan_timeline_negative_ring_delay_clamped():
    tl = mock_callee.plan_timeline("answer", ring_delay_s=-5.0, lines=1)
    assert tl[0] == ("join", 0.0)


def test_parse_args_defaults_and_roundtrip():
    args = mock_callee.parse_args(
        [
            "--url", "ws://127.0.0.1:7880",
            "--token", "jwt",
            "--identity", "sip-mock-10086",
        ]
    )
    assert args.url == "ws://127.0.0.1:7880"
    assert args.token == "jwt"
    assert args.identity == "sip-mock-10086"
    assert args.scenario == "answer"
    assert args.language == "cantonese"
    assert args.script_json == "[]"
    assert args.script() == []
    assert args.ring_delay == 3.0
    assert args.ringing_window == 35.0
    assert args.hangup_after_turns == 0


def test_parse_args_script_json_list():
    args = mock_callee.parse_args(
        [
            "--url", "ws://x", "--token", "t", "--identity", "i",
            "--script-json", '["你好","我個件未到"]',
            "--scenario", "reject", "--language", "en",
            "--ring-delay", "1.5", "--ringing-window", "20",
            "--hangup-after-turns", "2",
        ]
    )
    assert args.script() == ["你好", "我個件未到"]
    assert args.scenario == "reject"
    assert args.language == "en"
    assert args.ring_delay == 1.5
    assert args.ringing_window == 20.0
    assert args.hangup_after_turns == 2


def test_parse_args_script_json_malformed_is_empty():
    args = mock_callee.parse_args(
        ["--url", "ws://x", "--token", "t", "--identity", "i", "--script-json", "not-json"]
    )
    assert args.script() == []


def test_language_normalized_to_three_states():
    # 语言三态 zh/cantonese/en;未知值一律回落 cantonese(B 线规范值)。
    assert mock_callee.normalize_language("zh") == "zh"
    assert mock_callee.normalize_language("cantonese") == "cantonese"
    assert mock_callee.normalize_language("en") == "en"
    assert mock_callee.normalize_language("bogus") == "cantonese"
    assert mock_callee.normalize_language("") == "cantonese"


def test_log_event_line_format():
    line = mock_callee.event_line("join", 3.0, "sip-mock-64320111")
    assert line == "MOCK_CALLEE event=join at=3.0 identity=sip-mock-64320111"


# ---- 台词兜底（answer 剧本空台词=语言默认 2 句） ----

def test_default_script_per_language():
    """三语各 2 句：空台词 answer 剧本必须仍有声（否则客户静坐无声）。"""
    for lang in ("zh", "cantonese", "en"):
        lines = mock_callee.default_script(lang)
        assert len(lines) == 2 and all(isinstance(x, str) and x.strip() for x in lines)
    # 未知语言回落 cantonese（与 normalize_language 同语义）。
    assert mock_callee.default_script("bogus") == mock_callee.default_script("cantonese")


def test_default_script_lines_are_single_breath():
    """默认台词须逐句 <10 字：vad-pause 提交门槛是 10 字，超了会被劈轮。

    `len()` 直接数字符：en 台词按字符数算（"okay I see"=10）已贴门槛，
    这里取 ≤9 字符留余量。
    """
    for lang in ("zh", "cantonese", "en"):
        for line in mock_callee.default_script(lang):
            assert len(line) <= 9, f"{lang} 默认台词过长: {line!r} ({len(line)})"


def test_default_script_languages_are_canonical():
    """台词表键只用三态规范名 zh/cantonese/en（不写旧拼写）。"""
    assert set(mock_callee.DEFAULT_SCRIPTS) == {"zh", "cantonese", "en"}
    assert set(mock_callee.VALID_LANGUAGES) == {"zh", "cantonese", "en"}


# ---- 轮次对齐：首句也要等 AI 先出声（转写零丢字的测试床前提） ----

def _callee(script: list[str] | None = None) -> mock_callee.MockCallee:
    args = mock_callee.parse_args([
        "--url", "ws://x", "--token", "t", "--identity", "i",
        "--script-json", json.dumps(script or []),
    ])
    return mock_callee.MockCallee(args)


def test_wait_agent_quiet_without_voice_returns_immediately_unless_required():
    callee = _callee(["你好"])
    assert callee._agent_last_voice == 0.0
    t0 = time.monotonic()
    asyncio.run(callee._wait_agent_quiet(0.05, 5.0))
    assert time.monotonic() - t0 < 0.2  # 旧语义：没听到 AI 就直接放行


def test_wait_agent_quiet_required_voice_times_out_instead_of_racing():
    """require_voice=True 且 AI 从未出声 → 由 timeout 兜底放行（不死等）。"""
    callee = _callee(["你好"])
    t0 = time.monotonic()
    asyncio.run(callee._wait_agent_quiet(0.05, 0.3, require_voice=True))
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.25, f"首句抢跑了（{elapsed:.2f}s）"


def test_wait_agent_quiet_required_voice_returns_after_agent_spoke():
    callee = _callee(["你好"])
    callee._agent_last_voice = time.monotonic() - 5.0  # 早就讲完且静默
    t0 = time.monotonic()
    asyncio.run(callee._wait_agent_quiet(1.0, 5.0, require_voice=True))
    assert time.monotonic() - t0 < 0.2


def test_speak_waits_for_agent_voice_before_first_line(monkeypatch):
    """首句出声前必须走 require_voice 等待（否则撞 agent 会话起来前被整句吞掉）。"""
    callee = _callee(["你好我是快递公司的专员"])
    seen: dict = {}

    async def _fake_wait(quiet_s, timeout_s, require_voice=False):
        seen["require_voice"] = require_voice
        seen["quiet_s"] = quiet_s

    pushed: list[bytes] = []

    async def _fake_push(_src, pcm):
        pushed.append(pcm)

    monkeypatch.setattr(callee, "_wait_agent_quiet", _fake_wait)
    monkeypatch.setattr(mock_callee, "tts_pcm", lambda text, lang: b"\x01\x02")
    monkeypatch.setattr(mock_callee, "push_pcm", _fake_push)
    asyncio.run(callee._speak(0))
    assert seen["require_voice"] is True
    assert seen["quiet_s"] == mock_callee.AGENT_QUIET_S
    assert pushed == [b"\x01\x02"]


def _run_speak(callee: mock_callee.MockCallee) -> None:
    """跑一次 `_speak`（轮次等待短路——本测试只关心窄带变换落没落到推流上）。"""

    async def _no_wait(quiet_s, timeout_s, require_voice=False):
        return None

    callee._wait_agent_quiet = _no_wait  # type: ignore[method-assign]
    asyncio.run(callee._speak(0))


def test_speak_applies_narrowband_when_enabled(monkeypatch):
    """--narrowband 时推流前过窄带档；不开则原样推。"""
    pushed: list[bytes] = []

    async def _fake_push(_src, pcm):
        pushed.append(pcm)

    monkeypatch.setattr(mock_callee, "tts_pcm", lambda text, lang: b"\x10\x00\x20\x00")
    monkeypatch.setattr(mock_callee, "push_pcm", _fake_push)
    monkeypatch.setattr(mock_callee, "downsample_to_narrowband",
                        lambda pcm, src_rate=16000: b"NB" + pcm)

    _run_speak(_callee(["你好"]))
    assert pushed[-1] == b"\x10\x00\x20\x00"  # 宽带档不动音频

    nb_args = mock_callee.parse_args([
        "--url", "ws://x", "--token", "t", "--identity", "i",
        "--script-json", json.dumps(["你好"]), "--narrowband",
    ])
    _run_speak(mock_callee.MockCallee(nb_args))
    assert pushed[-1] == b"NB\x10\x00\x20\x00"


# ---- 8kHz 窄带档（spec 2026-09-13 §6 前置门测试床） ----

def _sine_pcm(freq_hz: float, *, seconds: float = 0.5, amp: int = 12000,
              rate: int = mock_callee.SAMPLE_RATE) -> bytes:
    n = int(rate * seconds)
    return struct.pack(f"<{n}h",
                       *(int(amp * math.sin(2 * math.pi * freq_hz * i / rate)) for i in range(n)))


def _energy(pcm: bytes) -> float:
    a = array.array("h", pcm[: len(pcm) // 2 * 2])
    if not a:
        return 0.0
    return sum(x * x for x in a) / len(a)


def test_downsample_to_narrowband_attenuates_4khz_energy():
    """4kHz 正弦（16k 采样）过窄带档后能量应塌到 <20%——电话线 3.4kHz 截止。

    计划 Task 5 的原始断言；但计划里「相邻样本均值」实现在 4kHz 只有 -3dB
    （能量 50%，见实现注释），故窄带档改用 3.4kHz 6 阶 Butterworth 抗混叠 +
    8k 抽降 + 零阶保持 + 镜像低通，本断言是它的验收口径。
    """
    pcm = _sine_pcm(4000)
    nb = mock_callee.downsample_to_narrowband(pcm)
    wide_energy, nb_energy = _energy(pcm), _energy(nb)
    assert wide_energy > 0
    assert nb_energy < wide_energy * 0.2, f"4kHz 能量未塌: {nb_energy / wide_energy:.3f}"


def test_downsample_to_narrowband_preserves_1khz_energy():
    """1kHz（电话通带内）基本保留：能量 >50%——窄带档不许把语音基频也削掉。"""
    pcm = _sine_pcm(1000)
    nb = mock_callee.downsample_to_narrowband(pcm)
    wide_energy, nb_energy = _energy(pcm), _energy(nb)
    assert nb_energy > wide_energy * 0.5, f"1kHz 被削: {nb_energy / wide_energy:.3f}"


def test_downsample_to_narrowband_keeps_3khz_passband():
    """3kHz 仍在电话通带内（-3dB 点 3.4kHz）：保留 >50%，防过度滤波。"""
    pcm = _sine_pcm(3000)
    nb = mock_callee.downsample_to_narrowband(pcm)
    assert _energy(nb) > _energy(pcm) * 0.5


def test_downsample_to_narrowband_length_and_tiny_inputs():
    """输出长度=输入长度（零阶保持升回 16k，rtc source 恒 16k）；空/奇数字节不炸。"""
    pcm = _sine_pcm(800, seconds=0.1)
    assert len(mock_callee.downsample_to_narrowband(pcm)) == len(pcm)
    assert mock_callee.downsample_to_narrowband(b"") == b""
    assert mock_callee.downsample_to_narrowband(b"\x01") == b""
    # 奇数字节尾部截掉（不足一个 int16 样本）。
    assert len(mock_callee.downsample_to_narrowband(pcm + b"\x7f")) == len(pcm)


def test_downsample_to_narrowband_cutoff_is_telephone_band():
    """截止频率锚在电话频带上沿（300-3400Hz），不是别的拍脑袋值。"""
    assert 3300.0 <= mock_callee.NB_CUTOFF_HZ <= 3500.0
    assert mock_callee.NB_TARGET_RATE == 8000


def test_parse_args_narrowband_flag():
    base = ["--url", "ws://x", "--token", "t", "--identity", "i"]
    assert mock_callee.parse_args(base).narrowband is False
    assert mock_callee.parse_args([*base, "--narrowband"]).narrowband is True
