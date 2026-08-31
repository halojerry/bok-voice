from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("BOK_SERVICE", "test-obs")

from bok_voice_obs.audit import AuditStore, record_audit
from bok_voice_obs.context import Correlation, clear_correlation, get_correlation, set_correlation
from bok_voice_obs.logging import configure_logging, get_logger, reset_logging


def test_correlation_context_flows_to_log_and_audit(tmp_path: Path):
    reset_logging()
    log_dir = tmp_path / "logs"
    configure_logging(log_dir=log_dir, console=False, file=True)

    set_correlation(Correlation.new(request_id="req-abc", call_id="call-1", account_id="acc-1"))
    logger = get_logger("test", component="test")
    logger.info("hello", extra={"event": "test.event", "data": {"k": "v"}})

    # The JSONL line should carry the request/call/account correlation + stable fields.
    line = json.loads((log_dir / "app.jsonl").read_text().strip().splitlines()[-1])
    assert line["request_id"] == "req-abc"
    assert line["call_id"] == "call-1"
    assert line["account_id"] == "acc-1"
    assert line["service"] == "test-obs"
    assert line["component"] == "test"
    assert line["event"] == "test.event"
    assert line["data"] == {"k": "v"}
    clear_correlation()
    reset_logging()


def test_audit_store_writes_jsonl_and_respects_correlation(tmp_path: Path):
    set_correlation(Correlation(request_id="req-xyz", call_id="call-9", account_id="acc-9"))
    store = AuditStore(directory=tmp_path / "audit")

    ev = store.emit(action="voice.clone", subject_type="tts_voice", subject_id="v1", detail={"lang": "yue"})
    path = tmp_path / "audit" / f"{ev.to_dict()['ts'][:10]}.jsonl"
    assert path.exists()
    payload = json.loads(path.read_text().strip())
    assert payload["action"] == "voice.clone"
    assert payload["request_id"] == "req-xyz"
    assert payload["call_id"] == "call-9"
    assert payload["account_id"] == "acc-9"
    assert payload["subject_id"] == "v1"
    clear_correlation()


def test_record_audit_defaults_actor_and_outcome(tmp_path: Path):
    clear_correlation()
    store = AuditStore(directory=tmp_path / "audit")
    ev = record_audit("template.update", subject_type="template", subject_id="t1", name="开场话术")
    payload = ev.to_dict()
    assert payload["actor"] == "system"
    assert payload["outcome"] == "ok"
    assert payload["detail"]["name"] == "开场话术"
