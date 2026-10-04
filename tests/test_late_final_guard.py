"""F2 迟到 FINAL 尾巴护栏单测（2026-09-20 验收实证）。

现象：句级提交已发 FINAL、回复（快路罐头/直念/QA 罐头）开播后，停嘴 finish
整窗重解在句尾幻听出「那。」级短碎片 → _uncommitted startswith 分支当真尾巴
原样返回（是「追加」唔係「修正」，相似度门结构性不进）→「带内容短尾豁免」
放行（「那」是实词、唔在 _PURE_ACK_TAIL_CHARS）→ 第二条 FINAL 成新用户轮 →
框架 _interrupt_by_audio_activity 掐断在播回复（0.4s 截断、assistant item
唔落库）。

护栏（BOK_LATE_FINAL_GUARD，默认开）：AI 生成/播报（thinking/speaking）时，
迟到 finish 尾巴相对已提交文本只是「极短追加」（净文 ≤
BOK_LATE_FINAL_MAX_TAIL_CHARS，默认 2）→ 判幻听碎片，唔补发、唔成轮；真·新话
（足够长/含数字字母 run）与 AI 空闲轮照旧成轮——2026-09-07「带内容短尾豁免」
（「我唔知」「四五七」补报号码）在 AI 空闲与长度门槛之上全保留。
"""

from __future__ import annotations

import asyncio
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))
sys.path.insert(0, str(ROOT / "tools"))

import bok  # noqa: E402
from _bok_src import bok_source  # noqa: E402
from livekit.agents.utils.aio.channel import ChanClosed, ChanEmpty  # noqa: E402

from agent_runtime.agent import agent_busy_for_state  # noqa: E402
from agent_runtime.providers import livekit_plugins as lp  # noqa: E402
from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    LanguageState,
    Qwen3ASRSTT,
    late_final_is_new_speech,
)

_COMMITTED = "我想问一下赔偿怎么算。"


# ---- 纯函数正反例 -----------------------------------------------------------


def test_tail_append_short_while_busy_is_dropped():
    """F2 实弹形态：AI 播报中，finish 重解幻听「那。」级极短追加 → 唔成轮。"""
    assert (
        late_final_is_new_speech("那。", _COMMITTED, agent_busy=True, max_tail_chars=2)
        is False
    )
    assert (
        late_final_is_new_speech("嗰。", _COMMITTED, agent_busy=True, max_tail_chars=2)
        is False
    )


def test_long_tail_while_busy_still_becomes_turn():
    """足够长的尾巴=真·客户新话 → 照旧成轮、允许打断（真打断没被关掉）。"""
    long_tail = "那我想再问一下理赔的具体流程是这样的"
    assert (
        late_final_is_new_speech(long_tail, _COMMITTED, agent_busy=True, max_tail_chars=2)
        is True
    )


def test_digit_or_letter_run_always_delivered():
    """数字/字母 run（补报单号/英文词）→ 永远成轮（数字零降级）。"""
    assert (
        late_final_is_new_speech("457。", _COMMITTED, agent_busy=True, max_tail_chars=2)
        is True
    )
    assert (
        late_final_is_new_speech("OK。", _COMMITTED, agent_busy=True, max_tail_chars=2)
        is True
    )


def test_idle_agent_keeps_content_short_tail_exemption():
    """AI 空闲（listening）→ 2026-09-07 带内容短尾豁免全保留（「我唔知」照发）。"""
    assert (
        late_final_is_new_speech("我唔知。", _COMMITTED, agent_busy=False, max_tail_chars=2)
        is True
    )
    # 数字补报在 AI 空闲时也走长度门照发
    assert (
        late_final_is_new_speech("四五七。", _COMMITTED, agent_busy=False, max_tail_chars=2)
        is True
    )


def test_short_content_tail_threshold_boundary():
    """阈值边界：净文 1-2 字忙时丢，3 字（「我唔知」）忙时仍成轮（默认 2）。"""
    assert late_final_is_new_speech("好。", _COMMITTED, agent_busy=True, max_tail_chars=2) is False
    assert (
        late_final_is_new_speech("我唔知。", _COMMITTED, agent_busy=True, max_tail_chars=2)
        is True
    )
    # 阈值可调大：调到 4 后「我唔知」忙时也判幻听尾巴
    assert (
        late_final_is_new_speech("我唔知。", _COMMITTED, agent_busy=True, max_tail_chars=4)
        is False
    )


