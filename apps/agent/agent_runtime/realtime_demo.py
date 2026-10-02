"""云端 Realtime S2S 演示档 worker（2026-09-25 阶段 B，agent_name=bok-realtime :8084）。

整通对话走 Qwen Realtime 端到端语音模型（适配器 agent_runtime.providers.
qwen_realtime.QwenRealtimeModel，契约由适配器侧提供），验证 S2S 形态的延迟与
听感——不接话术漏斗、不接 QA、不接心跳、不接垫话（演示档全旁路，room 事件最小面）。

- 派单：CP /api/token 对 mode=realtime_demo 的通话挂 RoomAgentDispatch
  (agent_name="bok-realtime")，metadata 契约见 control_plane/main.py 演示档分支
  （call_id/object_id/object_name/account_id/model/voice/instructions 七键，
  model/voice/instructions 空串=本 worker 内建缺省）。
- 出境红线（CP 建单侧已闸 root）：对象恒假数据——metadata.object_name 必须命中
  测试前缀族（demo- 等同款正则），否则拒绝接单打点退出，绝不把真实客户档案送
  出境；API key 缺失同样人话报错退出（不静默回落本地栈）。
- 计费护栏：会话时长熔断（BOK_REALTIME_DEMO_MAX_S，默认 300s）到点 farewell
  直念 + CP end_call 收线；usage 有适配器回调就转发打点，没有就只在收线时打点
  时长（REALTIME_DEMO usage ...）。
- 开关：BOK_QWEN_REALTIME=0 拒接一切 job（bok.py 侧 ="1" 才随栈拉起本 worker；
  手工直起 `python -m agent_runtime.realtime_demo` 无需设值）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time

# 演示对象名前缀族（出境红线闸）：与 bok_voice_core.testdata.TEST_OBJECT_NAME_RE
# 同源加 demo- 段。不改共享单源——A 线心跳豁免语义不被演示档扩面。
DEMO_OBJECT_NAME_RE = re.compile(r"^(E2E-|soak\d*-?|并发|LOAD-|边角-|多轮-|probe|demo-)")

# 云端 Realtime 缺省档（dispatch metadata 未带时的内建缺省；根常量与适配器
# 模块级 QWEN_REALTIME_WS_BASE 常量同源演进，缺省端点以适配器为准）。
DEFAULT_REALTIME_MODEL = "qwen3-omni-flash-realtime"
DEFAULT_REALTIME_VOICE = "Cherry"
DEFAULT_MAX_SECONDS = 300

# 演示人设（标准书面中文，简短客服演示口径；dispatch metadata.instructions
# 非空才覆盖）。演示档无话术总览，只保留最基座的应答纪律。话风收紧（真会话
# 冒烟实证：omni-flash 每轮回 7-10s/33 字+，软性「一次只说一两句话」压不住
# S2S 复读偏长）——简洁约束升格为硬规则并置顶，人设与演示说明句殿后。
DEMO_INSTRUCTIONS = (
    "回答必须简短：正常每轮一句话，最多两句，每句不超过 25 字；"
    "客户没追问就不要展开，不要主动补充说明，不要复述客户的问题；"
    "演示场景宁可短不可长。"
    "你是 Bok 智能语音助手的演示专员，正在与客户进行一通友好的演示通话，"
    "简短自然地回应客户的问题，不念稿不复读。"
    "遇到不确定的问题如实说明这是功能演示，并邀请客户继续体验。"
)

DEMO_FAREWELL = "今天的演示就到这里，感谢您的体验，再见。"


def is_demo_safe_object_name(name: str | None) -> bool:
    """假对象闸（纯函数，单测直喂）：命中测试前缀族才准出境。空名一律拒绝。"""
    text = str(name or "").strip()
    return bool(text) and bool(DEMO_OBJECT_NAME_RE.match(text))


def demo_max_seconds() -> int:
    """会话时长熔断档（读 env，单测 monkeypatch 喂）：缺省/非法回落 300s；<=0 视为
    未配置护栏，回落缺省（护栏不许关死——出境红线是硬约束）。"""
    raw = (os.environ.get("BOK_REALTIME_DEMO_MAX_S") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_SECONDS
    except ValueError:
        return DEFAULT_MAX_SECONDS
    return value if value > 0 else DEFAULT_MAX_SECONDS


def parse_demo_metadata(raw: str) -> dict:
    """派单元数据解析（宽容：坏 JSON/缺键落缺省，演示档配置错误绝不炸成无日志）。

    返回七键契约视图：call_id/object_id/object_name/account_id 原样（缺省空串），
    model/voice/instructions 空串补内建缺省。"""
    try:
        data = json.loads(str(raw or "") or "{}")
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "call_id": str(data.get("call_id") or ""),
        "object_id": str(data.get("object_id") or ""),
        "object_name": str(data.get("object_name") or ""),
        "account_id": str(data.get("account_id") or ""),
        "model": str(data.get("model") or "").strip() or DEFAULT_REALTIME_MODEL,
        "voice": str(data.get("voice") or "").strip() or DEFAULT_REALTIME_VOICE,
        "instructions": str(data.get("instructions") or "").strip() or DEMO_INSTRUCTIONS,
    }


def attach_usage_hook(model, on_usage) -> bool:
    """适配器 usage 回调探测（防御式：适配器由并行开发，钩子名未定死）。

    逐个探常见挂点，首个可调用者挂上返回 True；一个都没有返回 False（调用方
    按降级口径只在收线打点时长，不炸会话）。"""
    for attr in ("set_usage_callback", "on_usage", "usage_callback"):
        hook = getattr(model, attr, None)
        if callable(hook):
            try:
                hook(on_usage)
                return True
            except Exception as exc:  # noqa: BLE001 - 钩子挂失败按无回调降级
                print(f"REALTIME_DEMO usage hook {attr} attach failed: {exc!r}", flush=True)
                return False
    return False


def format_session_usage(usage) -> str | None:
    """SessionUsageUpdatedEvent.usage → REALTIME_DEMO usage 打点行主体（纯函数，
    鸭型桩可单测；无可记账目返回 None，调用方跳过打点）。

    payload 形状核实（livekit-agents 1.8.2，.venv312 源）：
    - SessionUsageUpdatedEvent（voice/events.py:407-410）：字段 usage: AgentSessionUsage；
      每次指标采集后重发（voice/agent_activity.py:2154-2156），usage 为会话累计
      快照（voice/agent_session.py:833-835 usage property → flatten() 深拷贝）。
    - AgentSessionUsage（metrics/usage.py:133-134）：dataclass，model_usage: list[ModelUsage]。
    - RealtimeModelMetrics 被收集器折进 LLMModelUsage 桶（metrics/usage.py
      ModelUsageCollector.collect），计数字段=input_tokens / output_tokens /
      session_duration（均为累计值），**无 total_tokens 字段**——total=input+output。
      本函数只取 llm_usage 桶：S2S 演示档无 STT/TTS 侧车，token 账全在 LLM 桶。
    """
    input_tokens = 0
    output_tokens = 0
    duration = 0.0
    seen = False
    for entry in getattr(usage, "model_usage", None) or []:
        if str(getattr(entry, "type", "")) != "llm_usage":
            continue
        seen = True
        input_tokens += int(getattr(entry, "input_tokens", 0) or 0)
        output_tokens += int(getattr(entry, "output_tokens", 0) or 0)
        duration += float(getattr(entry, "session_duration", 0.0) or 0.0)
    if not seen:
        return None
    return (
        f"total_tokens={input_tokens + output_tokens} "
        f"input_tokens={input_tokens} "
        f"output_tokens={output_tokens} "
        f"duration={round(duration, 3)}"
    )


async def entrypoint(ctx) -> None:
    from livekit.agents import Agent, AgentSession, RoomInputOptions, RoomOutputOptions

    from .control_plane import ControlPlaneClient

    meta = parse_demo_metadata(getattr(ctx.job, "metadata", "") or "")
    call_id = meta["call_id"] or getattr(ctx.room, "name", "")
    # kill-switch：BOK_QWEN_REALTIME=0 拒接（bok.py 只在 ="1" 时随栈拉起本 worker，
    # 手工直起默认放行——开关只表达「明确关闭」）。
    if os.environ.get("BOK_QWEN_REALTIME", "1") == "0":
        print("REALTIME_DEMO refused (BOK_QWEN_REALTIME=0)", flush=True)
        return
    # 出境红线：演示对象恒假数据——空名/真实命名一律拒绝接单。
    if not is_demo_safe_object_name(meta["object_name"]):
        print(
            f"REALTIME_DEMO refused (object {meta['object_name']!r} 未命中演示前缀族 "
            f"{DEMO_OBJECT_NAME_RE.pattern!r}；演示档必须绑定 demo- 等假对象)",
            flush=True,
        )
        return
    api_key = (os.environ.get("QWEN_REALTIME_KEY") or "").strip()
    if not api_key:
        print(
            "REALTIME_DEMO refused (QWEN_REALTIME_KEY 未配置——云端 Realtime 需要"
            " API key，请在启动环境设置后重启本 worker)",
            flush=True,
        )
        return

    # 适配器契约：QwenRealtimeModel(api_key=..., model=..., voice=...,
    # instructions=..., turn_detection=...)；WS 端点常量 QWEN_REALTIME_WS_BASE 与
    # 端点覆盖 env QWEN_REALTIME_BASE_URL 在适配器模块内。turn_detection 不传=
    # 适配器内建 server_vad 缺省（DashScope 旧平铺契约收 TurnDetection 对象，
    # 裸字符串会炸官方构造）；usage 走 session 的 session_usage_updated 事件
    # （适配器头注 #7：response.done 换算 RealtimeModelMetrics，会话级收集器累计
    # 后经该事件暴露——livekit-agents 1.8.2 起 metrics 事件官方标注弃用，本 worker
    # 已迁移），挂监听转发。延迟导入——纯函数测试面不依赖适配器在盘。
    from .providers.qwen_realtime import QwenRealtimeModel

    print(
        f"REALTIME_DEMO room={call_id} object={meta['object_name']!r} "
        f"model={meta['model']} voice={meta['voice']}",
        flush=True,
    )

    def _on_usage(usage) -> None:
        print(f"REALTIME_DEMO usage {usage!r}", flush=True)

    realtime_model = QwenRealtimeModel(
        api_key=api_key,
        model=meta["model"],
        voice=meta["voice"],
        instructions=meta["instructions"],
    )
    if attach_usage_hook(realtime_model, _on_usage):
        print("REALTIME_DEMO usage hook attached (model-level)", flush=True)

    cp = ControlPlaneClient(os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000"), call_id=call_id)
    session = AgentSession(llm=realtime_model)
    max_s = demo_max_seconds()
    started_monotonic = time.monotonic()
    print(f"REALTIME_DEMO fuse armed max_s={max_s}", flush=True)

    # 时长熔断（计费护栏）：到点 farewell 直念 → 播完 → CP 收线。任务在
    # session.start 之后才起（先挂后启会抢在会话就绪前 say）。
    fuse_fired = {"done": False}

    async def _duration_fuse() -> None:
        try:
            await asyncio.sleep(max_s)
        except asyncio.CancelledError:
            return
        fuse_fired["done"] = True
        elapsed = int(time.monotonic() - started_monotonic)
        print(f"REALTIME_DEMO fuse fired (max_s={max_s}, elapsed={elapsed}s)", flush=True)
        try:
            handle = session.say(DEMO_FAREWELL)
            await handle.wait_for_playout()
        except Exception as exc:  # noqa: BLE001 - 收线告别失败不阻 end_call
            print(f"REALTIME_DEMO farewell failed: {exc!r}", flush=True)
        try:
            await cp.end_call(call_id, disposition="completed")
            print(f"REALTIME_DEMO ended by fuse ({call_id})", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"REALTIME_DEMO end_call failed: {exc!r}", flush=True)

    def _on_item(ev) -> None:
        # 演示档不落 CP turns（不接漏斗），只打日志面便于现场对线。
        item = getattr(ev, "item", None)
        role = str(getattr(item, "role", "") or "")
        text = str(getattr(item, "text_content", None) or getattr(item, "raw_text_content", "") or "").strip()
        if text:
            print(f"REALTIME_DEMO turn role={role} chars={len(text)}", flush=True)

    session.on("conversation_item_added", _on_item)

    def _on_usage_event(ev) -> None:
        # usage 落账主路（适配器头注 #7）：RealtimeModelMetrics 由会话级收集器
        # 累计成 AgentSessionUsage，经 session_usage_updated 逐次暴露累计值——
        # 打点 input/output/total tokens + 累计时长，供云端账单对账。
        line = format_session_usage(getattr(ev, "usage", None))
        if line:
            print(f"REALTIME_DEMO usage {line}", flush=True)

    session.on("session_usage_updated", _on_usage_event)

    room = ctx.room

    @room.on("participant_connected")
    def _on_join(participant) -> None:
        print(f"REALTIME_DEMO participant_connected {getattr(participant, 'identity', '')}", flush=True)

    @room.on("participant_disconnected")
    def _on_leave(participant) -> None:
        print(f"REALTIME_DEMO participant_disconnected {getattr(participant, 'identity', '')}", flush=True)

    async def _shutdown() -> None:
        elapsed = int(time.monotonic() - started_monotonic)
        # usage 降级口径：适配器无回调时至少把收线时长落日志（对账云端账单用）。
        if not fuse_fired["done"]:
            print(f"REALTIME_DEMO usage seconds={elapsed} source=session_close (no adapter hook)", flush=True)
        try:
            await cp.aclose()
        except Exception:  # noqa: BLE001
            pass

    ctx.add_shutdown_callback(_shutdown)

    # 官方姿势显式 connect（同 B 线 interpret：RoomIO 转写输出要访问
    # local_participant，不先 connect 必抛）。
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


def run_realtime_demo() -> None:
    """启动演示档 worker：agent_name=bok-realtime，端口钉 8084（与 8081-8083 并存）。"""
    import sys

    from livekit.agents import WorkerOptions, cli

    from .worker_guard import worker_port_singleton_guard

    worker_port_singleton_guard(8084, "realtime-demo")
    # livekit-agents 1.7.x 的 cli.run_app 需要显式子命令（start），与 A/B 线同。
    if len(sys.argv) == 1:
        sys.argv.append("start")
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="bok-realtime",
            port=8084,
            # 空闲子进程池框架缺省 prod=min(cpu,4)：每个 worker 4 个空转子进程
            # （本机 3 worker=12 个 idle 吃 ~3.1GB）。演示档几乎不常开，1 个足够
            # 兜冷启动；框架无 env 旋钮，只能 WorkerOptions 传参（2026-10-02 内存瘦身）。
            num_idle_processes=1,
            # load=整机 psutil.cpu_percent（见 agent.py 同款注释）。0.99 同修。
            load_threshold=float(os.environ.get("BOK_WORKER_LOAD_THRESHOLD", "0.99") or 0.99),
        )
    )


if __name__ == "__main__":
    run_realtime_demo()
