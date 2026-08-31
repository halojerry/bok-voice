from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .context import get_correlation


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class AuditEvent:
    """An immutable, append-only business audit record.

    ``action`` is a short verb (e.g. ``voice.clone``, ``settle.create``,
    ``template.update``). ``subject`` is the resource being acted on and
    ``detail`` carries any context that is safe to log. Every event inherits
    the active request/call/account correlation identity.
    """

    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    ts: str = field(default_factory=_utcnow)
    action: str = ""
    subject_type: str = ""
    subject_id: str = ""
    actor: str = ""
    outcome: str = "ok"
    detail: dict[str, Any] = field(default_factory=dict)
    request_id: str = ""
    call_id: str = ""
    account_id: str = ""
    object_id: str = ""
    persona_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        corr = get_correlation()
        out = asdict(self)
        out["request_id"] = self.request_id or corr.request_id
        out["call_id"] = self.call_id or corr.call_id
        out["account_id"] = self.account_id or corr.account_id
        out["object_id"] = self.object_id or corr.object_id
        out["persona_id"] = self.persona_id or corr.persona_id
        if not out["actor"]:
            out["actor"] = "system"
        return out


def _default_audit_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path(os.environ.get("HOME", ".")) / "Library" / "Application Support"
    return base / "BokVoice" / "audit"


class AuditStore:
    """Append-only JSONLines audit trail with an optional tap.

    The primary sink is ``audit/<date>.jsonl`` (one event per line, never
    rewritten) so the trail is trivially exportable / queryable. A synchronous
    ``tap`` callback lets integrations persist the same event to SQL (see the
    control-plane wiring) without making the hot path depend on the DB.
    """

    def __init__(
        self,
        directory: Path | None = None,
        *,
        tap: Callable[[AuditEvent], None] | None = None,
    ) -> None:
        self.directory = directory or _default_audit_dir()
        self.tap = tap
        self._lock = threading.Lock()

    def emit(self, **fields: Any) -> AuditEvent:
        event = AuditEvent(**fields)
        payload = event.to_dict()
        self._lock.acquire()
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / f"{payload['ts'][:10]}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        finally:
            self._lock.release()
        if self.tap:
            try:
                self.tap(event)
            except Exception:  # pragma: no cover - tap must never break the hot path
                pass
        return event


_STORE: AuditStore | None = None


def audit_store(store: AuditStore | None = None) -> AuditStore:
    global _STORE
    if store is not None:
        _STORE = store
    if _STORE is None:
        _STORE = AuditStore()
    return _STORE


def record_audit(action: str, *, subject_type: str = "", subject_id: str = "", outcome: str = "ok", **detail: Any) -> AuditEvent:
    return audit_store().emit(
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        outcome=outcome,
        detail=detail,
    )
