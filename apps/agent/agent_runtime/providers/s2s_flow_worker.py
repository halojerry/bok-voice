"""S2S「步序钉死」流控 worker（feat/s2s-spike，2026-10-03）。

把级联架构 FlowController 的核心语义——「步序状态机 + WA 捕号 + 复述确认」——
搬到 s2s（vendored speech-to-speech serve）Realtime worker 侧，验证业务逻辑在
S2S 形态下可搬。**只 import 复用 `s2s_realtime.py`，不改其本体**（约束同任务）。

—— 机制（读 services/s2s/src/speech_to_speech/ 核实，非假设）————————————
1. **session.update 是深合并**：`api/openai_realtime/handlers/session.py`
   `handle_session_update` → `RuntimeConfig.apply_session_update` →
   `_apply_update`——只把 update `model_fields_set` 里显式设置过的字段递归覆盖
   进现有 session，未带字段保留（我们的投影只发 instructions，其余不动）。
2. **instructions 在 generation time 读取**：`LLM/language_model.py:634-638`
   `instructions = response.instructions if ... else runtime_config.session.instructions`
   ——每轮用户语音转写完成、响应真正生成那一刻才读 session 当前值。因此
   「用户说完→转写完成→生成」这条窗口里 session.update 进来的 instructions
   会被该轮采用（per-response `response.create.instructions` 优先，先例
   `realtime_demo` 的 generate_reply 路径）。
3. **推送时序双保险**（把第 2 点的窗口利用到极致）：
   ①「提前推」——助手回复播完（conversation_item_added，realtime 路径在
     wait_for_playout 之后发）立即推进步序并推「下一步」instructions；下一轮
     用户开口之前就已就位，隐式响应零竞态。
   ②「追捕推」——用户说话期间 ASR **增量转写**（user_input_transcribed
     is_final=False）到达即扫数字 run；步 3 捕到 WA 号立刻把「已捕获号码」
     补进 instructions，赶在「转写完成触发生成」之前送达（豆包 progressive
     模式说话期间持续吐增量，见 STT/doubao_sauc_handler.py `_open_seg/_feed`）。
   推送经 coalescing pusher 串行化：事件回调是同步位，update_instructions 是
   async——latest-wins、仅在渲染文本变化时真发，乱序/重复推送结构性排除。

—— 账本 ————————————————————————————————————————————————————————
每轮（含开场）向 stdout 打一行 `S2S_FLOW {json}`：
`{step, user_text, wa_captured, asst_text 前 40 字}`（任务契约四键）；
另打一行 `S2S_FLOW_FULL`（同 step + 全文）供探针做「逐位复述」「无编造号码」
断言与报告取证（40 字截断会截掉复述）。事件面打点走 `S2S_FLOW_EVT {json}`
（capture / step_advance / instructions_push / readback_missing 等）。

—— 入口 ————————————————————————————————————————————————————————
`run_flow_worker()`：**agent_name="bok-s2s-flow"（独立命名）**，默认端口 8086
（`S2S_FLOW_WORKER_PORT` 覆盖）。独立命名理由：`bok-realtime` 已被
`s2s_realtime.py` 的等价演示档占用（:8085），CP 派发约定里 bok-realtime 走
realtime_demo 的 metadata/usage 形状；本 worker 是「流控产品面」试点，独立
agent_name 让探针（roomConfig 直派）与 CP 派发可并行存在、互不轮转。
出境红线闸复用 realtime_demo 的 `is_demo_safe_object_name`（对象名必须命中测试
前缀族——试点档不许打真客户，硬约束与演示档同款）。

手工冒烟（探针自带房间/dispatch；本模块只需起服）：
    cd apps/agent && PYTHONPATH=... .venv312/bin/python \
      -m agent_runtime.providers.s2s_flow_worker start > /tmp/s2s_flow_worker.log 2>&1 &

env 面：`S2S_FLOW_BASE_URL`（默认 http://127.0.0.1:8795/v1；次级回退
`S2S_REALTIME_BASE_URL`）、`S2S_FLOW_WORKER_PORT`（默认 8086）、
`S2S_FLOW_MAX_S`（会话熔断，默认 300s）、`S2S_REALTIME_API_KEY`（假 key 覆盖）。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from livekit.agents import (
    Agent,
    AgentSession,
    RoomInputOptions,
    RoomOutputOptions,
)

from .s2s_realtime import (
    DEFAULT_API_KEY,
    DEFAULT_BASE_URL as _S2S_REALTIME_DEFAULT_BASE_URL,
    S2SRealtimeModel,
)

# —— 端点/端口缺省 ————————————————————————————————————————————————
# 本试点任务的 serve 在 :8795（s2s_realtime.py 的 :8796 是另一并行任务占位）。
FLOW_DEFAULT_BASE_URL = "http://127.0.0.1:8795/v1"
FLOW_WORKER_PORT_DEFAULT = 8086
DEFAULT_MAX_SECONDS = 300


def _resolve_base_url() -> str:
    """env 优先链：S2S_FLOW_BASE_URL > S2S_REALTIME_BASE_URL > :8795 缺省。"""
    for key in ("S2S_FLOW_BASE_URL", "S2S_REALTIME_BASE_URL"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            return raw
    return FLOW_DEFAULT_BASE_URL


def flow_max_seconds() -> int:
    """会话熔断档（env S2S_FLOW_MAX_S，缺省 300；<=0/非法回缺省，护栏不许关死）。"""
    raw = (os.environ.get("S2S_FLOW_MAX_SECONDS") or os.environ.get("S2S_FLOW_MAX_S") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_SECONDS
    except ValueError:
        return DEFAULT_MAX_SECONDS
    return value if value > 0 else DEFAULT_MAX_SECONDS


# —— 四步粤语话术（步序钉死数据面）——————————————————————————————————
FLOW_STEPS: dict[int, dict[str, str]] = {
    1: {
        "script": "您好，請問係陳大文先生嗎？我係快捷快遞嘅客服，你有個包裹今日到咗我哋倉。",
        "advance": "客戶答確認、質疑或者反問都可以；聽完本輪回應，下一輪就要講第二步正稿。",
    },
    2: {
        "script": "你件嘢係京東買嘅，而家喺我哋中轉倉，聽日可以派到。",
        "advance": "客戶有回應即可；下一輪就要講第三步正稿（收 WhatsApp 號碼）。",
    },
    3: {
        "script": "麻煩您留個WhatsApp號碼，方便我哋發收貨確認俾您。",
        "advance": "客戶講出 8 位或以上號碼之後，你要逐位複述確認；未收到號碼就再禮貌請求一次，繼續留喺第三步。",
    },
    4: {
        "script": "多謝您嘅配合，我哋聽日準時送到，再見。",
        "advance": "冇下一步；講完正稿就完。",
    },
}

BASE_RULES = (
    "你係「快捷快遞」嘅電話客服。"
    "今日呢通電話全程只准講香港粵語口語（廣東話），唔准講普通話，唔准講書面語。"
    "每一輪你只可以講『當前步驟』嘅正稿內容：正稿講咩你就講咩，唔好自己加料，唔好跳到後面嘅步驟，"
    "都唔好重複之前已經講過嘅步驟。"
    "正稿或者上下文入面出現嘅數字，一定要逐位讀出（例如 64321109 要讀成「六四三二一一零九」）。"
    "你絕對唔可以自己編任何號碼、訂單號或者電話號碼——冇喺上下文出現過嘅數字，一個都唔准講。"
    "客戶如果問其他嘢，你就用一句話禮貌回應，然後拉返去當前步驟。"
    "每次回答要簡短，一兩句就夠。"
)

CAPTURE_BLOCK = (
    "【已捕獲號碼={number}（逐位讀：{spoken}）】客戶今輪已經提供咗 WhatsApp 號碼。"
    "你要即刻逐位複述確認，例如：「好嘅，係唔係 {spoken}？」。"
    "只可以複述呢一串數字，一位都唔准改，更加唔准編其他號碼。"
)

_CAPTURE_HINT = (
    "客戶未講號碼嘅話，就照正稿禮貌請佢留 WhatsApp 號碼；"
    "如果佢已經講咗號碼，就逐位複述確認。"
)


def render_instructions(step: int, wa_captured: str | None) -> str:
    """渲染「當前步正稿 + 推進條件 + 禁止事項（+ 已捕獲號碼）」instructions（纯函数）。

    每轮把这份文本作为 session-level instructions 下发；服务端在 generation
    time 读取（见模块头注机制 2）。步 3 捕到号后额外携带捕获块——复述确认的
    内容源唯一（捕获账本），与级联线「号码守卫」的不变量同款：绝不让 LLM 猜。
    """
    spec = FLOW_STEPS[step]
    parts = [
        BASE_RULES,
        f"【當前步驟={step}/4】呢一步嘅正稿（你要講嘅嘢）：「{spec['script']}」",
        f"呢一步嘅推進條件：{spec['advance']}",
    ]
    if step == 3:
        if wa_captured:
            parts.append(CAPTURE_BLOCK.format(number=wa_captured, spoken=digits_to_cn(wa_captured)))
        else:
            parts.append(_CAPTURE_HINT)
    parts.append("記住：淨係講當前步驟嘅內容，講完就停，等客戶回應。")
    return "\n".join(parts)


# —— 数字 run 扫描/规范化（捕号 + 编造号码扫描共用）————————————————————
_CN_DIGIT_MAP: dict[str, str] = {
    "零": "0",
    "〇": "0",
    "洞": "0",
    "一": "1",
    "幺": "1",
    "二": "2",
    "两": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
}
_FULLWIDTH_DIGITS = {chr(0xFF10 + i): str(i) for i in range(10)}
# run 内分隔（不计位、不打断）：空格/全角空格/连字符族/波浪号/间隔号
_RUN_SEPARATORS = frozenset(" \u3000-–—~～·")


def _digit_value(ch: str) -> str | None:
    if "0" <= ch <= "9":
        return ch
    if ch in _FULLWIDTH_DIGITS:
        return _FULLWIDTH_DIGITS[ch]
    return _CN_DIGIT_MAP.get(ch)


def normalize_digit_runs(text: str | None, *, min_len: int = 8) -> list[str]:
    """扫描数字 run，按出现序返回规范化的阿拉伯数字串（≥min_len 位才收）。

    统一扫法：阿拉伯/全角数字、中文数字（零〇洞一幺二两三…九）都算数字位；
    空格/连字符族只作 run 内分隔（不计位、不打断）。run 被其他字符打断。
    用途：①用户转写捕 WA 号（min_len=8）；②助手文本「编造号码」扫描/复述
    检测（探针与 worker 自检换 min_len 复用同一语义）。
    """
    if not text:
        return []
    runs: list[str] = []
    cur: list[str] = []

    def _flush() -> None:
        if len(cur) >= min_len:
            runs.append("".join(cur))
        cur.clear()

    for ch in text:
        value = _digit_value(ch)
        if value is not None:
            cur.append(value)
        elif ch in _RUN_SEPARATORS:
            continue
        else:
            _flush()
    _flush()
    return runs


def first_captured_number(text: str | None) -> str | None:
    """首个 ≥8 位数字 run（阿拉伯或中文数字均可，规范化成 ASCII 串）。"""
    runs = normalize_digit_runs(text, min_len=8)
    return runs[0] if runs else None


_CN_SPOKEN = {
    "0": "零",
    "1": "一",
    "2": "二",
    "3": "三",
    "4": "四",
    "5": "五",
    "6": "六",
    "7": "七",
    "8": "八",
    "9": "九",
}


def digits_to_cn(number: str) -> str:
    return "".join(_CN_SPOKEN.get(c, c) for c in number)


def assistant_reads_back(asst_text: str, captured: str) -> bool:
    """助手文本是否逐位复述了捕获号码（规范化比较：整串子串或某一 run 命中）。"""
    if not captured:
        return False
    if captured in asst_text:
        return True
    return any(captured in run for run in normalize_digit_runs(asst_text, min_len=4))


# —— 状态机 ————————————————————————————————————————————————
@dataclass
class FlowMachine:
    """四步步序状态机（纯数据/纯方法，事件回调里同步调用）。

    推进规则（与 FLOW_STEPS 的 advance 对齐）：
      step1 讲完（开场白播完）→ step2；step2 讲完 → step3；
      step3 讲完且已捕号 → step4（未捕号则留守 step3 再要一次）；
      step4 讲完 → finished（不再推进/推送）。
    「推进」只发生在助手回复播完之后——保证「谁该讲什么」由回复账本决定，
    不靠 LLM 自觉，也不在用户话音中途改步（防跟当前轮竞态）。
    """

    step: int = 1
    wa_captured: str | None = None
    user_text: str = ""
    finished: bool = False

    def observe_user_text(self, text: str, *, final: bool) -> bool:
        """记录用户转写并尝试捕号；返回「捕获首次落定」（= 需要重推 instructions）。"""
        if final:
            self.user_text = str(text or "")
        if self.wa_captured is None:
            number = first_captured_number(text)
            if number:
                self.wa_captured = number
                return True
        return False

    def advance_after_assistant(self) -> bool:
        """助手回复播完后的步序推进；返回是否发生了推进（需推下一步 instructions）。"""
        if self.finished:
            return False
        if self.step in (1, 2):
            self.step += 1
            return True
        if self.step == 3:
            if self.wa_captured:
                self.step = 4
                return True
            return False
        if self.step == 4:
            self.finished = True
            return False
        return False


# —— 观测/账本打点 ————————————————————————————————————————————
def emit_ledger(step: int, user_text: str, wa_captured: str | None, asst_text: str) -> None:
    """任务契约：每轮一行 JSON（含开场轮）。另打 FULL 行供复述/编造断言。"""
    text = str(asst_text or "")
    row = {
        "step": int(step),
        "user_text": str(user_text or ""),
        "wa_captured": wa_captured,
        "asst_text": text[:40],
    }
    print("S2S_FLOW " + json.dumps(row, ensure_ascii=False), flush=True)
    print(
        "S2S_FLOW_FULL " + json.dumps({"step": int(step), "asst_text": text}, ensure_ascii=False),
        flush=True,
    )


def emit_event(kind: str, **fields: Any) -> None:
    payload = {"t": round(time.time(), 3), "kind": kind, **fields}
    print("S2S_FLOW_EVT " + json.dumps(payload, ensure_ascii=False, default=str), flush=True)


# —— instructions 串行推送器 ————————————————————————————————————
class InstructionPusher:
    """latest-wins 串行推送器：同步位 `set()` + 异步 pump 真发。

    事件回调（user_input_transcribed / conversation_item_added）跑在同步位，
    而 RealtimeSession.update_instructions 是 async；本类把两次以上连续 set 折叠
    成「只发最新且未发过的那一份」，天然消除乱序与重复。
    """

    def __init__(self, session_provider: Callable[[], Any]) -> None:
        self._provider = session_provider
        self._desired: str | None = None
        self._sent: str | None = None
        self._task: asyncio.Task[None] | None = None

    def set(self, text: str) -> None:
        if not text or text == self._desired:
            return
        self._desired = text
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._pump())

    @property
    def last_sent(self) -> str | None:
        return self._sent

    async def _pump(self) -> None:
        while True:
            text = self._desired
            session = self._provider()
            if not text or session is None or text == self._sent:
                return
            try:
                await session.update_instructions(text)
            except Exception as exc:  # noqa: BLE001 - 推送失败不炸会话（下轮事件会再试）
                emit_event("instructions_push_failed", error=repr(exc))
                return
            self._sent = text
            emit_event("instructions_push", chars=len(text))


# —— worker 入口 ————————————————————————————————————————————————
def parse_flow_metadata(raw: str) -> dict[str, str]:
    """派单元数据解析（宽容：坏 JSON/缺键落空串；绝不让配置错误炸成无日志）。"""
    try:
        data = json.loads(str(raw or "") or "{}")
    except Exception:  # noqa: BLE001
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "call_id": str(data.get("call_id") or ""),
        "object_id": str(data.get("object_id") or ""),
        "object_name": str(data.get("object_name") or ""),
        "account_id": str(data.get("account_id") or ""),
    }


class _FlowCaptureModel(S2SRealtimeModel):
    """S2SRealtimeModel + 会话捕获槽。

    AgentActivity 在 start 时自建 RealtimeSession（`llm.session(...)`），构造点
    不在我们手里；子类把最近一次创建的会话记下来，worker 才拿得到「本会话」
    调 update_instructions（每轮 instructions 的推送通道）。
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.captured_session: Any = None

    def session(self, *, turn_detection_disabled: bool = False):  # type: ignore[override]
        sess = super().session(turn_detection_disabled=turn_detection_disabled)
        self.captured_session = sess
        return sess


