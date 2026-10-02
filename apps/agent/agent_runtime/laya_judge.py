"""Laya 决策旁路客户端（2026-09-26，docs/LAYA-EVAL.md 第一落位：intent judge）。

话术图意图判定的快判旁路：关键词未中的模糊轮，当轮同步打 :8791 决策 sidecar
（0.4B 非自回归决策引擎，实测 p50 ~10ms、概率极锐 0.99+），高置信命中=当轮即
图命中；低置信（below_floor）/不可用=回落既有 9B 后台批量判（fail-open 铁律——
旁路挂了系统必须逐字节走旧路）。

边界纪律（镜像 intent_semantic.py）：
- 纯逻辑 + HTTP 客户端，**不 import flow.py / agent.py**（运行时引擎）；
- 日志不打在这里——返回结构化结果（`LayaPickResult`），agent.py 统一打点；
- httpx 延迟导入（测试假传输路径零 httpx 依赖）。

上下文截断纪律（LAYA-EVAL §2 关键坑）：Laya 上下文硬顶 1024 token 且**静默截尾**
（2000/3000/4500 字三种输入输出逐字节相同）——`build_intent_state` 在客户端侧把
state 压到 ≤600 字（实测 384 tok 内判读正常）且**客户原话置头**（原话放尾部会被
截没）。宁短勿长。

env 读取面（须入 tools/bok.py `_FORWARD_ENV`——D14 教训，由 bok 侧立法）：
- `BOK_LAYA_JUDGE`：意图快判总闸，默认 "1"=随栈启用（="0" 显式关=零调用零日志
  零变化；enabled 闸在最外层，fail-open 铁律不变）；
- `BOK_LAYA_SIDECAR_URL`：sidecar 基址，默认 http://127.0.0.1:8791；
- `BOK_LAYA_QA`：QA 复核车道总闸，默认 "0"=零调用零变化（与意图闸独立立法）；
- `BOK_LAYA_QA_TIMEOUT_MS`：QA 判定预算，默认 300ms；
- `BOK_LAYA_QA_P`：QA 命中概率门，默认 0.85。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# ---- 常量（对账 docs/LAYA-EVAL.md §2/§3）----

DEFAULT_BASE_URL = "http://127.0.0.1:8791"
# 实测暖态 p50 9.7ms——250ms 已是 25 倍余量；超了就当没它（回落 9B，fail-open）。
DEFAULT_DECIDE_TIMEOUT_S = 0.25
# /health 轻量但冷机可能慢一拍；只在 TTL 过期后每 60s 至多一次同步等待。
HEALTH_TIMEOUT_S = 0.5
# 健康缓存 TTL（模块级，按 base_url 分键）：失败也缓存——sidecar 不在时避免
# 每轮白打一次握手。worker 并发多通共享同一份。
HEALTH_TTL_S = 60.0
# 置信门（LAYA-EVAL §3：conf≥0.5 采纳，低置信回落 9B；实测 miss 置信 0.30-0.38、
# 命中最高 0.91，置信度与对错强相关是实测事实）。
DEFAULT_CONFIDENCE_FLOOR = 0.5
# state 客户端预算：600 字（≈384 tok）内判读正常，1200 字开始翻转（LAYA-EVAL §2）。
STATE_MAX_CHARS = 600
# 最近对话轮数（LAYA-EVAL：取最近 2-3 轮即可，宁短勿长）。
DEFAULT_RECENT_TURNS = 3
# 单条历史轮的截断（state 总预算的次要组成）。
HISTORY_LINE_MAX_CHARS = 120


# ---- env 门（test_forward_env 扫描面：键必须登记 bok._FORWARD_ENV 或豁免）----


def laya_judge_enabled() -> bool:
    """总闸（默认开 "1"）：`BOK_LAYA_JUDGE=="0"` 显式关才禁用旁路；未设=启用，
    任一失败路径仍 fail-open 落回 9B 旧路（零命中变化的结构性保证）。"""
    return os.environ.get("BOK_LAYA_JUDGE", "1") == "1"


def laya_base_url() -> str:
    raw = os.environ.get("BOK_LAYA_SIDECAR_URL", "").strip()
    return raw or DEFAULT_BASE_URL


# ---- 健康缓存（模块级，线程/协程安全简单实现）----

# threading.Lock 只护 dict 读写（临界区内零 await）：asyncio 单线程协作式调度下
# 不会长阻塞；跨线程 worker（如有）也安全。竞争窗口最坏=重复探一次 /health，幂等。
_HEALTH_LOCK = threading.Lock()
_HEALTH: dict[str, dict[str, Any]] = {}


def reset_health_cache() -> None:
    """清健康缓存（测试隔离 / 运维手动重探；生产路径不调用）。"""
    with _HEALTH_LOCK:
        _HEALTH.clear()


def _transport_reason(exc: BaseException) -> str:
    """传输异常归因（剥 cause 链找 httpx 异常名，免硬依赖 httpx 的分类）。"""
    cur: BaseException | None = exc
    while cur is not None:
        names = {cls.__name__ for cls in type(cur).__mro__}
        if "TimeoutException" in names or isinstance(cur, TimeoutError):
            return "timeout"
        if "ConnectError" in names or isinstance(cur, ConnectionRefusedError):
            return "connect"
        cur = cur.__cause__ or cur.__context__
    return "error"


class LayaJudgeClient:
    """决策 sidecar 薄客户端：/health 健康缓存 + POST /v1/decide 单问封装。

    fail-open 铁律：任何异常/超时/坏形响应一律返回 None=回落旧路，**绝不抛**。
    传输缝（`_get_health`/`_post_decide`）供测试以子类覆写注入假传输。
    """

    def __init__(
        self,
        base_url: str = "",
        *,
        confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    ) -> None:
        self.base_url = (base_url or laya_base_url()).rstrip("/")
        try:
            self.confidence_floor = min(1.0, max(0.0, float(confidence_floor)))
        except (TypeError, ValueError):
            self.confidence_floor = DEFAULT_CONFIDENCE_FLOOR

    # ---- 传输缝（测试覆写点） ----

    async def _get_health(self) -> tuple[int, Any]:
        import httpx  # 延迟导入：假传输测试路径零 httpx 依赖

        async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT_S) as http:
            resp = await http.get(f"{self.base_url}/health")
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, None

    async def _post_decide(self, payload: dict, timeout_s: float) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=timeout_s) as http:
            resp = await http.post(f"{self.base_url}/v1/decide", json=payload)
            resp.raise_for_status()
            return resp.json()

    # ---- 健康缓存 ----

    async def ensure_health(self) -> tuple[bool, str]:
        """首次调用前 GET /health；TTL 内读缓存。返回 (ok, reason)。

        reason ∈ ""|timeout|connect|error|http_status|model_missing（/health
        ok=false = 模型缺位）。失败同样入缓存——挂掉的 sidecar 不值得每轮重探。
        """
        now = time.monotonic()
        with _HEALTH_LOCK:
            entry = _HEALTH.get(self.base_url)
            if entry is not None and now - entry["checked_at"] < HEALTH_TTL_S:
                return bool(entry["ok"]), str(entry["reason"])
        ok, reason = await self._probe_health()
        with _HEALTH_LOCK:
            _HEALTH[self.base_url] = {
                "checked_at": time.monotonic(),
                "ok": ok,
                "reason": reason,
            }
        return ok, reason

    async def _probe_health(self) -> tuple[bool, str]:
        try:
            status, data = await self._get_health()
        except Exception as exc:  # noqa: BLE001 - 传输异常归因后一律 fail-open
            return False, _transport_reason(exc)
        if status != 200:
            return False, "http_status"
        if not isinstance(data, dict) or data.get("ok") is not True:
            return False, "model_missing"
        return True, ""

    # ---- 单问判定 ----

    async def decide_choice(
        self,
        state: str,
        instructions: str,
        criteria: Sequence[str],
        *,
        timeout_s: float = DEFAULT_DECIDE_TIMEOUT_S,
        qid: str = "intent",
        skip_gate: bool = False,
    ) -> dict | None:
        """单问封装：choice 面=`criteria`（缺 NONE 自动补）。任何异常/超时/坏形 → None。

        返回 dict：{"choice","confidence","below_floor","probabilities",
        "state_truncated"}——below_floor 透传 sidecar 旗；缺席时按 confidence_floor
        本地补算。choice 不在候选面（含 NONE）=坏形 → None（宁可回落，绝不乱命中）。

        - `qid`：问题键（意图车道默认 "intent"；姊妹车道用自带键如 "qa_match"）；
        - `skip_gate`：姊妹车道自带 env 总闸时置 True 跳过 BOK_LAYA_JUDGE 复检
          （闸责任归调用方单点化，两把闸独立立法）——健康缓存/坏形防御不豁免。
        """
        if not skip_gate and not laya_judge_enabled():
            return None
        face = [str(c or "").strip() for c in (criteria or []) if str(c or "").strip()]
        if not face:
            return None
        if "NONE" not in face:
            face.append("NONE")
        ok, _reason = await self.ensure_health()
        if not ok:
            return None
        payload = {
            "state": str(state or ""),
            "questions": {
                qid: {
                    "type": "choice",
                    "instructions": str(instructions or ""),
                    "criteria": face,
                }
            },
            "confidence_floor": self.confidence_floor,
        }
        try:
            data = await self._post_decide(payload, timeout_s)
        except Exception:  # noqa: BLE001 - 超时/异常/坏形一律 None=回落
            return None
        answers = data.get("answers") if isinstance(data, dict) else None
        ans = answers.get(qid) if isinstance(answers, dict) else None
        if not isinstance(ans, dict):
            return None
        choice = str(ans.get("choice") or "").strip()
        if not choice or choice not in face:
            return None
        # confidence 必须在场且是数值（bool 是 int 子类要单列）：缺席=坏形 → None
        # （保守回落 9B，绝不当 0 分静默降级——0 分与 below_floor 同效但归因含糊）。
        raw_conf = ans.get("confidence")
        if not isinstance(raw_conf, (int, float)) or isinstance(raw_conf, bool):
            return None
        conf = float(raw_conf)
        below = ans.get("below_floor")
        below = bool(below) if isinstance(below, bool) else (conf < self.confidence_floor)
        probs = ans.get("probabilities")
        truncated = data.get("state_truncated") if isinstance(data, dict) else None
        return {
            "choice": choice,
            "confidence": conf,
            "below_floor": below,
            "probabilities": probs if isinstance(probs, dict) else {},
            "state_truncated": bool(truncated),
        }


# ---- 模块级共享客户端与单问便捷口 ----

_CLIENT_LOCK = threading.Lock()
_DEFAULT_CLIENT: LayaJudgeClient | None = None


def default_client() -> LayaJudgeClient:
    global _DEFAULT_CLIENT
    with _CLIENT_LOCK:
        if _DEFAULT_CLIENT is None:
            _DEFAULT_CLIENT = LayaJudgeClient()
        return _DEFAULT_CLIENT


async def decide_choice(
    state: str,
    instructions: str,
    criteria: Sequence[str],
    *,
    timeout_s: float = DEFAULT_DECIDE_TIMEOUT_S,
) -> dict | None:
    """模块级单问封装（共享默认客户端）；契约同 `LayaJudgeClient.decide_choice`。"""
    return await default_client().decide_choice(
        state, instructions, criteria, timeout_s=timeout_s
    )


async def ensure_health() -> tuple[bool, str]:
    """模块级健康口（agent 先问健康再决定打点 unavailable 归因）。"""
    return await default_client().ensure_health()


# ---- state 组装（纯函数：≤800 token 客户端纪律）----


def recent_turn_pairs(
    items: Iterable[Any],
    *,
    exclude: Any = None,
    recent_turns: int = DEFAULT_RECENT_TURNS,
) -> list[tuple[str, str]]:
    """从 ChatContext.items 抽最近 N 轮 (role, text)，role ∈ customer|agent。

    纯函数可测：只认 user/assistant 两种 role（框架的 [流程状态] system 标记、
    function call 条目一律跳过），空文本跳过，`exclude`（本轮 new_message 本体）
    按对象身份剔除——原话由调用方单独置头，不进 history。
    """
    out: list[tuple[str, str]] = []
    for item in reversed(list(items or [])):
        if exclude is not None and item is exclude:
            continue
        role = getattr(item, "role", None)
        if role not in ("user", "assistant"):
            continue
        text = str(
            getattr(item, "text_content", None)
            or getattr(item, "raw_text_content", "")
            or ""
        ).strip()
        if not text:
            continue
        out.append(("customer" if role == "user" else "agent", text))
        if len(out) >= max(0, int(recent_turns)):
            break
    out.reverse()
    return out


def build_intent_state(
    *,
    user_text: str,
    history: Sequence[tuple[str, str]] = (),
    step_1based: int = 0,
    goal: str = "",
    recent_turns: int = DEFAULT_RECENT_TURNS,
    max_chars: int = STATE_MAX_CHARS,
) -> str:
    """组 Laya state（纯函数）：**客户原话置头** → 当前步 → 最近轮（新→旧）。

    截断纪律（LAYA-EVAL 截尾坑）：超预算从尾部丢——所以重要度递减排：原话
    （判定信号本体）在头、步骤语境次之、历史垫底。原话自身超半预算才截
    （ASR 轮通常一句短话），历史逐条 budget 不够就停（宁短勿长）。
    """
    utt = " ".join(str(user_text or "").split())
    if len(utt) > max_chars // 2:
        utt = utt[: max_chars // 2]
    lines = [f"客户原话：{utt}"]
    if step_1based or goal:
        lines.append(f"当前第{int(step_1based or 0)}步：{str(goal or '').strip() or '(无)'}")
    role_names = {"customer": "客户", "user": "客户", "agent": "AI", "assistant": "AI"}
    budget = max_chars - sum(len(line) + 1 for line in lines)
    for role, text in reversed(list(history or [])[-max(0, int(recent_turns)) :]):
        line = f"{role_names.get(str(role), str(role))}：{' '.join(str(text).split())[:HISTORY_LINE_MAX_CHARS]}"
        if len(line) + 1 > budget:
            break
        lines.append(line)
        budget -= len(line) + 1
    return "\n".join(lines)


def build_intent_instructions(intents: Sequence[Any]) -> str:
    """choice 问题的 instructions（纯函数）：候选语义表进 instructions，criteria 只放 id。

    宽口径候选（纯关键词意图也可入面）：判据空时用关键词清单顶上，都没有就用
    名称。标准书面中文（prompt 语言纯度铁律——该文本无条件进判定请求）。
    """
    lines = [
        "根据客户刚说的话，从候选意图中选出最贴合的一个；只按意图判据判断，不要凭名称猜测；",
        "全部不贴合时选 NONE。候选意图：",
    ]
    for it in intents:
        intent_id = str(getattr(it, "id", "") or "")
        label = str(getattr(it, "label", "") or "")
        judge = str(getattr(it, "judge_prompt", "") or "").strip()
        if not judge:
            kws = [str(k).strip() for k in (getattr(it, "keywords", None) or []) if str(k or "").strip()]
            judge = "、".join(kws)
        lines.append(f"- {intent_id}｜名称 {label or '(无)'}｜判据 {judge or '(无)'}")
    return "\n".join(lines)


# ---- 每轮快判入口（纯装配：agent 闭包薄壳调用；单测直接打这里）----


@dataclass
class LayaPickResult:
    """单轮快判结果：verdict ∈ hit|abstain|off|unavailable|silent。

    hit=高置信命中（answer.choice=意图 id，消费方走 pick_graph_action(judge_hit=)）；
    abstain=below_floor / NONE（回落 9B 后台判）；off=总闸关；unavailable=健康
    失败/判定失败（reason 归因，回落 9B）；silent=无可判定候选（零调用，静默）。
    """

    verdict: str = "off"
    answer: dict | None = None
    reason: str = ""
    ms: int = 0
    intents: int = 0
    choices: list[str] = field(default_factory=list)


async def pick_intent_laya(
    *,
    graph: Any,
    step_1based: int,
    fired: set,
    user_text: str,
    history: Sequence[tuple[str, str]] = (),
    goal: str = "",
    play_allowed: bool = True,
    client: LayaJudgeClient | None = None,
    enabled: bool | None = None,
) -> LayaPickResult:
    """关键词未中轮的当轮同步快判（离线可测的模块级入口）。

    候选=**宽口径** eligible（enabled + 步 scope + 至少一条可触发绑定；纯关键词
    意图也纳入 choice 面）——复用 intent_semantic.eligible_semantic_intents 同一份
    四闸实现。返回 hit 之外一律让消费方落回旧路（fail-open）。
    """
    if enabled is None:
        enabled = laya_judge_enabled()
    if not enabled:
        return LayaPickResult(verdict="off")
    from .intent_semantic import eligible_semantic_intents  # 延迟导入：避开环

    candidates = eligible_semantic_intents(
        graph, step_1based=step_1based, fired=fired, play_allowed=play_allowed
    )
    if not candidates:
        return LayaPickResult(verdict="silent")
    cli = client if client is not None else default_client()
    choices = [i.id for i in candidates]
    t0 = time.monotonic()
    ok, reason = await cli.ensure_health()
    ans = None
    if ok:
        ans = await cli.decide_choice(
            build_intent_state(
                user_text=user_text,
                history=history,
                step_1based=step_1based,
                goal=goal,
            ),
            build_intent_instructions(candidates),
            choices,
        )
    ms = int((time.monotonic() - t0) * 1000)
    if ans is None:
        return LayaPickResult(
            verdict="unavailable",
            reason=(reason if not ok else "decide_failed"),
            ms=ms,
            intents=len(choices),
            choices=choices,
        )
    choice = str(ans.get("choice") or "")
    if ans.get("below_floor") or choice == "NONE" or choice not in choices:
        return LayaPickResult(
            verdict="abstain", answer=ans, ms=ms, intents=len(choices), choices=choices
        )
    return LayaPickResult(
        verdict="hit", answer=ans, ms=ms, intents=len(choices), choices=choices
    )


# ---- QA 验证车道（2026-09-26，第二落位：qa fastpath 词面未中轮复核）----
#
# 实测纪律（多选一面概率锐利 0.99+、二元 match/no_match 面偏糊）：**只用多选一
# 形状**——召回 top-K 词条做候选面，一次 decide 多选一。短 state（成本驱动是
# state 长度）：客户原话置头 + 当前步 goal 一行，**不带会话史**。车道闸
# `BOK_LAYA_QA` 默认 "0"=零调用零变化；fail-open 铁律同意图车道：off/空候选/
# 健康失败/判定失败/坏形一律回落旧路（QA_SEM/LLM），绝不抛。

# 召回面参数：K 与地板的**单点在 qa_gate（qa_recall_k()/qa_recall_floor()，
# env BOK_QA_RECALL_K/BOK_QA_RECALL_FLOOR）**——本模块只保留候选面容量上限
# （+NONE 自动补=9 顶格）。2026-09-25 审计：废除本处常量副本防双源漂移。
QA_MAX_CANDIDATES = 8

# 【零样本标定结论（2026-09-25 实测,scripts/qa_laya_calibrate.py 100 正/30 负
# golden 全量）——勿在无新证据时开启本车道】：
#   - winner==expect 62%（面缩到 4+NONE 仍 63%——缩面救不了）；
#   - 概率面平坦：p_win p50=0.53、confidence p50=0.20、below_floor 89%
#     （confidence_floor=0.5 在九选一面上结构性全弃权）；
#   - 30 条邻近/离题负样本 14 条被选非 NONE（最高 p=0.55）——无达标阈值档
#     （neg_false=0 与 accuracy≥0.97 不可同时满足）。
# 结论=零样本 multilingual 档在「多条释义词条 discrimination」任务上不可用
# （与官方「zero-shot 接近随机,微调才是预期用法」一致;LAYA-EVAL 的 0.99+ 锐利
# 面是 3-4 选项意图判定,勿外推到词条匹配）。开启路径=微调（官方 RLCD 配方,
# 弱标签=9B judge 历史输出+fastpath 命中账本,见 LAYA-EVAL §微调）——微调后
# 必须重跑 qa_laya_calibrate.py 出达标阈值档才准翻 BOK_LAYA_QA。
# 短 state 预算（LAYA-EVAL §2：超 1024 token 静默截尾，原话放尾部会被截没）。
QA_STATE_MAX_CHARS = 300
QA_GOAL_MAX_CHARS = 60
# 选项描述（question_text）截断。
QA_OPTION_TEXT_MAX = 40

# BOK_LAYA_QA_TIMEOUT_MS 默认 300ms（多选一单问实测 15-40ms，300ms≈7 倍余量）。
DEFAULT_QA_TIMEOUT_S = 0.3
# BOK_LAYA_QA_P 命中概率门：hit/none 都要 p 过门才算数（命中乱播与拒绝翻案
# 都比落旧路贵——门下一律 fail-open 落 QA_SEM）。
DEFAULT_QA_HIT_P = 0.85


def laya_qa_enabled() -> bool:
    """QA 复核车道总闸（默认关）：`BOK_LAYA_QA=="1"` 才启用——零变化的结构性保证。"""
    return os.environ.get("BOK_LAYA_QA", "0") == "1"


def qa_timeout_s() -> float:
    """判定预算（毫秒→秒）；缺省/坏值/非正数/inf/nan 回默认 0.3s。"""
    raw = str(os.environ.get("BOK_LAYA_QA_TIMEOUT_MS", "") or "").strip()
    try:
        ms = float(raw)
    except ValueError:
        return DEFAULT_QA_TIMEOUT_S
    if not (0.0 < ms < 1e9):
        return DEFAULT_QA_TIMEOUT_S
    return ms / 1000.0


def qa_hit_floor_p() -> float:
    """命中概率门；缺省/坏值/越界（≤0 或 >1，含 nan）回默认 0.85。"""
    raw = str(os.environ.get("BOK_LAYA_QA_P", "") or "").strip()
    try:
        p = float(raw)
    except ValueError:
        return DEFAULT_QA_HIT_P
    if not (0.0 < p <= 1.0):
        return DEFAULT_QA_HIT_P
    return p


def build_qa_state(user_text: str, step_goal: str) -> list[str]:
    """短 state（纯函数）：客户原话置头 + 当前步 goal 一行，**不带会话史**。

    QA 复核语义上只需「客户问什么 + 现在聊到哪步」——历史只会添乱加钱
    （实测：成本驱动是 state 长度）。返回行表（decide 前由调用方 join）。
    """
    utt = " ".join(str(user_text or "").split())
    if len(utt) > QA_STATE_MAX_CHARS // 2:
        utt = utt[: QA_STATE_MAX_CHARS // 2]
    lines = [f"客户原话：{utt}"]
    goal = " ".join(str(step_goal or "").split())
    if goal:
        if len(goal) > QA_GOAL_MAX_CHARS:
            goal = goal[:QA_GOAL_MAX_CHARS]
        lines.append(f"当前流程：{goal}")
    return lines


def build_qa_instructions(entries: Sequence[dict]) -> str:
    """choice 问题的 instructions（纯函数）：候选词条问法表进 instructions，criteria 只放 id。

    语义=判定客户问句与哪条词条同义（快答库匹配域）；选项描述=question_text
    截 ≤40 字。标准书面中文（prompt 语言纯度铁律——该文本无条件进判定请求）。
    """
    lines = [
        "根据客户刚说的话，从候选知识库词条中选出与他所问问题同义的一条；",
        "只按问题含义判断，不要凭编号猜测；全部不贴合时选 NONE。候选词条：",
    ]
    for e in entries:
        eid = str((e or {}).get("id") or "")
        q = " ".join(str((e or {}).get("question_text") or "").split())
        if len(q) > QA_OPTION_TEXT_MAX:
            q = q[:QA_OPTION_TEXT_MAX]
        lines.append(f"- {eid}｜问法 {q or '(无)'}")
    return "\n".join(lines)


_QA_OFF: dict = {"verdict": "off", "choice": "", "p": 0.0, "conf": 0.0}
_QA_UNAVAILABLE: dict = {"verdict": "unavailable", "choice": "", "p": 0.0, "conf": 0.0}


async def decide_qa_match(
    state_lines: list[str],
    candidates: list[dict],
    *,
    client: LayaJudgeClient | None = None,
    enabled: bool | None = None,
) -> dict:
    """QA 复核单问（模块级入口，离线可测）。返回**原始面**：

    {"verdict": hit|none|abstain|off|unavailable, "choice": str, "p": float,
    "conf": float}——hit=choice 命中候选且未 below_floor；none=choice==NONE 且
    未 below_floor（高置信拒绝）；abstain=below_floor（置信门不裁边界案）；
    off=总闸关（默认档，零调用）；unavailable=空候选/健康失败/判定失败/坏形
    响应（客户端 fail-open 已兜，合并归因）。p=胜者面概率（probabilities 缺席
    回退 confidence）；conf=sidecar confidence。候选超 QA_MAX_CANDIDATES 截前
    8；任何异常一律 unavailable（消费方落 QA_SEM，绝不抛）。
    """
    if enabled is None:
        enabled = laya_qa_enabled()
    if not enabled:
        return dict(_QA_OFF)
    try:
        face: list[dict] = []
        seen: set[str] = set()
        for e in candidates or []:
            eid = str((e or {}).get("id") or "").strip()
            if not eid or eid in seen:
                continue
            seen.add(eid)
            face.append(e)
            if len(face) >= QA_MAX_CANDIDATES:
                break
        if not face:
            return dict(_QA_UNAVAILABLE)
        cli = client if client is not None else default_client()
        ans = await cli.decide_choice(
            "\n".join(str(line or "") for line in (state_lines or [])),
            build_qa_instructions(face),
            [str(e.get("id") or "") for e in face],
            timeout_s=qa_timeout_s(),
            qid="qa_match",
            skip_gate=True,  # 车道闸=BOK_LAYA_QA（上方），不复检 BOK_LAYA_JUDGE
        )
        if ans is None:
            return dict(_QA_UNAVAILABLE)
        choice = str(ans.get("choice") or "")
        conf = float(ans.get("confidence") or 0.0)
        probs = ans.get("probabilities")
        raw_p = probs.get(choice) if isinstance(probs, dict) else None
        try:
            p = float(raw_p)
        except (TypeError, ValueError):
            p = conf
        if ans.get("below_floor"):
            verdict = "abstain"
        elif choice == "NONE":
            verdict = "none"
        else:
            verdict = "hit"
        return {"verdict": verdict, "choice": choice, "p": p, "conf": conf}
    except Exception:  # noqa: BLE001 - fail-open：一切异常=unavailable（落旧路）
        return dict(_QA_UNAVAILABLE)
