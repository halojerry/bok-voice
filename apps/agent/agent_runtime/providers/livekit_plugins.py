"""LiveKit-compatible provider plugins: OpenAI-compatible LLMs + offline fakes."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass

import httpx
from livekit.agents import APIConnectOptions, llm, stt, tts, vad


@dataclass
class LanguageState:
    """Shared between ASR and TTS so replies use the language the user spoke."""

    lang: str = "zh"

    def update(self, lang: str | None) -> None:
        key = (lang or "").strip().lower()
        if key in {"chinese", "zh", "mandarin"}:
            self.lang = "zh"
        elif key in {"cantonese", "yue"}:
            self.lang = "yue"
        elif key in {"english", "en"}:
            self.lang = "en"


class OpenAICompatLLM(llm.LLM):
    provider = "openai-compat"
    model = ""

    def __init__(self, api_key, model, base_url):
        from openai import AsyncOpenAI

        super().__init__()
        self._model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "256"))

    def chat(self, *, chat_ctx, tools=None, conn_options=None, parallel_tool_calls=None, tool_choice=None, extra_kwargs=None):
        messages = _chat_messages(chat_ctx)
        return _OpenAICompatStream(
            self, chat_ctx, messages, conn_options or APIConnectOptions(), self._max_tokens
        )._real


def _chat_messages(chat_ctx) -> list[dict]:
    messages = []
    for item in getattr(chat_ctx, "items", []):
        if isinstance(item, llm.ChatMessage):
            content = getattr(item, "content", "")
            if isinstance(content, str):
                text = content
            else:
                # content 里文本部分是纯 str（ChatContent = str | ImageContent | AudioContent），
                # 之前用 getattr(c,"text") 会把用户文本全部丢成空串，导致 LLM 听不见用户。
                parts = [
                    c if isinstance(c, str) else (getattr(c, "text", "") or "")
                    for c in content
                ]
                text = "\n".join(parts)
            messages.append({"role": item.role, "content": text})
    if not messages:
        messages = [{"role": "system", "content": "你是 Bok Voice 客服助手。"}]
    return messages


class DeepSeekLLM(OpenAICompatLLM):
    provider = "deepseek"
    model = "deepseek-chat"

    def __init__(self, api_key, model="deepseek-chat", base_url="https://api.deepseek.com/v1"):
        super().__init__(api_key=api_key, model=model, base_url=base_url)


class MlxLlmLLM(OpenAICompatLLM):
    """Local MLX LLM served by our own mlx_lm OpenAI-compatible server.

    Serves the same huihui Qwen3.5 9B weights via `python -m mlx_lm server`
    with `--chat-template-args '{"enable_thinking":false}'`, which avoids the
    LM Studio engine bug that forces thinking mode on Qwen3.5 models. Warm
    replies are ~1s vs 4.5s+ when thinking is stuck on.
    """

    provider = "mlx"
    model = "local"

    def __init__(
        self,
        api_key="mlx",
        model=None,
        base_url="http://host.docker.internal:1235/v1",
    ):
        super().__init__(
            api_key=api_key,
            model=model
            or os.environ.get(
                "MLX_LLM_MODEL",
                "/Users/halo/.lmstudio/models/huihui-ai/Huihui-Qwen3.5-9B-abliterated-mlx-4bit",
            ),
            base_url=base_url
            or os.environ.get("MLX_LLM_BASE_URL", "http://host.docker.internal:1235/v1"),
        )


class OllamaLLM(llm.LLM):
    """Local Ollama via the native /api/chat endpoint.

    Qwen3.x models default to thinking mode: every reply spends the token
    budget on `reasoning` (which OpenAI-compat surfaces in a separate field),
    leaving the actual answer empty and taking minutes per turn. The native
    endpoint accepts `"think": false`, which makes replies fast and content-only.
    """

    provider = "ollama"
    model = "huihui_ai/qwen3.5-abliterated:9b"

    def __init__(self, base_url="http://host.docker.internal:11434/v1", model="huihui_ai/qwen3.5-abliterated:9b", api_key="ollama"):
        super().__init__()
        self._base_url = str(base_url or "").rstrip("/")
        if self._base_url.endswith("/v1"):
            self._base_url = self._base_url[:-3]
        self.model = model
        self._think = os.environ.get("OLLAMA_THINK", "0") == "1"
        self._max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "256"))

    def chat(self, *, chat_ctx, tools=None, conn_options=None, parallel_tool_calls=None, tool_choice=None, extra_kwargs=None):
        messages = _chat_messages(chat_ctx)
        return _OllamaNativeStream(self, chat_ctx, messages, conn_options or APIConnectOptions())._real


class _OllamaNativeStream:
    def __init__(self, plugin, chat_ctx, messages, conn_options):
        class _Stream(llm.LLMStream):
            async def _run(self):
                print("OLLAMA_REQUEST_START", flush=True)
                try:
                    async with httpx.AsyncClient(timeout=180) as client:
                        async with client.stream(
                            "POST",
                            f"{plugin._base_url}/api/chat",
                            json={
                                "model": plugin.model,
                                "messages": messages,
                                "stream": True,
                                "think": plugin._think,
                                "options": {"num_predict": plugin._max_tokens},
                            },
                        ) as resp:
                            resp.raise_for_status()
                            async for line in resp.aiter_lines():
                                if not line.strip():
                                    continue
                                try:
                                    obj = json.loads(line)
                                except json.JSONDecodeError:
                                    continue
                                content = (obj.get("message") or {}).get("content") or ""
                                if content:
                                    print("OLLAMA_REPLY_CHUNK", len(content), flush=True)
                                    self._event_ch.send_nowait(
                                        llm.ChatChunk(
                                            id=str(obj.get("model", "ollama")),
                                            delta=llm.ChoiceDelta(content=content, role="assistant"),
                                        )
                                    )
                except asyncio.CancelledError:
                    # 会话关闭时取消流式响应；不吞异常，让框架正常收尾。
                    raise

        self._real = _Stream(llm=plugin, chat_ctx=chat_ctx, tools=[], conn_options=conn_options)

    def __aiter__(self):
        return self._real


class ScriptedLLM(llm.LLM):
    """Deterministic LLM used by the offline E2E/CI path.

    Inspects the assembled chat context for ``expect_kw`` (a token from the imported
    knowledge/instructions) and returns ``output`` verbatim. This makes the
    "knowledge is injected -> LLM replies per specified script" behaviour testable
    without any cloud API.
    """

    provider = "scripted"
    model = "scripted"

    def __init__(self, expect_kw: str = "", output: str = ""):
        super().__init__()
        self._expect = expect_kw
        self._output = output or "（脚本回复）"

    def chat(self, *, chat_ctx, tools=None, conn_options=None, parallel_tool_calls=None, tool_choice=None, extra_kwargs=None):
        return _ScriptedLLMStream(self, chat_ctx, conn_options or APIConnectOptions())._real


class _ScriptedLLMStream:
    def __init__(self, plugin, chat_ctx, conn_options):
        class _Stream(llm.LLMStream):
            async def _run(self):
                joined = " ".join(
                    str(getattr(x, "text_content", "") or "") for x in getattr(chat_ctx, "items", [])
                )
                hit = (not plugin._expect) or (plugin._expect in joined)
                print("SCRIPTED_LLM_CHECK", f"expect={plugin._expect!r}", f"hit={hit}", flush=True)
                self._event_ch.send_nowait(
                    llm.ChatChunk(
                        id="scripted",
                        delta=llm.ChoiceDelta(content=plugin._output, role="assistant"),
                    )
                )

        self._real = _Stream(llm=plugin, chat_ctx=chat_ctx, tools=[], conn_options=conn_options)

    def __aiter__(self):
        return self._real


class _OpenAICompatStream:
    def __init__(self, plugin, chat_ctx, messages, conn_options, max_tokens=256):
        class _Stream(llm.LLMStream):
            async def _run(self):
                print("OLLAMA_REQUEST_START", flush=True)
                try:
                    stream = await plugin._client.chat.completions.create(
                        model=plugin._model,
                        messages=messages,
                        stream=True,
                        max_tokens=max_tokens,
                    )
                    async for chunk in stream:
                        if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                            print("OLLAMA_REPLY_CHUNK", len(chunk.choices[0].delta.content), flush=True)
                            delta = llm.ChoiceDelta(content=chunk.choices[0].delta.content, role="assistant")
                            self._event_ch.send_nowait(llm.ChatChunk(id=getattr(chunk, "id", "stream"), delta=delta))
                except asyncio.CancelledError:
                    raise

        self._real = _Stream(llm=plugin, chat_ctx=chat_ctx, tools=[], conn_options=conn_options)

    def __aiter__(self):
        return self._real


class _ExprPrependStream(llm.LLMStream):
    """在真实 LLM 流之前先发一个 <expr type="expression" label="..."/> 标记块。"""

    def __init__(self, plugin, inner: "llm.LLMStream", tag: str):
        super().__init__(llm=plugin, chat_ctx=llm.ChatContext(), tools=[], conn_options=APIConnectOptions())
        self._inner = inner
        self._tag = tag

    async def _run(self):
        self._event_ch.send_nowait(
            llm.ChatChunk(id="expr-tag", delta=llm.ChoiceDelta(content=self._tag, role="assistant"))
        )
        async for ev in self._inner:
            self._event_ch.send_nowait(ev)


class ExprAwareLLM(llm.LLM):
    """确定性 mood 通道（Path B 的兜底保障，见 AGENT.md §3）。

    官方 expressive 依赖 LLM 输出里的 <expr type="expression" label="英文mood"/> 标记，
    真实模型未必遵守指令。本包装器在每次 assistant 回复前强制前置一个标记——
    情绪取对话中最后一条 user 消息的文本分类（EmotionProcessor，11 类英文 label）。
    - 转录管线（TranscriptForwarder）会无条件剥离该标记并发布 lk.expression → 前端 mood；
    - 进 TTS 的一路由 agent.py 的 tts_text_transforms 剥掉，保证不被朗读。
    """

    provider = "expr-aware"

    def __init__(self, inner: llm.LLM):
        super().__init__()
        self._inner = inner
        from ..plugins.emotion import EmotionProcessor

        self._emotion = EmotionProcessor()

    def chat(self, *, chat_ctx, tools=None, conn_options=None, parallel_tool_calls=None, tool_choice=None, extra_kwargs=None):
        last_user = ""
        for item in reversed(getattr(chat_ctx, "items", []) or []):
            if getattr(item, "role", None) == "user":
                last_user = getattr(item, "text_content", None) or ""
                break
        tag = f'<expr type="expression" label="{self._emotion.classify(last_user)}"/>'
        inner = self._inner.chat(
            chat_ctx=chat_ctx,
            tools=tools,
            conn_options=conn_options,
            parallel_tool_calls=parallel_tool_calls,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
        )
        return _ExprPrependStream(self, inner, tag)


class FakeLiveKitVAD(vad.VAD):
    model = "fake"
    provider = "fake-vad"

    def __init__(self):
        super().__init__(capabilities=vad.VADCapabilities(update_interval=0.1))

    def stream(self):
        return _FakeVADStream(self)


class _FakeVADStream(vad.VADStream):
    async def _main_task(self):
        import asyncio

        spoke = False
        done = False
        frames = []
        async for item in self._input_ch:
            if done:
                # After one clean turn, swallow everything until the stream is re-used.
                # This stops the old continuous START/END loop that forced the scheduler into
                # a permanently paused state during the client's join/greeting audio.
                continue
            if isinstance(item, self._FlushSentinel):
                if spoke:
                    self._event_ch.send_nowait(vad.VADEvent(type=vad.VADEventType.END_OF_SPEECH, samples_index=0, timestamp=0.0, speech_duration=0.0, silence_duration=0.0, probability=1.0, speaking=False, frames=frames))
                    spoke = False
                    done = True
                    frames = []
                else:
                    done = True
                continue
            frames.append(item)
            if not spoke:
                spoke = True
                print("FAKE_VAD_START", flush=True)
                self._event_ch.send_nowait(vad.VADEvent(type=vad.VADEventType.START_OF_SPEECH, samples_index=0, timestamp=0.0, speech_duration=0.0, silence_duration=0.0, probability=1.0, speaking=True))
                # Keep buffering frames for the configured segment length, then emit one END.
                # This makes the fake VAD fire a single turn per stream instead of endless
                # START/END pairs, which is what the LiveKit turn detector expects.
                await asyncio.sleep(0.5)
                print("FAKE_VAD_END", flush=True)
                self._event_ch.send_nowait(vad.VADEvent(type=vad.VADEventType.END_OF_SPEECH, samples_index=0, timestamp=0.0, speech_duration=0.5, silence_duration=0.0, probability=1.0, speaking=False, frames=frames))
                spoke = False
                done = True
                frames = []


class FakeLiveKitSTT(stt.STT):
    model = "fake"
    provider = "fake-stt"

    def __init__(self, text=None):
        text = text or os.environ.get("FAKE_STT_TEXT", "你好，请介绍一下你们的产品。")
        super().__init__(capabilities=stt.STTCapabilities(streaming=True, interim_results=False, diarization=False, aligned_transcript=False, offline_recognize=False, keyterms=False, chat_context=False))
        self._text = text

    def stream(self, *, language=None, conn_options=None):
        return _FakeSTTStream(self, conn_options or APIConnectOptions(), self._text)

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        return stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT, alternatives=[stt.SpeechData(language="zh", text=self._text)])


class _FakeSTTStream(stt.RecognizeStream):
    def __init__(self, stt_, conn_options, text):
        super().__init__(stt=stt_, conn_options=conn_options)
        self._text = text
        self._emitted = False

    async def _run(self):
        import asyncio

        async for item in self._input_ch:
            if not self._emitted and not isinstance(item, self._FlushSentinel):
                # Buffer a little, then emit a single FINAL once we know the current segment
                # is underway. Debounce so multiple frames don't fan out duplicates.
                await asyncio.sleep(0.2)
                if not self._emitted:
                    self._emitted = True
                    print("FAKE_STT_FINAL", flush=True)
                    self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT, alternatives=[stt.SpeechData(language="zh", text=self._text)]))
                continue
            if isinstance(item, self._FlushSentinel):
                if not self._emitted:
                    self._emitted = True
                    print("FAKE_STT_FINAL", flush=True)
                    self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT, alternatives=[stt.SpeechData(language="zh", text=self._text)]))


class FakeLiveKitTTS(tts.TTS):
    model = "fake"
    provider = "fake-tts"

    def __init__(self, sample_rate=16000):
        # streaming=False so LiveKit wraps it in `tts.StreamAdapter`, which calls our
        # `synthesize()` per sentence. Declaring streaming=True but only implementing the
        # non-streaming `synthesize()` made `tts_node` call the unimplemented `stream()`.
        super().__init__(capabilities=tts.TTSCapabilities(streaming=False, aligned_transcript=True), sample_rate=sample_rate, num_channels=1)

    def synthesize(self, text, *, conn_options=None):
        return _FakeTTSStream(self, text, conn_options or APIConnectOptions())


class _FakeTTSStream(tts.ChunkedStream):
    def __init__(self, tts_, text, conn_options):
        super().__init__(tts=tts_, input_text=text, conn_options=conn_options)

    async def _run(self, output_emitter):
        print("FAKE_TTS_PUSH", flush=True)
        output_emitter.initialize(
            request_id="fake-tts",
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
            stream=False,
        )
        samples = int(self._tts.sample_rate * 0.2)
        pcm = bytes(samples * 2)  # 16-bit mono silence
        output_emitter.push(pcm)
        output_emitter.flush()


class SherpaSenseVoiceSTT(stt.STT):
    """Local SenseVoice ASR via sherpa-onnx (zh/en/ja/ko/yue)."""

    model = "sherpa-sense-voice"
    provider = "sherpa-onnx"

    def __init__(self, model_dir=None, language_state: LanguageState | None = None):
        import os

        import sherpa_onnx

        model_dir = model_dir or os.environ.get("SHERPA_MODEL_DIR", "data/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue")
        self._model_dir = model_dir
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=os.path.join(model_dir, "model.int8.onnx"),
            tokens=os.path.join(model_dir, "tokens.txt"),
            num_threads=2,
            sample_rate=16000,
            feature_dim=80,
            decoding_method="greedy_search",
            use_itn=True,
        )
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=False,
                interim_results=False,
                diarization=False,
                aligned_transcript=False,
                offline_recognize=True,
                keyterms=False,
                chat_context=False,
            )
        )
        # SenseVoice 情绪标签（<|HAPPY|> 等）在 _decode_pcm 里被解析后记录在此，
        # 供上下文装配/情绪分析使用（文本本身保持干净）。
        self.last_emotion: str | None = None
        self.last_language: str = "zh"
        self._language_state = language_state or LanguageState()

    def stream(self, *, language=None, conn_options=None):
        return _SherpaSTTStream(self, conn_options or APIConnectOptions())

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        # Batch contract consumed by `stt.StreamAdapter` at VAD END_OF_SPEECH. Must return a
        # `SpeechEvent`, not a plain string (the old code returned a str, which the adapter
        # then tried to treat as an event and blew up in production).
        text, lang = _SherpaSTTStream(self, conn_options or APIConnectOptions())._recognize_buffer(buffer)
        self._language_state.update(lang)
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id="",
            alternatives=[stt.SpeechData(language=self._language_state.lang, text=text)] if text else [],
        )


class _SherpaSTTStream(stt.RecognizeStream):
    def __init__(self, stt_, conn_options):
        super().__init__(stt=stt_, conn_options=conn_options, sample_rate=16000)
        self._stt_ = stt_
        self._frames = []

    async def _run(self):
        async for item in self._input_ch:
            if isinstance(item, self._FlushSentinel):
                text, lang = self._recognize_frames()
                if text:
                    self._stt_._language_state.update(lang)
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(
                            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                            alternatives=[stt.SpeechData(language=self._stt_._language_state.lang, text=text)],
                        )
                    )
                self._frames = []
            else:
                self._frames.append(item)

    def _recognize_buffer(self, buffer):
        import numpy as np

        data = getattr(buffer, "data", b"")
        return self._decode_pcm(data, getattr(buffer, "sample_rate", 16000))

    def _recognize_frames(self):
        pcm = b"".join(f.data for f in self._frames)
        return self._decode_pcm(pcm, 16000)

    def _decode_pcm(self, pcm: bytes, sample_rate: int):
        import numpy as np

        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if samples.size == 0:
            return "", ""
        s = self._stt_._recognizer.create_stream()
        s.accept_waveform(sample_rate, samples)
        self._stt_._recognizer.decode_stream(s)
        text = s.result.text or ""
        # SenseVoice 富标签：剥离语言/事件标签（<|zh|>、<|nospeech|>…），
        # 但保留情绪标签信息（<|HAPPY|> 等）——不再像旧版那样一刀切洗掉。
        import re

        _EMOTION_TAGS = {
            "HAPPY", "SAD", "ANGRY", "NEUTRAL", "FEARFUL", "DISGUSTED", "SURPRISED", "EMO_UNKNOWN",
        }
        _tag_re = re.compile(r"<\|([^|]*)\|>")

        def _repl(m: "re.Match[str]") -> str:
            tag = m.group(1).strip().upper()
            if tag in _EMOTION_TAGS:
                self._stt_.last_emotion = tag.lower()
            if tag in {"ZH", "EN", "YUE"}:
                self._stt_.last_language = {"ZH": "zh", "EN": "en", "YUE": "yue"}[tag]
            return ""  # 所有标签都不进入显示文本

        clean = _tag_re.sub(_repl, text).strip()
        return clean, self._stt_.last_language


class VolcanoTTS(tts.TTS):
    """Volcengine (火山) small-model WebSocket streaming TTS.

    Uses the official V3 unidirectional streaming protocol:
    ``wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream``.
    If credentials are missing or the upstream call fails, it degrades to a short beep so the
    voice pipeline (VAD -> STT -> LLM -> TTS -> playout) can be validated offline.
    """

    model = "volcano-tts"
    provider = "volcengine"

    def __init__(self, sample_rate=24000):
        super().__init__(
            # The Volcano stream here is exposed through the non-streaming `synthesize()`;
            # LiveKit wraps it with `tts.StreamAdapter` so we don't need a `stream()`.
            capabilities=tts.TTSCapabilities(streaming=False, aligned_transcript=False),
            sample_rate=sample_rate,
            num_channels=1,
        )
        self._sample_rate = sample_rate

    def synthesize(self, text, *, conn_options=None):
        return _VolcanoTTSStream(self, text, conn_options or APIConnectOptions())


class _VolcanoTTSStream(tts.ChunkedStream):
    def __init__(self, tts_, text, conn_options):
        super().__init__(tts=tts_, input_text=text, conn_options=conn_options)
        self._text = text
        self._tts_ = tts_

    async def _run(self, output_emitter):
        output_emitter.initialize(
            request_id="volcano-tts",
            sample_rate=self._tts_.sample_rate,
            num_channels=self._tts_.num_channels,
            mime_type="audio/pcm",
            stream=False,
        )
        import os

        app_id = os.environ.get("VOLC_APP_ID", "")
        token = os.environ.get("VOLC_ACCESS_TOKEN", "")
        if not app_id or not token:
            print("VOLC_TTS_MISSING_CREDENTIALS", flush=True)
            await self._emit_beep(output_emitter)
            return

        try:
            import asyncio
            import json
            import uuid

            import websockets

            from .volc_v3_protocol import EventType, MsgType, MsgTypeFlagBits, Message, receive_message

            resource_id = os.environ.get("VOLC_RESOURCE_ID", "seed-tts-2.0")
            speaker = os.environ.get("VOLC_SPEAKER", "zh_female_vv_uranus_bigtts")
            language = os.environ.get("VOLC_LANGUAGE", "")
            dialect = os.environ.get("VOLC_DIALECT", "")

            req_params: dict = {
                "text": self._text,
                "speaker": speaker,
                "audio_params": {"format": "pcm", "sample_rate": self._tts_.sample_rate},
                "speech_rate": int(os.environ.get("VOLC_SPEECH_RATE", "0")),
                "loudness_rate": int(os.environ.get("VOLC_LOUDNESS_RATE", "0")),
            }
            if language:
                req_params["explicit_language"] = language
            if dialect:
                req_params["explicit_dialect"] = dialect

            uri = "wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream"
            addr = os.environ.get("VOLC_TTS_ENDPOINT", uri)  # 允许测试/降级时覆盖端点
            ws = await websockets.connect(
                addr,
                additional_headers={
                    "X-Api-App-Id": app_id,
                    "X-Api-Access-Key": token,
                    "X-Api-Resource-Id": resource_id,
                    "X-Api-Request-Id": str(uuid.uuid4()),
                },
                open_timeout=15,
                max_size=20_000_000,
            )
            # 单向流式：一帧 FullClientRequest（无事件号 flag），携带 user + req_params。
            body = json.dumps(
                {"user": {"uid": "bok-voice"}, "req_params": req_params},
                ensure_ascii=False,
            ).encode("utf-8")
            frame = Message(type=MsgType.FullClientRequest, flag=MsgTypeFlagBits.NoSeq, payload=body)
            await ws.send(frame.marshal())

            audio_bytes = 0
            while True:
                msg = await asyncio.wait_for(receive_message(ws), timeout=30)
                if msg.type == MsgType.Error:
                    break
                if msg.type == MsgType.AudioOnlyServer or msg.event == EventType.TTSResponse:
                    if msg.payload:
                        audio_bytes += len(msg.payload)
                        output_emitter.push(msg.payload)
                if msg.event in (EventType.SessionFinished, EventType.ConnectionFinished):
                    break
            await ws.close()
            print("VOLC_TTS_AUDIO_BYTES", audio_bytes, flush=True)
        except Exception as exc:
            print("VOLC_TTS_ERROR", repr(exc), flush=True)
            await self._emit_beep(output_emitter)
        finally:
            output_emitter.flush()

    async def _emit_beep(self, output_emitter):
        import math

        sr = self._tts_.sample_rate
        n = int(sr * 0.4)
        pcm = bytearray()
        for i in range(n):
            v = int(12000 * math.sin(2 * math.pi * 440 * i / sr))
            pcm += v.to_bytes(2, "little", signed=True)
        output_emitter.push(bytes(pcm))
        output_emitter.flush()


class Qwen3TTSTTS(tts.TTS):
    """LiveKit TTS adapter for the local Qwen3-TTS sidecar."""

    model = "qwen3-tts"
    provider = "qwen3-tts"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8788",
        voice: str | dict = "",
        language_state: LanguageState | None = None,
        instruct: str = "",
        sample_rate: int = 24000,
    ):
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False, aligned_transcript=False),
            sample_rate=sample_rate,
            num_channels=1,
        )
        self._base_url = base_url.rstrip("/")
        self._voice = voice
        self._language_state = language_state or LanguageState()
        self._instruct = instruct

    def synthesize(self, text, *, conn_options=None):
        return _Qwen3TTSStream(self, text, conn_options or APIConnectOptions())

    def _resolve_voice(self) -> str:
        if isinstance(self._voice, dict):
            return str(self._voice.get(self._language_state.lang) or self._voice.get("zh") or "")
        raw = str(self._voice or "")
        if raw.startswith("{"):
            try:
                mapping = json.loads(raw)
                return str(mapping.get(self._language_state.lang) or mapping.get("zh") or "")
            except Exception:
                return raw
        return raw


class _Qwen3TTSStream(tts.ChunkedStream):
    def __init__(self, tts_, text, conn_options):
        super().__init__(tts=tts_, input_text=text, conn_options=conn_options)
        self._text = text
        self._tts_ = tts_

    async def _run(self, output_emitter):
        try:
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    async with httpx.AsyncClient(timeout=120) as client:
                        resp = await client.post(
                            f"{self._tts_._base_url}/v1/audio/speech",
                            json={
                                "input": self._text,
                                "voice": self._tts_._resolve_voice(),
                                "language": self._tts_._language_state.lang,
                                "instruct": self._tts_._instruct,
                                "sample_rate": self._tts_.sample_rate,
                                "response_format": "pcm",
                                "streaming": True,
                                "chunk_ms": 200,
                            },
                        )
                        resp.raise_for_status()
                        # Stream the PCM body into 200ms frames. The sidecar
                        # streams with non_streaming_mode=False (Qwen3-TTS
                        # Dual-Track fast path), and pushing frames here lets
                        # LiveKit start playback/barge-in handling as soon as
                        # the first frames are available instead of one blob.
                        pcm_total = 0
                        frame_bytes = (
                            self._tts_.sample_rate // 5
                        ) * 2  # 200ms, 16-bit mono
                        buf = bytearray()
                        first_audio = False
                        output_emitter.initialize(
                            request_id="qwen3-tts",
                            sample_rate=self._tts_.sample_rate,
                            num_channels=self._tts_.num_channels,
                            mime_type="audio/pcm",
                            stream=True,
                        )
                        output_emitter.start_segment(segment_id="qwen3-tts")
                        async for data in resp.aiter_bytes():
                            buf.extend(data)
                            # Push the first partial frame as soon as ~40ms is
                            # available instead of waiting for a full 200ms
                            # buffer: the sidecar streams ~83ms model chunks
                            # (QWEN3_TTS_STREAM_INTERVAL=0.1), so this shaves
                            # ~150ms off the time-to-first-audio without
                            # changing steady-state frame size.
                            if not first_audio and len(buf) >= frame_bytes // 5:
                                output_emitter.push(bytes(buf))
                                output_emitter.flush()
                                pcm_total += len(buf)
                                buf.clear()
                                first_audio = True
                            while len(buf) >= frame_bytes:
                                output_emitter.push(bytes(buf[:frame_bytes]))
                                output_emitter.flush()
                                del buf[:frame_bytes]
                                pcm_total += frame_bytes
                        if buf:
                            output_emitter.push(bytes(buf))
                            output_emitter.flush()
                            pcm_total += len(buf)
                        output_emitter.end_segment()
                        print("QWEN3_TTS_BYTES", pcm_total, flush=True)
                        return
                except Exception as exc:  # noqa: BLE001 - retry transient gateway failures
                    last_exc = exc
                    print("QWEN3_TTS_RETRY", attempt + 1, repr(exc), flush=True)
                    await asyncio.sleep(0.5 * (attempt + 1))
            if not asyncio.current_task().cancelling():
                print("QWEN3_TTS_ERROR", repr(last_exc), flush=True)
                await self._emit_beep(output_emitter)
        except asyncio.CancelledError:
            # 会话关闭/打断时不播放故障蜂鸣，直接收尾。
            raise
        except Exception as exc:
            print("QWEN3_TTS_FATAL", repr(exc), flush=True)
            await self._emit_beep(output_emitter)
        finally:
            output_emitter.flush()

    async def _emit_beep(self, output_emitter):
        import math

        sr = self._tts_.sample_rate
        n = int(sr * 0.4)
        pcm = bytearray()
        for i in range(n):
            v = int(12000 * math.sin(2 * math.pi * 440 * i / sr))
            pcm += v.to_bytes(2, "little", signed=True)
        output_emitter.push(bytes(pcm))
        output_emitter.flush()


class Qwen3ASRSTT(stt.STT):
    """LiveKit STT adapter for the local Qwen3-ASR sidecar."""

    model = "qwen3-asr"
    provider = "qwen3-asr"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8787",
        language_state: LanguageState | None = None,
    ):
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=False,
                interim_results=False,
                diarization=False,
                aligned_transcript=False,
                offline_recognize=True,
                keyterms=False,
                chat_context=False,
            )
        )
        self._base_url = base_url.rstrip("/")
        self._language_state = language_state or LanguageState()

    def stream(self, *, language=None, conn_options=None):
        return _Qwen3ASRStream(self, conn_options or APIConnectOptions())

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        text, lang = await _Qwen3ASRStream(
            self, conn_options or APIConnectOptions()
        )._recognize_buffer(buffer)
        if text:
            self._language_state.update(lang)
            return stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                request_id="",
                alternatives=[stt.SpeechData(language=self._language_state.lang, text=text)],
            )
        return stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT, request_id="", alternatives=[])


class _Qwen3ASRStream(stt.RecognizeStream):
    def __init__(self, stt_, conn_options):
        super().__init__(stt=stt_, conn_options=conn_options, sample_rate=16000)
        self._stt_ = stt_
        self._frames = []

    async def _run(self):
        try:
            async for item in self._input_ch:
                if isinstance(item, self._FlushSentinel):
                    try:
                        text, lang = await self._recognize_frames()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        text, lang = "", ""
                    if text:
                        self._stt_._language_state.update(lang)
                        self._event_ch.send_nowait(
                            stt.SpeechEvent(
                                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                                alternatives=[stt.SpeechData(language=self._stt_._language_state.lang, text=text)],
                            )
                        )
                    self._frames = []
                else:
                    self._frames.append(item)
        except asyncio.CancelledError:
            raise
        finally:
            self._frames = []

    async def _recognize_buffer(self, buffer):
        data = getattr(buffer, "data", b"")
        pcm = bytes(data)
        return await self._post_audio(pcm)

    async def _recognize_frames(self):
        pcm = b"".join(getattr(f, "data", b"") for f in self._frames)
        return await self._post_audio(pcm)

    async def _post_audio(self, pcm: bytes):
        if not pcm:
            return "", ""
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    start = await client.post(f"{self._stt_._base_url}/api/start")
                    start.raise_for_status()
                    session_id = start.json()["session_id"]
                    for i in range(0, len(pcm), 3200):
                        await client.post(
                            f"{self._stt_._base_url}/api/chunk",
                            params={"session_id": session_id},
                            content=pcm[i : i + 3200],
                            headers={"Content-Type": "application/octet-stream"},
                        )
                    final = await client.post(
                        f"{self._stt_._base_url}/api/finish",
                        params={"session_id": session_id},
                    )
                    final.raise_for_status()
                    data = final.json()
                    text = str(data.get("text") or "")
                    lang = str(data.get("language") or "")
                    print("QWEN3_ASR_TEXT", repr(text[:120]), lang, flush=True)
                    return text, lang
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - retry transient gateway failures
                last_exc = exc
                print("QWEN3_ASR_RETRY", attempt + 1, repr(exc), flush=True)
                await asyncio.sleep(0.5 * (attempt + 1))
        print("QWEN3_ASR_ERROR", repr(last_exc), flush=True)
        return "", ""
