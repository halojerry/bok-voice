from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from bok_voice_core.providers import BusinessRepository
from bok_voice_core.types import (
    CallMode,
    CallSession,
    CallStatus,
    ConversationTemplate,
    ObjectProfile,
    PersonaProfile,
    SessionManifest,
    TurnEvent,
)

from . import models


def _uuid() -> str:
    return uuid.uuid4().hex[:12]


class SqlAlchemyBusinessRepository:
    """Concrete BusinessRepository backed by SQLAlchemy (Postgres/SQLite)."""

    def __init__(self, session: Session):
        self.session = session

    # ---- calls ----

    def create_call(self, manifest: SessionManifest) -> dict:
        call = models.CallSession(
            id=manifest.session_id or _uuid(),
            account_id=manifest.account_id,
            object_id=manifest.object_id,
            persona_id=manifest.persona_id,
            template_id=getattr(manifest, "template_id", "") or "",
            mode=manifest.mode.value if isinstance(manifest.mode, CallMode) else str(manifest.mode),
            direction=manifest.direction,
            language=manifest.language,
            status=CallStatus.RINGING.value,
            kind=getattr(manifest, "kind", "") or "",
            target_lang=getattr(manifest, "target_lang", "") or "",
        )
        self.session.add(call)
        self.session.commit()
        return self._call_to_dict(call)

    def get_call(self, call_id: str) -> dict | None:
        call = self.session.get(models.CallSession, call_id)
        return self._call_to_dict(call) if call else None

    def update_call(self, call_id: str, **fields) -> dict | None:
        row = self.session.get(models.CallSession, call_id)
        if not row:
            return None
        for key, value in fields.items():
            if hasattr(row, key):
                setattr(row, key, value)
        self.session.commit()
        return self._call_to_dict(row)

    def delete_call(self, call_id: str) -> bool:
        """删除通话及连带数据(turns/settlements)。审计事件保留(只读历史)。"""
        row = self.session.get(models.CallSession, call_id)
        if not row:
            return False
        # 连带清掉该通话的转写与结算,避免孤儿行。
        for t in self.session.scalars(select(models.Turn).filter_by(call_id=call_id)):
            self.session.delete(t)
        for s in self.session.scalars(select(models.Settlement).filter_by(call_id=call_id)):
            self.session.delete(s)
        self.session.delete(row)
        self.session.commit()
        return True

    def list_calls(self, account_id: str, status: str = "") -> list[dict]:
        stmt = select(models.CallSession)
        if account_id:
            stmt = stmt.filter_by(account_id=account_id)
        if status:
            stmt = stmt.filter_by(status=status)
        return [self._call_to_dict(c) for c in self.session.scalars(stmt)]

    def create_turn(self, turn: TurnEvent) -> dict:
        row = models.Turn(
            id=f"{turn.call_id}:{turn.turn_id}",
            call_id=turn.call_id,
            turn_id=turn.turn_id,
            role=turn.role,
            transcript=turn.transcript,
            emotion=turn.emotion,
            provider=turn.provider,
            latency_ms=turn.latency_ms,
            language=turn.language,
        )
        try:
            self.session.add(row)
            self.session.commit()
            return {"id": row.id}
        except IntegrityError:
            # 并发 add_turn 可能算出相同 turn_id（t0/t1...）；幂等返回已存在行，
            # 并回滚坏事务，避免后续请求全部 500。
            self.session.rollback()
            existing = self.session.get(models.Turn, row.id)
            if existing is not None:
                return {"id": existing.id, "duplicate": True}
            raise

    def get_turns(self, call_id: str) -> list[TurnEvent]:
        stmt = select(models.Turn).filter_by(call_id=call_id).order_by(models.Turn.created_at)
        rows = self.session.scalars(stmt)
        return [
            TurnEvent(
                trace_id=call_id,
                call_id=row.call_id,
                turn_id=row.turn_id,
                role=row.role,
                transcript=row.transcript,
                emotion=row.emotion,
                provider=row.provider,
                latency_ms=row.latency_ms,
                language=row.language,
                created_at=row.created_at.isoformat() if row.created_at else "",
            )
            for row in rows
        ]

    def turn_stats(self) -> dict[str, dict]:
        """每通通话的轮数/延迟聚合（审计闭环:列表页一屏可见健康度）。"""
        from sqlalchemy import func

        rows = self.session.execute(
            select(
                models.Turn.call_id,
                func.count(models.Turn.id),
                func.avg(models.Turn.latency_ms),
            ).group_by(models.Turn.call_id)
        ).all()
        return {
            call_id: {"turns": n, "avg_latency_ms": int(float(avg or 0))}
            for call_id, n, avg in rows
        }

    # ---- 快答库(Q→A 快路,2026-09-09) ----

    @staticmethod
    def _qa_to_dict(row) -> dict:
        return {
            "id": row.id,
            "account_id": row.account_id,
            "question_text": row.question_text,
            "answer_text": row.answer_text,
            "lang": row.lang,
            "scope": row.scope,
            "step_index": int(row.step_index or -1),
            "voice_id": row.voice_id,
            "enabled": bool(row.enabled),
            "hit_count": int(row.hit_count or 0),
            "source": row.source,
            "template_id": row.template_id,
            "created_at": row.created_at.isoformat() if row.created_at else "",
        }

    def list_qa_entries(self, account_id: str = "", enabled: bool | None = None) -> list[dict]:
        stmt = select(models.QaEntry).order_by(models.QaEntry.created_at)
        if account_id:
            stmt = stmt.filter_by(account_id=account_id)
        if enabled is not None:
            stmt = stmt.filter_by(enabled=enabled)
        return [self._qa_to_dict(r) for r in self.session.scalars(stmt)]

    def create_qa_entry(self, data: dict) -> dict:
        row = models.QaEntry(
            id=data.get("id") or f"qa:{uuid.uuid4().hex[:12]}",
            account_id=data.get("account_id") or "acc-001",
            question_text=data.get("question_text") or "",
            answer_text=data.get("answer_text") or "",
            lang=data.get("lang") or "zh",
            scope=data.get("scope") or "global",
            step_index=int(data.get("step_index") or -1),
            voice_id=data.get("voice_id") or "",
            enabled=bool(data.get("enabled", True)),
            source=data.get("source") or "curated",
            template_id=data.get("template_id") or "",
        )
        self.session.add(row)
        self.session.commit()
        return self._qa_to_dict(row)

    def update_qa_entry(self, entry_id: str, patch: dict) -> dict | None:
        row = self.session.get(models.QaEntry, entry_id)
        if row is None:
            return None
        if "question_text" in patch and patch["question_text"] is not None:
            row.question_text = str(patch["question_text"])
        if "answer_text" in patch and patch["answer_text"] is not None:
            row.answer_text = str(patch["answer_text"])
        if "lang" in patch and patch["lang"]:
            row.lang = str(patch["lang"])
        if "scope" in patch and patch["scope"]:
            row.scope = str(patch["scope"])
        if "step_index" in patch and patch["step_index"] is not None:
            row.step_index = int(patch["step_index"])
        if "voice_id" in patch and patch["voice_id"] is not None:
            row.voice_id = str(patch["voice_id"])
        if "enabled" in patch and patch["enabled"] is not None:
            row.enabled = bool(patch["enabled"])
        self.session.commit()
        return self._qa_to_dict(row)

    def delete_qa_entry(self, entry_id: str) -> bool:
        row = self.session.get(models.QaEntry, entry_id)
        if row is None:
            return False
        self.session.delete(row)
        self.session.commit()
        return True

    def incr_qa_hit(self, entry_id: str, n: int = 1) -> None:
        row = self.session.get(models.QaEntry, entry_id)
        if row is not None:
            row.hit_count = int(row.hit_count or 0) + int(n)
            self.session.commit()

    def iter_call_conversations(self, account_id: str = "") -> list[list[dict]]:
        """跨通话按序轮次(高频问答对挖掘用):join calls 过账号,created_at 排序。

        turns 无 seq 列,同通内轮次天然串行、同刻风险极低(created_at 排序足够)。
        """
        stmt = (
            select(models.Turn, models.CallSession.account_id)
            .join(models.CallSession, models.Turn.call_id == models.CallSession.id)
            .order_by(models.Turn.call_id, models.Turn.created_at)
        )
        if account_id:
            stmt = stmt.where(models.CallSession.account_id == account_id)
        grouped: dict[str, list[dict]] = {}
        for row, _acct in self.session.execute(stmt):
            grouped.setdefault(row.call_id, []).append(
                {"role": row.role, "text": row.transcript, "lang": row.language}
            )
        return list(grouped.values())

    def append_template_revision(self, template_id: str, revision: int, snapshot: str) -> dict:
        row = models.TemplateRevision(
            id=f"rev:{template_id}:{revision}", template_id=template_id, revision=revision, snapshot=snapshot
        )
        try:
            self.session.add(row)
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            return {"id": row.id, "duplicate": True}
        return {"id": row.id, "revision": revision}

    def list_template_revisions(self, template_id: str) -> list[dict]:
        stmt = (
            select(models.TemplateRevision)
            .filter_by(template_id=template_id)
            .order_by(models.TemplateRevision.revision)
        )
        return [
            {"revision": r.revision, "snapshot": r.snapshot, "updated_at": r.updated_at.isoformat()}
            for r in self.session.scalars(stmt)
        ]

    def update_object_digest(self, object_id: str, digest: str) -> bool:
        obj = self.session.get(models.ObjectProfile, object_id)
        if obj is None:
            return False
        obj.digest = digest
        self.session.commit()
        return True

    def get_usage_record(self, call_id: str) -> dict | None:
        row = self.session.get(models.UsageRecord, f"usage:{call_id}")
        if not row:
            return None
        return {"id": row.id, "call_id": row.call_id, "tokens": row.tokens}

    def get_settlement(self, call_id: str) -> dict | None:
        row = self.session.get(models.Settlement, call_id)
        if not row:
            return None
        return {
            "call_id": row.call_id,
            "status": row.status,
            "metrics": json.loads(row.metrics_json or "{}"),
            "summary": row.summary,
            "transcript_doc_path": row.transcript_doc_path,
            "settlement_doc_path": row.settlement_doc_path,
            "new_topics": json.loads(row.new_topics_json or "[]"),
            "global_insight_id": row.global_insight_id,
            "error": row.error,
        }

    def append_settlement(self, call_id: str, result: dict) -> dict:
        row = self.session.get(models.Settlement, call_id)
        if row is None:
            row = models.Settlement(id=call_id, call_id=call_id)
            self.session.add(row)
        row.status = result.get("status", row.status)
        row.metrics_json = json.dumps(result.get("metrics", {}), ensure_ascii=False)
        row.summary = result.get("summary", row.summary or "")
        row.transcript_doc_path = result.get("transcript_doc_path", row.transcript_doc_path)
        row.settlement_doc_path = result.get("settlement_doc_path", row.settlement_doc_path)
        row.new_topics_json = json.dumps(result.get("new_topics", []), ensure_ascii=False)
        row.global_insight_id = result.get("global_insight_id", row.global_insight_id)
        row.error = result.get("error", row.error)
        self.session.commit()
        return {"call_id": call_id, "status": row.status}

    # ---- objects / personas ----

    def list_objects(self, account_id: str) -> list[dict]:
        stmt = select(models.ObjectProfile).filter_by(account_id=account_id)
        return [self._to_dict(o) for o in self.session.scalars(stmt)]

    def create_object(self, account_id: str, data: dict) -> dict:
        obj = models.ObjectProfile(
            id=data.get("id") or _uuid(),
            account_id=account_id,
            display_name=data.get("display_name", ""),
            role_template=data.get("role_template", "customer"),
            language=data.get("language", "zh"),
            background=data.get("background", ""),
            phone=data.get("phone", ""),
            tracking_no=data.get("tracking_no", ""),
            courier=data.get("courier", ""),
            address=data.get("address", ""),
            contact_channel=data.get("contact_channel", ""),
            template_id=data.get("template_id", ""),
            status=data.get("status", "active"),
        )
        self.session.add(obj)
        self.session.commit()
        return self._to_dict(obj)

    def get_object(self, object_id: str) -> dict | None:
        obj = self.session.get(models.ObjectProfile, object_id)
        return self._to_dict(obj) if obj else None

    def update_object(self, object_id: str, data: dict) -> dict | None:
        obj = self.session.get(models.ObjectProfile, object_id)
        if not obj:
            return None
        allowed = {"display_name", "role_template", "language", "background", "phone", "tracking_no", "courier", "address", "contact_channel", "template_id", "status"}
        for key, value in data.items():
            if key in allowed and hasattr(obj, key):
                setattr(obj, key, value)
        self.session.commit()
        return self._to_dict(obj)

    def delete_object(self, object_id: str) -> bool:
        obj = self.session.get(models.ObjectProfile, object_id)
        if not obj:
            return False
        self.session.delete(obj)
        self.session.commit()
        return True

    def create_persona(self, data: dict) -> dict:
        persona = models.PersonaProfile(
            id=data.get("id") or _uuid(),
            account_id=data.get("account_id", ""),
            name=data.get("name", ""),
            company=data.get("company", ""),
            tone=data.get("tone", ""),
            language=data.get("language", "zh"),
            reference_audio=data.get("reference_audio", ""),
            tts_provider=data.get("tts_provider", ""),
        )
        self.session.add(persona)
        self.session.commit()
        return self._to_dict(persona)

    def update_persona(self, persona_id: str, data: dict) -> dict | None:
        persona = self.session.get(models.PersonaProfile, persona_id)
        if not persona:
            return None
        allowed = {"account_id", "name", "company", "tone", "language", "reference_audio", "tts_provider"}
        for key, value in data.items():
            if key in allowed and hasattr(persona, key):
                setattr(persona, key, value)
        self.session.commit()
        return self._to_dict(persona)

    def delete_persona(self, persona_id: str) -> bool:
        persona = self.session.get(models.PersonaProfile, persona_id)
        if not persona:
            return False
        self.session.delete(persona)
        self.session.commit()
        return True

    def get_persona(self, persona_id: str) -> dict | None:
        persona = self.session.get(models.PersonaProfile, persona_id)
        return self._to_dict(persona) if persona else None

    def list_personas(self, account_id: str = "") -> list[dict]:
        stmt = select(models.PersonaProfile)
        if account_id:
            stmt = stmt.filter_by(account_id=account_id)
        return [self._to_dict(p) for p in self.session.scalars(stmt)]

    def list_templates(self, account_id: str) -> list[dict]:
        stmt = select(models.ConversationTemplate).filter_by(account_id=account_id)
        return [self._to_dict(t) for t in self.session.scalars(stmt)]

    def create_template(self, data: dict) -> dict:
        tpl = models.ConversationTemplate(
            id=data.get("id") or _uuid(),
            account_id=data.get("account_id", ""),
            name=data.get("name", ""),
            opening=data.get("opening", ""),
            core=data.get("core", ""),
            objection=data.get("objection", ""),
            closing=data.get("closing", ""),
            tone_override=data.get("tone_override", ""),
            language=data.get("language", "zh"),
            steps_json=data.get("steps_json", ""),
            hotwords=data.get("hotwords", ""),
        )
        self.session.add(tpl)
        self.session.commit()
        return self._to_dict(tpl)

    def get_template(self, template_id: str) -> dict | None:
        tpl = self.session.get(models.ConversationTemplate, template_id)
        return self._to_dict(tpl) if tpl else None

    def update_template(self, template_id: str, data: dict) -> dict | None:
        tpl = self.session.get(models.ConversationTemplate, template_id)
        if not tpl:
            return None
        allowed = {"account_id", "name", "opening", "core", "objection", "closing", "tone_override", "language", "steps_json", "hotwords"}
        for key, value in data.items():
            if key in allowed and hasattr(tpl, key):
                setattr(tpl, key, value)
        self.session.commit()
        return self._to_dict(tpl)

    def delete_template(self, template_id: str) -> bool:
        tpl = self.session.get(models.ConversationTemplate, template_id)
        if not tpl:
            return False
        self.session.delete(tpl)
        # 同步清空引用该模板的对象卡，避免悬空引用（agent 侧 get_template 404 会静默降级）。
        objs = self.session.scalars(select(models.ObjectProfile).filter_by(template_id=template_id)).all()
        for obj in objs:
            obj.template_id = ""
        self.session.commit()
        return True

    def list_object_topics(self, object_id: str) -> list[dict]:
        stmt = select(models.ObjectTopic).filter_by(object_id=object_id)
        return [self._to_dict(t) for t in self.session.scalars(stmt)]

    def append_object_topics(self, object_id: str, account_id: str, topics: list[dict]) -> dict:
        created = 0
        for t in topics:
            topic = models.ObjectTopic(
                id=_uuid(),
                object_id=object_id,
                account_id=account_id,
                topic=t.get("topic", ""),
                summary=t.get("summary", ""),
            )
            self.session.add(topic)
            created += 1
        self.session.commit()
        return {"count": created}

    def list_global_insights(self, kind: str = "") -> list[dict]:
        stmt = select(models.GlobalInsight)
        if kind:
            stmt = stmt.filter_by(kind=kind)
        return [self._to_dict(g) for g in self.session.scalars(stmt)]

    def append_global_insight(self, insight: dict) -> dict:
        row = models.GlobalInsight(
            id=_uuid(),
            kind=insight.get("kind", "insight"),
            statement=insight.get("statement", ""),
            confidence=float(insight.get("confidence", 0.0)),
            language=insight.get("language", "zh"),
            status=insight.get("status", "active"),
        )
        self.session.add(row)
        self.session.commit()
        return self._to_dict(row)

    def get_settings(self) -> dict:
        row = self.session.get(models.GlobalSetting, "global")
        if not row:
            return self.default_settings()
        return {
            "asr": json.loads(row.asr_json or "{}"),
            "llm": json.loads(row.llm_json or "{}"),
            "tts": json.loads(row.tts_json or "{}"),
            "vad": json.loads(row.vad_json or "{}"),
            "policy": row.policy,
        }

    def save_settings(self, settings: dict) -> dict:
        row = self.session.get(models.GlobalSetting, "global")
        if row is None:
            row = models.GlobalSetting(id="global")
            self.session.add(row)
        row.asr_json = json.dumps(settings.get("asr", {}), ensure_ascii=False)
        row.llm_json = json.dumps(settings.get("llm", {}), ensure_ascii=False)
        row.tts_json = json.dumps(settings.get("tts", {}), ensure_ascii=False)
        row.vad_json = json.dumps(settings.get("vad", {}), ensure_ascii=False)
        row.policy = settings.get("policy", row.policy or "offline_first")
        self.session.commit()
        return self.get_settings()

    # ---- audit trail ----

    def append_audit(self, event: dict) -> dict:
        row = models.AuditEventRecord(
            id=event.get("event_id", _uuid()),
            ts=event.get("ts", ""),
            action=event.get("action", ""),
            subject_type=event.get("subject_type", ""),
            subject_id=event.get("subject_id", ""),
            actor=event.get("actor", ""),
            outcome=event.get("outcome", "ok"),
            detail_json=json.dumps(event.get("detail", {}), ensure_ascii=False),
            request_id=event.get("request_id", ""),
            call_id=event.get("call_id", ""),
            account_id=event.get("account_id", ""),
            object_id=event.get("object_id", ""),
            persona_id=event.get("persona_id", ""),
        )
        self.session.add(row)
        self.session.commit()
        return {"id": row.id, "action": row.action}

    def list_audit_events(self, *, account_id: str = "", action: str = "", call_id: str = "", limit: int = 200) -> list[dict]:
        stmt = select(models.AuditEventRecord)
        if account_id:
            stmt = stmt.filter_by(account_id=account_id)
        if action:
            stmt = stmt.filter_by(action=action)
        if call_id:
            stmt = stmt.filter_by(call_id=call_id)
        stmt = stmt.order_by(models.AuditEventRecord.ts.desc()).limit(limit)
        out = []
        for row in self.session.scalars(stmt):
            item = {c.name: getattr(row, c.name) for c in models.AuditEventRecord.__table__.columns}
            item["detail"] = json.loads(item.pop("detail_json", "{}"))
            out.append(item)
        return out

    @staticmethod
    def default_settings() -> dict:
        return {
            # ASR：agent 只消费 provider/base_url；model/backend 是误导性死配置，不再下发。
            # language 仅在 language_mode=fixed 时生效（钉死识别语言）；
            # language_mode: auto=锚定+滞回跟随(默认) | fixed=钉死 language。
            "asr": {
                "provider": "qwen3_asr",
                "base_url": "http://127.0.0.1:8787",
                "language_mode": "auto",
                "language": "",
            },
            # Packaged/dev default: local OpenAI-compatible LLM on :1235
            # (mlx_lm on macOS, llama-server on Windows). Zero-Ollama.
            # local_model: 本地模型选择(ML Studio repo),bok serve 据此起 :1235 模型;空用默认。
            "llm": {"provider": "local_openai", "model": "", "base_url": "http://127.0.0.1:1235/v1", "local_model": ""},
            # TTS：provider= qwen3_tts | volcano_streaming | fake；音色按语言
            # speaker_zh/speaker_cantonese/en（persona 绑定音色优先；旧键已由启动迁移改写）。
            # voice_mode: single=整场同声(默认,collapse 成主音色) | per_language=分语言键逐轮切换。
            "tts": {
                "provider": "qwen3_tts",
                "base_url": "http://127.0.0.1:8788",
                "voice_mode": "single",
                "speaker_zh": "",
                "speaker_cantonese": "",
                "speaker_en": "",
                "instruct": "",
                "sample_rate": 24000,
            },
            # VAD/打断：agent 运行时会读取（不再走环境变量），interruption 控制打断开关。
            # min_silence_duration 0.45：离线式 ASR 从停嘴到转写回来需 ~0.5-1.2s，
            # 端点判定要等得起它；低于 ~0.3 会「转写晚于轮次提交」被丢弃 → agent 不回话。
            "vad": {
                "provider": "silero",
                "max_buffered_speech": 15.0,
                "min_speech_duration": 0.15,
                "min_silence_duration": 0.45,
                "interruption": True,
            },
            "policy": "offline_first",
        }

    @staticmethod
    def _call_to_dict(call: models.CallSession) -> dict:
        return {c.name: getattr(call, c.name) for c in models.CallSession.__table__.columns}

    @staticmethod
    def _to_dict(obj: Any) -> dict:
        return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


