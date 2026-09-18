"""外呼战役调度循环（spec 2026-09-12 Wave3；2026-09-17 调度接线）。

5s 巡检:①收割——dialing/in_call 的 item 其 call 已终态 → item 落结果（命中
重拨策略回 pending 等 interval_minutes）;②起拨——双层时段窗（全局 ∩ 任务）+并发
闸+重拨间隔过滤后补一通（create_call + create_dispatch(metadata 带 dial 块)置
dialing）;③名单尽 → campaign done。dispatcher 可注入(fake 供单测)。

拨号结果联动:agent dial-result 端点(Wave3 扩展)直接写 item 状态,收割段幂等跳过
(dispatcher 是「房间级」派发,真正拨号由 agent 依 metadata 的 dial 块执行)。

`campaign.py` 对 `main.py` 的一切引用都在函数体内延迟 import——main 在 startup 里
反向 import 本模块的 `_campaign_loop`,模块级互相 import 会成环。
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from bok_voice_core.types import CallMode, CallStatus
from bok_voice_obs.logging import get_logger

from .dispatch_utils import has_active_dispatch

log = get_logger("control-plane.campaign", component="control-plane", service="control-plane")

POLL_S = 5.0
DEFAULT_ACCOUNT_ID = "acc-001"
AGENT_NAME = "bok-voice"
# item 的「进行中」集合：这两种状态下的 item 独占一路，串行循环不再起下一通。
_INFLIGHT_ITEM_STATUSES = ("dialing", "in_call")
# item 的终态集合：这些 item 已经跑完一轮（不管成败），起拨冷却以它们的 updated_at 为锚。
_TERMINAL_ITEM_STATUSES = ("done", "no_answer", "rejected", "failed", "skipped")
DEFAULT_GAP_SECONDS = 5.0

Dispatcher = Callable[[str, str], Awaitable[None]]


def item_status_for_call(call: dict) -> str:
    """拨号终态通话 → campaign item 状态（纯函数）。

    通话级 FAILED(call 状态 failed) 恒落 failed；已 ENDED 时按 disposition 分：
    no_answer/rejected/failed 原样落同名，其余（completed/declined/abandoned/
    空等）都算名单跑过一轮 → done。
    """
    if str(call.get("status") or "") == CallStatus.FAILED.value:
        return "failed"
    d = str(call.get("disposition") or "")
    return {"no_answer": "no_answer", "rejected": "rejected", "failed": "failed"}.get(d, "done")


def _dial_mode(sip: dict) -> str:
    """dial.mode 拼装：env BOK_SIP_MODE 合法值优先 > settings sip.mode > mock。

    与 agent 侧 `dialer.resolve_dial_mode` 同语义但独立实现（跨包分层：CP 不 import
    agent 包）。env 非法值**忽略并回落 settings**（与 agent 侧「非法 env 直落 mock」
    略有差异，但 agent 收到 metadata 后会按自己的语义二次归一，最终一致）。
    """
    env_mode = str(os.environ.get("BOK_SIP_MODE", "") or "").strip().lower()
    if env_mode in ("mock", "real"):
        return env_mode
    mode = str(sip.get("mode") or "").strip().lower()
    return mode if mode in ("mock", "real") else "mock"


def build_dial_block(
    *,
    number: str,
    language: str,
    sip: dict,
    site: dict | None = None,
    scenario: str = "",
    script: list[str] | None = None,
    speak_interval_s: float = 0.0,
    campaign_item_id: str = "",
    narrowband: bool = False,
) -> dict:
    """组 dial 块（campaign 建通链与单发外呼共用；spec 2026-09-13 P1.5 T4）。

    **键序与 campaign 旧 dial 块逐键一致**（agent 侧按键读，形状是有意契约）：
    to/mode/scenario/script/speak_interval_s/language/trunk_id/campaign_item_id/
    max_call_duration_s/ringing_timeout_s。

    trunk 解析（T2 语义）：`site.trunk_id` 非空优先，否则回退 `sip.trunk_id`
    ——`site=None`/站点不存在/站点未注册 trunk/虚拟 `site-local` 都安全回退，
    单站点旧行为零变化。数字字段一律 `or 默认` 兜底：settings 里 0/空会静默
    变成「无保险丝/零振铃窗」。

    窄带档（T5，8kHz 重验测试床）：**只在 True 时追加键**——旧 10 键键序与
    「缺键=False」语义零变化（agent 侧 `bool(_dial.get("narrowband"))`）。
    mock 档专属（真中继的窄带来自运营商本身，real 档无消费）。
    """
    trunk_id = str((site or {}).get("trunk_id") or "") or str(sip.get("trunk_id") or "")
    dial = {
        "to": number,
        "mode": _dial_mode(sip),
        "scenario": scenario,
        "script": list(script or []),
        # mock 台词句间隔（campaign 级可选钩子）：E2E 把客户报号句对齐到 AI 的
        # 收号步用；缺省 0=子进程自带 6s。
        "speak_interval_s": speak_interval_s,
        "language": language,
        "trunk_id": trunk_id,
        "campaign_item_id": campaign_item_id,
        # 数字字段必须 `or 默认` 兜底：settings 里 0/空会静默变成「无保险丝/零振铃窗」。
        "max_call_duration_s": int(sip.get("max_call_duration_s") or 600),
        "ringing_timeout_s": int(sip.get("ringing_timeout_s") or 30),
    }
    if narrowband:
        dial["narrowband"] = True
    return dial


async def _default_dispatcher(room: str, metadata: str) -> None:
    """默认派发：LiveKit 官方 explicit agent dispatch（metadata 带 dial 块）。"""
    from .main import _lkapi_client  # 延迟 import 防 main↔campaign 循环

    client = _lkapi_client()
    if client is None:
        raise RuntimeError("livekit credentials missing")
    try:
        if await has_active_dispatch(client, room, AGENT_NAME):
            return  # 防重：同 agent 已有活跃 job 不再叠加第二套
        from livekit.api import CreateAgentDispatchRequest

        await client.agent_dispatch.create_dispatch(
            CreateAgentDispatchRequest(agent_name=AGENT_NAME, room=room, metadata=metadata)
        )
    finally:
        await client.aclose()


async def campaign_tick(repo=None, *, dispatcher: Dispatcher | None = None,
                        now: datetime | None = None) -> dict:
    """一轮巡检：终态收割（含重拨预约回 pending）→ 时段窗/并发闸 → 起拨 → 名单尽判 done。

    now 注入（2026-09-17 Task 3）：测试/探针固定时钟用。**两个时间域，接线已分域**：
    - 时段窗判定（全局窗 ∩ 任务窗）：`now` 按**本地墙钟 naive** 解释——无 tz 视为
      本地（运营语义）；带 tz 先 `astimezone()` 转本地再剥；
    - 重拨间隔比较（redispatch_due）：**UTC naive 域**（与 `_utcnow_naive`/落库
      updated_at 同域）；带 tz 由 redispatch_due 内部转 UTC，naive 原样透传。
      gap 冷却锚沿用真实钟（`_utcnow_naive` 域），不随注入 now 偏移。
    注入固定时钟做重拨断言时，item.updated_at 与 now 取同一时间线即可。

    返回 `{"harvested": n, "started": n, "finished": n}`；单条战役/单个 item 出错
    不阻其余战役（各自 try 包裹，异常记 warning 后继续）。
    """
    from .main import _repo

    # repo 归属（2026-09-18 连接池泄漏修复）：自建=本函数收口，注入=调用方所有
    # （既有单测跨轮复用同一注入 repo，不能替人 close）。
    owns_repo = repo is None
    dispatcher = dispatcher or _default_dispatcher
    out = {"harvested": 0, "started": 0, "finished": 0}
    try:
        # 自建 repo 放 try 首行：_repo() 若抛（引擎劣化等）也落在 close-finally
        # 管辖内，不留「建了没关」的死角（评审 Minor 收口）。
        if owns_repo:
            repo = _repo()
        for campaign in repo.list_campaigns("", status="running"):
            try:
                await _tick_campaign(repo, campaign, dispatcher, out, now=now)
            except Exception as exc:  # noqa: BLE001 - 单条战役失败不阻其余
                log.warning(
                    "campaign_tick_failed",
                    extra={"event": "campaign.tick.error",
                           "data": {"campaign": campaign.get("id", ""), "error": str(exc)}},
                )
    finally:
        # SQL 仓的 Session 从不 commit 的读（list_campaigns/list_items/get_call…）
        # autobegin 后即占住一条池连接，此前只靠 GC 归还——本循环 5s 一轮每轮
        # 新建 Session，DB 抖动时（生产实证 psycopg SSL EOF）钉住的死连接在
        # GC 间隔内持续计入 QueuePool 容量直至 5+10 全满：每轮 30s 池超时
        # （campaign_tick_failed），巡检的同步 DB 调用跑在事件循环上，30s 阻塞
        # 连带 /health 超时。finally 收口让连接确定性归还，一轮一清。
        # 内存仓/测试 fake 无 close，duck-typing 跳过。
        if owns_repo:
            closer = getattr(repo, "close", None)
            if closer is not None:
                try:
                    closer()
                except Exception as exc:  # noqa: BLE001 - 收口失败不杀巡检循环
                    log.warning(
                        "campaign_repo_close_failed",
                        extra={"event": "campaign.repo.close.error",
                               "data": {"error": str(exc)}},
                    )
    return out


async def _tick_campaign(repo, campaign: dict, dispatcher: Dispatcher, out: dict,
                         now: datetime | None = None) -> None:
    """单条战役一轮巡检（2026-09-17 调度接线：双层时段窗/并发槽位/自动重拨）。

    并发语义：`max_concurrency=0` 不限时；≥1 = 同刻至多 N 通在途（在途=dialing/
    in_call），`slots=cap-inflight` 由并发闸表达——**一轮至多补一通**，本轮起拨后
    下一轮巡检再补位，天然逐步填满 N 槽（5s 巡检粒度下逐通起拨，对运营节奏更可
    控，也让「在途满员→释放→补位」的转移在 tick 边界上可观测、可注入测试）。

    now 分域见 campaign_tick docstring：窗口判定用本地 naive（无 tz 视为本地），
    重拨比较沿用 UTC naive 域（redispatch_due 内部约定）。
    """
    from .main import _TERMINAL_CALL_STATUSES

    now_local = (now if now is not None else localnow_naive())
    if now_local.tzinfo is not None:  # aware → 转本地墙钟再剥 tz（M2 分域，窗口判定域）
        now_local = now_local.astimezone().replace(tzinfo=None)

    # ① 收割：进行中 item 的通话已终态 → 回写 item 结果（重拨命中回 pending：
    for item in [i for i in repo.list_items(campaign["id"])
                 if i.get("status") in _INFLIGHT_ITEM_STATUSES]:
        call = repo.get_call(str(item.get("call_id") or ""))
        if not call:
            continue
        if str(call.get("status") or "") not in _TERMINAL_CALL_STATUSES:
            continue
        result = item_status_for_call(call)
        updates = {"status": result,
                   "last_error": str(call.get("disposition") or "")[:250],
                   "updated_at": _utcnow_iso()}
        # 重拨预约：回 pending 等 interval_minutes（终审 C-1：判定抽纯函数
        # redispatch_harvest_updates，与 main.report_dial_result 直写路共用——
        # attempts 不重置、+1 = 下一次尝试编号，等待中的 pending item 靠
        # attempts>=2 与首拨区分，redispatch_due 据此闸门（attempts<=1 恒不放等）。
        redispatch = redispatch_harvest_updates(item, result, campaign)
        if redispatch:
            updates.update(redispatch)
        repo.update_item(str(item["id"]), **updates)
        out["harvested"] += 1

    # ②/③ 以收割后的最新名单为准：收尾 → 并发闸 → 时段窗 → 重拨过滤 → 起拨。
    items = repo.list_items(campaign["id"])
    inflight = sum(1 for i in items if i.get("status") in _INFLIGHT_ITEM_STATUSES)
    pending = [i for i in items if i.get("status") == "pending"]
    if not pending and inflight == 0:
        repo.update_campaign(str(campaign["id"]), status="done",
                             finished_at=_utcnow_iso())
        out["finished"] += 1
        return
    cap = concurrency_cap(campaign)
    if cap and inflight >= cap:
        return  # 并发闸：在途已满 N 槽（cap=0 不限时），本轮不再起拨
    # 两通之间留 gap_seconds 冷却：上一通终态刚落就起拨会让运营配的间隔失效。
    if not _in_gap_cooldown(items, campaign):
        # 时段窗：全局窗（settings.campaign.call_windows）∩ 任务窗，两层都过才起拨。
        global_windows = _global_call_windows(repo)
        if within_call_windows(now_local, global_windows) and \
           within_call_windows(now_local, campaign.get("call_windows")):
            # 等重拨的 pending（redispatch_due=True）本轮跳过，到点自然回到 eligible。
            eligible = [i for i in pending if not redispatch_due(i, campaign, now)]
            if eligible:
                # 一轮至多补一通：下一轮巡检再补位，天然逐步填满 N 槽（cap=0 不限时
                # 亦逐通起拨，见函数 docstring 的并发语义）。
                await _start_call(repo, campaign, eligible[0], dispatcher)
                out["started"] += 1
                return
    # pending 全在等重拨/窗未到/冷却中：保持现状（campaign 不判 done）


def _global_call_windows(repo) -> list[dict]:
    """全局外呼时段窗：settings.campaign.call_windows（空=不限）。读失败恒 []。

    与任务窗取交集（两层都过才起拨）；写侧端点不在本计划内，读侧缺省 []。
    """
    try:
        section = (repo.get_settings() or {}).get("campaign") or {}
        windows = section.get("call_windows") if isinstance(section, dict) else None
    except Exception:  # noqa: BLE001 - 设置读取失败不阻拨
        return []
    return parse_call_windows(windows)


def _in_gap_cooldown(items: list[dict], campaign: dict, now: datetime | None = None) -> bool:
    """最近一个终态 item 距今不足 gap_seconds → 本轮不起拨（纯函数，便于单测）。

    updated_at 解析不出的终态 item 不算锚（视为陈旧）；一个终态锚都没有=首通，放行。

    锚集合 = `_TERMINAL_ITEM_STATUSES` 减去 `skipped`：建仓即 skipped 的 item（对象
    无电话）带的是**建仓时间戳**，若计入锚，波次一开始就被自己的建仓时间门控住，
    首通至多延迟 gap 秒。冷却语义是「两通真实电话之间留间隔」，从未拨过的 item
    不构成锚。
    """
    now = now or _utcnow_naive()
    anchors = tuple(s for s in _TERMINAL_ITEM_STATUSES if s != "skipped")
    last_terminal = max(
        (dt for dt in (_parse_updated_at(i.get("updated_at"))
                       for i in items if i.get("status") in anchors)
         if dt is not None),
        default=None,
    )
    if last_terminal is None:
        return False
    return (now - last_terminal).total_seconds() < _gap_seconds(campaign)


async def _start_call(repo, campaign: dict, item: dict, dispatcher: Dispatcher) -> None:
    """给首个 pending item 建通话 + 派 agent；派发失败落 item failed 不抛。"""
    from .main import _create_call_in  # 延迟 import（同步 handler 内核，见 main）
    from .schemas import CreateCallRequest

    req = CreateCallRequest(
        account_id=str(campaign.get("account_id") or DEFAULT_ACCOUNT_ID),
        object_id=str(item.get("object_id") or ""),
        persona_id=str(campaign.get("persona_id") or ""),
        # 战役选定的话术建单即快照(agent 装配读通话快照优先)——此前该字段
        # 只存不读,运营在战役里选的话术被静默忽略、通话仍走对象卡绑定话术。
        template_id=str(campaign.get("template_id") or ""),
        mode=CallMode.LIVE,
        direction="outbound",
        language=str(campaign.get("language") or "zh"),
    )
    call = _create_call_in(repo, req)
    call_id = str(call.get("id") or "")
    phone = str(item.get("phone") or "")
    if phone:
        repo.update_call(call_id, contact_phone=phone)
    settings = repo.get_settings() or {}
    sip = dict(settings.get("sip") or {})
    # 站点的 trunk 优先、settings 兜底（spec 2026-09-13 P1.5）：战役挂了站点且该
    # 站点注册过 outbound trunk（`ST_...`）→ 用站点的；否则（无 site_id/站点不存在/
    # `site-local` 恒合成不入库/站点 trunk 未注册）逐字回退 settings `sip.trunk_id`
    # ——单站点旧行为零变化。
    site = repo.get_site(str(campaign.get("site_id") or "")) if campaign.get("site_id") else None
    # mock 演练台词（campaign 级 object_id→[句子]）：只有 mock 档需要（真 SIP 对端
    # 是真客户）。读取失败/缺键一律空数组——agent 侧 dial_outbound 的 script 缺省
    # 已是 []，子进程再有语言默认兜底，三层都不会因缺台词卡住。
    try:
        _scripts = repo.get_campaign_scripts(str(campaign.get("id") or ""))
        script = _scripts.get(str(item.get("object_id") or ""), [])
        # 句间隔与台词同源（campaign 级 mock 钩子，键 "__speak_interval__" 避开
        # object_id 命名空间；0/缺省=子进程自带 6s）。
        pace = float(_scripts.get("__speak_interval__") or 0)
        # 窄带档（T5）：同一份保留键命名空间的 mock 钩子，缺省 False=旧战役零变化。
        narrowband = bool(_scripts.get("__narrowband__") or False)
    except Exception as exc:  # noqa: BLE001 - 台词是演练钩子，取不到照常拨号
        log.warning(
            "campaign_scripts_read_failed",
            extra={"event": "campaign.scripts.error",
                   "data": {"campaign": campaign.get("id", ""), "error": str(exc)}},
        )
        script, pace, narrowband = [], 0.0, False
    # dial 块单点在 build_dial_block（单发外呼走同一函数，键序/取值同源）。
    dial = build_dial_block(
        number=phone,
        language=str(campaign.get("language") or "zh"),
        sip=sip,
        site=site,
        scenario=str(item.get("scenario") or ""),
        script=script,
        speak_interval_s=pace,
        campaign_item_id=str(item.get("id") or ""),
        narrowband=narrowband,
    )
    metadata = json.dumps({"call_id": call_id, "dial": dial}, ensure_ascii=False)
    try:
        await dispatcher(call_id, metadata)
    except Exception as exc:  # noqa: BLE001 - 派发失败落 item（下一轮不再重试该 item）
        log.warning(
            "campaign_dispatch_failed",
            extra={"event": "campaign.dispatch.error",
                   "data": {"campaign": campaign.get("id", ""), "call": call_id,
                            "error": str(exc)}},
        )
        repo.update_item(str(item["id"]), status="failed", call_id=call_id,
                         last_error=f"dispatch: {exc}"[:250],
                         updated_at=_utcnow_iso())
        return
    repo.update_item(str(item["id"]), status="dialing", call_id=call_id,
                     updated_at=_utcnow_iso())
    log.info(
        "campaign_call_started",
        extra={"event": "campaign.call_started",
               "data": {"campaign": campaign.get("id", ""), "item": item.get("id", ""),
                        "call": call_id}},
    )


def _utcnow_naive() -> datetime:
    """UTC 墙钟 naive datetime（比较用）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utcnow_iso() -> str:
    """落库用的 UTC ISO 串（naive 墙钟，无 +00:00 后缀）。

    M-2：`updated_at`/`finished_at` 内存仓与 SQL 仓混存过 datetime 与 str 两形态，
    读侧（`_parse_updated_at`）两种都收，但写入统一成 ISO 串——与内存仓
    `create_campaign` 的 `datetime.now(timezone.utc).isoformat()` 同族、SQL 侧
    `DateTime` 列 `fromisoformat` 收串同姿势（`update_item` 已有该分支）。
    """
    return _utcnow_naive().isoformat()


