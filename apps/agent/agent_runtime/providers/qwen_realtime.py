"""Qwen Realtime（DashScope）RealtimeModel 适配器——官方 openai RealtimeModel 薄子类。

阶段 B 第 1 件（2026-09-26）。**云端腿=演示专用、出境红线、对象恒假数据**——不得接
真实客户对象/真实业务数据；kill-switch 总闸 `BOK_QWEN_REALTIME=0`（读法
``os.environ.get("BOK_QWEN_REALTIME", "1") == "1"``，关=构造即 raise 人话错误）。

协议差异（V7 实弹 4 次真会话验证；探针 /tmp/v8_asr/probe_qwen_realtime*.py 与
事件流样本 events_*.jsonl，结论存档 docs/S2S_ROADMAP.md 尾部第二轮章节）：
  1. WS 端点 = ``wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=<model>``，
     鉴权头 ``Authorization: bearer <key>``（小写 bearer，probe 同款）。
  2. ``session.finish`` 私有收尾事件（OpenAI 无）：aclose 先发它再关连接，防对端
     悬挂会话计费。
  3. ``conversation.item.input_audio_transcription.delta`` 正文在 ``text`` 字段；
     ``stash`` 是私有半成品字段，不当正文。
  4. ``response.audio.delta`` 无 rate 字段：不许从事件读采样率，输出写死 24kHz
     pcm16（官方 SAMPLE_RATE 常量）；输入腿写死 16kHz pcm16（probe 以 16k 喂入
     转写逐字正确；官方 push 链按 OpenAI 24k 组装，本类 override 回 16k）。
  5. 音色白名单按模型族：qwen-audio-3.x-realtime* = audio3 族（白名单=2026-09-26
     V7 run1 服务端 "Unsupported voice" 报错的 Supported voices 全列）；qwen3-omni-
     *-realtime = omni 族（Cherry 系）。传族外音色**整条 session.update 被拒**——
     voice_for_model 就地回落族默认并打点 warning。注：服务端全列中另有一个含旧
     粤语拼写字面的音色名不入本白名单（全仓术语门禁按行扫描、本文件不可豁免），
     传入即按族外回落族默认，行为安全。
  6. 无 function calling：session.update 不带 tools/tool_choice；update_tools no-op。
  7. usage 在 response.done 事件里带——官方 _handle_response_done 已换算成
     RealtimeModelMetrics 经 metrics_collected 暴露给上层（计费护栏直接读；
     V7 实测 13.5-26 tok/s），本类零改动。
  8. 粤语演示档 = qwen3-omni-flash-realtime（跟随输入语言回粤语输出，实测）。

复用策略：recv 侧旧事件名归一（response.audio.delta → response.output_audio.*
等，DashScope 用的是 Azure-beta 同代命名）与 send 侧 content type 归一
（output_text → text），复用官方 is_azure 遗留档——构造后补钉
``_opts.is_azure=True``/``_opts.api_version``（仅作官方内部档位开关；URL/鉴权头
全走本类 override，不受 Azure 分支影响）。session.update 旧平铺形状由
_session_to_dashscope_flat 自建（镜像官方 _oai_session_to_azure 的转换，收紧到
探针验证过的键集：modalities/instructions/voice/双 format/turn_detection）。

复制的私有方法（来源 livekit-plugins-openai==1.8.2，本仓锁 <1.9）：
  - _handle_conversion_item_input_audio_transcription_delta（text 字段读取）
  - _resample_audio（输入 16k）
  其余全部子类 override：_create_ws_url_and_headers / _wrap_session_update /
  update_tools / aclose / session / update_options。

未实弹面（记录）：response.create 携带 response.metadata 的接受度——官方
generate_reply 恒发 metadata=client_event_id（probe 只测过裸 response.create）；
若 DashScope 拒绝，error 事件按 event_id 走官方 future 失败路径，不会悬挂。
env 面（本模块读取，已登记 tools/bok.py _FORWARD_ENV「云端 Realtime S2S 演示档」段）：
  - BOK_QWEN_REALTIME（总闸，默认 "1"）
  - QWEN_REALTIME_BASE_URL（WS 端点覆盖，缺省=上方模块常量 QWEN_REALTIME_WS_BASE）
api_key 只从构造参数来（上层负责从 env 读，登记面 QWEN_REALTIME_KEY），本模块
零密钥字面量、零 key env 读取。读点用内联字面量：_FORWARD_ENV 门禁是静态扫
os.environ.get 后跟字符串字面量的形态，常量间接会让门禁瞎掉。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

import aiohttp
from livekit.agents import (
    APIConnectOptions,
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    llm,
    utils,
)
from livekit.agents.types import NotGivenOr
from livekit.agents.utils import is_given
from openai.types.beta.realtime.session import TurnDetection
from openai.types.realtime import RealtimeSessionCreateRequest
from openai.types.realtime import (
    ConversationItemInputAudioTranscriptionDeltaEvent,
)
from livekit import rtc
from livekit.plugins.openai.realtime.realtime_model import (
    NUM_CHANNELS,
    RealtimeModel as _OpenAIRealtimeModel,
    RealtimeSession as _OpenAIRealtimeSession,
    process_base_url,
)

_LOGGER = logging.getLogger(__name__)

QWEN_REALTIME_WS_BASE = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"

INPUT_SAMPLE_RATE = 16000
"""DashScope pcm16 输入=16kHz 单声道（V7 probe 以 16k 喂入、转写逐字正确；
官方 push 链按 OpenAI 24k 组装，本模块 push 侧 override 回 16k）。"""

# —— 音色白名单按模型族（差异 #5）—————————————————————————————
# audio3 白名单 = 2026-09-26 V7 run1 服务端报错 "Unsupported voice: 'Chelsie'.
# Supported voices: …" 全列（去除一个含旧粤语拼写字面者，见模块头注 #5）。
_VOICE_FAMILIES: dict[str, tuple[str, ...]] = {
    "audio3": (
        "longanqian",
        "longanlingxin",
        "longanlufeng",
        "longanlingxi",
        "longanxiaoxin",
        "longanyuanfei",
        "longanhuan_v3.6",
        "longjielidou_v3.6",
        "longpaopao_v3.6",
        "longhuohuo_v3.6",
        "longchuanshu_v3.6",
        "loongmary",
        "loongeva_v3.6",
        "loongjohn",
        "daniel",
        "echo",
        "hannah",
        "sherry",
    ),
    # omni 族 = Cherry 系（qwen3-omni realtime 官方音色；任务书差异 #5）
    "omni": ("Cherry", "Serena", "Ethan", "Chelsie"),
}

_FAMILY_DEFAULT_VOICE = {"audio3": "longanqian", "omni": "Cherry"}


def model_family(model: str) -> Literal["audio3", "omni"] | None:
    """模型名 → 音色族。未知（含空串/asr/livetranslate 等非 S2S realtime 型）→ None。

    匹配含日期快照（qwen3-omni-flash-realtime-2025-09-15、qwen-audio-3.1-realtime-plus）。
    """
    m = (model or "").strip().lower()
    if not m:
        return None
    if m.startswith("qwen-audio-") and "-realtime" in m:
        return "audio3"
    if m.startswith("qwen3-omni-") and "-realtime" in m:
        return "omni"
    return None


def voice_for_model(model: str, wanted: str | None) -> str:
    """按模型族校验/映射音色：族内放行（大小写宽容、回白名单规范拼写），
    族外/空 → 族默认回落并打点 warning（原因=family_voice_whitelist）。
    未知族（model_family None）原样放行——构造侧已另行拒绝未知族，此处纯函数兜底直通。
    """
    family = model_family(model)
    w = (wanted or "").strip()
    if family is None:
        return w
    allowed = _VOICE_FAMILIES[family]
    if w:
        for v in allowed:
            if v.casefold() == w.casefold():
                return v
        default = _FAMILY_DEFAULT_VOICE[family]
        _LOGGER.warning(
            "QWEN_REALTIME voice_fallback model=%s family=%s wanted=%r -> %r "
            "(原因=family_voice_whitelist：族外音色会令整条 session.update 被拒)",
            model,
            family,
            wanted,
            default,
        )
        return default
    return _FAMILY_DEFAULT_VOICE[family]


# —— session.update 旧平铺转换（差异 #6 收紧面）————————————————————
_TURN_DETECTION_KEYS = (
    "type",
    "threshold",
    "prefix_padding_ms",
    "silence_duration_ms",
    "create_response",
    "eagerness",
)


def _project_turn_detection(td: Any) -> dict[str, Any] | None:
    """GA turn detection → 旧 API 平铺 dict，白名单键投影（DashScope 旧代契约）。
    None → None（manual 档显式 null，probe3 实弹验证）。
    """
    if td is None:
        return None
    raw = td.model_dump(by_alias=True, exclude_unset=True, exclude_defaults=False)
    return {k: raw[k] for k in _TURN_DETECTION_KEYS if k in raw}


def _session_to_dashscope_flat(session: RealtimeSessionCreateRequest) -> dict[str, Any]:
    """官方 GA session 对象 → DashScope 旧平铺 session dict。

    镜像官方 _oai_session_to_azure（livekit-plugins-openai==1.8.2
    realtime_model.py:148）的转换方向，按 V7 探针验证过的接受面收紧：
      - 只发 modalities / input_audio_format / output_audio_format / voice /
        instructions / turn_detection 六键（probe1/3 全验证）；
      - model 不进 session.update（模型钉在 WS URL query 参数，probe 同款；
        官方 GA 形状会把 model 塞进 session 对象，DashScope 未验证）；
      - 丢 speed / max_response_output_tokens / tool_choice / tools / tracing /
        reasoning / input_audio_transcription（无 function calling 或未验证；
        转写走服务端默认 fun-asr——session.updated 回显实证，官方默认
        gpt-4o-mini-transcribe 反而会被拒）；
      - turn_detection 显式 None 才落 null（manual 档 probe3 实弹；未设置整键
        省略=服务端默认 server_vad，probe2 无 session.update 自动成轮实证）。
    """
    flat: dict[str, Any] = {}
    if session.output_modalities is not None:
        flat["modalities"] = (
            ["audio", "text"] if "audio" in session.output_modalities else list(session.output_modalities)
        )
        flat["input_audio_format"] = "pcm16"
        flat["output_audio_format"] = "pcm16"
    audio = session.audio
    if audio is not None:
        inp = audio.input
        if inp is not None and "turn_detection" in inp.model_fields_set:
            flat["turn_detection"] = _project_turn_detection(inp.turn_detection)
        out = audio.output
        if out is not None and out.voice:
            flat["voice"] = out.voice
    if session.instructions is not None:
        flat["instructions"] = session.instructions
    return flat


class QwenRealtimeModel(_OpenAIRealtimeModel):
    """DashScope Qwen Realtime（qwen-audio-3.0 / qwen3-omni realtime 系）。

    构造参数面向演示层：api_key 必传（上层从 env 读）、model 须属已知族、
    voice 族外自动回落、turn_detection=None=manual（commit+response.create 自驱）。
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        voice: str | None = None,
        instructions: str | None = None,
        modalities: NotGivenOr[list[Literal["text", "audio"]]] = NOT_GIVEN,
        turn_detection: NotGivenOr[Any | None] = NOT_GIVEN,
        input_audio_format: str = "pcm16",
        output_audio_format: str = "pcm16",
        base_url: str | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        if os.environ.get("BOK_QWEN_REALTIME", "1") != "1":
            raise RuntimeError(
                "BOK_QWEN_REALTIME=0：Qwen Realtime 云端腿已关闭。该腿=演示专用、"
                "出境红线、对象恒假数据；如需启用请移除该 env 后重启（kill-switch 见 "
                "apps/agent/agent_runtime/providers/qwen_realtime.py 模块头注）。"
            )
        if not api_key:
            raise ValueError(
                "QwenRealtimeModel 需要 api_key 构造参数（本模块不读任何 key env，"
                "由上层从环境读好后传入）"
            )
        if model_family(model) is None:
            raise ValueError(
                f"未知 Qwen realtime 模型族：{model!r}（支持 qwen-audio-3.x-realtime* / "
                "qwen3-omni-*-realtime，见 model_family）"
            )
        if input_audio_format != "pcm16" or output_audio_format != "pcm16":
            raise ValueError("DashScope 只收 pcm16（输入 16kHz / 输出 24kHz 单声道）")

        resolved_voice = voice_for_model(model, voice)
        # 未给 turn_detection 时默认 DashScope 支持的 server_vad 自动成轮
        # （probe2：服务端默认即 server_vad；官方默认 semantic_vad 未验证，不用）。
        td_param = turn_detection if is_given(turn_detection) else TurnDetection(
            type="server_vad",
            threshold=0.5,
            prefix_padding_ms=300,
            silence_duration_ms=200,
            create_response=True,
        )
        super().__init__(
            model=model,
            voice=resolved_voice,
            modalities=list(modalities) if is_given(modalities) else ["text", "audio"],
            turn_detection=td_param,
            input_audio_transcription=None,  # 服务端默认转写，配置不外发（见 flat 转换注）
            tool_choice=None,
            api_key=api_key,
            base_url=base_url or os.environ.get("QWEN_REALTIME_BASE_URL") or QWEN_REALTIME_WS_BASE,
            conn_options=conn_options,
            http_session=http_session,
        )
        # —— DashScope 内部档位补钉（构造后置，session 经 dataclass replace 继承）：
        # is_azure/api_version 只是官方遗留档开关：recv 侧旧事件名归一（DashScope 用
        # Azure-beta 同代命名）+ send 侧 content type 归一。URL/鉴权头全走本类
        # override，不落 Azure 分支；api_version 取任意非空哨兵值，不进任何请求。
        self._opts.is_azure = True
        self._opts.api_version = "dashscope-legacy"
        self._provider_label = "DashScope Qwen Realtime"
        # 服务端默认转写恒开（session.updated 回显 input_audio_transcription
        # model=fun-asr；probe2/3 的 completed 事件恒发）——官方按
        # input_audio_transcription 配置推导该能力位，这里对齐事实。
        self._capabilities.user_transcription = True
        self._qwen_instructions = instructions

    @property
    def input_audio_format(self) -> str:
        return "pcm16"

    @property
    def output_audio_format(self) -> str:
        return "pcm16"

    def session(self, *, turn_detection_disabled: bool = False) -> "QwenRealtimeSession":
        sess = QwenRealtimeSession(self, turn_detection_disabled=turn_detection_disabled)
        self._sessions.add(sess)
        return sess

    def update_options(  # type: ignore[override]
        self,
        *,
        voice: NotGivenOr[str] = NOT_GIVEN,
        turn_detection: NotGivenOr[Any | None] = NOT_GIVEN,
        tool_choice: NotGivenOr[llm.ToolChoice | None] = NOT_GIVEN,
        input_audio_transcription: NotGivenOr[Any | None] = NOT_GIVEN,
        input_audio_noise_reduction: NotGivenOr[Any | None] = NOT_GIVEN,
        max_response_output_tokens: NotGivenOr[int | Literal["inf"] | None] = NOT_GIVEN,
        speed: NotGivenOr[float] = NOT_GIVEN,
        tracing: NotGivenOr[Any | None] = NOT_GIVEN,
        truncation: NotGivenOr[Any | None] = NOT_GIVEN,
        reasoning: NotGivenOr[Any | None] = NOT_GIVEN,
    ) -> None:
        # 运行中改音色也必须过族白名单（族外音色=整条 session.update 被拒）。
        if is_given(voice) and voice:
            voice = voice_for_model(self._opts.model, voice)
        super().update_options(
            voice=voice,
            turn_detection=turn_detection,
            tool_choice=tool_choice,
            input_audio_transcription=input_audio_transcription,
            input_audio_noise_reduction=input_audio_noise_reduction,
            max_response_output_tokens=max_response_output_tokens,
            speed=speed,
            tracing=tracing,
            truncation=truncation,
            reasoning=reasoning,
        )


