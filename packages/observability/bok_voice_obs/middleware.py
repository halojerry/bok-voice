from __future__ import annotations

import time
import uuid
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .context import Correlation, clear_correlation, set_correlation
from .logging import get_logger


log = get_logger("http")


def _norm(value: str | None) -> str:
    return (value or "").strip()[:128]


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Inject a request-scoped correlation context and audit each HTTP call.

    The middlewares read and propagate well-known headers so a browser call and
    its backend agent work share the same identity:

      X-Request-ID, X-Call-ID, X-Account-ID, X-Object-ID, X-Persona-ID

    If a request ID is absent a fresh one is minted; the request and response
    both carry it so the front-end can trace a turn end-to-end.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        incoming = request.headers
        request_id = _norm(incoming.get("x-request-id")) or uuid.uuid4().hex[:16]
        corr = Correlation(
            request_id=request_id,
            call_id=_norm(incoming.get("x-call-id")),
            account_id=_norm(incoming.get("x-account-id")),
            object_id=_norm(incoming.get("x-object-id")),
            persona_id=_norm(incoming.get("x-persona-id")),
            user_id=_norm(incoming.get("x-user-id")),
            span_id=uuid.uuid4().hex[:12],
        )
        set_correlation(corr)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            if "x-request-id" not in response.headers:
                response.headers["x-request-id"] = request_id
            data = {
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(duration_ms, 2),
            }
            log.info(
                "http_request",
                extra={"event": "http.request", "component": "http", "data": data},
            )
            clear_correlation()
        return response
