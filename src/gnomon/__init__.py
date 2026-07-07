"""gnomon — shared per-invocation SQLite telemetry core for the-orrery tools.

A tool passes its identity once via `Cfg(tool=..., version=...)`; the same core
writes to that tool's own ledger under a shared, identical `calls` schema (so
ledgers can be unioned for cross-tool analysis). Two capture postures: the
in-process `run_instrumented` wrapper, or a direct `record` for tools that can't
be wrapped. Tool-specific analysis stays in the tool, reading via `connect`/`db_path`.
"""

from gnomon.telemetry import (
    Cfg,
    connect,
    db_path,
    detect_caller,
    record,
    run_instrumented,
    stats,
)

__version__ = "0.3.0"

__all__ = [
    "Cfg",
    "connect",
    "db_path",
    "detect_caller",
    "record",
    "run_instrumented",
    "stats",
]
