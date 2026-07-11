"""Versioned task and attempt events stored beside the legacy calls ledger."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from gnomon.telemetry import Cfg


EVENT_SCHEMA_VERSION = "gnomon.event/v2"
EVENT_TYPES = {"attempt.finished", "task.finished"}
CAPTURE_STATUSES = {"complete", "python-tee", "not-captured", "unknown"}

_ATTEMPT_ONLY_FIELDS = {
    "attempt_id",
    "adapter_name",
    "adapter_version",
    "upstream_name",
    "upstream_version",
    "command_id",
    "invoked_as",
    "exit_code",
    "result_class",
    "duration_ms",
    "stdout_bytes",
    "stderr_bytes",
    "capture_status",
}
_TASK_ONLY_FIELDS = {"task_outcome", "task_outcome_source"}

_EVENTS_V2_TABLE = """
CREATE TABLE IF NOT EXISTS events_v2 (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id              TEXT    NOT NULL UNIQUE,
    schema_version        TEXT    NOT NULL,
    event_type            TEXT    NOT NULL,
    occurred_at           TEXT    NOT NULL,
    task_id               TEXT,
    attempt_id            TEXT,
    producer_name         TEXT    NOT NULL,
    producer_version      TEXT,
    adapter_name          TEXT,
    adapter_version       TEXT,
    upstream_name         TEXT,
    upstream_version      TEXT,
    command_id            TEXT,
    invoked_as            TEXT    NOT NULL DEFAULT '[]',
    exit_code             INTEGER,
    result_class          TEXT,
    duration_ms           INTEGER,
    stdout_bytes          INTEGER,
    stderr_bytes          INTEGER,
    capture_status        TEXT    NOT NULL DEFAULT 'unknown',
    caller                TEXT,
    is_tty                INTEGER,
    is_ci                 INTEGER,
    task_outcome          TEXT,
    task_outcome_source   TEXT,
    context               TEXT    NOT NULL DEFAULT '{}',
    meta                  TEXT    NOT NULL DEFAULT '{}',
    CHECK (event_type IN ('attempt.finished', 'task.finished')),
    CHECK (duration_ms IS NULL OR duration_ms >= 0),
    CHECK (stdout_bytes IS NULL OR stdout_bytes >= 0),
    CHECK (stderr_bytes IS NULL OR stderr_bytes >= 0),
    CHECK (is_tty IS NULL OR is_tty IN (0, 1)),
    CHECK (is_ci IS NULL OR is_ci IN (0, 1)),
    CHECK (capture_status IN ('complete', 'python-tee', 'not-captured', 'unknown')),
    CHECK (capture_status != 'complete' OR
           (stdout_bytes IS NOT NULL AND stderr_bytes IS NOT NULL)),
    CHECK (
        (event_type = 'attempt.finished' AND command_id IS NOT NULL AND
         task_outcome IS NULL AND task_outcome_source IS NULL)
        OR
        (event_type = 'task.finished' AND task_id IS NOT NULL AND
         task_outcome IS NOT NULL AND task_outcome_source IS NOT NULL AND
         attempt_id IS NULL AND adapter_name IS NULL AND adapter_version IS NULL AND
         upstream_name IS NULL AND upstream_version IS NULL AND command_id IS NULL AND
         invoked_as = '[]' AND exit_code IS NULL AND result_class IS NULL AND
         duration_ms IS NULL AND stdout_bytes IS NULL AND stderr_bytes IS NULL AND
         capture_status = 'unknown')
    )
);
"""

_EVENT_COLUMNS = (
    "event_id",
    "schema_version",
    "event_type",
    "occurred_at",
    "task_id",
    "attempt_id",
    "producer_name",
    "producer_version",
    "adapter_name",
    "adapter_version",
    "upstream_name",
    "upstream_version",
    "command_id",
    "invoked_as",
    "exit_code",
    "result_class",
    "duration_ms",
    "stdout_bytes",
    "stderr_bytes",
    "capture_status",
    "caller",
    "is_tty",
    "is_ci",
    "task_outcome",
    "task_outcome_source",
    "context",
    "meta",
)


def ensure_event_schema(conn: sqlite3.Connection) -> None:
    """Create the event table and indexes without changing legacy calls."""
    conn.executescript(_EVENTS_V2_TABLE)
    conn.execute("CREATE INDEX IF NOT EXISTS events_v2_task_id ON events_v2(task_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS events_v2_command_id ON events_v2(command_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS events_v2_type_time "
        "ON events_v2(event_type, occurred_at)"
    )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("event integer fields must be non-negative integers")
    return value


def _optional_bool(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int) and value in (0, 1):
        return value
    raise ValueError("event boolean fields must be bool or 0/1")


def _has_value(event: dict[str, Any], field: str) -> bool:
    return field in event and event[field] is not None


def _normalized_event(event: dict[str, Any], cfg: Cfg) -> dict[str, Any] | None:
    from gnomon.telemetry import _now_iso, detect_caller

    event_type = _optional_text(event.get("event_type"))
    if event_type not in EVENT_TYPES:
        return None
    if event.get("schema_version", EVENT_SCHEMA_VERSION) != EVENT_SCHEMA_VERSION:
        return None

    event_id = _optional_text(event.get("event_id")) or str(uuid.uuid4())
    task_id = _optional_text(event.get("task_id"))
    command_id = _optional_text(event.get("command_id"))
    task_outcome = _optional_text(event.get("task_outcome"))
    task_outcome_source = _optional_text(event.get("task_outcome_source"))
    if event_type == "attempt.finished" and command_id is None:
        return None
    if event_type == "attempt.finished" and any(
        _has_value(event, field) for field in _TASK_ONLY_FIELDS
    ):
        return None
    if event_type == "task.finished" and (
        task_id is None or task_outcome is None or task_outcome_source is None
    ):
        return None
    if event_type == "task.finished" and any(
        _has_value(event, field) for field in _ATTEMPT_ONLY_FIELDS
    ):
        return None

    attempt_id = _optional_text(event.get("attempt_id"))
    if event_type == "attempt.finished" and attempt_id is None:
        attempt_id = event_id

    invoked_as = event.get("invoked_as", [])
    if not isinstance(invoked_as, list):
        return None
    context = event.get("context", {})
    meta = event.get("meta", {})
    if not isinstance(context, dict) or not isinstance(meta, dict):
        return None

    caller = _optional_text(event.get("caller")) or detect_caller()
    capture_status = _optional_text(event.get("capture_status")) or "unknown"
    if capture_status not in CAPTURE_STATUSES:
        return None
    stdout_bytes = _optional_int(event.get("stdout_bytes"))
    stderr_bytes = _optional_int(event.get("stderr_bytes"))
    if capture_status == "complete" and (stdout_bytes is None or stderr_bytes is None):
        return None
    return {
        "event_id": event_id,
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_type": event_type,
        "occurred_at": _optional_text(event.get("occurred_at")) or _now_iso(),
        "task_id": task_id,
        "attempt_id": attempt_id,
        "producer_name": _optional_text(event.get("producer_name")) or cfg.tool,
        "producer_version": _optional_text(event.get("producer_version"))
        or _optional_text(cfg.version),
        "adapter_name": _optional_text(event.get("adapter_name")),
        "adapter_version": _optional_text(event.get("adapter_version")),
        "upstream_name": _optional_text(event.get("upstream_name")),
        "upstream_version": _optional_text(event.get("upstream_version")),
        "command_id": command_id,
        "invoked_as": json.dumps(invoked_as, ensure_ascii=False),
        "exit_code": _optional_int(event.get("exit_code")),
        "result_class": _optional_text(event.get("result_class")),
        "duration_ms": _optional_int(event.get("duration_ms")),
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "capture_status": capture_status,
        "caller": caller,
        "is_tty": _optional_bool(event.get("is_tty")),
        "is_ci": _optional_bool(event.get("is_ci")),
        "task_outcome": task_outcome,
        "task_outcome_source": task_outcome_source,
        "context": json.dumps(context, ensure_ascii=False),
        "meta": json.dumps(meta, ensure_ascii=False),
    }


def record_event(
    event: dict[str, Any], cfg: Cfg, *, path: Path | None = None
) -> str | None:
    """Best-effort insert of one v2 event; returns its event id when persisted."""
    from gnomon.telemetry import _disabled, connect, db_path

    if _disabled(cfg):
        return None
    try:
        normalized = _normalized_event(event, cfg)
        if normalized is None:
            return None
        conn = connect(path or db_path(cfg))
        try:
            placeholders = ",".join("?" * len(_EVENT_COLUMNS))
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"INSERT INTO events_v2 ({','.join(_EVENT_COLUMNS)}) "
                f"VALUES ({placeholders})",
                tuple(normalized[column] for column in _EVENT_COLUMNS),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()
        return str(normalized["event_id"])
    except Exception:
        return None


def read_events(*, cfg: Cfg, path: Path | None = None) -> list[dict[str, Any]]:
    """Read v2 events for local analysis; missing or unreadable ledgers are empty."""
    from gnomon.telemetry import connect, db_path

    ledger = path or db_path(cfg)
    if not ledger.exists():
        return []
    try:
        conn = connect(ledger)
        try:
            rows = conn.execute(
                f"SELECT {','.join(_EVENT_COLUMNS)} FROM events_v2 ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    result = []
    for row in rows:
        item = dict(zip(_EVENT_COLUMNS, row, strict=True))
        for field, fallback in (("invoked_as", []), ("context", {}), ("meta", {})):
            try:
                item[field] = json.loads(item[field])
            except (TypeError, json.JSONDecodeError):
                item[field] = fallback
        result.append(item)
    return result