def test_empty_or_pure_punct_tail_never_a_turn():
    """空/纯标点尾巴 → 永不成轮（无论忙闲）。"""
    assert late_final_is_new_speech("。。。", _COMMITTED, agent_busy=False, max_tail_chars=2) is False
    assert late_final_is_new_speech("", _COMMITTED, agent_busy=True, max_tail_chars=2) is False


# ---- closing_say 窗（F4 二修：收线/告别直念期，把告别说完）--------------------


def test_closing_say_drops_any_length_including_digits():
    """closing_say=True：任何长度（含数字/字母 run）都不成轮——电话本就要结束，
    数字零降级在此窗让位（告别念完比补报号码优先，电话马上挂断无补报场景）。"""
    assert (
        late_final_is_new_speech(
            "那我再问一下理赔流程好吧", _COMMITTED, agent_busy=True, max_tail_chars=2, closing_say=True
        )
        is False
    )
    assert (
        late_final_is_new_speech("457。", _COMMITTED, agent_busy=True, max_tail_chars=2, closing_say=True)
        is False
    )
    assert (
        late_final_is_new_speech("那。", _COMMITTED, agent_busy=False, max_tail_chars=2, closing_say=True)
        is False
    )


def test_closing_say_false_keeps_real_interrupt():
    """closing_say=False：同一长尾照旧成轮——真打断没有被关掉（逐字节旧行为）。"""
    assert (
        late_final_is_new_speech(
            "那我再问一下理赔流程好吧", _COMMITTED, agent_busy=True, max_tail_chars=2, closing_say=False
        )
        is True
    )


# ---- hotword_only 否决层（词表幻听迟到 FINAL 掐断在播罐头）--------------------
# 现象：停嘴 finish 整窗重解把词表热词幻听成独立迟到 FINAL「打错电话。」——
# 4 字净文 > max_tail_chars(2) 连「极短追加」门都够不着 → 判真新话放行 → 掐断
# 在播罐头（assistant transcript 只落「这」）。它恰好逃过全部回声闸：
# _vocab_echo_guard 要 ≥4 连续词表段才剥；贪心门要零分隔符而它带「。」；孤词
# 门要 echo_seen 先例。修法：AI 忙+净文无数字字母 run+按词表贪心最长匹配剥离
# 后严格为空 → 判幻听丢弃；真插话剥后必有词表外残留（「打错电话啊」剩「啊」）
# 照放行。kill-switch BOK_LATE_FINAL_HOTWORD_GUARD（默认开）。


_VOCAB = ("打错电话", "賠償", "拼多多", "順豐速運")


def test_hotword_only_tail_dropped_while_busy():
    """busy+「打错电话。」+词表含「打错电话」→ False（丢弃）——词表幻听迟到
    FINAL 唔成轮（旧行为放行=掐断在播罐头根因）。"""
    assert (
        late_final_is_new_speech(
            "打错电话。", _COMMITTED, agent_busy=True, max_tail_chars=2, vocab_terms=_VOCAB
        )
        is False
    )


def test_hotword_with_residual_particle_still_passes():
    """busy+「打错电话啊」→ True（放行）——剥后剩「啊」，真插话唔拦（刻意比
    F3 的「剩余 ≤2 字」严：只认剥后严格为空）。"""
    assert (
        late_final_is_new_speech(
            "打错电话啊", _COMMITTED, agent_busy=True, max_tail_chars=2, vocab_terms=_VOCAB
        )
        is True
    )


def test_digit_tail_exempt_from_hotword_layer():
    """busy+「我的单号是457」→ True（数字豁免）——数字零降级同 L5611 口径，
    词表层在数字 run 放行之后、结构性够不着数字尾巴。"""
    assert (
        late_final_is_new_speech(
            "我的单号是457", _COMMITTED, agent_busy=True, max_tail_chars=2, vocab_terms=_VOCAB
        )
        is True
    )