def _parse_updated_at(value: Any) -> datetime | None:
    """updated_at 归一为 naive UTC datetime；解析不出（空/非法/类型不符）→ None。

    内存仓与 SQL 仓混存两种形态（M-2 备案）：SQL 读侧回 ISO 串（`_item_to_dict`
    对 DateTime 列 `.isoformat()`），内存仓历史上也可能存 datetime 对象。
    两种都收；naive 视为 UTC 墙钟，aware 转 UTC 后剥 tz（与 `_utcnow_naive` 同域）。
    """
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    return None


def _gap_seconds(campaign: dict) -> float:
    """冷却秒数：campaign.gap_seconds 为 0/空/非法时兜 5s（0 值不静默变无冷却）。"""
    try:
        return float(campaign.get("gap_seconds") or DEFAULT_GAP_SECONDS)
    except (TypeError, ValueError):
        return DEFAULT_GAP_SECONDS


async def _campaign_loop() -> None:
    while True:
        try:
            await campaign_tick()
        except Exception as exc:  # noqa: BLE001 - 单轮失败不杀循环
            log.warning(
                "campaign_tick_failed",
                extra={"event": "campaign.tick.error", "data": {"error": str(exc)}},
            )
        await asyncio.sleep(POLL_S)


# ---------------------------------------------------------------------------
# 调度决策纯函数（spec 2026-09-17 Task 2）：时段窗/重拨/并发。
# 全部无副作用、时间可注入（now 参数），Task 3/4 的起拨门控直接消费。
# ---------------------------------------------------------------------------

