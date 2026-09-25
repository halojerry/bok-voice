from __future__ import annotations

import asyncio
import os
from collections import deque

import httpx

# M-30 turns 断窗重放（2026-09-23 修复波#2，task-9 腿 9.6 实证 ~14 轮永久丢）：
# add_turn 失败（连接错误/5xx/429）→ 本地有界暂存 + 背景重放，CP 恢复按序补齐。
# 重放周期有界（连续失败 _TURN_REPLAY_MAX_ATTEMPTS 次收工），下次 add_turn 失败
# 再踢新周期——CP 长断窗自愈不跑飞。4xx 不重放（404 已删单/401 鉴权，重放无益）。
# 杀开关 BOK_TURNS_REPLAY（默认 "1"；"0"=回旧行为失败即抛，调用方 REPORT_TASK_ERR）。
# 乱序口径（fix round 1 评审 Minor-3）：断窗恢复的混合窗内，重放补交与直投新轮
# 交错 → CP 侧 turns 到达序可能与生成序不一致（重放组内部恒按原序）。仅影响
# 「断窗+恢复」混合窗内按到达序做配对/排序的下游；相比修复前整段永久丢失，属
# 可接受权衡——CP 侧展示/学习账本以单客户端到达序为准，读侧如需生成序请用
# started_ms/ended_ms 轮时间轴列。
_TURN_SPOOL_MAX = 512
_TURN_REPLAY_BACKOFF_S = 2.0
_TURN_REPLAY_MAX_ATTEMPTS = 5


