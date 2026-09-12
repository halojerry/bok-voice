"""外呼战役串行循环（spec 2026-09-12 Wave3）。

5s 巡检:①收割——dialing/in_call 的 item 其 call 已终态 → item 落结果;
②串行——无进行中 item 且有 pending → create_call + create_dispatch(metadata 带 dial 块)
置 dialing;③名单尽 → campaign done。dispatcher 可注入(fake 供单测)。

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


async def campaign_tick(repo=None, *, dispatcher: Dispatcher | None = None) -> dict:
    """一轮巡检：终态收割 → 串行起下一通 → 名单尽判 done。

    返回 `{"harvested": n, "started": n, "finished": n}`；单条战役/单个 item 出错
    不阻其余战役（各自 try 包裹，异常记 warning 后继续）。
    """
    from .main import _repo

    repo = repo if repo is not None else _repo()
    dispatcher = dispatcher or _default_dispatcher
    out = {"harvested": 0, "started": 0, "finished": 0}
    for campaign in repo.list_campaigns(DEFAULT_ACCOUNT_ID, status="running"):
        try:
            await _tick_campaign(repo, campaign, dispatcher, out)
        except Exception as exc:  # noqa: BLE001 - 单条战役失败不阻其余
            log.warning(
                "campaign_tick_failed",
                extra={"event": "campaign.tick.error",
                       "data": {"campaign": campaign.get("id", ""), "error": str(exc)}},
            )
    return out


async def _tick_campaign(repo, campaign: dict, dispatcher: Dispatcher, out: dict) -> None:
    from .main import _TERMINAL_CALL_STATUSES

    # ① 收割：进行中 item 的通话已终态 → 回写 item 结果。
    for item in [i for i in repo.list_items(campaign["id"])
                 if i.get("status") in _INFLIGHT_ITEM_STATUSES]:
        call = repo.get_call(str(item.get("call_id") or ""))
        if not call:
            continue
        if str(call.get("status") or "") not in _TERMINAL_CALL_STATUSES:
            continue
        repo.update_item(
            str(item["id"]),
            status=item_status_for_call(call),
            last_error=str(call.get("disposition") or "")[:250],
            updated_at=_utcnow_iso(),
        )
        out["harvested"] += 1

    # ②/③ 以收割后的最新名单为准判断串行与收尾。
    items = repo.list_items(campaign["id"])
    if any(i.get("status") in _INFLIGHT_ITEM_STATUSES for i in items):
        return  # 串行：一路进行中就等它出终态
    pending = [i for i in items if i.get("status") == "pending"]
    if not pending:
        repo.update_campaign(str(campaign["id"]), status="done",
                             finished_at=_utcnow_iso())
        out["finished"] += 1
        return
    # 两通之间留 gap_seconds 冷却：上一通终态刚落就起拨会让运营配的间隔失效
    # （5s 巡检粒度下起拨最早也只比配置快 5s，但同轮收割+起拨会压成 0s）。
    if _in_gap_cooldown(items, campaign):
        return
    await _start_call(repo, campaign, pending[0], dispatcher)
    out["started"] += 1


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
    dial = {
        "to": phone,
        "mode": _dial_mode(sip),
        "scenario": str(item.get("scenario") or ""),
        "language": str(campaign.get("language") or "zh"),
        "trunk_id": str(sip.get("trunk_id") or ""),
        "campaign_item_id": str(item.get("id") or ""),
        # 数字字段必须 `or 默认` 兜底：settings 里 0/空会静默变成「无保险丝/零振铃窗」。
        "max_call_duration_s": int(sip.get("max_call_duration_s") or 600),
    }
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