def test_hotword_layer_idle_agent_not_gated():
    """idle+「打错电话。」→ True（AI 不忙不拦）——无回复可掐，旧行为保留。"""
    assert (
        late_final_is_new_speech(
            "打错电话。", _COMMITTED, agent_busy=False, max_tail_chars=2, vocab_terms=_VOCAB
        )
        is True
    )


def test_hotword_layer_default_empty_matches_old_behavior():
    """vocab_terms=() 默认 → 层短路不评估，行为与旧版逐字节一致（老用例矩阵
    带不传 kwarg 与显式 () 两种姿势同值）。"""
    cases = [
        # (payload, busy, max_tail_chars, closing_say, expected)
        ("那。", True, 2, False, False),
        ("嗰。", True, 2, False, False),
        ("那我想再问一下理赔的具体流程是这样的", True, 2, False, True),
        ("457。", True, 2, False, True),
        ("OK。", True, 2, False, True),
        ("我唔知。", False, 2, False, True),
        ("好。", True, 2, False, False),
        ("我唔知。", True, 2, False, True),
        ("我唔知。", True, 4, False, False),
        ("。。。", False, 2, False, False),
        ("", True, 2, False, False),
        ("那我再问一下理赔流程好吧", True, 2, True, False),
        ("457。", True, 2, True, False),
        ("那我再问一下理赔流程好吧", True, 2, False, True),
    ]
    for payload, busy, mx, closing, expected in cases:
        assert (
            late_final_is_new_speech(
                payload, _COMMITTED, agent_busy=busy, max_tail_chars=mx, closing_say=closing
            )
            is expected
        )
        assert (
            late_final_is_new_speech(
                payload,
                _COMMITTED,
                agent_busy=busy,
                max_tail_chars=mx,
                closing_say=closing,
                vocab_terms=(),
            )
            is expected
        )


def test_hotword_layer_closing_say_priority_unchanged():
    """closing_say 优先级不变：词表覆盖与否、剥后剩不剩字，窗内一律 False。"""
    assert (
        late_final_is_new_speech(
            "打错电话。",
            _COMMITTED,
            agent_busy=True,
            max_tail_chars=2,
            closing_say=True,
            vocab_terms=_VOCAB,
        )
        is False
    )
    assert (
        late_final_is_new_speech(
            "打错电话啊。",
            _COMMITTED,
            agent_busy=True,
            max_tail_chars=2,
            closing_say=True,
            vocab_terms=_VOCAB,
        )
        is False
    )


def test_vocab_only_net_normalization_matches_echo_guard():
    """_vocab_only_net 归一口径与词表/转写两侧同表（_to_simp 繁→简+剥标点）：
    繁体幻听对同源词表经同一映射后全覆盖命中；词表词顺串（多条首尾相接）
    贪心剥到空；词表外残留判 False。已知口径缺口（与 _is_hotword_vocab_echo
    同族共用 _T2S_MAP，本修复不改映射表）：錯→错/話→话 未覆盖透传，繁体
    「打錯電話」对简体词表唔命中——既有回声闸同一盲区，非本层引入。"""
    assert lp._vocab_only_net("順豐速運。", _VOCAB) is True  # 繁→简映射覆盖字
    assert lp._vocab_only_net("拼多多赔偿", _VOCAB) is True  # 词表词首尾相接
    assert lp._vocab_only_net("打错电话", _VOCAB) is True
    assert lp._vocab_only_net("打错电话啊", _VOCAB) is False
    assert lp._vocab_only_net("打错电话。", ()) is False  # 空词表恒 False
    assert lp._vocab_only_net("", _VOCAB) is False


def test_hotword_guard_kill_switch(monkeypatch):
    monkeypatch.delenv("BOK_LATE_FINAL_HOTWORD_GUARD", raising=False)
    assert lp._late_final_hotword_guard_on() is True  # 默认开
    monkeypatch.setenv("BOK_LATE_FINAL_HOTWORD_GUARD", "0")
    assert lp._late_final_hotword_guard_on() is False  # 0=整层否决不评估


