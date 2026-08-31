from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .context import get_correlation


def _service_name() -> str:
    return os.environ.get("BOK_SERVICE", "bok")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line with stable, auditable fields.

    Every record carries the request/call/account correlation fields plus a
    `component` and an optional `event` discriminator so logs can be filtered,
    correlated and replayed without parsing prose.
    """

    def format(self, record: logging.LogRecord) -> str:
        corr = get_correlation()
        record_name = record.name or ""
        # Strip the "<service>." prefix so component reads as e.g. "asr" not "bok.asr".
        component = getattr(record, "component", None) or record_name.split(".", 1)[-1]
        service = getattr(record, "service", None) or _service_name()
        payload: dict[str, Any] = {
            "ts": _utcnow(),
            "level": record.levelname,
            "service": service,
            "component": component,
            "message": record.getMessage(),
        }
        if getattr(record, "event", None):
            payload["event"] = record.event
        clean = corr.as_fields()
        if clean:
            payload.update(clean)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "data", None)
        if isinstance(extra, dict):
            payload["data"] = {k: v for k, v in extra.items() if v is not None}
        return json.dumps(payload, ensure_ascii=False, default=str)


_LOCK = threading.Lock()
_LOG_DIR: Path | None = None
_HANDLERS: list[logging.Handler] = []


def _default_log_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path(os.environ.get("HOME", ".")) / "Library" / "Application Support"
    return base / "BokVoice" / "logs"


def _attach(handler: logging.Handler) -> None:
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    _HANDLERS.append(handler)


def configure_logging(
    log_dir: Path | None = None,
    level: str | int = "INFO",
    *,
    console: bool = True,
    file: bool = True,
    rotate_bytes: int = 20 * 1024 * 1024,
    rotate_count: int = 10,
) -> Path:
    """Idempotently install JSON handlers (stream + rotating file).

    Returns the resolved log directory so callers (e.g. the desktop shell /
    ``bok`` launcher) can surface "open logs" to the user.
    """
    global _LOG_DIR
    log_dir = log_dir or _default_log_dir()
    _LOG_DIR = log_dir
    root = logging.getLogger()
    root.setLevel(level)
    if file:
        log_dir.mkdir(parents=True, exist_ok=True)
        from logging.handlers import RotatingFileHandler

        fh = RotatingFileHandler(
            log_dir / "app.jsonl",
            maxBytes=rotate_bytes,
            backupCount=rotate_count,
            encoding="utf-8",
        )
        _attach(fh)
    if console:
        sh = logging.StreamHandler(sys.stderr)
        _attach(sh)
    return log_dir


def reset_logging() -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:  # pragma: no cover
            pass
    _HANDLERS.clear()


def get_logger(name: str, *, component: str | None = None, service: str | None = None) -> logging.Logger:
    """Return a logger bound to a stable ``component`` tag.

    Using ``logger.info("...", extra={"event": "asr.final", "data": {...}})``
    keeps everything machine-readable.
    """
    logger = logging.getLogger(f"{_service_name()}.{name}")
    logger.component = component or name
    if service:
        logger.service = service
    return logger