class ControlPlaneClient:
    """Thin HTTP client from the Agent worker to the Control Plane REST API."""

    def __init__(self, base_url: str, call_id: str = ""):
        self.base_url = base_url.rstrip("/")
        # 会话级 correlation:全部请求带 X-Call-ID → CP 审计行的 call_id 列
        # 自动填充(此前恒空,web 按 callId 过滤审计查不到)。
        headers = {"X-Call-ID": call_id} if call_id else {}
        # 机器通道自报（D1 修复）：纯 auth-off 形态 CP 无凭据可判通道，agent
        # 全量请求带 X-Bok-Channel: agent——模板详情据此吃发布冻结版 overlay。
        # auth-on 下 CP 不认这个头（overlay 只认机器凭据），携带无害。
        headers["X-Bok-Channel"] = "agent"
        # 机器通道（2026-09-16 深测 P2-8）：CP 设 BOK_CP_TOKEN 时全部请求自动
        # 携带——auth-on 下 turns/QA/垫话/设置上报不再 401（此前 env 无任何代码
        # 读取，文档「agent env 必须带同值」是 aspirational）。未设=零变化。
        cp_token = (os.environ.get("BOK_CP_TOKEN") or "").strip()
        if cp_token:
            headers["Authorization"] = f"Bearer {cp_token}"
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=15, headers=headers)
        # M-30：turns 断窗暂存（(url, params) 对）+ 单飞重放任务强引用（防 GC 中途
        # 回收——同 _SETTLE_TASKS 教训）。进程内即可，不必落盘（brief 拍板）。
        self._turn_replay_enabled = os.environ.get("BOK_TURNS_REPLAY", "1") != "0"
        self._turn_spool: deque = deque()
        self._replay_task: asyncio.Task | None = None

    async def get_call(self, call_id: str) -> dict:
        r = await self._client.get(f"/api/calls/{call_id}")
        r.raise_for_status()
        return r.json()

    async def get_object(self, object_id: str) -> dict:
        r = await self._client.get(f"/api/objects/{object_id}")
        r.raise_for_status()
        return r.json()

    async def get_persona(self, persona_id: str) -> dict:
        r = await self._client.get(f"/api/personas/{persona_id}")
        r.raise_for_status()
        return r.json()

    async def get_template(self, template_id: str) -> dict:
        r = await self._client.get(f"/api/templates/{template_id}")
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        return r.json()

    async def get_settings(self) -> dict:
        r = await self._client.get("/api/settings", params={"internal": "1"})
        r.raise_for_status()
        return r.json()

    async def search_knowledge(self, query: str, account_id: str, limit: int = 5) -> list[dict]:
        r = await self._client.get(
            "/api/knowledge/search",
            params={"query": query, "account_id": account_id, "limit": limit},
        )
        r.raise_for_status()
        return r.json()

    async def _post_turn_once(self, url: str, params: dict) -> httpx.Response:
        r = await self._client.post(url, params=params)
        if r.status_code >= 500 or r.status_code == 429:
            raise httpx.HTTPStatusError(
                f"turns post {r.status_code}", request=r.request, response=r
            )
        return r

    def _spool_turn(self, url: str, params: dict, reason: str) -> None:
        if len(self._turn_spool) >= _TURN_SPOOL_MAX:
            self._turn_spool.popleft()  # drop-oldest：长断窗丢最旧，新轮保命
            print("TURNS_REPLAY overflow dropped oldest", flush=True)
        self._turn_spool.append((url, params))
        print(
            f"TURNS_REPLAY spooled n={len(self._turn_spool)} ({reason})",
            flush=True,
        )
        self._kick_replay()

    def _kick_replay(self) -> None:
        if self._replay_task is not None and not self._replay_task.done():
            return
        self._replay_task = asyncio.get_running_loop().create_task(self._replay_loop())

    async def _replay_loop(self) -> None:
        failures = 0
        while self._turn_spool:
            if failures:
                await asyncio.sleep(min(_TURN_REPLAY_BACKOFF_S * failures, 8.0))
            url, params = self._turn_spool[0]
            try:
                await self._post_turn_once(url, params)
            except (httpx.RequestError, httpx.HTTPStatusError):
                failures += 1
                if failures >= _TURN_REPLAY_MAX_ATTEMPTS:
                    print(
                        "TURNS_REPLAY cycle exhausted "
                        f"n={len(self._turn_spool)} — next turn failure re-kicks",
                        flush=True,
                    )
                    return
                continue
            except RuntimeError:
                # 客户端已关（aclose 超时残尾，评审 Minor-4）：重试无意义，安静
                # 收工防「Task exception was never retrieved」GC 噪声；暂存保留。
                return
            self._turn_spool.popleft()
            failures = 0  # 恢复后零间歇连续清空（补交不逐条等退避）
        if failures == 0:
            print("TURNS_REPLAY drained", flush=True)

    async def add_turn(
        self,
        call_id: str,
        role: str,
        transcript: str,
        emotion: str = "",
        provider: str = "",
        latency_ms: int = 0,
        language: str = "",
        # 分析账本列（P0 turns ledger）：line=线别(a/b)、speaker=说话人
        # (customer/agent_ai/agent_human|me/other)、gen=生成源(llm/script/
        # qa_fastpath)、template_step=话术步、started/ended_ms=轮时间轴
        # (call 相对)、perceived_ms=用户讲完→AI 出声北极星。CP 侧全可选带
        # 缺省,旧调用零破坏。
        line: str = "",
        speaker: str = "",
        gen: str = "",
        template_step: int = 0,
        started_ms: int = 0,
        ended_ms: int = 0,
        perceived_ms: int = 0,
    ) -> None:
        url = f"/api/calls/{call_id}/turns"
        params = {
            "role": role,
            "transcript": transcript,
            "emotion": emotion,
            "provider": provider,
            "latency_ms": latency_ms,
            "language": language,
            "line": line,
            "speaker": speaker,
            "gen": gen,
            "template_step": template_step,
            "started_ms": started_ms,
            "ended_ms": ended_ms,
            "perceived_ms": perceived_ms,
        }
        if not self._turn_replay_enabled:
            await self._client.post(url, params=params)  # 旧行为：单发即弃
            return
        try:
            await self._post_turn_once(url, params)
        except httpx.RequestError as exc:
            self._spool_turn(url, params, repr(exc))
        except httpx.HTTPStatusError:
            # 4xx 不重放（404 已删单/401 鉴权——重放无益，与旧行为同弃）。
            # _post_turn_once 只对 5xx/429 抛，故这里必是可重试态。
            self._spool_turn(url, params, "server error")
        else:
            if self._turn_spool:
                # 补交追平（CP 刚恢复）：成功轮也踢重放，不必等下一次失败。
                self._kick_replay()

    async def settle(self, call_id: str) -> dict:
        r = await self._client.post(f"/api/calls/{call_id}/settle")
        r.raise_for_status()
        return r.json()

    async def post_session_report(self, call_id: str, report: dict) -> None:
        """上報官方 SessionReport(真实逐模型 usage/权威 chat_history)。settle 前调。

        失败由 caller 打日志——报表缺真数据回退估算口径,唔阻结算。
        """
        r = await self._client.post(f"/api/calls/{call_id}/session-report", json=report)
        r.raise_for_status()

    async def end_call(
        self, call_id: str, disposition: str = "declined", intent_code: str = ""
    ) -> dict:
        """AI 收尾后主动结束通话:置 ENDED 并断房。

        disposition=declined(客户拒绝,默认)| no_response(沉默心跳两次无回应)。
        intent_code=W4 意向规则命中码(2026-09-19):仅非空才带——未命中规则时请求
        URL 与旧版逐字节相同(四个既有收线调用点零语义变化)。CP 侧截 32 落列。
        失败(404 已结束/网络抖动)由 caller 打日志即可,结算另有 _on_close 幂等兜底。
        """
        params: dict = {"disposition": disposition}
        if intent_code:
            params["intent_code"] = intent_code
        r = await self._client.post(
            f"/api/supervisor/{call_id}/end",
            params=params,
        )
        r.raise_for_status()
        return r.json()

    async def report_assist(self, call_id: str, status: str = "notified", source: str = "intent") -> None:
        """上報人工協助打鈴(W4 notify_human 動作,fire-and-forget)。

        status=notified(打鈴)| done(坐席已接管,CP takeover 端點置);source=觸發源
        (intent=話術圖 notify_human 綁定)。server 幂等(done 不降級 notified),
        raise_for_status 俾 caller 知失敗——失敗回滚 once 鍵,後續輪信號補報。
        """
        r = await self._client.post(
            f"/api/calls/{call_id}/assist",
            json={"status": status, "source": source},
        )
        r.raise_for_status()

    async def report_whatsapp(self, call_id: str, number: str = "", channel: str = "") -> None:
        """上報偵測到客戶俾 WhatsApp。number 有值=captured,空=offered。fire-and-forget。

        channel=客户原话渠道词(whatsapp|wechat,flow.channel_from_text),空则由 CP
        按对象 contact_channel 推断(名册入册用)。raise_for_status 俾 caller 知失敗
        (清 key 等下次偵測補報)——server 幂等,重複 POST 唔會造成重複爆閃。
        """
        r = await self._client.post(
            f"/api/calls/{call_id}/whatsapp",
            json={"number": number, "channel": channel},
        )
        r.raise_for_status()

    async def report_dial_result(self, call_id: str, status: str, detail: str = "") -> None:
        """上报外呼拨号结果(spec Wave2):answered→CP 置 ACTIVE;三失败态→ENDED+disposition。

        server 幂等(已终态原样返回);raise_for_status 俾 caller 知失败——失败收线另有
        ctx.shutdown()+删房兜底,上报失败唔阻收线。
        """
        r = await self._client.post(
            f"/api/calls/{call_id}/dial-result",
            json={"status": status, "detail": detail},
        )
        r.raise_for_status()

    async def create_followup(self, call_id: str, kind: str = "followup", note: str = "") -> dict | None:
        """跟进工单登记(漏斗 v2 工具层,spec §3.3):judge route=register_followup
        或规则命中后落单。CP 侧幂等(同 call 同 kind 已有 open 单 → created:false
        原样返回原单)。失败返回 None(fire-with-log,唔阻 judge 任务/通话)。
        """
        try:
            r = await self._client.post(
                f"/api/calls/{call_id}/followups",
                json={"kind": kind, "note": note},
            )
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            print(f"[followup] create failed: {exc!r} (call {call_id})", flush=True)
            return None

    async def list_qa_entries(self, account_id: str = "acc-001", owner_scope: str | None = None) -> list[dict]:
        """快答库启用条目(Q→A 快路,PR-3):每通装配拉一次,变更下一通生效。

        B3:account_id 必传本通账号(旧版硬编码 acc-001,多账号错库);owner_scope=
        建单人 user_id → CP 返回「共享+建单人个人」,战役等无主通话传 ''=仅共享。
        """
        params: dict = {"account_id": account_id, "enabled": 1}
        if owner_scope is not None:
            params["owner_scope"] = owner_scope
        r = await self._client.get("/api/qa-entries", params=params)
        r.raise_for_status()
        data = r.json()
        return list(data) if isinstance(data, list) else []

    async def qa_hit(self, entry_id: str) -> None:
        """快路命中计数(fire-and-forget,失败静默——计数唔阻通话)。"""
        try:
            await self._client.post(f"/api/qa-entries/{entry_id}/hit")
        except Exception:  # noqa: BLE001
            pass

    async def list_filler_entries(self, account_id: str = "acc-001") -> list[dict]:
        """垫话罐头启用条目(2026-09-13 乙节):每通装配拉一次,变更下一通生效。

        B3:account_id 必传本通账号(垫话罐头保持账号级,无 owner 维度)。
        """
        r = await self._client.get("/api/fillers", params={"account_id": account_id, "enabled": 1})
        r.raise_for_status()
        data = r.json()
        return list(data) if isinstance(data, list) else []

    async def list_intent_rules(self, account_id: str = "acc-001") -> list[dict]:
        """意向规则行(W4-T2,2026-09-19):每通装配拉一次,挂断时评估 disposition
        覆盖+intent_code。

        account_id 传本通账号;CP 返回两级行合并(全局 ''∪本账号,disabled 行由
        eval 端按 enabled 跳过)。失败由 caller 兜底空表——挂断走原 disposition,
        零行为变化。
        """
        r = await self._client.get("/api/intent-rules", params={"account_id": account_id})
        r.raise_for_status()
        data = r.json()
        return list(data) if isinstance(data, list) else []

    async def filler_hit(self, entry_id: str) -> None:
        """垫话罐头命中计数(fire-and-forget,失败静默)。"""
        try:
            await self._client.post(f"/api/fillers/{entry_id}/hit")
        except Exception:  # noqa: BLE001
            pass

    async def aclose(self) -> None:
        # M-30：会话收尾有界等待在途重放——断窗轮次尽力补交（CP 已恢复时通常
        # 立即清空）；仍失败/超时则放弃（进程内暂存随进程消亡，不落盘）。
        if self._turn_spool and (
            self._replay_task is None or self._replay_task.done()
        ):
            self._kick_replay()  # 末轮补交窗：周期可能已收工，收尾再踢一次
        if self._replay_task is not None and not self._replay_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._replay_task), timeout=5.0)
            except Exception:  # noqa: BLE001 - 收尾 drain 尽力而为
                pass
        await self._client.aclose()