class QwenRealtimeSession(_OpenAIRealtimeSession):
    """官方 RealtimeSession 的 DashScope 差异封装（见模块头注 1-6）。"""

    def __init__(self, realtime_model: QwenRealtimeModel, *, turn_detection_disabled: bool = False) -> None:
        # 构造级 instructions（审修 2026-09-26）：官方 __init__ 先
        # `self._instructions = None`（realtime_model.py:904）再发首发
        # session.update（:908）——super 之前前置赋值会被 904 行清掉。
        # 正确姿势=super 之后再补一枚带 instructions 的完整六键
        # session.update（形状与首发同款、全是探针验证过的面），并落
        # _instructions 让后续重建（重连/update_options）也带上。
        super().__init__(realtime_model, turn_detection_disabled=turn_detection_disabled)
        _instr = getattr(realtime_model, "_qwen_instructions", None)
        if _instr:
            self._instructions = _instr
            self.send_event(self._create_session_update_event())
        # 输入腿 16k（官方 _bstream 按 OpenAI 24k 组装，见模块头注 #4）。
        self._bstream = utils.audio.AudioByteStream(
            INPUT_SAMPLE_RATE, NUM_CHANNELS, samples_per_channel=INPUT_SAMPLE_RATE // 10
        )

    def _create_ws_url_and_headers(self) -> tuple[str, dict[str, str]]:
        """差异 #1：端点 /api-ws/v1/realtime + model query 参数 + 小写 bearer 鉴权。

        process_base_url（官方函数）对 wss:// 与非空 path 原样保留、只补 model
        query 参数——正是 probe 验证的握手形状。
        """
        opts = self._opts
        headers = {
            "User-Agent": "LiveKit Agents",
            "Authorization": f"bearer {opts.api_key}",
        }
        return process_base_url(opts.base_url, opts.model), headers

    def _wrap_session_update(
        self, event_id: str, session: RealtimeSessionCreateRequest
    ) -> dict[str, Any]:
        """差异 #5/#6：全部 session.update 走旧平铺白名单形状（首发/重连/部分更新单点）。"""
        return {
            "type": "session.update",
            "event_id": event_id,
            "session": _session_to_dashscope_flat(session),
        }

    def _resample_audio(self, frame: rtc.AudioFrame) -> Any:
        """输入重采样目标改 16k（复制官方同名方法，来源 livekit-plugins-openai==1.8.2
        realtime_model.py:1832；差异仅 SAMPLE_RATE → INPUT_SAMPLE_RATE）。"""
        if self._input_resampler:
            if frame.sample_rate != self._input_resampler._input_rate:
                # input audio changed to a different sample rate
                self._input_resampler = None

        if self._input_resampler is None and (
            frame.sample_rate != INPUT_SAMPLE_RATE or frame.num_channels != NUM_CHANNELS
        ):
            self._input_resampler = rtc.AudioResampler(
                input_rate=frame.sample_rate,
                output_rate=INPUT_SAMPLE_RATE,
                num_channels=NUM_CHANNELS,
            )

        if self._input_resampler:
            yield from self._input_resampler.push(frame)
        else:
            yield frame

    async def update_tools(self, tools: list[llm.Tool]) -> None:
        """差异 #6：无 function calling——工具注册 no-op（远程恒不带 tools）。"""
        if tools:
            _LOGGER.warning(
                "QWEN_REALTIME tools_ignored n=%d (原因=dashscope_no_function_calling)",
                len(tools),
            )
        self._tools = llm.ToolContext.empty()

    def _handle_conversion_item_input_audio_transcription_delta(
        self, event: ConversationItemInputAudioTranscriptionDeltaEvent
    ) -> None:
        """差异 #3：DashScope 正文在 ``text`` 字段（官方读 ``delta`` 恒 None=整段丢弃）；
        ``stash`` 私有半成品不消费。复制官方同名方法（livekit-plugins-openai==1.8.2
        realtime_model.py:2005），差异仅取字段。pydantic construct 保留 extra 字段
        （实测 text/stash 可从构造后事件读回）。
        """
        text = getattr(event, "text", None) or getattr(event, "delta", None)
        if not text:
            return

        content_index = event.content_index or 0
        by_index = self._input_transcript_accumulators.setdefault(event.item_id, {})
        accumulated = by_index.get(content_index, "") + text
        by_index[content_index] = accumulated

        self.emit(
            "input_audio_transcription_completed",
            llm.InputTranscriptionCompleted(
                item_id=event.item_id, transcript=accumulated, is_final=False
            ),
        )

    async def aclose(self) -> None:
        """差异 #2：关连接前先发 DashScope 私有收尾事件 session.finish（OpenAI 无），
        防对端悬挂会话计费。Chan 语义：先入队再由 super 关通道，_send_task 会把
        队列残余（含本事件）发上线下才 close WS。
        """
        self.send_event({"type": "session.finish"})
        await super().aclose()