def test_agent_busy_for_state():
    """状态→回复在途旗（agent.py 侧纯函数）。"""
    assert agent_busy_for_state("thinking") is True
    assert agent_busy_for_state("speaking") is True
    assert agent_busy_for_state("listening") is False
    assert agent_busy_for_state("") is False
    assert agent_busy_for_state("initializing") is False


# ---- env 读法 / kill-switch --------------------------------------------------


def test_kill_switch_default_on_and_off(monkeypatch):
    monkeypatch.delenv("BOK_LATE_FINAL_GUARD", raising=False)
    assert lp._late_final_guard_on() is True  # 默认开
    monkeypatch.setenv("BOK_LATE_FINAL_GUARD", "0")
    assert lp._late_final_guard_on() is False  # 0=回退旧行为


def test_max_tail_chars_env(monkeypatch):
    monkeypatch.delenv("BOK_LATE_FINAL_MAX_TAIL_CHARS", raising=False)
    assert lp._late_final_max_tail_chars() == 2  # 默认 2
    monkeypatch.setenv("BOK_LATE_FINAL_MAX_TAIL_CHARS", "4")
    assert lp._late_final_max_tail_chars() == 4
    monkeypatch.setenv("BOK_LATE_FINAL_MAX_TAIL_CHARS", "abc")
    assert lp._late_final_max_tail_chars() == 2  # 坏值回默认
    monkeypatch.setenv("BOK_LATE_FINAL_MAX_TAIL_CHARS", "0")
    assert lp._late_final_max_tail_chars() == 1  # 地板 1


def test_forward_env_registered():
    """立法：四个新 env 键（F2 两键+F4 两键）都进 bok._FORWARD_ENV（prod 死门防复发）。"""
    for key in (
        "BOK_LATE_FINAL_GUARD",
        "BOK_LATE_FINAL_MAX_TAIL_CHARS",
        "BOK_BRANCH_REFUSE_CONFIRM",
        "BOK_BRANCH_REFUSE_HOTWORD_GUARD",
    ):
        assert key in bok._FORWARD_ENV, f"{key} 未登记 _FORWARD_ENV"


# ---- 源级 pin：流内接线 ------------------------------------------------------


def test_stream_wiring_source_pins():
    """源级 pin：两条停嘴路径（_run EOS / _hold_flush）都挂护栏；
    Qwen3ASRLiveSTT 有 set_reply_busy；agent 钩子喂旗。"""
    lp_src = (ROOT / "apps/agent/agent_runtime/providers/livekit_plugins.py").read_text(
        encoding="utf-8"
    )
    assert lp_src.count("QWEN3_ASR_LATE_FINAL_DROP") == 2, "停嘴与 join-flush 两路都要打点"
    assert lp_src.count("late_final_is_new_speech(") >= 3  # 定义 + 两处调用
    assert "def set_reply_busy" in lp_src
    assert "_reply_busy = False" in lp_src  # 内芯默认旗
    ag_src = (ROOT / "apps/agent/agent_runtime/agent.py").read_text(encoding="utf-8")
    assert "set_reply_busy(agent_busy_for_state(state))" in ag_src
    # 注册条件改为恒挂（partial 抑制档=0 时旗也要喂）
    assert "if _partial_gate_stt is not None:\n        # 与心跳钩子同规" in ag_src


def test_closing_say_wiring_source_pins():
    """源级 pin（F4 二修）：流三闸（segment_eos/join_flush/句级提交跳过）+ agent
    在收线直念前后置/撤旗。"""
    lp_src = (ROOT / "apps/agent/agent_runtime/providers/livekit_plugins.py").read_text(
        encoding="utf-8"
    )
    assert lp_src.count("QWEN3_ASR_CLOSING_SAY_SUPPRESS") == 2  # segment_eos + join_flush
    assert "closing_say' if _cs else 'tail_append" in lp_src  # 尾巴路径丢弃归因（closing/tail_append）
    assert "def _closing_say_active" in lp_src
    assert "def set_closing_say" in lp_src
    assert "_closing_say = False" in lp_src  # 内芯默认旗
    assert "and not _closing_say_active(self._stt_):" in lp_src  # 句级提交窗跳过
    ag_src = (ROOT / "apps/agent/agent_runtime/agent.py").read_text(encoding="utf-8")
    assert "set_closing_say(True)" in ag_src
    assert "set_closing_say(False)" in ag_src
    # 旗的置/撤必须包住收线台词直念（try/finally 保证必撤）
    i_on = ag_src.index("set_closing_say(True)")
    i_say = ag_src.index("await _say_script(", i_on)  # 收线台词那次调用（跳过模块级函数定义）
    i_off = ag_src.index("set_closing_say(False)", i_say)
    assert i_on < i_say < i_off


