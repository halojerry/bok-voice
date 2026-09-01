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
            mode=manifest.mode.value if isinstance(manifest.mode, CallMode) else str(manifest.mode),
            direction=manifest.direction,
            language=manifest.language,
            status=CallStatus.RINGING.value,
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
            )
            for row in rows
        ]

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
        allowed = {"display_name", "role_template", "language", "background", "phone", "template_id", "status"}
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
        )
        self.session.add(persona)
        self.session.commit()
        return self._to_dict(persona)

    def update_persona(self, persona_id: str, data: dict) -> dict | None:
        persona = self.session.get(models.PersonaProfile, persona_id)
        if not persona:
            return None
        allowed = {"account_id", "name", "company", "tone", "language", "reference_audio"}
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
        allowed = {"account_id", "name", "opening", "core", "objection", "closing", "tone_override", "language"}
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
            "asr": {
                "provider": "qwen3_asr",
                "model": "Qwen/Qwen3-ASR-0.6B",
                "base_url": "http://127.0.0.1:8787",
                "backend": "transformers",
                "language": "zh",
            },
            # Packaged/dev default: local OpenAI-compatible LLM on :1235
            # (mlx_lm on macOS, llama-server on Windows). Zero-Ollama.
            "llm": {"provider": "local_openai", "model": "", "base_url": "http://127.0.0.1:1235/v1"},
            "tts": {
                "provider": "qwen3_tts",
                "model": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
                "base_url": "http://127.0.0.1:8788",
                "sample_rate": 24000,
            },
            "vad": {"provider": "silero", "model": "silero"},
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
        self.settlements: dict[str, dict] = {}
        self.objects: dict[str, dict] = {}
        self.personas: dict[str, dict] = {}
        self.templates: dict[str, dict] = {}
        self.object_topics: dict[str, list[dict]] = {}
        self.global_insights: list[dict] = []
        self.audit_events: list[dict] = []
        self.settings: dict = SqlAlchemyBusinessRepository.default_settings()

    def create_call(self, manifest: SessionManifest) -> dict:
        call_id = manifest.session_id or _uuid()
        self.calls[call_id] = {
            "id": call_id,
            "account_id": manifest.account_id,
            "object_id": manifest.object_id,
            "persona_id": manifest.persona_id,
            "mode": manifest.mode.value if isinstance(manifest.mode, CallMode) else str(manifest.mode),
            "status": CallStatus.RINGING.value,
        }
        return self.calls[call_id]

    def get_call(self, call_id: str) -> dict | None:
        return self.calls.get(call_id)

    def update_call(self, call_id: str, **fields) -> dict | None:
        if call_id not in self.calls:
            return None
        self.calls[call_id].update(fields)
        return self.calls[call_id]

    def list_calls(self, account_id: str, status: str = "") -> list[dict]:
        return [
            c for c in self.calls.values()
            if (not account_id or c["account_id"] == account_id) and (not status or c["status"] == status)
        ]

    def create_turn(self, turn: TurnEvent) -> dict:
        self.turns.setdefault(turn.call_id, []).append(turn)
        return {"id": f"{turn.call_id}:{turn.turn_id}"}

    def get_turns(self, call_id: str) -> list[TurnEvent]:
        return list(self.turns.get(call_id, []))

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
            template_id=data.get("template_id", ""),
            status=data.get("status", "active"),
        ).__dict__
        self.objects[obj["id"]] = obj
        return obj

    def update_object(self, object_id: str, data: dict) -> dict | None:
        if object_id not in self.objects:
            return None
        self.objects[object_id].update({k: v for k, v in data.items() if k in {"display_name", "role_template", "language", "background", "phone", "template_id", "status"}})
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
        ).__dict__
        self.personas[persona["id"]] = persona
        return persona

    def update_persona(self, persona_id: str, data: dict) -> dict | None:
        if persona_id not in self.personas:
            return None
        self.personas[persona_id].update({k: v for k, v in data.items() if k in {"account_id", "name", "company", "tone", "language", "reference_audio"}})
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
        ).__dict__
        self.templates[tpl["id"]] = tpl
        return tpl

    def get_template(self, template_id: str) -> dict | None:
        return self.templates.get(template_id)

    def update_template(self, template_id: str, data: dict) -> dict | None:
        if template_id not in self.templates:
            return None
        self.templates[template_id].update({k: v for k, v in data.items() if k in {"account_id", "name", "opening", "core", "objection", "closing", "tone_override", "language"}})
        return self.templates[template_id]

    def delete_template(self, template_id: str) -> bool:
        return self.templates.pop(template_id, None) is not None

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
