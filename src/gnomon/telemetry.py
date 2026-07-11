"""Per-invocation telemetry: capture each CLI run into a local SQLite ledger and
expose `stats` for per-command count / p50·p95 duration / error-rate.

Shared core for the-orrery tools. Each tool passes a `Cfg(tool=...)`, so the same
machinery writes to that tool's own ledger (`~/.local/share/<tool>/telemetry.db`)
under that tool's env switches — while the `calls` schema stays IDENTICAL across
tools, so their ledgers can be ATTACH-ed + UNION-ed for cross-tool analysis.

Local-only, no network — usage observability to drive the tool's own improvement
(which commands run, how long, what fails), not product analytics.

Best-effort throughout: telemetry must never fail the command or change its exit
code — every write path swallows its own errors.

Two capture postures (a tool picks by its shape):
  - `run_instrumented(app, argv, cfg)` — in-process Typer/Click apps: the wrapper
    tees stdout/stderr, times the run, records a row. Needs the `typer` extra.
  - `record(rec, cfg)` — for tools that can't be wrapped in-process (e.g. a
    subprocess-dispatch shell whose children write fds directly, so in-process
    capture sees nothing): the caller times the run and assembles the row itself.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# stderr is small + high-value (errors/warnings) so sample generously; stdout can
# be huge so keep only a head sample (full size lives in out_bytes).
STDOUT_CAP = 2048
STDERR_CAP = 4096

# exit codes >= this are hard faults (usage / unknown command); exit 1 without an
# err message is the tool working (e.g. a lint that found problems), not a fault.
_FAULT_EXIT = 2
# percentile at/above this means "the max" — nearest-rank degenerates to the last.
_MAX_PCTILE = 100

# Public agent-product env vars — not internal identifiers, safe in a public package.
_AGENT_ENV_KEYS = (
    "AI_AGENT",
    "CLAUDECODE",
    "CLAUDE_CODE",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_SANDBOX",
    "CODEX_SESSION_ID",
    "CODEX_THREAD_ID",
    "OPENAI_CODEX",
)


@dataclass(frozen=True)
class Cfg:
    """A tool's telemetry identity, passed by the consuming repo on every call.

    tool        tool name (kebab/snake) → ledger dir + stats heading.
    version     the tool's own __version__, stamped into each row by run_instrumented.
    env_prefix  prefix for the db-path override + opt-out env vars; defaults to
                tool upper-cased ("crux" → CRUX_TELEMETRY_DB / CRUX_TELEMETRY_OFF).
    """

    tool: str
    version: str = ""
    env_prefix: str = ""

    @property
    def prefix(self) -> str:
        return self.env_prefix or self.tool.upper().replace("-", "_")


def db_path(cfg: Cfg) -> Path:
    """Resolve the ledger path: $<PREFIX>_TELEMETRY_DB override,
    else ($XDG_DATA_HOME or ~/.local/share)/<tool>/telemetry.db."""
    override = os.environ.get(f"{cfg.prefix}_TELEMETRY_DB")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / cfg.tool / "telemetry.db"


def _disabled(cfg: Cfg) -> bool:
    # DO_NOT_TRACK is the de-facto opt-out standard; honour it plus a per-tool flag.
    return bool(
        os.environ.get(f"{cfg.prefix}_TELEMETRY_OFF") or os.environ.get("DO_NOT_TRACK")
    )


class Tee:
    """Wraps the real stdout/stderr: writes through while counting total UTF-8
    bytes and keeping the first `cap` bytes as a sample — lets telemetry observe
    output size without the command knowing it's watched."""

    def __init__(self, real: Any, cap: int) -> None:
        self.real = real
        self.cap = cap
        self.total = 0
        self._buf = bytearray()

    def write(self, s: str) -> int:
        n = self.real.write(s)  # pass through; the real stream owns its own errors
        try:
            # telemetry accounting must NEVER raise into the command's write path
            b = s.encode("utf-8", "replace") if isinstance(s, str) else bytes(s)
            self.total += len(b)
            if len(self._buf) < self.cap:
                self._buf.extend(b[: self.cap - len(self._buf)])
        except Exception:
            pass
        return n if isinstance(n, int) else len(s)

    def flush(self) -> None:
        self.real.flush()

    def isatty(self) -> bool:
        try:
            return self.real.isatty()
        except Exception:
            return False

    @property
    def sample(self) -> str:
        return self._buf.decode("utf-8", "replace")


_SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS calls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL DEFAULT '',
    pid          INTEGER NOT NULL DEFAULT 0,
    command_path TEXT    NOT NULL DEFAULT '[]',
    args         TEXT    NOT NULL DEFAULT '[]',
    exit_code    INTEGER NOT NULL DEFAULT 0,
    duration_ms  INTEGER NOT NULL DEFAULT 0,
    out_bytes    INTEGER NOT NULL DEFAULT 0,
    stdout       TEXT    NOT NULL DEFAULT '',
    stderr       TEXT    NOT NULL DEFAULT '',
    err          TEXT    NOT NULL DEFAULT '',
    cwd          TEXT    NOT NULL DEFAULT '',
    version      TEXT    NOT NULL DEFAULT '',
    is_tty       INTEGER NOT NULL DEFAULT 0,
    is_ci        INTEGER NOT NULL DEFAULT 0,
    caller       TEXT    NOT NULL DEFAULT '',
    context      TEXT    NOT NULL DEFAULT '{}',
    meta         TEXT    NOT NULL DEFAULT '{}'
);
"""

# Applied after migration so the index always references a column that exists.
_SCHEMA_INDEX = "CREATE INDEX IF NOT EXISTS calls_command_path ON calls(command_path);"

_COLUMNS = (
    "ts",
    "pid",
    "command_path",
    "args",
    "exit_code",
    "duration_ms",
    "out_bytes",
    "stdout",
    "stderr",
    "err",
    "cwd",
    "version",
    "is_tty",
    "is_ci",
    "caller",
    "context",
    "meta",
)


def _ancestor_commands(limit: int = 8) -> list[str]:
    """Best-effort parent-process command chain for caller detection.

    Agent subprocesses do not always expose agent env vars. On macOS the reliable
    signal is often an ancestor process such as `/opt/.../codex`.
    """
    commands: list[str] = []
    pid = os.getppid()
    for _ in range(limit):
        if pid <= 1:
            break
        try:
            proc = subprocess.run(
                ["ps", "-o", "ppid=", "-o", "command=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=1,
            )
        except Exception:
            break
        line = (proc.stdout or "").strip()
        if not line:
            break
        parts = line.split(None, 1)
        if not parts:
            break
        try:
            next_pid = int(parts[0])
        except ValueError:
            break
        command = parts[1] if len(parts) > 1 else ""
        commands.append(command)
        pid = next_pid
    return commands


def detect_caller(env: dict[str, str] | None = None) -> str:
    """Classify the CLI caller as 'agent' or 'human'.

    Checks agent-product env vars, TERM_PROGRAM, and (when env=None) ancestor
    process names. Passing a custom `env` dict suppresses the live process scan
    so callers can test in isolation."""
    source = os.environ if env is None else env
    if any(source.get(key) for key in _AGENT_ENV_KEYS):
        return "agent"
    term = source.get("TERM_PROGRAM", "").lower()
    if "claude" in term or "codex" in term:
        return "agent"
    if env is None:
        ancestors = "\n".join(_ancestor_commands()).lower()
        if "codex" in ancestors or "claude" in ancestors:
            return "agent"
    return "human"


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """If the table has 'verb' but not 'command_path', migrate in-place.

    Only adds columns that are genuinely absent — some tools
    already have caller/context columns in their private schema, so we must not
    blindly ADD them again (duplicate column name would abort the migration
    before the UPDATE that backfills command_path).

    Also ensures all v2 columns that _COLUMNS expects are present, since tools
    with private schemas may be missing columns like 'version'."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(calls)").fetchall()}
    if "verb" not in cols or "command_path" in cols:
        # If command_path already exists, ensure any remaining v2 columns are present
        # (handles partial migrations or private schemas with extra columns).
        _ensure_v2_columns(conn, cols)
        return
    conn.execute("ALTER TABLE calls ADD COLUMN command_path TEXT NOT NULL DEFAULT '[]'")
    if "caller" not in cols:
        conn.execute("ALTER TABLE calls ADD COLUMN caller TEXT NOT NULL DEFAULT ''")
    if "context" not in cols:
        conn.execute("ALTER TABLE calls ADD COLUMN context TEXT NOT NULL DEFAULT '{}'")
    _ensure_v2_columns(conn, cols | {"command_path", "caller", "context"})
    conn.execute("UPDATE calls SET command_path = json_array(verb)")
    conn.execute("PRAGMA user_version=2")


