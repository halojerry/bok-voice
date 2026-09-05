"""MiniMaxTTS 真流式(streaming=True + SynthesizeStream)契约与增量语义。

核心防回归点：
1. capabilities.streaming 必须为 True —— livekit 才会走 stream()(SynthesizeStream)，
   而不是被 StreamAdapter+SentenceTokenizer 包(那会等整句/全文,首包 8-18s)。
2. stream() 返回 SynthesizeStream 实例(而非 NotImplementedError)。
3. task_continue 的 text 是「累积全文」而非纯增量(实测纯增量丢 ~40% 音频)。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

import asyncio  # noqa: E402

import pytest  # noqa: E402

from agent_runtime.providers.livekit_plugins import MiniMaxTTS  # noqa: E402


def _make_tts():
    return MiniMaxTTS(
        voice={"zh": "male-qn-qingse", "cantonese": "Cantonese_Male_news_anchor_vv2"},
        sample_rate=24000,
        api_key="test-key",
    )


def test_minimax_capabilities_streaming_true():
    # 真流式:streaming=True 让 voice 管线走 stream(),不包 StreamAdapter 句子切分。
    tts = _make_tts()
    assert tts.capabilities.streaming is True


def test_minimax_stream_returns_synthesize_stream():
    from livekit.agents import tts as lk_tts

    tts = _make_tts()

    async def _make():
        return tts.stream()

    stream = asyncio.run(_make())
    # SynthesizeStream 实例:livekit 会 async with stream / push_text / end_input / async for
    assert isinstance(stream, lk_tts.SynthesizeStream)


def test_minimax_stream_is_not_chunked_stream():
    # ChunkedStream(synthesize 返回)不代表流式;stream() 必须返回 SynthesizeStream。
    from livekit.agents import tts as lk_tts

    tts = _make_tts()

    async def _make():
        synth = tts.synthesize("你好")
        stream = tts.stream()
        return synth, stream

    synth, stream = asyncio.run(_make())
    assert isinstance(synth, lk_tts.ChunkedStream)
    assert not isinstance(synth, lk_tts.SynthesizeStream)
    assert isinstance(stream, lk_tts.SynthesizeStream)
    assert not isinstance(stream, lk_tts.ChunkedStream)


def test_minimax_splits_sentences_for_task_continue(monkeypatch):
    """增量语义:按句切分,每个 task_continue 发单句增量(非累积全文)。

    MiniMax WS 实测:发累积全文会重复合成前面句子;按句发增量不重复且首句即出声。
    """
    import json

    from agent_runtime.providers.livekit_plugins import _MiniMaxSynthesizeStream

    sent: list[dict] = []
    recv_count = {"n": 0}

    class FakeWS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def recv(self):
            recv_count["n"] += 1
            if recv_count["n"] == 1:
                return json.dumps({"event": "connected_success"})
            if recv_count["n"] == 2:
                return json.dumps({"event": "task_started"})
            # 之后立刻断开:结束 recv_loop,让 _run 快速收尾
            raise Exception("fake ws closed")

        async def send(self, payload):
            sent.append(json.loads(payload))

        async def close(self):
            pass

    calls: dict[str, int] = {"n": 0}

    async def fake_connect(*a, **kw):
        return FakeWS()

    monkeypatch.setattr("websockets.connect", fake_connect)

    tts = _make_tts()

    async def run():
        # 正常生命周期:构造即起 _main_task 跑 _run(连 FakeWS→task_start→读 input_ch)
        from livekit.agents import APIConnectOptions

        from agent_runtime.providers.livekit_plugins import _MiniMaxSynthesizeStream

        s = _MiniMaxSynthesizeStream(tts, APIConnectOptions())
        # 跨多块输入,含两句话
        s.push_text("你好，我係林先生。")
        s.push_text("想問下包裹幾時到？")
        s.end_input()
        # 等 _run 消费 input_ch 并发出 task_continue
        for _ in range(100):
            if len([m for m in sent if m.get("event") == "task_continue"]) >= 2:
                break
            await asyncio.sleep(0.05)
        s._task.cancel()  # 测试结束,取消后台任务(FakeWS.recv 挂起不拖慢)

    asyncio.run(run())

    continues = [m for m in sent if m.get("event") == "task_continue"]
    assert continues, "应发出 task_continue"
    texts = [m["text"] for m in continues]
    # 按句切分:每句单独发送,不累积
    assert texts[0] == "你好，我係林先生。", f"首句应为完整单句: {texts}"
    assert texts[-1] == "想問下包裹幾時到？", f"次句应为完整单句: {texts}"
    # 不重复:没有任何 task_continue 的 text 包含前面已发过的句子
    for i, t in enumerate(texts):
        for prev in texts[:i]:
            assert prev not in t, f"task_continue 不应重发前面句子: {texts}"


def test_minimax_stream_missing_key_no_crash():
    tts = MiniMaxTTS(voice="x", sample_rate=24000, api_key="")

    async def _make():
        return tts.stream()

    stream = asyncio.run(_make())
    assert stream is not None


def test_truncate_chat_items_keeps_recent_turns():
    from agent_runtime.providers.livekit_plugins import _truncate_chat_items

    from livekit.agents import llm as lk_llm

    def msg(role, txt):
        return lk_llm.ChatMessage(role=role, content=[txt])

    items = [msg("system", "你是客服"), msg("system", "【知识】规则")]
    for i in range(12):  # 12 轮 user+assistant
        items.append(msg("user", f"客户{i}"))
        items.append(msg("assistant", f"回复{i}"))
    out = _truncate_chat_items(items, max_turns=4)
    roles = [getattr(m, "role", "") for m in out]
    # 开头 system 全保留
    assert roles[0] == "system" and roles[1] == "system"
    # 对话只剩最近 4 对(8 条)
    dialog = roles[2:]
    assert dialog == ["user", "assistant"] * 4, f"应保留最近4对: {dialog}"
    # 最近一轮在
    assert out[-1].content == ["回复11"]
    assert out[-2].content == ["客户11"]
    # 最早轮被截掉
    assert not any(getattr(m, "content", "") == ["客户0"] for m in out)


def test_truncate_chat_items_short_untouched():
    from agent_runtime.providers.livekit_plugins import _truncate_chat_items

    from livekit.agents import llm as lk_llm

    items = [lk_llm.ChatMessage(role="system", content=["s"]),
             lk_llm.ChatMessage(role="user", content=["u"]),
             lk_llm.ChatMessage(role="assistant", content=["a"])]
    out = _truncate_chat_items(items, max_turns=4)
    assert len(out) == 3


def test_truncate_chat_items_amortized_hysteresis():
    """摊销式截断(滞回):超过 max_turns 对但未到 2×max_turns 对 → 不截(纯追加,
    KV-cache 逐轮命中);到 2×max_turns 对才一次剪回 max_turns 对。

    旧实现每轮剪到 max_turns 对,序列头部每轮都动 → mlx 缓存每轮重锚(实测
    +1.3s/轮),截断反而比不截慢。
    """
    from agent_runtime.providers.livekit_plugins import _truncate_chat_items

    from livekit.agents import llm as lk_llm

    def msg(role, txt):
        return lk_llm.ChatMessage(role=role, content=[txt])

    def build(pairs):
        items = [msg("system", "s")]
        for i in range(pairs):
            items.append(msg("user", f"客户{i}"))
            items.append(msg("assistant", f"回复{i}"))
        return items

    # 6 对:超过 max_turns(4) 但未到 2×max_turns(8) → 原样返回(不截)
    items6 = build(6)
    assert _truncate_chat_items(items6, max_turns=4) is items6

    # 9 对:到 2×max_turns 对 → 剪回 max_turns 对(8 条)
    out = _truncate_chat_items(build(9), max_turns=4)
    roles = [getattr(m, "role", "") for m in out]
    assert roles[0] == "system"
    assert roles[1:] == ["user", "assistant"] * 4
    assert out[-1].content == ["回复8"]


def test_preflight_throttled_by_prefix_growth(monkeypatch):
    """PREFLIGHT 节流:首发 ≥6 字;再发须比上次发射多 ≥4 字。

    每个 PREFLIGHT 吃一次框架抢跑预算(max_retries);旧「比 _stable 长 1 字就发」
    会把预算在长句说话中途烧光(FINAL 到达只 cancel 不重建 → 从零生成)。
    """
    import types

    from agent_runtime.providers import livekit_plugins as lp
    from agent_runtime.providers.livekit_plugins import (
        Qwen3ASRSTT,
        _ASR_PREFLIGHT_MIN_GROWTH_CHARS,
        _Qwen3ASRLiveStream,
    )

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"text": _FakeClient.text, "language": "cantonese"}

    class _FakeClient:
        text = ""

        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return _FakeResp()

    monkeypatch.setattr(lp, "httpx", types.SimpleNamespace(AsyncClient=_FakeClient))

    async def partial_with_text(stream, text):
        stream._session_id = "sid"
        stream._last_post = 0.0
        stream._pending = bytearray(16000 * 2)  # ≥0.6s PCM,过门槛
        _FakeClient.text = text
        await stream._maybe_partial()

    async def scenario():
        stream = _Qwen3ASRLiveStream(
            # 锚定语言=粤语：P1.5 起 PREFLIGHT 有语言门（partial 语言≠锚定语言
            # 不发），partial 回传 cantonese 必须与锚定一致先行到节流逻辑。
            Qwen3ASRSTT(base_url="http://127.0.0.1:8787", language_state=lp.LanguageState(lang="cantonese")),
            vad=object(),
            conn_options=lp.APIConnectOptions(),
        )
        try:
            # 逐窗增长:common 前缀长度 = 0 / 2 / 4 / 8(首发) / 11(只 +3,唔发) / 12(+4,再发)
            texts = [
                "唔該",                      # common=""(首窗无参照)
                "唔該幫我",                  # common=2 <6
                "唔該幫我查下件貨",          # common=4 <6
                "唔該幫我查下件貨幾時到",    # common=8 ≥6 且 +8 ≥4 → 首发
                "唔該幫我查下件貨幾時到呀",  # common=11,+3 <4 → 节流不发(旧码会发)
                "唔該幫我查下件貨幾時到呀唔該",  # common=12,+4 → 再发
            ]
            stables = []
            for t in texts:
                await partial_with_text(stream, t)
                stables.append(stream._stable)
            return stables
        finally:
            stream._event_ch.close()
            try:
                await asyncio.wait_for(stream._metrics_task, 1)
            except Exception:  # noqa: BLE001 - 监视任务收尾失败不影响断言
                pass

    stables = asyncio.run(scenario())
    # 首发在 8 字窗(≥6),第三窗(4 字)仍未发
    assert stables[2] == "" and stables[3] == "唔該幫我查下件貨"
    # +3 字窗被节流(_stable 不动);+4 字窗才再发——正好等于增长阈值
    assert stables[4] == "唔該幫我查下件貨"
    assert len(stables[5]) == len(stables[4]) + _ASR_PREFLIGHT_MIN_GROWTH_CHARS
    assert stables[5] == "唔該幫我查下件貨幾時到呀"


def test_minimax_overlap_flushes_cjk_fragment(monkeypatch):
    """overlap 对 CJK 生效:≥12 字带软停顿的片段喺 end_input 前就 task_continue。

    旧 _flushable 用裸 isalpha()/isdigit() 拦尾——CJK 汉字 isalpha()==True,
    中文片段全被拦,MINIMAX_TTS_OVERLAP 对中文流量全死。
    """
    import json

    sent: list[dict] = []
    recv_count = {"n": 0}

    class FakeWS:
        async def recv(self):
            recv_count["n"] += 1
            if recv_count["n"] == 1:
                return json.dumps({"event": "connected_success"})
            if recv_count["n"] == 2:
                return json.dumps({"event": "task_started"})
            await asyncio.Event().wait()  # 挂起:等 _task.cancel 收尾

        async def send(self, payload):
            sent.append(json.loads(payload))

        async def close(self):
            pass

    async def fake_connect(*a, **kw):
        return FakeWS()

    monkeypatch.setattr("websockets.connect", fake_connect)

    tts = _make_tts()

    async def run():
        from livekit.agents import APIConnectOptions

        from agent_runtime.providers.livekit_plugins import _MiniMaxSynthesizeStream

        s = _MiniMaxSynthesizeStream(tts, APIConnectOptions())
        s.push_text("今日天氣好好，")  # 7 字 < 12:唔送
        await asyncio.sleep(0.35)  # 超过默认 MINIMAX_TTS_OVERLAP_MS=300
        s.push_text("我哋去飲茶啦")  # 凑够 13 字,软停顿喺后半 → 可送
        for _ in range(100):
            if [m for m in sent if m.get("event") == "task_continue"]:
                break
            await asyncio.sleep(0.05)
        s._task.cancel()

    asyncio.run(asyncio.wait_for(run(), timeout=10))
    cont = [m for m in sent if m.get("event") == "task_continue"]
    assert cont, "CJK 片段应喺 end_input 前由 overlap 提前 task_continue"
    assert cont[0]["text"] == "今日天氣好好，我哋去飲茶啦"


def test_minimax_stall_watchdog_times_from_first_text_and_resends(monkeypatch):
    """看门狗二修(2026-09-06):计时起点=【首段文本发出】而非 task_started,
    超时断开重连后按序重发已发文本(积压不丢)。

    - 慢 LLM 轮(文本未发出)不再误触发——无 task_continue 就无计时起点;
    - 云端收了文本却不吐音频 → 阈值到点重连,新连接收到同样的 task_continue。
    """
    import json

    from agent_runtime.providers.livekit_plugins import _MiniMaxSynthesizeStream

    monkeypatch.setenv("MINIMAX_FIRST_AUDIO_TIMEOUT_S", "1")
    monkeypatch.setenv("MINIMAX_WS_POOL", "0")

    instances: list["FakeWS"] = []

    class FakeWS:
        def __init__(self):
            self.log: list[dict] = []
            self._recv_step = 0
            instances.append(self)

        async def recv(self):
            self._recv_step += 1
            if self._recv_step == 1:
                return json.dumps({"event": "connected_success"})
            if self._recv_step == 2:
                return json.dumps({"event": "task_started"})
            await asyncio.Event().wait()  # 卡死:握手后永不吐音频,等 cancel

        async def send(self, payload):
            self.log.append(json.loads(payload))

        async def close(self):
            pass

    async def fake_connect(*a, **kw):
        return FakeWS()

    monkeypatch.setattr("websockets.connect", fake_connect)

    tts = _make_tts()

    async def run():
        from livekit.agents import APIConnectOptions

        s = _MiniMaxSynthesizeStream(tts, APIConnectOptions())
        await asyncio.sleep(0.3)  # 先让首连接握手完成(计时起点仍=0,唔该触发)
        s.push_text("你好，我係林先生。想問下包裹幾時到？")
        s.end_input()
        # 等看门狗重连(阈值 1s + 轮询 0.5s)且新连接收到重发
        for _ in range(120):
            if len(instances) >= 2 and [m for m in instances[1].log if m.get("event") == "task_continue"]:
                break
            await asyncio.sleep(0.05)
        s._task.cancel()

    asyncio.run(asyncio.wait_for(run(), timeout=15))

    assert len(instances) >= 2, "看门狗应已重连(新连接)"
    first_texts = [m["text"] for m in instances[0].log if m.get("event") == "task_continue"]
    resent_texts = [m["text"] for m in instances[1].log if m.get("event") == "task_continue"]
    assert first_texts, "首连接应已发出 task_continue(计时起点由此起算)"
    assert resent_texts == first_texts, f"重连后应按序重发已发文本: {resent_texts} != {first_texts}"
