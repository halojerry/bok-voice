"""LiveKit-compatible provider plugins: OpenAI-compatible LLMs + offline fakes."""

from __future__ import annotations

import os

from livekit.agents import APIConnectOptions, llm, stt, tts, vad


class OpenAICompatLLM(llm.LLM):
    provider = "openai-compat"
    model = ""

    def __init__(self, api_key, model, base_url):
        from openai import AsyncOpenAI

        super().__init__()
        self._model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    def chat(self, *, chat_ctx, tools=None, conn_options=None, parallel_tool_calls=None, tool_choice=None, extra_kwargs=None):
        messages = []
        for item in getattr(chat_ctx, "items", []):
            if isinstance(item, llm.ChatMessage):
                content = getattr(item, "content", "")
                if isinstance(content, str):
                    text = content
                else:
                    text = "".join(getattr(c, "text", "") for c in content)
                messages.append({"role": item.role, "content": text})
        if not messages:
            messages = [{"role": "system", "content": "你是 Bok Voice 客服助手。"}]
        return _OpenAICompatStream(self, chat_ctx, messages, conn_options or APIConnectOptions())._real


class DeepSeekLLM(OpenAICompatLLM):
    provider = "deepseek"
    model = "deepseek-chat"

    def __init__(self, api_key, model="deepseek-chat", base_url="https://api.deepseek.com/v1"):
        super().__init__(api_key=api_key, model=model, base_url=base_url)


class OllamaLLM(OpenAICompatLLM):
    provider = "ollama"
    model = "huihui_ai/qwen3.5-abliterated:9b"

    def __init__(self, base_url="http://host.docker.internal:11434/v1", model="huihui_ai/qwen3.5-abliterated:9b", api_key="ollama"):
        super().__init__(api_key=api_key, model=model, base_url=base_url)


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
    def __init__(self, plugin, chat_ctx, messages, conn_options):
        class _Stream(llm.LLMStream):
            async def _run(self):
                print("OLLAMA_REQUEST_START", flush=True)
                stream = await plugin._client.chat.completions.create(
                    model=plugin._model,
                    messages=messages,
                    stream=True,
                )
                async for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                        print("OLLAMA_REPLY_CHUNK", len(chunk.choices[0].delta.content), flush=True)
                        delta = llm.ChoiceDelta(content=chunk.choices[0].delta.content, role="assistant")
                        self._event_ch.send_nowait(llm.ChatChunk(id=getattr(chunk, "id", "stream"), delta=delta))

        self._real = _Stream(llm=plugin, chat_ctx=chat_ctx, tools=[], conn_options=conn_options)

    def __aiter__(self):
        return self._real


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

    def __init__(self, model_dir=None):
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

    def stream(self, *, language=None, conn_options=None):
        return _SherpaSTTStream(self, conn_options or APIConnectOptions())

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        # Batch contract consumed by `stt.StreamAdapter` at VAD END_OF_SPEECH. Must return a
        # `SpeechEvent`, not a plain string (the old code returned a str, which the adapter
        # then tried to treat as an event and blew up in production).
        text = _SherpaSTTStream(self, conn_options or APIConnectOptions())._recognize_buffer(buffer)
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id="",
            alternatives=[stt.SpeechData(language="zh", text=text)] if text else [],
        )


class _SherpaSTTStream(stt.RecognizeStream):
    def __init__(self, stt_, conn_options):
        super().__init__(stt=stt_, conn_options=conn_options, sample_rate=16000)
        self._stt_ = stt_
        self._frames = []

    async def _run(self):
        async for item in self._input_ch:
            if isinstance(item, self._FlushSentinel):
                text = self._recognize_frames()
                if text:
                    self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT, alternatives=[stt.SpeechData(language="zh", text=text)]))
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
            return ""
        s = self._stt_._recognizer.create_stream()
        s.accept_waveform(sample_rate, samples)
        self._stt_._recognizer.decode_stream(s)
        text = s.result.text or ""
        # strip SenseVoice rich tags like <|zh|> <|HAPPY|>
        import re

        return re.sub(r"<\|[^|]*\|>", "", text).strip()


class VolcanoTTS(tts.TTS):
    """Volcengine (火山) small-model WebSocket streaming TTS.

    Uses the official V1 binary protocol:  ``wss://openspeech.bytedance.com/api/v1/tts/ws_binary``.
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
        import os
        import json
        import asyncio
        import struct
        import uuid

        import websockets

        app_id = os.environ.get("VOLC_APP_ID", "")
        token = os.environ.get("VOLC_ACCESS_TOKEN", "")
        output_emitter.initialize(
            request_id="volcano-tts",
            sample_rate=self._tts_.sample_rate,
            num_channels=self._tts_.num_channels,
            mime_type="audio/pcm",
            stream=False,
        )
        if not app_id or not token:
            await self._emit_beep(output_emitter)
            return

        try:
            uri = "wss://openspeech.bytedance.com/api/v1/tts/ws_binary"
            ws = await websockets.connect(
                uri,
                additional_headers={"Authorization": f"Bearer; {token}"},
                open_timeout=10,
            )
            header = bytes([0x11, 0x10, 0x00, 0x00])
            body = json.dumps(
                {
                    "app": {"appid": app_id, "token": token, "cluster": "volcano_tts"},
                    "user": {"uid": "bok-voice"},
                    "audio": {
                        "voice_type": "BV001_streaming",
                        "encoding": "pcm",
                        "speed_ratio": 1.0,
                        "rate": 24000,
                        "volume_ratio": 1.0,
                        "pitch_ratio": 1.0,
                    },
                    "request": {
                        "reqid": str(uuid.uuid4()),
                        "text": self._text,
                        "operation": "submit",
                    },
                },
                ensure_ascii=False,
            ).encode("utf-8")
            await ws.send(header + struct.pack(">I", len(body)) + body)
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=10)
                if msg and isinstance(msg, (bytes, bytearray)):
                    payload = self._strip_binary_header(bytes(msg))
                    if payload[:1] == b"{":
                        break
                    if payload:
                        output_emitter.push(payload)
                else:
                    break
            output_emitter.flush()
            await ws.close()
        except Exception:
            await self._emit_beep(output_emitter)

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

    def _strip_binary_header(self, packet: bytes) -> bytes:
        # V1 audio-only header: 1 byte flags + 3 bytes payload length (big-endian).
        if len(packet) < 4:
            return b""
        payload_len = int.from_bytes(packet[1:4], "big")
        return packet[4 : 4 + payload_len]
