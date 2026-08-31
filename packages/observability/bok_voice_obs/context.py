from __future__ import annotations

import contextvars
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Correlation:
    """A single request-scoped correlation identity.

    These fields flow through every log line and every audit event so a single
    user request, a single conversation turn, or a single call can always be
    reconstructed across all components (web -> control-plane -> agent ->
    sidecar -> model).
    """

    request_id: str = ""
    call_id: str = ""
    account_id: str = ""
    object_id: str = ""
    persona_id: str = ""
    user_id: str = ""
    span_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(cls, request_id: str | None = None, **kwargs: Any) -> "Correlation":
        return cls(
            request_id=request_id or uuid.uuid4().hex[:16],
            **{k: v for k, v in kwargs.items() if v},
        )

    def as_fields(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "request_id": self.request_id,
            "call_id": self.call_id,
            "account_id": self.account_id,
            "object_id": self.object_id,
            "persona_id": self.persona_id,
            "user_id": self.user_id,
            "span_id": self.span_id,
        }
        return {k: v for k, v in base.items() if v}


_current: contextvars.ContextVar[Correlation] = contextvars.ContextVar("bok_correlation", default=Correlation())


def set_correlation(corr: Correlation) -> None:
    _current.set(corr)


def get_correlation() -> Correlation:
    return _current.get()


def clear_correlation() -> None:
    _current.set(Correlation())


def _reset() -> None:  # pragma: no cover - helper for tests
    clear_correlation()