class InMemoryBusinessRepository:
    """Test double implementing BusinessRepository without a database."""

    def __init__(self) -> None:
        self.calls: dict[str, dict] = {}
        self.turns: dict[str, list[TurnEvent]] = {}
        self.usage_records: dict[str, dict] = {}
        self.template_revisions: dict[str, list[dict]] = {}
        self.settlements: dict[str, dict] = {}
        self.objects: dict[str, dict] = {}
        self.personas: dict[str, dict] = {}
        self.templates: dict[str, dict] = {}
        self.object_topics: dict[str, list[dict]] = {}
        self.global_insights: list[dict] = []
        self.audit_events: list[dict] = []
        self.qa_entries: dict[str, dict] = {}
        self.settings: dict = SqlAlchemyBusinessRepository.default_settings()

    def create_call(self, manifest: SessionManifest) -> dict:
        call_id = manifest.session_id or _uuid()
        self.calls[call_id] = {
            "id": call_id,
            "account_id": manifest.account_id,
            "object_id": manifest.object_id,
            "persona_id": manifest.persona_id,
            "template_id": getattr(manifest, "template_id", "") or "",
            "mode": manifest.mode.value if isinstance(manifest.mode, CallMode) else str(manifest.mode),
            "status": CallStatus.RINGING.value,
            "whatsapp_status": "",
            "customer_whatsapp": "",
            "kind": getattr(manifest, "kind", "") or "",
            "target_lang": getattr(manifest, "target_lang", "") or "",
        }
        return self.calls[call_id]

    def get_call(self, call_id: str) -> dict | None:
        return self.calls.get(call_id)

    def update_call(self, call_id: str, **fields) -> dict | None:
        if call_id not in self.calls:
            return None
        self.calls[call_id].update(fields)
        return self.calls[call_id]

    def delete_call(self, call_id: str) -> bool:
        if call_id not in self.calls:
            return False
        del self.calls[call_id]
        self.turns.pop(call_id, None)
        self.settlements.pop(call_id, None)
        return True

    def list_calls(self, account_id: str, status: str = "") -> list[dict]:
        return [
            c for c in self.calls.values()
            if (not account_id or c["account_id"] == account_id) and (not status or c["status"] == status)
        ]

    def create_turn(self, turn: TurnEvent) -> dict:
        from datetime import datetime, timezone

        if not turn.created_at:
            turn.created_at = datetime.now(timezone.utc).isoformat()
        self.turns.setdefault(turn.call_id, []).append(turn)
        return {"id": f"{turn.call_id}:{turn.turn_id}"}

    def get_turns(self, call_id: str) -> list[TurnEvent]:
        return list(self.turns.get(call_id, []))

    def turn_stats(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for call_id, turns in self.turns.items():
            lats = [t.latency_ms for t in turns if t.latency_ms]
            out[call_id] = {
                "turns": len(turns),
                "avg_latency_ms": int(sum(lats) / len(lats)) if lats else 0,
            }
        return out

    # ---- 快答库(Q→A 快路,2026-09-09) ----

    def list_qa_entries(self, account_id: str = "", enabled: bool | None = None) -> list[dict]:
        rows = [
            v
            for v in getattr(self, "qa_entries", {}).values()
            if (not account_id or v.get("account_id") == account_id)
            and (enabled is None or bool(v.get("enabled")) == enabled)
        ]
        return sorted(rows, key=lambda v: v.get("created_at") or "")

    def create_qa_entry(self, data: dict) -> dict:
        if not hasattr(self, "qa_entries"):
            self.qa_entries = {}
        row = {
            "id": data.get("id") or f"qa:{uuid.uuid4().hex[:12]}",
            "account_id": data.get("account_id") or "acc-001",
            "question_text": data.get("question_text") or "",
            "answer_text": data.get("answer_text") or "",
            "lang": data.get("lang") or "zh",
            "scope": data.get("scope") or "global",
            "step_index": int(data.get("step_index") or -1),
            "voice_id": data.get("voice_id") or "",
            "enabled": bool(data.get("enabled", True)),
            "hit_count": int(data.get("hit_count") or 0),
            "source": data.get("source") or "curated",
            "template_id": data.get("template_id") or "",
            "created_at": data.get("created_at") or "",
        }
        self.qa_entries[row["id"]] = row
        return dict(row)

    def update_qa_entry(self, entry_id: str, patch: dict) -> dict | None:
        row = getattr(self, "qa_entries", {}).get(entry_id)
        if row is None:
            return None
        for k in ("question_text", "answer_text", "lang", "scope", "step_index", "voice_id", "enabled"):
            if k in patch and patch[k] is not None:
                row[k] = patch[k]
        return dict(row)

    def delete_qa_entry(self, entry_id: str) -> bool:
        return getattr(self, "qa_entries", {}).pop(entry_id, None) is not None

    def incr_qa_hit(self, entry_id: str, n: int = 1) -> None:
        row = getattr(self, "qa_entries", {}).get(entry_id)
        if row is not None:
            row["hit_count"] = int(row.get("hit_count") or 0) + int(n)

    def iter_call_conversations(self, account_id: str = "") -> list[list[dict]]:
        out: list[list[dict]] = []
        for call_id, turns in self.turns.items():
            if account_id:
                call = self.calls.get(call_id) or {}
                if call.get("account_id") != account_id:
                    continue
            out.append([{"role": t.role, "text": t.transcript, "lang": t.language} for t in turns])
        return out

    def get_usage_record(self, call_id: str) -> dict | None:
        return self.usage_records.get(call_id)

    def append_template_revision(self, template_id: str, revision: int, snapshot: str) -> dict:
        row = {"revision": revision, "snapshot": snapshot, "updated_at": ""}
        self.template_revisions.setdefault(template_id, []).append(row)
        return {"revision": revision}

    def list_template_revisions(self, template_id: str) -> list[dict]:
        return list(self.template_revisions.get(template_id, []))

    def update_object_digest(self, object_id: str, digest: str) -> bool:
        if object_id in self.objects:
            self.objects[object_id]["digest"] = digest
            return True
        return False

    def create_usage_record(self, record: dict) -> dict:
        self.usage_records[record.get("call_id", "")] = record
        return record

    def get_settlement(self, call_id: str) -> dict | None:
        return self.settlements.get(call_id)

    def append_settlement(self, call_id: str, result: dict) -> dict:
        self.settlements[call_id] = result
        return {"call_id": call_id, "status": result.get("status")}

    def list_objects(self, account_id: str) -> list[dict]:
        return [o for o in self.objects.values() if o["account_id"] == account_id]

    def create_object(self, account_id: str, data: dict) -> dict:
        obj = ObjectProfile(
            id=data.get("id") or _uuid(),
            account_id=account_id,
            display_name=data.get("display_name", ""),
            role_template=data.get("role_template", "customer"),
            language=data.get("language", "zh"),
            background=data.get("background", ""),
            phone=data.get("phone", ""),
            tracking_no=data.get("tracking_no", ""),
            courier=data.get("courier", ""),
            address=data.get("address", ""),
            contact_channel=data.get("contact_channel", ""),
            template_id=data.get("template_id", ""),
            status=data.get("status", "active"),
        ).__dict__
        self.objects[obj["id"]] = obj
        return obj

    def update_object(self, object_id: str, data: dict) -> dict | None:
        if object_id not in self.objects:
            return None
        self.objects[object_id].update({k: v for k, v in data.items() if k in {"display_name", "role_template", "language", "background", "phone", "tracking_no", "courier", "address", "contact_channel", "template_id", "status"}})
        return self.objects[object_id]

    def delete_object(self, object_id: str) -> bool:
        return self.objects.pop(object_id, None) is not None

    def create_persona(self, data: dict) -> dict:
        persona = PersonaProfile(
            id=data.get("id") or _uuid(),
            account_id=data.get("account_id", ""),
            name=data.get("name", ""),
            company=data.get("company", ""),
            tone=data.get("tone", ""),
            language=data.get("language", "zh"),
            reference_audio=data.get("reference_audio", ""),
            tts_provider=data.get("tts_provider", ""),
        ).__dict__
        self.personas[persona["id"]] = persona
        return persona

    def update_persona(self, persona_id: str, data: dict) -> dict | None:
        if persona_id not in self.personas:
            return None
        self.personas[persona_id].update({k: v for k, v in data.items() if k in {"account_id", "name", "company", "tone", "language", "reference_audio", "tts_provider"}})
        return self.personas[persona_id]

    def delete_persona(self, persona_id: str) -> bool:
        return self.personas.pop(persona_id, None) is not None

    def get_object(self, object_id: str) -> dict | None:
        return self.objects.get(object_id)

    def get_persona(self, persona_id: str) -> dict | None:
        return self.personas.get(persona_id)

    def list_personas(self, account_id: str = "") -> list[dict]:
        return [
            p for p in self.personas.values()
            if not account_id or p.get("account_id", "") == account_id
        ]

    def list_templates(self, account_id: str) -> list[dict]:
        return [t for t in self.templates.values() if t.get("account_id", "") == account_id]

    def create_template(self, data: dict) -> dict:
        tpl = ConversationTemplate(
            id=data.get("id") or _uuid(),
            account_id=data.get("account_id", ""),
            name=data.get("name", ""),
            opening=data.get("opening", ""),
            core=data.get("core", ""),
            objection=data.get("objection", ""),
            closing=data.get("closing", ""),
            tone_override=data.get("tone_override", ""),
            language=data.get("language", "zh"),
            steps_json=data.get("steps_json", ""),
            hotwords=data.get("hotwords", ""),
        ).__dict__
        self.templates[tpl["id"]] = tpl
        return tpl

    def get_template(self, template_id: str) -> dict | None:
        return self.templates.get(template_id)

    def update_template(self, template_id: str, data: dict) -> dict | None:
        if template_id not in self.templates:
            return None
        self.templates[template_id].update({k: v for k, v in data.items() if k in {"account_id", "name", "opening", "core", "objection", "closing", "tone_override", "language", "steps_json", "hotwords"}})
        return self.templates[template_id]

    def delete_template(self, template_id: str) -> bool:
        if self.templates.pop(template_id, None) is None:
            return False
        # 同步清空引用该模板的对象卡，避免悬空引用。
        for obj in self.objects.values():
            if obj.get("template_id") == template_id:
                obj["template_id"] = ""
        return True

    def list_object_topics(self, object_id: str) -> list[dict]:
        return [t for t in self.object_topics.get(object_id, [])]

    def append_object_topics(self, object_id: str, account_id: str, topics: list[dict]) -> dict:
        key = object_id
        self.object_topics.setdefault(key, [])
        for t in topics:
            self.object_topics[key].append({
                "id": _uuid(),
                "object_id": object_id,
                "account_id": account_id,
                "topic": t.get("topic", ""),
                "summary": t.get("summary", ""),
            })
        return {"count": len(topics)}

    def list_global_insights(self, kind: str = "") -> list[dict]:
        return [g for g in self.global_insights if not kind or g.get("kind") == kind]

    def append_global_insight(self, insight: dict) -> dict:
        row = {
            "id": _uuid(),
            "kind": insight.get("kind", "insight"),
            "statement": insight.get("statement", ""),
            "confidence": float(insight.get("confidence", 0.0)),
            "language": insight.get("language", "zh"),
            "status": insight.get("status", "active"),
        }
        self.global_insights.append(row)
        return row

    def get_settings(self) -> dict:
        return self.settings

    def save_settings(self, settings: dict) -> dict:
        self.settings = {
            "asr": settings.get("asr", {}),
            "llm": settings.get("llm", {}),
            "tts": settings.get("tts", {}),
            "vad": settings.get("vad", {}),
            "policy": settings.get("policy", "offline_first"),
        }
        return self.settings

    def append_audit(self, event: dict) -> dict:
        self.audit_events.insert(0, event)
        return {"id": event.get("event_id", _uuid()), "action": event.get("action", "")}

    def list_audit_events(self, *, account_id: str = "", action: str = "", call_id: str = "", limit: int = 200) -> list[dict]:
        items = self.audit_events
        if account_id:
            items = [e for e in items if e.get("account_id") == account_id]
        if action:
            items = [e for e in items if e.get("action") == action]
        if call_id:
            items = [e for e in items if e.get("call_id") == call_id]
        return items[:limit]