async def entrypoint(ctx: Any) -> None:
    """bok-s2s-flow worker：四步钉死 + WA 捕号 + 复述确认 + 轮账本。"""
    from ..realtime_demo import is_demo_safe_object_name

    meta = parse_flow_metadata(getattr(ctx.job, "metadata", "") or "")
    call_id = meta["call_id"] or getattr(ctx.room, "name", "")

    # 出境红线（与演示档同闸）：对象名必须命中测试前缀族。
    if not is_demo_safe_object_name(meta["object_name"]):
        print(
            f"S2S_FLOW refused (object {meta['object_name']!r} 未命中演示前缀族；"
            "试点档必须绑定 demo-/probe 等假对象)",
            flush=True,
        )
        return

    print(
        f"S2S_FLOW_START room={call_id} object={meta['object_name']!r} "
        f"base_url={_resolve_base_url()}",
        flush=True,
    )

    machine = FlowMachine()
    model = _FlowCaptureModel(
        api_key=os.environ.get("S2S_REALTIME_API_KEY") or DEFAULT_API_KEY,
        model=(os.environ.get("S2S_FLOW_MODEL") or "").strip() or "s2s-flow",
        voice="",  # 空串：s2s 侧沿用 CLI 音色（minimax voice 由 serve 旗标定）
        instructions=render_instructions(1, None),
        base_url=_resolve_base_url(),
    )
    pusher = InstructionPusher(lambda: model.captured_session)
    session = AgentSession(llm=model)
    started_monotonic = time.monotonic()
    fuse_fired = {"done": False}

    # —— 用户转写面：捕号（增量期就抢推）+ 记录本轮用户文本 ————————————
    @session.on("user_input_transcribed")
    def _on_user_transcribed(ev: Any) -> None:
        text = str(getattr(ev, "transcript", "") or "")
        final = bool(getattr(ev, "is_final", False))
        if not text:
            return
        emit_event("user_transcript", final=final, chars=len(text), head=text[:24])
        first_capture = machine.observe_user_text(text, final=final)
        if first_capture:
            emit_event("wa_captured", number=machine.wa_captured, final=final, step=machine.step)
            # 追捕推：在「转写完成→生成」窗口之前把捕获块送进 session instructions。
            pusher.set(render_instructions(machine.step, machine.wa_captured))

    # —— 助手消息面：播完即记账 + 推进步序 + 提前推下一步 ——————————————
    @session.on("conversation_item_added")
    def _on_item_added(ev: Any) -> None:
        item = getattr(ev, "item", None)
        role = str(getattr(item, "role", "") or "")
        if role != "assistant":
            return
        text = str(
            getattr(item, "text_content", None) or getattr(item, "raw_text_content", "") or ""
        ).strip()
        step_at_reply = machine.step
        emit_ledger(step_at_reply, machine.user_text, machine.wa_captured, text)
        emit_event(
            "assistant_turn",
            step=step_at_reply,
            chars=len(text),
            captured=machine.wa_captured,
            finished=machine.finished,
        )
        if step_at_reply == 3 and machine.wa_captured and not assistant_reads_back(
            text, machine.wa_captured
        ):
            # 复述缺失（不炸会话）：留给探针断言与报告取证。
            emit_event("readback_missing", number=machine.wa_captured, asst_head=text[:60])
        advanced = machine.advance_after_assistant()
        if advanced:
            emit_event("step_advance", step=machine.step)
            pusher.set(render_instructions(machine.step, machine.wa_captured))

    async def _duration_fuse() -> None:
        try:
            await asyncio.sleep(flow_max_seconds())
        except asyncio.CancelledError:
            return
        fuse_fired["done"] = True
        elapsed = int(time.monotonic() - started_monotonic)
        print(f"S2S_FLOW fuse fired (max_s={flow_max_seconds()}, elapsed={elapsed}s)", flush=True)
        try:
            await session.aclose()
        except Exception as exc:  # noqa: BLE001
            print(f"S2S_FLOW fuse close failed: {exc!r}", flush=True)

    async def _shutdown() -> None:
        elapsed = int(time.monotonic() - started_monotonic)
        print(f"S2S_FLOW_END elapsed={elapsed}s ledger_done={machine.finished}", flush=True)

    ctx.add_shutdown_callback(_shutdown)

    await ctx.connect()
    await session.start(
        room=ctx.room,
        agent=Agent(instructions=render_instructions(1, None)),
        room_input_options=RoomInputOptions(),
        room_output_options=RoomOutputOptions(audio_enabled=True),
    )
    # 开场白：显式 response.create（不带 per-response instructions——服务端在
    # generation time 读 session instructions=步 1 正稿，机制 2 的直接利用）。
    try:
        session.generate_reply()
        emit_event("opening_triggered", step=1)
    except Exception as exc:  # noqa: BLE001
        emit_event("opening_failed", error=repr(exc))

    fuse_task = asyncio.create_task(_duration_fuse())
    try:
        closed = asyncio.Event()
        session.on("close", lambda _ev: closed.set())
        await closed.wait()
    finally:
        fuse_task.cancel()


def run_flow_worker() -> None:
    """启动 S2S 流控 worker：agent_name=bok-s2s-flow，默认端口 8086。

    独立 agent_name（不并入 bok-realtime）：探针用 roomConfig 直派、CP 侧
    派发约定各自独立，互不轮转；端口 `S2S_FLOW_WORKER_PORT` 可覆盖。
    """
    import sys

    from livekit.agents import WorkerOptions, cli

    from ..worker_guard import worker_port_singleton_guard

    raw_port = (os.environ.get("S2S_FLOW_WORKER_PORT") or "").strip()
    try:
        port = int(raw_port) if raw_port else FLOW_WORKER_PORT_DEFAULT
    except ValueError:
        port = FLOW_WORKER_PORT_DEFAULT
    worker_port_singleton_guard(port, "s2s-flow")
    if len(sys.argv) == 1:
        sys.argv.append("start")
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="bok-s2s-flow",
            port=port,
            num_idle_processes=1,
            load_threshold=float(os.environ.get("BOK_WORKER_LOAD_THRESHOLD", "0.99") or 0.99),
        )
    )


if __name__ == "__main__":
    run_flow_worker()