def _ensure_v2_columns(conn: sqlite3.Connection, cols: set[str]) -> None:
    """Add any v2 columns missing from the table (idempotent)."""
    missing: list[tuple[str, str]] = [
        ("version", "TEXT NOT NULL DEFAULT ''"),
        ("caller", "TEXT NOT NULL DEFAULT ''"),
        ("context", "TEXT NOT NULL DEFAULT '{}'"),
    ]
    for col, typedef in missing:
        if col not in cols:
            conn.execute(f"ALTER TABLE calls ADD COLUMN {col} {typedef}")


def connect(path: Path) -> sqlite3.Connection:
    """Open the ledger in WAL mode with a busy timeout so concurrent CLI
    processes queue rather than fail. isolation_level=None → explicit txns.
    Closes itself if schema setup fails, so callers never leak a connection.

    Public so repos can read their own ledger for tool-specific analysis
    (e.g. `connect(db_path(cfg))`) without re-implementing the open path.

    Automatically migrates v1 ledgers (verb column) to v2 (command_path/caller/context)
    on first open, preserving all historical rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(5):
        conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            current_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
            if current_mode.lower() != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA_TABLE)
            _migrate_v1_to_v2(conn)
            conn.execute(_SCHEMA_INDEX)
            from gnomon.events import ensure_event_schema

            ensure_event_schema(conn)
            conn.execute("PRAGMA user_version=2")
            return conn
        except sqlite3.OperationalError as exc:
            conn.close()
            contended = "locked" in str(exc).lower() or "busy" in str(exc).lower()
            if not contended or attempt == 4:
                raise
            time.sleep(0.01 * (attempt + 1))
        except Exception:
            conn.close()
            raise
    raise RuntimeError("unreachable")


def record(rec: dict, cfg: Cfg, *, path: Path | None = None) -> None:
    """Insert one invocation row. Best-effort: any failure (locked db, bad fs,
    disabled) is swallowed so telemetry never affects the command's outcome.

    rec may contain:
      command_path  list[str] — subcommand hierarchy, e.g. ["deploy", "pipeline", "run"]
      caller        str — "agent" or "human"; auto-detected if absent
      context       dict — arbitrary tool-private data; core treats it as opaque JSON
    """
    if _disabled(cfg):
        return
    try:
        conn = connect(path or db_path(cfg))
        try:
            cp = rec.get("command_path")
            if isinstance(cp, list):
                command_path = json.dumps(cp, ensure_ascii=False)
            else:
                command_path = json.dumps([str(cp)] if cp else [], ensure_ascii=False)
            caller = str(rec["caller"]) if "caller" in rec else detect_caller()
            values = (
                str(rec.get("ts") or _now_iso()),
                int(rec.get("pid") or os.getpid()),
                command_path,
                json.dumps(rec.get("args", []), ensure_ascii=False),
                int(rec.get("exit_code", 0)),
                int(rec.get("duration_ms", 0)),
                int(rec.get("out_bytes", 0)),
                str(rec.get("stdout", "")),
                str(rec.get("stderr", "")),
                str(rec.get("err", "")),
                str(rec.get("cwd", "")),
                str(rec.get("version") or cfg.version),
                1 if rec.get("is_tty") else 0,
                1 if rec.get("is_ci") else 0,
                caller,
                json.dumps(rec.get("context", {}), ensure_ascii=False),
                json.dumps(rec.get("meta", {}), ensure_ascii=False),
            )
            placeholders = ",".join("?" * len(_COLUMNS))
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"INSERT INTO calls ({','.join(_COLUMNS)}) VALUES ({placeholders})",
                values,
            )
            conn.execute("COMMIT")
        finally:
            conn.close()
    except Exception:
        return


def _now_iso() -> str:
    # local time, ms precision, with offset — e.g. 2026-06-09T20:00:00.123+08:00
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def run_instrumented(  # noqa: PLR0913 — public capture entry: cfg + keyword-only knobs are each independent caller-tunable overrides, not a cohesive object
    app: Any,
    argv: list[str],
    cfg: Cfg,
    *,
    command_path: list[str] | None = None,
    prog_name: str | None = None,
    meta: dict | None = None,
    path: Path | None = None,
) -> int:
    """Run a Typer/Click `app` for one invocation under telemetry capture, record
    a row, and return the exit code. Lets click own its exit semantics
    (standalone_mode=True); the wrapper just captures stdout/stderr + the exit
    code + any unexpected fault, uniformly for every command. Wire it as the
    console-script entry: `def run(): raise SystemExit(run_instrumented(app, sys.argv[1:], CFG))`.

    command_path: explicit subcommand hierarchy. When absent, derived from the
    leading non-flag tokens of argv (e.g. ["deploy", "pipeline"] from
    ["deploy", "pipeline", "--env", "prod"]).

    Requires the `typer` extra (in-process capture is a no-op for tools that
    can't be wrapped — those call `record` directly instead)."""
    import typer

    cmd = typer.main.get_command(app) if isinstance(app, typer.Typer) else app

    start = time.monotonic()

    if command_path is None:
        # Derive from leading non-flag tokens as a best-effort default.
        derived: list[str] = []
        for a in argv:
            if a.startswith("-"):
                break
            derived.append(a)
        command_path = derived if derived else []

    try:
        is_tty = sys.stdout.isatty()
    except Exception:
        is_tty = False

    real_out, real_err = sys.stdout, sys.stderr
    out_tee, err_tee = Tee(real_out, STDOUT_CAP), Tee(real_err, STDERR_CAP)
    sys.stdout, sys.stderr = out_tee, err_tee

    exit_code = 0
    err_msg = ""
    try:
        cmd(args=argv, standalone_mode=True, prog_name=prog_name or cfg.tool)
    except SystemExit as e:
        # click exits via SystemExit: 0 ok, 2 usage error, 1 abort, ...
        exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except Exception as e:
        # an unexpected fault inside a command (click re-raises non-click errors):
        # map to a clean exit + a recorded row, never a traceback.
        print(f"error: {e}", file=sys.stderr)
        err_msg = str(e)
        exit_code = 1
    finally:
        sys.stdout, sys.stderr = real_out, real_err

    # Stamp the agent session id when present: Claude Code sets
    # CLAUDE_CODE_SESSION_ID on the Bash subprocess, so telemetry from several
    # tools in one agent task can be grouped/sequenced by meta->>'session' —
    # this is the hook the cross-tool sequence mining depends on.
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    record(
        {
            "ts": _now_iso(),
            "pid": os.getpid(),
            "command_path": command_path,
            "args": argv[len(command_path) :],
            "exit_code": exit_code,
            "duration_ms": int((time.monotonic() - start) * 1000),
            "out_bytes": out_tee.total,
            "stdout": out_tee.sample,
            "stderr": err_tee.sample,
            "err": err_msg,
            "cwd": str(Path.cwd()),
            "version": cfg.version,
            "is_tty": is_tty,
            "is_ci": bool(os.environ.get("CI")),
            "meta": {**(meta or {}), **({"session": session} if session else {})},
        },
        cfg,
        path=path,
    )
    return exit_code


# ---- stats ----


def _pctile(xs: list[int], p: int) -> int:
    if not xs:
        return 0
    s = sorted(xs)
    if p >= _MAX_PCTILE:
        return s[-1]
    idx = p * len(s) // 100
    return s[min(idx, len(s) - 1)]


def _is_fault(exit_code: int, err: str) -> bool:
    """A real fault = a non-empty err message OR a hard exit (>= 2: usage /
    unknown command). An intentional exit-1-without-message (e.g. a lint that
    found problems) is the tool working, not failing — not counted as an error."""
    return bool(err.strip()) or exit_code >= _FAULT_EXIT


def _first_line(s: str) -> str:
    s = s.strip()
    i = s.find("\n")
    return s[:i] if i >= 0 else s


def _cp_display(command_path_json: str) -> str:
    """Convert a JSON-array command_path to a human-readable string."""
    try:
        parts = json.loads(command_path_json)
        if isinstance(parts, list):
            return " ".join(str(p) for p in parts) if parts else "(empty)"
    except (TypeError, json.JSONDecodeError):
        pass
    return command_path_json or "(empty)"


def stats(cfg: Cfg, *, path: Path | None = None) -> str:
    """Return a per-command summary (count / p50·p95·max ms via nearest-rank / error
    count) plus the most recent faults. Returns text (callers print, tests assert)
    and is itself best-effort: a missing/unreadable ledger yields a human note,
    never a traceback.

    Tool-specific sections (e.g. crux's recall-query mix) are NOT added here —
    a repo composes them at its own CLI layer: `print(stats(cfg)); print(my_section())`.

    Note: the ledger grows unbounded (one row per call). It's local and cheap to
    delete to reset; heavy users can prune `DELETE FROM calls WHERE id < ...`."""
    p = path or db_path(cfg)
    if not p.exists():
        return f"no telemetry yet ({p}): run the CLI a few times first"
    try:
        conn = connect(p)
        try:
            # bulk pass pulls only the light columns (no stdout/stderr) so memory
            # stays flat as the ledger grows; stderr is fetched only for the few
            # recent fault rows below.
            rows = conn.execute(
                "SELECT command_path, exit_code, duration_ms, err "
                "FROM calls WHERE command_path != '[]' ORDER BY id"
            ).fetchall()
            recent_rows = conn.execute(
                "SELECT ts, command_path, err, stderr FROM calls "
                "WHERE command_path != '[]' AND (err != '' OR exit_code >= 2) "
                "ORDER BY id DESC LIMIT 10"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return f"telemetry ledger unreadable ({p}); remove it to reset"

    if not rows:
        return "telemetry is empty"

    count: dict[str, int] = {}
    durs: dict[str, list[int]] = {}
    err_cnt: dict[str, int] = {}
    total = total_err = 0

    for command_path, exit_code, duration_ms, err in rows:
        total += 1
        count[command_path] = count.get(command_path, 0) + 1
        durs.setdefault(command_path, []).append(int(duration_ms or 0))
        if _is_fault(int(exit_code or 0), err or ""):
            total_err += 1
            err_cnt[command_path] = err_cnt.get(command_path, 0) + 1

    cps = sorted(count, key=lambda v: (-count[v], v))
    rate = 100.0 * total_err / total
    lines = [
        f"{cfg.tool} telemetry — {total} calls · {total_err} errors ({rate:.1f}%)",
        "",
    ]
    lines.append(f"{'command':<20}{'count':>7}{'p50':>8}{'p95':>8}{'max':>8}{'err':>6}")
    for cp in cps:
        d = durs[cp]
        display = _cp_display(cp)
        lines.append(
            f"{display:<20}{count[cp]:>7}"
            f"{str(_pctile(d, 50)) + 'ms':>8}"
            f"{str(_pctile(d, 95)) + 'ms':>8}"
            f"{str(_pctile(d, 100)) + 'ms':>8}"
            f"{err_cnt.get(cp, 0):>6}"
        )
    if recent_rows:
        lines.append("")
        lines.append("recent errors (last 10):")
        for ts, cp, err, stderr in reversed(recent_rows):  # oldest → newest
            msg = (err or "").strip() or _first_line(stderr or "")
            display = _cp_display(cp)
            lines.append(f"  {ts}  {display:<16} {msg}")
    return "\n".join(lines)