def test_hotword_guard_wiring_source_pins():
    """源级 pin（hotword_only 否决层）：两个调用点都传 vocab_terms；kill-switch
    键在 livekit_plugins（读取面）与 tools/bok.py（_FORWARD_ENV 登记面）各出现
    一次；DROP 打点仍只有两处 print（计数不破坏）。"""
    lp_src = (ROOT / "apps/agent/agent_runtime/providers/livekit_plugins.py").read_text(
        encoding="utf-8"
    )
    assert lp_src.count("vocab_terms=_vt") == 2, "停嘴与 join-flush 两路都要传词表"
    assert lp_src.count("BOK_LATE_FINAL_HOTWORD_GUARD") == 1, "kill-switch 键只准一处读取"
    assert "def _late_final_hotword_guard_on" in lp_src
    assert "def _vocab_only_net" in lp_src
    assert "if agent_busy and vocab_terms and _vocab_only_net(norm, vocab_terms):" in lp_src
    assert "hotword_only'} " in lp_src  # DROP 打点 reason 三态（closing/tail_append/hotword_only）
    assert lp_src.count("hotword_only'} ") == 2  # 两路打点都要能归因 hotword_only
    bok_src = bok_source()
    assert bok_src.count("BOK_LATE_FINAL_HOTWORD_GUARD") == 1, "登记面只准一处"
    assert "BOK_LATE_FINAL_HOTWORD_GUARD" in bok._FORWARD_ENV


# ---- 行为面：真 _run 端到端（fake VAD + fake sidecar）------------------------


class _FakeResp:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _FakeClient:
    finish_body: dict = {"text": "", "language": ""}

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url: str, *a, **kw):
        if "start" in url:
            return _FakeResp({"session_id": "sid"})
        if "finish" in url:
            return _FakeResp(dict(_FakeClient.finish_body))
        return _FakeResp({"text": "", "language": ""})


def _make_stream() -> lp._Qwen3ASRLiveStream:
    stt_inner = Qwen3ASRSTT(
        base_url="http://127.0.0.1:8787", language_state=LanguageState(lang="zh")
    )
    return lp._Qwen3ASRLiveStream(stt_inner, vad=object(), conn_options=lp.APIConnectOptions())


async def _close(stream) -> None:
    stream._event_ch.close()
    # 已 cancel 的监视任务用 return_exceptions 收殓（wait_for 会把 CancelledError 抛出来）
    await asyncio.gather(stream._metrics_task, return_exceptions=True)


