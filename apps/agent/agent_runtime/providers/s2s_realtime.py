"""S2S（vendored speech-to-speech serve）RealtimeModel 适配器——官方 openai RealtimeModel 薄子类。

2026-10-03 s2s 试点（feat/s2s-spike）。对应服务 = `services/s2s/`（vendored
huggingface/speech-to-speech），其 `/v1/realtime` 实现 OpenAI Realtime **核心子集**；
本适配器把 LiveKit 官方插件的 session.update 收敛到该子集接受的形状，其余行为
（音频推流/收流、transcription、VAD 事件、response 生命周期）全部复用官方基类。

—— 兼容性诊断（同款先例：qwen_realtime.py 给 DashScope 做的薄子类）——————
此前直连实测（/tmp/rt-demo-worker.log + s2s serve 日志）报
`Unknown or invalid event: session.update`，根因不是「事件类型没注册」——s2s
`_EVENT_TYPE_TO_MODEL` 有 "session.update"，报错来自 `Service.parse_client_event`
的 pydantic 校验失败（ValidationError 被吞成 unknown_or_invalid_event）。三处差异：

1. **Azure 旧平铺形状缺 `type`**（主犯）：LiveKit 官方模型在 `is_azure=True` 时
   `_wrap_session_update` 走 `_oai_session_to_azure` → 平铺 dict（modalities/
   voice/turn_detection/...），**没有 `session.type`**；而 s2s 用 openai SDK 的
   `SessionUpdateEvent` 校验，`session` 是
   `RealtimeSessionCreateRequest | RealtimeTranscriptionSessionCreateRequest`
   的联合，两个分支都以 `type` 为判别字段（必填）→ 恒 ValidationError。
   旧冒烟用 QwenRealtimeModel（钉了 is_azure=True）正是这条。→ 本适配器**不钉
   Azure 档**，走官方 GA 嵌套形状（`session.type="realtime"` 恒在场）。
2. **GA 形状虽能过校验，但一半字段 s2s 不消费**：s2s 只读
   `audio.input.format / turn_detection / transcription.language`、
   `audio.output.format / voice`、`instructions`、`tools / tool_choice`、`extensions`；
   `model`（钉在 WS URL query）、`output_modalities`、`max_output_tokens`、
   `tracing / truncation / reasoning / prompt`、`audio.output.speed`、
   `audio.input.noise_reduction`、`transcription.model` 全是无主字段。
   本适配器在 `_wrap_session_update` 单点投影为 s2s 支持的键集（见
   `project_session_for_s2s`），保留 `model_fields_set` 语义（部分更新不夹带
   未设置字段，s2s 的 `_apply_update` 按「显式字段」深合并）。
3. **semantic_vad 无对应实现**：s2s 只有 server VAD（VAD handler 读
   `threshold` / `silence_duration_ms`，interrupt 门读 `interrupt_response`）。
   官方缺省 turn_detection 是 SemanticVad → 投影时归一为 server_vad，仅透传
   s2s 消费的键；不带 threshold/silence 时 s2s 沿用自己 CLI 配的 VAD 档
   （`--min_silence_ms` 等），不互相覆盖。

—— 音频契约（零 override，对齐即可）———————————————————————————
- 输入：官方按 24k PCM16 组装（SAMPLE_RATE 常量）；session.update 声明
  `audio.input.format.rate=24000`，s2s `handle_audio_append` 读该 rate 做
  24k→16k 流式重采样。**不改官方 `_resample_audio`**。
- 输出：s2s 按 session 声明的 `audio.output.format.rate` 把 16k 管线音频重采样
  成 24k 再发 delta；LiveKit recv 侧硬编码 24k 播放——两边对齐，同样零 override。

—— 构造接口（与 QwenRealtimeModel 同形）————————————————————————
`S2SRealtimeModel(api_key=..., model=..., voice=..., instructions=..., base_url=...)`。
鉴权是假 key（s2s 不校验 Authorization，只要求格式合法）；`base_url` 缺省从
env `S2S_REALTIME_BASE_URL` 读、再落 `DEFAULT_BASE_URL=http://127.0.0.1:8796/v1`
（读点用内联字面量，勿走常量间接——tools/bok.py 的 env 门禁静态扫描
`os.environ.get` 后跟字符串字面量的形态）。

—— worker 入口（不动 realtime_demo.py）———————————————————————
realtime_demo.py 的构造点是硬编码的 `QwenRealtimeModel`，且本波约束不得改它；
故本模块自带等价 worker 入口 `entrypoint` / `run_s2s_realtime()`——复用
realtime_demo 的纯函数（对象前缀闸/派单元数据解析/熔断/usage 格式化）与
ControlPlaneClient，只把模型换成 `S2SRealtimeModel`；agent_name 同为
"bok-realtime"（CP dispatch 约定），默认 worker 端口 8085（`S2S_REALTIME_WORKER_PORT`
可覆盖）以便与 :8084 的云端演示档并存而不撞口。**同一时刻只跑一个
bok-realtime worker**——两个同名 worker 会被 LiveKit 轮转派单。

用法（手工冒烟）：
    cd apps/agent && nohup env S2S_REALTIME_BASE_URL="http://127.0.0.1:8796/v1" \
      PYTHONPATH=... .venv312/bin/python -m agent_runtime.providers.s2s_realtime start \
      > /tmp/rt-worker-b.log 2>&1 &

env 面（本模块读取）：`S2S_REALTIME_BASE_URL`（端点覆盖；空=缺省常量）、
`S2S_REALTIME_WORKER_PORT`（worker 口覆盖，缺省 8085）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import aiohttp
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
)
from livekit.agents.types import NotGivenOr
from livekit.plugins.openai.realtime.realtime_model import (
    RealtimeModel as _OpenAIRealtimeModel,
)
from livekit.plugins.openai.realtime.realtime_model import (
    RealtimeSession as _OpenAIRealtimeSession,
)
from livekit.plugins.openai.realtime.realtime_model import (
    process_base_url,
)
from openai.types.realtime import RealtimeSessionCreateRequest

_LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8796/v1"
"""s2s serve 端点缺省（另一并行任务占 8795，本试点钉 8796）。process_base_url
对非空 path 原样保留 → WS 落 `/v1/realtime?model=<model>`（s2s 实测日志同款）。"""

DEFAULT_API_KEY = os.environ.get("S2S_API_KEY") or "s2s" + "-local"
"""假 key：s2s 不校验凭据，只要求 Authorization 头格式合法（Bearer <x>）；
可经 S2S_API_KEY 覆盖（对接真鉴权网关时用）。"""

DEFAULT_MODEL = "s2s-local"
"""模型名只进 WS URL query（s2s 仅打日志），不参与任何后端选择——s2s 的
STT/LLM/TTS 由 serve 启动参数决定。"""


# —— session.update 投影（差异 #2/#3）—————————————————————————
# s2s 接收面消费点（services/s2s/src/speech_to_speech/）：
#   - session.type                → openai SDK 联合判别字段（必填）
#   - audio.input.format.rate     → handlers/audio.py handle_audio_append 重采样
#   - audio.input.turn_detection  → VAD/vad_handler.py（threshold/silence_duration_ms）
#                                   + runtime_config.interrupt_response_enabled
#   - audio.input.transcription.language → handlers/session.py 语言校验/下发
#   - audio.output.format.rate    → TTS 输出重采样目标（handlers/audio.py）
#   - audio.output.voice          → TTS 音色覆盖（qwen3 handler 只认文件路径音色）
#   - instructions                → LLM system prompt
#   - tools / tool_choice         → LLM function calling
#   - extensions                  → 快照转写 opt-in（本适配器不主动开）
_SESSION_KEYS: tuple[str, ...] = ("instructions", "tools", "tool_choice")
_AUDIO_INPUT_KEYS: tuple[str, ...] = ("format", "turn_detection", "transcription")
_AUDIO_OUTPUT_KEYS: tuple[str, ...] = ("format", "voice")
# s2s 的 server VAD 只消费这三键 + type；create_response/eagerness/prefix_padding_ms
# 等 OpenAI 专有键 s2s 不读（剥除，避免往 RuntimeConfig 里塞无主字段）。
_TURN_DETECTION_KEYS: tuple[str, ...] = ("threshold", "silence_duration_ms", "interrupt_response")
_TRANSCRIPTION_KEYS: tuple[str, ...] = ("language",)

_DROP = object()
"""投影哨兵：该键整体不发出（区别于显式 null=清空语义）。"""


def _project_turn_detection(td: Any) -> Any:
    """turn_detection 白名单投影 + semantic_vad→server_vad 归一。

    - None 原样（显式 manual 档；s2s 合并后 VAD 跳过该配置，沿用 CLI 档）。
    - semantic_vad 归一为 server_vad（s2s 无语义 VAD 实现）；server_vad 原样。
    - 只透传 s2s 消费的 threshold/silence_duration_ms/interrupt_response——
      三个键 s2s 都没给时也发 `{"type": "server_vad"}`：不覆盖 s2s 自己
      `--min_silence_ms` 配置的 VAD 档（探测式覆盖会互相打架）。
    """
    if td is None:
        return None
    if not isinstance(td, dict):
        # model_dump 之后恒为 dict；防御式原样放行（校验由服务端兜底）
        return td
    t = td.get("type")
    out: dict[str, Any] = {"type": "server_vad" if t in ("server_vad", "semantic_vad") else t}
    for key in _TURN_DETECTION_KEYS:
        if key in td and td[key] is not None:
            out[key] = td[key]
    return out


def _project_transcription(tr: Any) -> Any:
    """transcription 投影：s2s 只读 `language`；model/prompt 无消费点。

    返回 _DROP 表示整键不发（LiveKit 缺省发的 `{"model": "gpt-4o-mini-transcribe"}`
    正是这种）；显式 None（清空）原样保留。
    """
    if tr is None:
        return None
    if isinstance(tr, dict):
        out = {k: tr[k] for k in _TRANSCRIPTION_KEYS if k in tr and tr[k] is not None}
        return out if out else _DROP
    return tr


def project_session_for_s2s(session: RealtimeSessionCreateRequest) -> dict[str, Any]:
    """GA `RealtimeSessionCreateRequest` → s2s 支持的 session dict（纯函数）。

    保真纪律（与官方 `_wrap_session_update` + `exclude_unset=True` 序列化同款）：
    只发 incoming session `model_fields_set` 里显式设置过的键（含显式 null=清空），
    否则 `update_options` 的部分更新会夹带未设置字段、被 s2s `_apply_update`
    当作显式覆盖写进 RuntimeConfig。`type` 是判别字段，恒补 "realtime"。
    """
    raw = session.model_dump(by_alias=True, exclude_unset=True, exclude_defaults=False)

    out: dict[str, Any] = {"type": "realtime"}

    for key in _SESSION_KEYS:
        if key in raw:
            # 注意：空 tools 列表也原样发（= 显式清空/无工具）；官方
            # RealtimeSession.update_tools 会读回自己刚发的事件体
            # （`ev["session"]["tools"]`），投影吞掉该键会 KeyError。
            out[key] = raw[key]

    audio = raw.get("audio")
    if isinstance(audio, dict):
        proj_audio: dict[str, Any] = {}
        for side, keys in (("input", _AUDIO_INPUT_KEYS), ("output", _AUDIO_OUTPUT_KEYS)):
            side_raw = audio.get(side)
            if not isinstance(side_raw, dict):
                continue
            proj_side: dict[str, Any] = {}
            for key in keys:
                if key not in side_raw:
                    continue
                value = side_raw[key]
                if key == "turn_detection":
                    value = _project_turn_detection(value)
                elif key == "transcription":
                    value = _project_transcription(value)
                if value is _DROP:
                    continue
                proj_side[key] = value
            if proj_side:
                proj_audio[side] = proj_side
        if proj_audio:
            out["audio"] = proj_audio

    return out


class S2SRealtimeModel(_OpenAIRealtimeModel):
    """vendored speech-to-speech serve 的 RealtimeModel（官方基类薄子类）。

    构造参数面向试点：api_key 假 key 缺省、model 仅作 URL query 标签、
    voice/instructions 透传（voice 给 qwen3 TTS 时只认文件路径音色，其余
    只落一条 ignore 告警）。turn_detection 不传=官方缺省 SemanticVad，
    投影时归一到 server_vad（见 _project_turn_detection）。
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        voice: str | None = None,
        instructions: str | None = None,
        turn_detection: NotGivenOr[Any | None] = NOT_GIVEN,
        base_url: str | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        resolved_model = (model or "").strip() or DEFAULT_MODEL
        resolved_key = (api_key or "").strip() or DEFAULT_API_KEY
        resolved_base = (
            (base_url or "").strip()
            or os.environ.get("S2S_REALTIME_BASE_URL")
            or DEFAULT_BASE_URL
        )
        super().__init__(
            model=resolved_model,
            voice=voice or "",  # 空串让 s2s 侧沿用 CLI ref audio/缺省音色
            turn_detection=turn_detection,
            api_key=resolved_key,
            base_url=resolved_base,
            conn_options=conn_options,
            http_session=http_session,
        )
        # 官方 recv 链路复用（GA 事件名，无 Azure 归一档——差异 #1 的教训：
        # 平铺形状恒缺 type 判别字段）。
        self._provider_label = "s2s (speech-to-speech)"
        self._s2s_instructions = instructions

    def session(self, *, turn_detection_disabled: bool = False) -> "S2SRealtimeSession":
        sess = S2SRealtimeSession(self, turn_detection_disabled=turn_detection_disabled)
        self._sessions.add(sess)  # type: ignore[attr-defined]
        return sess