MAX_CALL_WINDOWS = 3


def parse_call_windows(raw: Any) -> list[dict]:
    """外呼时段窗归一（纯函数）：≤3 组、days⊆{1..7} 升序去重、HH:MM、start<end。

    逐项校验，非法项静默丢弃不抛——运营表单一个错字不废整单（与 create 端点
    scenarios 白名单同哲学）。非 list/解析失败 → []（不限时段语义由调用方空表表达）。
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for entry in raw:
        if len(out) >= MAX_CALL_WINDOWS:
            break
        if not isinstance(entry, dict):
            continue
        days = entry.get("days")
        if not isinstance(days, list):
            continue
        try:
            norm_days = sorted({int(d) for d in days
                                if isinstance(d, (int, str)) and str(d).isdigit()
                                and 1 <= int(d) <= 7})
        except (TypeError, ValueError):
            continue
        start = _hhmm(entry.get("start"))
        end = _hhmm(entry.get("end"))
        if not norm_days or start is None or end is None or start >= end:
            continue
        out.append({"days": norm_days, "start": start, "end": end})
    return out


def _hhmm(value: Any) -> str | None:
    """"08:00"/"08:00:00" → "08:00"；非法 → None。"""
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def within_call_windows(now_local: datetime, windows: Any) -> bool:
    """now（本地 naive）落任一窗 → True；窗空 → True（不限）；窗形状非法 → False。

    解析层保证 start<end（跨零点窗不支持解析层）；本函数对 end<=start 的窗按
    「start→次日 end」防御判定（接口契约，days 判起点日，次日清晨段看昨日是否
    在窗天内）。秒级比较、端点含——18:00:00 在窗内、18:00:01 已出窗。
    """
    if windows is None:
        return True
    if not isinstance(windows, list):
        return False
    if windows and all(isinstance(w, dict) and "start" in w for w in windows):
        parsed: list[dict] = windows  # 已归一形状（Task 1 仓储读侧）直接用
    else:
        parsed = parse_call_windows(windows)
    if not parsed:
        return not windows  # 空表=不限（True）；给了窗但全非法=不放行（False）
    now_seconds = now_local.hour * 3600 + now_local.minute * 60 + now_local.second
    iso_weekday = now_local.isoweekday()
    for window in parsed:
        try:
            day_set = {int(d) for d in window.get("days", [])}
        except (TypeError, ValueError):
            continue
        if not day_set:
            continue
        start = _hhmm_to_minute(window.get("start"))
        end = _hhmm_to_minute(window.get("end"))
        if start is None or end is None:
            continue
        start_s, end_s = start * 60, end * 60
        if end > start:
            if iso_weekday in day_set and start_s <= now_seconds <= end_s:
                return True
        elif iso_weekday in day_set and now_seconds >= start_s:
            return True  # 跨零点前半段：起点日 start→24:00
        elif now_seconds <= end_s and (iso_weekday - 2) % 7 + 1 in day_set:
            return True  # 跨零点后半段：昨日为起点日 00:00→end
    return False


def _hhmm_to_minute(value: Any) -> int | None:
    """HH:MM → 当日分钟数；非法/越界（小时 >23、分钟 >59）→ None。

    M1（2026-09-17 评审遗留）：`within_call_windows` 的「已像窗」fast-path 直收
    dict 绕过 parse_call_windows 归一，本函数必须自带值域校验兜底——否则
    `{"start":"08:00","end":"99:99"}` 类垃圾窗经 fast-path 放行。
    """
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def redispatch_policy(campaign: dict) -> dict:
    """redispatch 解析（纯函数）：空/非法 → 不重拨（max_attempts=0）。"""
    raw = campaign.get("redispatch")
    if not isinstance(raw, dict):
        return {"max_attempts": 0, "interval_minutes": 0.0, "on": frozenset()}
    try:
        max_attempts = max(0, int(raw.get("max_attempts") or 0))
    except (TypeError, ValueError):
        max_attempts = 0
    try:
        interval = max(0.0, float(raw.get("interval_minutes") or 0.0))
    except (TypeError, ValueError):
        interval = 0.0
    allowed = {"no_answer", "rejected", "failed"}
    outcomes = frozenset(str(x) for x in (raw.get("on") or []) if str(x) in allowed)
    return {"max_attempts": max_attempts, "interval_minutes": interval, "on": outcomes}


def redispatch_harvest_updates(item: dict, result: str, campaign: dict) -> dict | None:
    """重联回队判定（纯函数，终审 C-1）：dial-result 直写与循环收割两路共用。

    命中（result ∈ policy["on"] 且 attempts < max_attempts）→ 返回
    `{"status": "pending", "attempts": attempts+1, "updated_at": _utcnow_iso()}`
    由调用方落库（last_error 各自带：收割路=call.disposition、直写路同源）；
    不命中 → None（调用方维持各自现行为写终态）。attempts 语义与收割段一致：
    不重置、+1=下一次尝试编号，redispatch_due 据 attempts≥2 闸等待窗。

    此前重拨回队只活在 `_tick_campaign` 收割段（只扫在途 item），而
    report_dial_result 把失败三态直写终态——生产主路 item 一轮即终态、永不进
    收割，attempts 永不加、重拨静默 no-op（终审 C-1）。
    """
    policy = redispatch_policy(campaign)
    try:
        attempts = int(item.get("attempts") or 1)
    except (TypeError, ValueError):
        attempts = 1
    if result in policy["on"] and attempts < policy["max_attempts"]:
        return {"status": "pending", "attempts": attempts + 1,
                "updated_at": _utcnow_iso()}
    return None


def redispatch_due(item: dict, campaign: dict, now: datetime | None = None) -> bool:
    """pending item 还没到重拨时刻 → True（本轮跳过）。首拨（attempts≤1）恒 False。

    updated_at 解析不出 → False（放行，不因脏数据卡死名单）。
    now 参数须为 UTC 域 naive（默认 `_utcnow_naive` 同域）；本地时域勿传——
    落库 updated_at 全是 UTC naive，本地 naive 直接比较会差一个时区偏移。
    """
    try:
        attempts = int(item.get("attempts") or 1)
    except (TypeError, ValueError):
        attempts = 1
    if attempts <= 1:
        return False
    policy = redispatch_policy(campaign)
    if policy["max_attempts"] <= 0 or policy["interval_minutes"] <= 0:
        return False
    updated = _parse_updated_at(item.get("updated_at"))
    if updated is None:
        return False
    now = now if now is not None else _utcnow_naive()
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    return (now - updated).total_seconds() < policy["interval_minutes"] * 60


def concurrency_cap(campaign: dict) -> int:
    """max_concurrency：0=不限；空/非法=1（旧串行行为）。"""
    try:
        return max(0, int(campaign.get("max_concurrency")
                          if campaign.get("max_concurrency") is not None else 1))
    except (TypeError, ValueError):
        return 1


def localnow_naive() -> datetime:
    """服务器本地时区 naive datetime（时段窗判定用，运营语义）。"""
    return datetime.now().astimezone().replace(tzinfo=None)