async def _drive_vad_stop(stream, *, committed: str, finish_text: str) -> list[str]:
    """START→EOS（fake VAD）跑真 _run；预置已提交前缀，返回事件类型序列。"""

    class _FakeVADStream:
        def __init__(self, stream_ref):
            self._ref = stream_ref
            self._n = 0

        def flush(self):
            pass

        def end_input(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            self._n += 1
            if self._n == 1:
                return types.SimpleNamespace(
                    type=lp.vad.VADEventType.START_OF_SPEECH,
                    speech_duration=0.0,
                    silence_duration=0.0,
                    inference_duration=0.0,
                    probability=1.0,
                    speaking=True,
                    frames=[],
                )
            if self._n == 2:
                return types.SimpleNamespace(
                    type=lp.vad.VADEventType.END_OF_SPEECH,
                    speech_duration=2.0,
                    silence_duration=0.2,
                    inference_duration=0.05,
                    probability=0.0,
                    speaking=False,
                    frames=[],
                )
            self._ref._input_ch.close()
            raise StopAsyncIteration

    class _FakeVAD:
        def __init__(self, stream_ref):
            self._ref = stream_ref

        def stream(self):
            return _FakeVADStream(self._ref)

    _FakeClient.finish_body = {"text": finish_text, "language": "zh"}
    stream._metrics_task.cancel()  # 监视任务会让事件，断言期全部留给 channel
    stream._vad = _FakeVAD(stream)
    stream._committed_text = committed
    stream._last_sentence = committed
    stream._commit_idx = len(committed)
    stream._pending = bytearray(b"\x00\x00")
    try:
        await asyncio.wait_for(stream._task, 2)
    finally:
        names: list[str] = []
        while True:
            try:
                names.append(stream._event_ch.recv_nowait().type.name)
            except (ChanEmpty, ChanClosed):
                break
        stream._event_ch.close()
        await asyncio.gather(stream._metrics_task, return_exceptions=True)
    return names


def _fresh_gates(monkeypatch) -> None:
    monkeypatch.delenv("QWEN3_ASR_SENTENCE_COMMIT", raising=False)
    monkeypatch.delenv("TURN_DETECTION", raising=False)
    monkeypatch.delenv("BOK_LATE_FINAL_GUARD", raising=False)
    monkeypatch.delenv("QWEN3_HOTWORD_ECHO_GUARD", raising=False)
    monkeypatch.setattr(lp, "httpx", types.SimpleNamespace(AsyncClient=_FakeClient))


def test_run_hallucinated_tail_dropped_while_busy(monkeypatch, capsys):
    """F2 端到端：AI 播报中，finish 幻听「那。」尾巴 → 第二条 FINAL 唔发。"""
    _fresh_gates(monkeypatch)

    async def scenario():
        stream = _make_stream()
        stream._stt_._reply_busy = True  # agent_state=speaking（快路罐头在播）
        try:
            return await _drive_vad_stop(
                stream, committed=_COMMITTED, finish_text=f"{_COMMITTED}那。"
            )
        finally:
            await _close(stream)

    names = asyncio.run(scenario())
    assert names == ["START_OF_SPEECH", "END_OF_SPEECH"], names
    out = capsys.readouterr().out
    assert "QWEN3_ASR_LATE_FINAL_DROP" in out
    assert "那。" in out


def test_run_new_speech_tail_still_final_while_busy(monkeypatch, capsys):
    """AI 播报中但尾巴是真新话（够长）→ 照发 FINAL（真打断保留）。"""
    _fresh_gates(monkeypatch)
    long_tail = "那我想再问一下理赔的具体流程是这样的"

    async def scenario():
        stream = _make_stream()
        stream._stt_._reply_busy = True
        try:
            return await _drive_vad_stop(
                stream, committed=_COMMITTED, finish_text=f"{_COMMITTED}{long_tail}"
            )
        finally:
            await _close(stream)

    names = asyncio.run(scenario())
    assert names == ["START_OF_SPEECH", "END_OF_SPEECH", "FINAL_TRANSCRIPT"], names
    assert "QWEN3_ASR_LATE_FINAL_DROP" not in capsys.readouterr().out


def test_run_idle_agent_short_tail_still_final(monkeypatch):
    """AI 空闲（listening）→ 极短尾巴照发（带内容短尾豁免旧行为全保留）。"""
    _fresh_gates(monkeypatch)

    async def scenario():
        stream = _make_stream()
        assert stream._stt_._reply_busy is False  # 默认旗=False（getattr 兜底同值）
        try:
            return await _drive_vad_stop(
                stream, committed=_COMMITTED, finish_text=f"{_COMMITTED}那。"
            )
        finally:
            await _close(stream)

    names = asyncio.run(scenario())
    assert names == ["START_OF_SPEECH", "END_OF_SPEECH", "FINAL_TRANSCRIPT"], names


def test_run_kill_switch_restores_old_behavior(monkeypatch):
    """BOK_LATE_FINAL_GUARD=0 → 幻听尾巴照发（回退口）。"""
    _fresh_gates(monkeypatch)
    monkeypatch.setenv("BOK_LATE_FINAL_GUARD", "0")

    async def scenario():
        stream = _make_stream()
        stream._stt_._reply_busy = True
        try:
            return await _drive_vad_stop(
                stream, committed=_COMMITTED, finish_text=f"{_COMMITTED}那。"
            )
        finally:
            await _close(stream)

    names = asyncio.run(scenario())
    assert names == ["START_OF_SPEECH", "END_OF_SPEECH", "FINAL_TRANSCRIPT"], names


def test_run_closing_say_suppresses_whole_segment(monkeypatch, capsys):
    """F4 二修端到端：收线直念窗内，新段 EOS 连同 finish FINAL 整段静默丢弃
    （stt 模式框架收到 EOS 就 commit 轮——所以 EOS 必须一并扣住，才能不打断
    在播收线台词）。"""
    _fresh_gates(monkeypatch)

    async def scenario():
        stream = _make_stream()
        stream._stt_._closing_say = True  # agent 正在播【收线】台词
        try:
            return await _drive_vad_stop(
                stream, committed=_COMMITTED, finish_text=f"{_COMMITTED}我不是这个人。"
            )
        finally:
            await _close(stream)

    names = asyncio.run(scenario())
    assert names == ["START_OF_SPEECH"], names  # EOS/FINAL 都没发
    out = capsys.readouterr().out
    assert "QWEN3_ASR_CLOSING_SAY_SUPPRESS src=segment_eos" in out


def test_run_closing_say_suppress_off_after_window(monkeypatch):
    """收线窗结束后（旗已撤），同一段音频照常 EOS+FINAL——窗外语义零变化。"""
    _fresh_gates(monkeypatch)

    async def scenario():
        stream = _make_stream()
        stream._stt_._closing_say = True
        stream._stt_._closing_say = False  # 台词念完撤旗
        try:
            return await _drive_vad_stop(
                stream, committed="", finish_text="我不是这个人。"
            )
        finally:
            await _close(stream)

    names = asyncio.run(scenario())
    assert names == ["START_OF_SPEECH", "END_OF_SPEECH", "FINAL_TRANSCRIPT"], names


def test_run_hotword_only_tail_dropped_with_reason(monkeypatch, capsys):
    """hotword_only 端到端：AI 播报中 + 流级词表含「打错电话」，finish 幻听
    「打错电话。」→ 第二条 FINAL 唔发，DROP 打点 reason=hotword_only（与
    tail_append 同一条打印区分，不新增 print）。"""
    _fresh_gates(monkeypatch)

    async def scenario():
        stream = _make_stream()
        stream._stt_._reply_busy = True
        stream._vocab_terms = ("打错电话", "赔偿")  # __init__ 反解位（:5889）的测试注入
        try:
            return await _drive_vad_stop(
                stream, committed=_COMMITTED, finish_text=f"{_COMMITTED}打错电话。"
            )
        finally:
            await _close(stream)

    names = asyncio.run(scenario())
    assert names == ["START_OF_SPEECH", "END_OF_SPEECH"], names
    out = capsys.readouterr().out
    assert "QWEN3_ASR_LATE_FINAL_DROP" in out
    assert "reason=hotword_only" in out


def test_run_hotword_guard_kill_switch_restores_f2(monkeypatch, capsys):
    """BOK_LATE_FINAL_HOTWORD_GUARD=0 → 整层否决不评估，「打错电话。」回 F2
    现状照发 FINAL（外层极短追加门拦不住 4 字）。"""
    _fresh_gates(monkeypatch)
    monkeypatch.setenv("BOK_LATE_FINAL_HOTWORD_GUARD", "0")

    async def scenario():
        stream = _make_stream()
        stream._stt_._reply_busy = True
        stream._vocab_terms = ("打错电话", "赔偿")
        try:
            return await _drive_vad_stop(
                stream, committed=_COMMITTED, finish_text=f"{_COMMITTED}打错电话。"
            )
        finally:
            await _close(stream)

    names = asyncio.run(scenario())
    assert names == ["START_OF_SPEECH", "END_OF_SPEECH", "FINAL_TRANSCRIPT"], names
    assert "reason=hotword_only" not in capsys.readouterr().out