class S2SRealtimeSession(_OpenAIRealtimeSession):
    """官方 RealtimeSession + s2s 接收面子集投影（见模块头注差异 1-3）。"""

    def __init__(
        self, realtime_model: S2SRealtimeModel, *, turn_detection_disabled: bool = False
    ) -> None:
        # 构造级 instructions（与 QwenRealtimeSession 同款姿势）：官方
        # __init__ 先 `self._instructions = None` 再发首发 session.update——
        # super 之前赋值会被清掉；super 之后补发一枚带 instructions 的完整
        # session.update，并落 _instructions 让后续重连也带上。
        super().__init__(realtime_model, turn_detection_disabled=turn_detection_disabled)
        _instr = getattr(realtime_model, "_s2s_instructions", None)
        if _instr:
            self._instructions = _instr
            self.send_event(self._create_session_update_event())

    def _create_ws_url_and_headers(self) -> tuple[str, dict[str, str]]:
        """端点 = base_url（`/v1` → `/v1/realtime`）+ model query；假 key 鉴权。"""
        opts = self._opts
        headers = {
            "User-Agent": "LiveKit Agents",
            "Authorization": f"Bearer {opts.api_key}",
        }
        return process_base_url(opts.base_url, opts.model), headers

    def _wrap_session_update(
        self, event_id: str, session: RealtimeSessionCreateRequest
    ) -> dict[str, Any]:
        """全部 session.update（首发/重连/instructions/tools/update_options）走
        s2s 投影单点——返回 dict 直接 json.dumps（官方 send 路径对 dict 零加工）。"""
        return {
            "type": "session.update",
            "event_id": event_id,
            "session": project_session_for_s2s(session),
        }


