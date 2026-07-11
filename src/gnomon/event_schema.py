"""SQLite schema for versioned task and attempt events."""

from __future__ import annotations

import sqlite3

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
