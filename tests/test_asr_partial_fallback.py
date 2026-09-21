"""ASR partial 暴露口 + E2 热词泄漏清洗 ``fallback_text`` 接线(2026-09-21)。

**问题**(计划档 §28.3 已知残余):``hotword_leak.sanitize`` 的**单词泄漏**判据
依赖 ``fallback_text``(客户真讲过的话=上一条 partial);两个 ``_asr_postprocess``
调用点都传空串 → 该判据结构性不成立,只剩「标签」与「≥2 连续热词」两条判据。

**修复**:``_Qwen3ASRLiveStream`` 把每窗 partial 末稿(未提交坐标系)贴到内芯
``Qwen3ASRSTT._turn_partial_text``;``Qwen3ASRLiveSTT.last_partial_text()`` 是
agent 侧的只读取口;两个调用点都喂进 ``fallback_text``。清零点=``_reset``(本段
已提交)+ ``_start_session``(新语音段=新一轮)⇒ 值只属**本轮**:上一轮的话不会
喂给下一轮(短句无 partial 的轮也拿不到上一轮的话)。停嘴/join-flush 两条 FINAL
路径在 ``_reset`` 之后按 **pre-reset 快照**把「这条 FINAL 的 fallback」重贴回来
(agent 钩子在 FINAL 之后才跑,那时 ``_reset`` 已过);无 FINAL 发出(纯 dump/
短尾丢弃/迟到护栏丢弃)时保持空。流收官不清(在途钩子还要读;内芯每通一个,无
跨通残留)。

参照 ``test_snippet_leak_wiring.py`` / ``test_late_final_guard.py`` 的姿势:
纯函数面直打 + 源级锚 + 真 ``_run`` 端到端(fake VAD + fake sidecar),无网络、
无真栈、无模型。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from livekit.agents.utils.aio.channel import ChanClosed, ChanEmpty  # noqa: E402

from agent_runtime import agent as ag  # noqa: E402
from agent_runtime.providers import livekit_plugins as lp  # noqa: E402
from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    LanguageState,
    Qwen3ASRLiveSTT,
    Qwen3ASRSTT,
)

_ROOT = Path(__file__).resolve().parents[1]
_AG_SRC = (_ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
_LP_SRC = (
    _ROOT / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py"
).read_text(encoding="utf-8")

_HOTWORDS = ["拼多多", "京东"]
_PARTIAL = "我想查下快递"
_DUMP_FINAL = f"拼多多 {_PARTIAL}"  # 单词打头 dump + 真实内容(=partial)
_COMMITTED = "我想问一下赔偿怎么算。"


# --------------------------------------------------------------- fail-soft 取口


def test_agent_helper_is_fail_soft():
    """取口绝不因「拿不到 partial」把一轮搞崩:缺方法/None/异常一律空串。"""
    assert ag._last_partial_text(None) == ""  # 非 live 包装(QWEN3_ASR_STREAM=0)
    assert ag._last_partial_text(object()) == ""  # 假 STT/官方 StreamAdapter

    class _Boom:
        def last_partial_text(self):
            raise RuntimeError("boom")

    assert ag._last_partial_text(_Boom()) == ""

    class _None:
        def last_partial_text(self):
            return None

    assert ag._last_partial_text(_None()) == ""

    class _Good:
        def last_partial_text(self):
            return "查下快递"

    assert ag._last_partial_text(_Good()) == "查下快递"


# ------------------------------------------------------- 流内暴露口(unit)


class _FakeResp:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _FakeClient:
    """httpx.AsyncClient 替身:start/chunk/finish 按类属性作答。"""

    chunk_body: dict = {"text": "", "language": ""}
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
        return _FakeResp(dict(_FakeClient.chunk_body))


def _fresh_gates(monkeypatch) -> None:
    monkeypatch.delenv("QWEN3_ASR_SENTENCE_COMMIT", raising=False)
    monkeypatch.delenv("TURN_DETECTION", raising=False)
    monkeypatch.delenv("BOK_LATE_FINAL_GUARD", raising=False)
    monkeypatch.delenv("QWEN3_HOTWORD_ECHO_GUARD", raising=False)
    monkeypatch.delenv("QWEN3_ASR_HESITATION_GATE", raising=False)
    monkeypatch.setenv("QWEN3_ASR_JOIN_HOLD_MS", "0")  # 隔离 hold 腿(另有专测)
    monkeypatch.setattr(lp, "httpx", types.SimpleNamespace(AsyncClient=_FakeClient))


def _make_stream() -> tuple[lp._Qwen3ASRLiveStream, Qwen3ASRSTT]:
    inner = Qwen3ASRSTT(
        base_url="http://127.0.0.1:8787", language_state=LanguageState(lang="zh")
    )
    stream = lp._Qwen3ASRLiveStream(inner, vad=object(), conn_options=lp.APIConnectOptions())
    return stream, inner


def _wrapper(inner: Qwen3ASRSTT) -> Qwen3ASRLiveSTT:
    return Qwen3ASRLiveSTT(stt_=inner, vad_=object())


def test_accessor_empty_before_any_partial(monkeypatch):
    """没 partial(短句 <0.6s 窗)时=空串:调用方行为同未接线。"""
    _fresh_gates(monkeypatch)

    async def scenario():
        stream, inner = _make_stream()
        try:
            return _wrapper(inner).last_partial_text()
        finally:
            await _close_stream(stream)

    assert asyncio.run(scenario()) == ""


def test_accessor_returns_latest_partial_then_reset_clears(monkeypatch):
    """最新一窗 partial 上暴露位;``_reset``(本段已提交)清零。"""
    _fresh_gates(monkeypatch)
    _FakeClient.chunk_body = {"text": "我個單號係三七七八九零", "language": "zh"}

    async def scenario():
        stream, inner = _make_stream()
        w = _wrapper(inner)
        try:
            stream._session_id = "sid"
            stream._pending = bytearray(b"\x00\x19" * 12800)  # 0.8s:PCM 过 0.6s 门槛
            stream._last_post = 0.0
            assert w.last_partial_text() == ""
            await stream._maybe_partial()
            first = w.last_partial_text()
            # 第二窗(文本变了):暴露位跟到最新一窗
            _FakeClient.chunk_body = {"text": "我個單號係三七七八九零，唔該", "language": "zh"}
            stream._pending = bytearray(b"\x00\x19" * 12800)
            stream._last_post = 0.0
            await stream._maybe_partial()
            second = w.last_partial_text()
            stream._reset()
            return first, second, w.last_partial_text()
        finally:
            await _close_stream(stream)

    first, second, after_reset = asyncio.run(scenario())
    assert first == "我個單號係三七七八九零"
    assert second == "我個單號係三七七八九零，唔該"
    assert after_reset == ""


def test_new_turn_start_clears_previous_turn_partial(monkeypatch):
    """新一轮语音段开场(``_start_session``)= 清零:上一轮的话喂不进下一轮(短句轮)。"""
    _fresh_gates(monkeypatch)

    async def scenario():
        stream, inner = _make_stream()
        w = _wrapper(inner)
        try:
            inner._turn_partial_text = "上一轮说过的话"  # 模拟上一轮末稿仍在
            await stream._start_session()
            return w.last_partial_text()
        finally:
            await _close_stream(stream)

    assert asyncio.run(scenario()) == ""


def test_stream_teardown_keeps_partial_for_inflight_hook(monkeypatch):
    """流收官**不清**暴露位:收官 FINAL 的 agent 钩子可能仍在途(清了就白丢).

    跨通残留由「内芯 Qwen3ASRSTT 每通一个」+ 新一轮 START 清零兜住,不需要流
    关闭清(清了反而与在途钩子抢)。
    """
    _fresh_gates(monkeypatch)

    async def scenario():
        stream, inner = _make_stream()
        w = _wrapper(inner)
        inner._turn_partial_text = "本通最后一段的话"
        await _close_stream(stream)
        return w.last_partial_text()

    assert asyncio.run(scenario()) == "本通最后一段的话"


# ------------------------------------------------ 端到端:真 _run(停嘴 FINAL)


async def _close_stream(stream) -> None:
    """收殓:关事件通道 + 收掉两个任务(未驱动的流 _run 会以 AttributeError 结束,
    用 return_exceptions 取回,免得 asyncio 打「exception was never retrieved」)。"""
    stream._event_ch.close()
    if not stream._task.done():
        stream._task.cancel()
    await asyncio.gather(stream._task, stream._metrics_task, return_exceptions=True)


def _fake_vad(stream) -> object:
    class _FakeVADStream:
        def __init__(self, ref):
            self._ref = ref
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
        def __init__(self, ref):
            self._ref = ref

        def stream(self):
            return _FakeVADStream(self._ref)

    return _FakeVAD(stream)


def _drain(stream) -> list[str]:
    names: list[str] = []
    while True:
        try:
            names.append(stream._event_ch.recv_nowait().type.name)
        except (ChanEmpty, ChanClosed):
            break
    return names


def test_stop_mouth_final_carries_partial_as_fallback(monkeypatch):
    """停嘴 FINAL 发出后,暴露位=这条 FINAL 的 partial 末稿(pre-reset 快照重贴)。

    agent 的 ``on_user_turn_completed``/``_on_conversation_item`` 在本 FINAL 之后
    才跑(那时 ``_reset`` 已过)——所以「清零」与「可取」必须同时成立。
    """
    _fresh_gates(monkeypatch)

    async def scenario():
        stream, inner = _make_stream()
        try:
            _FakeClient.finish_body = {"text": f"{_PARTIAL}，几时到。", "language": "zh"}
            stream._vad = _fake_vad(stream)
            stream._metrics_task.cancel()
            stream._pending = bytearray(b"\x00\x00")
            stream._last_partial = _PARTIAL  # 本段最后一窗 partial
            await asyncio.wait_for(stream._task, 2)
            names = _drain(stream)
            return names, _wrapper(inner).last_partial_text()
        finally:
            await _close_stream(stream)

    names, fallback = asyncio.run(scenario())
    assert names == ["START_OF_SPEECH", "END_OF_SPEECH", "FINAL_TRANSCRIPT"], names
    assert fallback == _PARTIAL


def test_dropped_tail_leaves_no_fallback(monkeypatch):
    """FINAL 被丢弃(迟到短尾护栏)= 没有 FINAL → 暴露位保持空(宁可无,不可错轮)。"""
    _fresh_gates(monkeypatch)

    async def scenario():
        stream, inner = _make_stream()
        try:
            stream._stt_._reply_busy = True  # AI 播报中
            _FakeClient.finish_body = {"text": f"{_COMMITTED}那。", "language": "zh"}
            stream._vad = _fake_vad(stream)
            stream._metrics_task.cancel()
            stream._pending = bytearray(b"\x00\x00")
            stream._last_partial = _COMMITTED
            stream._committed_text = _COMMITTED
            stream._last_sentence = _COMMITTED
            stream._commit_idx = len(_COMMITTED)
            await asyncio.wait_for(stream._task, 2)
            names = _drain(stream)
            return names, _wrapper(inner).last_partial_text()
        finally:
            await _close_stream(stream)

    names, fallback = asyncio.run(scenario())
    assert names == ["START_OF_SPEECH", "END_OF_SPEECH"], names  # 无 FINAL
    assert fallback == ""


def test_join_flush_final_carries_partial_as_fallback(monkeypatch):
    """join-flush(跨段拼接 hold)走同一套 pre-reset 快照重贴。"""
    _fresh_gates(monkeypatch)

    async def scenario():
        stream, inner = _make_stream()
        try:
            _FakeClient.finish_body = {"text": "我嘅WhatsApp係六四三二一七二", "language": "zh"}
            stream._metrics_task.cancel()
            stream._session_id = "sid"
            stream._pending = bytearray(b"\x00\x00")
            stream._last_partial = "我嘅WhatsApp係"
            stream._join_hold_active = True
            await stream._hold_flush()
            names = _drain(stream)
            return names, _wrapper(inner).last_partial_text()
        finally:
            await _close_stream(stream)

    names, fallback = asyncio.run(scenario())
    assert names == ["END_OF_SPEECH", "FINAL_TRANSCRIPT"], names
    assert fallback == "我嘅WhatsApp係"


# --------------------------------------------- 功能面:单词泄漏判据真被点亮


def test_single_hotword_leak_needs_fallback(monkeypatch):
    """integration-ish:同一个终稿,``fallback_text`` 有无=是否剥掉单词泄漏。

    ``sanitize`` 偏差②:单词命中判据要求 fallback 非空且与 remainder 归一化后
    相等/互为后缀 ⇒ 无 fallback 时结构性不成立(修复前 agent 恒传空串=该判据
    永不触发)。
    """
    monkeypatch.delenv("BOK_HOTWORD_LEAK_SANITIZE", raising=False)
    stripped, state, _ = ag._asr_postprocess(
        _DUMP_FINAL, snippet_rules=[], hotword_terms=_HOTWORDS, fallback_text=_PARTIAL
    )
    assert (stripped, state) == (_PARTIAL, "trimmed")

    kept, state_no, _ = ag._asr_postprocess(
        _DUMP_FINAL, snippet_rules=[], hotword_terms=_HOTWORDS, fallback_text=""
    )
    assert (kept, state_no) == (_DUMP_FINAL, "")


def test_real_chain_partial_feeds_single_hotword_strip(monkeypatch):
    """整链:流内 partial → 暴露口 → ``_asr_postprocess`` 真的剥掉单词泄漏。

    这是本改动的存在理由——修复前 fallback 恒空,同一终稿一字不动。
    """
    _fresh_gates(monkeypatch)
    monkeypatch.delenv("BOK_HOTWORD_LEAK_SANITIZE", raising=False)
    _FakeClient.chunk_body = {"text": _PARTIAL, "language": "zh"}

    async def scenario():
        stream, inner = _make_stream()
        w = _wrapper(inner)
        try:
            stream._session_id = "sid"
            stream._pending = bytearray(b"\x00\x19" * 12800)
            stream._last_post = 0.0
            await stream._maybe_partial()  # 真 partial 窗
            fallback = w.last_partial_text()
            out, state, _ = ag._asr_postprocess(
                _DUMP_FINAL, snippet_rules=[], hotword_terms=_HOTWORDS, fallback_text=fallback
            )
            return fallback, out, state
        finally:
            await _close_stream(stream)

    fallback, out, state = asyncio.run(scenario())
    assert fallback == _PARTIAL
    assert (out, state) == (_PARTIAL, "trimmed")


def test_fallback_must_agree_with_remainder(monkeypatch):
    """fallback 与终稿剩余不一致 → 单词判据不成立,文本一字不动(防乱剥)。"""
    monkeypatch.delenv("BOK_HOTWORD_LEAK_SANITIZE", raising=False)
    out, state, _ = ag._asr_postprocess(
        _DUMP_FINAL,
        snippet_rules=[],
        hotword_terms=_HOTWORDS,
        fallback_text="完全无关的一句话",
    )
    assert (out, state) == (_DUMP_FINAL, "")


def test_multi_hotword_leak_ignores_fallback(monkeypatch):
    """≥2 连续热词判据不依赖 fallback(修复前也能命中)——但输出面会被 fallback 影响。

    ≥2 命中且 remainder 与 fallback 不相似时,``sanitize`` 按设计**回吐 fallback**
    (模块 docstring 步骤 6)。这正是「只喂本轮 partial、每轮清零」的动机:喂了别轮
    的话,就会把别轮的话当本轮客户话落库。
    """
    monkeypatch.delenv("BOK_HOTWORD_LEAK_SANITIZE", raising=False)
    text = "拼多多 京东 嗯。"
    out, state, _ = ag._asr_postprocess(
        text, snippet_rules=[], hotword_terms=_HOTWORDS, fallback_text=_PARTIAL
    )
    assert (out, state) == (_PARTIAL, "trimmed")  # 回吐 fallback(本轮的 partial)
    bare, bare_state, _ = ag._asr_postprocess(
        text, snippet_rules=[], hotword_terms=_HOTWORDS, fallback_text=""
    )
    assert (bare, bare_state) == ("嗯。", "trimmed")  # 无 fallback=留真剩余


def test_pure_dump_dropped_with_or_without_fallback(monkeypatch):
    """纯 dump 恒整段丢弃(偏差①),fallback 不改变这条。"""
    monkeypatch.delenv("BOK_HOTWORD_LEAK_SANITIZE", raising=False)
    for fb in (_PARTIAL, ""):
        out, state, _ = ag._asr_postprocess(
            "Vocabulary: 拼多多, 京东", snippet_rules=[], hotword_terms=_HOTWORDS, fallback_text=fb
        )
        assert (out, state) == ("", "dropped")


def test_e2_kill_switch_off_ignores_fallback(monkeypatch):
    """kill-switch 仍全轨生效:``BOK_HOTWORD_LEAK_SANITIZE=0`` → fallback 也进不了场。"""
    monkeypatch.setenv("BOK_HOTWORD_LEAK_SANITIZE", "0")
    out, state, applied = ag._asr_postprocess(
        _DUMP_FINAL, snippet_rules=[], hotword_terms=_HOTWORDS, fallback_text=_PARTIAL
    )
    assert (out, state, applied) == (_DUMP_FINAL, "", [])


# --------------------------------------------------------------- 结构级锚


def test_both_call_sites_pass_partial_fallback():
    """两个调用点都喂 partial(钩子 + ``_on_conversation_item`` 落库复算)。

    任一漏喂 = 两边文本分叉(听 A 记 B 禁令)或单词判据悄悄失效,故以源级锚钉死。
    """
    assert _AG_SRC.count("fallback_text=_last_partial_text(_partial_gate_stt)") == 2
    assert 'fallback_text=""' not in _AG_SRC  # 旧「拿不到所以传空串」不留残骸

    item_def = _AG_SRC.index("def _on_conversation_item(ev):")
    item_end = _AG_SRC.index("_spawn_report(_report_assistant_turn", item_def)
    assert "fallback_text=_last_partial_text(_partial_gate_stt)" in _AG_SRC[item_def:item_end]

    hook_call = _AG_SRC.index("_clean_text, _leak_state, _snip_applied = _asr_postprocess(")
    assert "fallback_text=_last_partial_text(_partial_gate_stt)" in _AG_SRC[hook_call : hook_call + 400]

    # 取口只在 live 包装时非 None:非 live/假 STT 一路拿不到方法 → 空串
    assert "_partial_gate_stt = stt_provider if isinstance(stt_provider, Qwen3ASRLiveSTT) else None" in _AG_SRC


def test_stream_publishes_and_resets_partial():
    """流内写点/清点齐全:每窗贴、``_reset`` 清、新一轮开场清、两条 FINAL 路径重贴。"""
    assert "def _publish_turn_partial" in _LP_SRC
    assert "def _turn_partial_for_fallback" in _LP_SRC  # 未提交坐标系派生(零副作用)
    assert "self._publish_turn_partial(self._turn_partial_for_fallback())" in _LP_SRC  # 每窗 partial
    assert _LP_SRC.count("self._publish_turn_partial(fallback_tail)") == 2  # 停嘴 + join-flush
    assert _LP_SRC.count("fallback_tail = self._turn_partial_for_fallback()") == 2
    assert _LP_SRC.count("self._stt_._turn_partial_text = \"\"") == 2  # _reset / _start_session
    assert "def last_partial_text" in _LP_SRC  # agent 侧取口
    assert "self._turn_partial_text: str = \"\"" in _LP_SRC  # 内芯字段
    # 句级提交路径的时序:贴 partial 必须在「提交」之前——该 FINAL 的 fallback
    # 正是提交前那一版(_maybe_partial 内的相对次序也能被源级锚钉住)。
    pub = _LP_SRC.index("self._publish_turn_partial(self._turn_partial_for_fallback())")
    commit = _LP_SRC.index("if sentence_commit_enabled() and not _closing_say_active(self._stt_):", pub)
    assert pub < commit


def test_turn_partial_derivation_is_side_effect_free(monkeypatch):
    """派生用前缀剥离(而非 ``_uncommitted``):不触发重解日志、坐标失配时不猜。

    ``_uncommitted`` 在重解修正分支会打 ``QWEN3_ASR_REDECODE_DROP``——每窗多叫一次
    =同一窗重复日志,故 fallback 派生走独立的小方法。
    """
    _fresh_gates(monkeypatch)

    async def scenario():
        stream, _inner = _make_stream()
        try:
            stream._last_partial = "我想查下快递，几时到"
            no_commit = stream._turn_partial_for_fallback()
            stream._committed_text = "我想查下快递，"
            stream._last_sentence = "我想查下快递，"
            with_commit = stream._turn_partial_for_fallback()
            # 坐标失配(窗口被重写,已提交前缀定位不到)→ 原样返回整窗,不猜
            stream._committed_text = "完全对不上的另一句话。"
            stream._last_sentence = ""
            mismatched = stream._turn_partial_for_fallback()
            stream._last_partial = ""
            return no_commit, with_commit, mismatched, stream._turn_partial_for_fallback()
        finally:
            await _close_stream(stream)

    no_commit, with_commit, mismatched, empty = asyncio.run(scenario())
    assert no_commit == "我想查下快递，几时到"
    assert with_commit == "几时到"
    assert mismatched == "我想查下快递，几时到"
    assert empty == ""