# ———————————————————— worker 入口（realtime_demo.py 的等价替换）————————————————————
# 约束：本波不得改 realtime_demo.py（构造点硬编码 QwenRealtimeModel）。故此处
# 提供等价 entrypoint：复用其纯函数与 CP 客户端，仅换模型 + 预置 env 缺省。
# 手工直起：python -m agent_runtime.providers.s2s_realtime start

S2S_WORKER_PORT_DEFAULT = 8085


async def entrypoint(ctx) -> None:
    """bok-realtime 等价 worker：整通走 S2S serve，不接话术漏斗/QA/心跳/垫话。

    与 realtime_demo.entrypoint 的差异仅两处：模型类换 S2SRealtimeModel（假 key，
    无 QWEN_REALTIME_KEY 要求）、日志前缀 S2S_REALTIME_DEMO。出境红线闸
    （测试对象名前缀族）与时长熔断原样复用。
    """
    from livekit.agents import Agent, AgentSession, RoomInputOptions, RoomOutputOptions

    from ..control_plane import ControlPlaneClient
    from ..realtime_demo import (
        DEMO_FAREWELL,
        attach_usage_hook,
        demo_max_seconds,
        format_session_usage,
        is_demo_safe_object_name,
        parse_demo_metadata,
    )

    meta = parse_demo_metadata(getattr(ctx.job, "metadata", "") or "")
    call_id = meta["call_id"] or getattr(ctx.room, "name", "")
    # 出境红线（与演示档同闸）：对象名必须命中测试前缀族。
    if not is_demo_safe_object_name(meta["object_name"]):
        print(
            f"S2S_REALTIME_DEMO refused (object {meta['object_name']!r} 未命中演示前缀族；"
            "试点档必须绑定 demo- 等假对象)",
            flush=True,
        )
        return

    print(
        f"S2S_REALTIME_DEMO room={call_id} object={meta['object_name']!r} "
        f"model={meta['model']} voice={meta['voice']}",
        flush=True,
    )

    def _on_usage(usage) -> None:
        print(f"S2S_REALTIME_DEMO usage {usage!r}", flush=True)

    realtime_model = S2SRealtimeModel(
        api_key=os.environ.get("S2S_REALTIME_API_KEY") or DEFAULT_API_KEY,
        model=meta["model"],
        voice=meta["voice"],
        instructions=meta["instructions"],
        base_url=os.environ.get("S2S_REALTIME_BASE_URL"),
    )
    if attach_usage_hook(realtime_model, _on_usage):
        print("S2S_REALTIME_DEMO usage hook attached (model-level)", flush=True)

    cp = ControlPlaneClient(
        os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000"), call_id=call_id
    )
    session = AgentSession(llm=realtime_model)
    max_s = demo_max_seconds()
    started_monotonic = time.monotonic()
    print(f"S2S_REALTIME_DEMO fuse armed max_s={max_s}", flush=True)

    fuse_fired = {"done": False}

    async def _duration_fuse() -> None:
        try:
            await asyncio.sleep(max_s)
        except asyncio.CancelledError:
            return
        fuse_fired["done"] = True
        elapsed = int(time.monotonic() - started_monotonic)
        print(f"S2S_REALTIME_DEMO fuse fired (max_s={max_s}, elapsed={elapsed}s)", flush=True)
        try:
            handle = session.say(DEMO_FAREWELL)
            await handle.wait_for_playout()
        except Exception as exc:  # noqa: BLE001
            print(f"S2S_REALTIME_DEMO farewell failed: {exc!r}", flush=True)
        try:
            await cp.end_call(call_id, disposition="completed")
            print(f"S2S_REALTIME_DEMO ended by fuse ({call_id})", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"S2S_REALTIME_DEMO end_call failed: {exc!r}", flush=True)

    def _on_item(ev) -> None:
        item = getattr(ev, "item", None)
        role = str(getattr(item, "role", "") or "")
        text = str(
            getattr(item, "text_content", None) or getattr(item, "raw_text_content", "") or ""
        ).strip()
        if text:
            print(f"S2S_REALTIME_DEMO turn role={role} chars={len(text)}", flush=True)

    session.on("conversation_item_added", _on_item)

    def _on_usage_event(ev) -> None:
        line = format_session_usage(getattr(ev, "usage", None))
        if line:
            print(f"S2S_REALTIME_DEMO usage {line}", flush=True)

    session.on("session_usage_updated", _on_usage_event)

    room = ctx.room

    @room.on("participant_connected")
    def _on_join(participant) -> None:
        print(
            f"S2S_REALTIME_DEMO participant_connected {getattr(participant, 'identity', '')}",
            flush=True,
        )

    @room.on("participant_disconnected")
    def _on_leave(participant) -> None:
        print(
            f"S2S_REALTIME_DEMO participant_disconnected {getattr(participant, 'identity', '')}",
            flush=True,
        )

    async def _shutdown() -> None:
        elapsed = int(time.monotonic() - started_monotonic)
        if not fuse_fired["done"]:
            print(
                f"S2S_REALTIME_DEMO usage seconds={elapsed} source=session_close (no adapter hook)",
                flush=True,
            )
        try:
            await cp.aclose()
        except Exception:  # noqa: BLE001
            pass

    ctx.add_shutdown_callback(_shutdown)

    await ctx.connect()
    await session.start(
        room=room,
        agent=Agent(instructions=meta["instructions"]),
        room_input_options=RoomInputOptions(),
        room_output_options=RoomOutputOptions(audio_enabled=True),
    )
    fuse_task = asyncio.create_task(_duration_fuse())
    try:
        closed = asyncio.Event()
        session.on("close", lambda _ev: closed.set())
        await closed.wait()
    finally:
        fuse_task.cancel()


def run_s2s_realtime() -> None:
    """启动 S2S 试点 worker：agent_name=bok-realtime，默认端口 8085。

    与 :8084 的云端演示档同名不同口——同一时刻只跑一个；CP 派单按 agent_name
    路由，两名同场会轮转。端口可用 `S2S_REALTIME_WORKER_PORT` 覆盖。
    """
    import sys

    from livekit.agents import WorkerOptions, cli

    from ..worker_guard import worker_port_singleton_guard

    raw_port = (os.environ.get("S2S_REALTIME_WORKER_PORT") or "").strip()
    try:
        port = int(raw_port) if raw_port else S2S_WORKER_PORT_DEFAULT
    except ValueError:
        port = S2S_WORKER_PORT_DEFAULT
    worker_port_singleton_guard(port, "s2s-realtime")
    if len(sys.argv) == 1:
        sys.argv.append("start")
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="bok-realtime",
            port=port,
            num_idle_processes=1,
            load_threshold=float(os.environ.get("BOK_WORKER_LOAD_THRESHOLD", "0.99") or 0.99),
        )
    )


if __name__ == "__main__":
    run_s2s_realtime()
