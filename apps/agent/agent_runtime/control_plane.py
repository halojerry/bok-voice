from __future__ import annotations

import os

import httpx


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
        await self._client.post(
            f"/api/calls/{call_id}/turns",
            params={
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
            },
        )

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
        await self._client.aclose()
