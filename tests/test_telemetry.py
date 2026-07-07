from __future__ import annotations

import json
import sqlite3

import pytest

from gnomon import (
    Cfg,
    connect,
    db_path,
    detect_caller,
    record,
    run_instrumented,
    stats,
)

CFG = Cfg(tool="demo", version="1.2.3")


def test_prefix_derived_and_overridden():
    assert Cfg(tool="my-tool").prefix == "MY_TOOL"
    assert Cfg(tool="x", env_prefix="Y").prefix == "Y"


def test_db_path_uses_tool_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_TELEMETRY_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert db_path(CFG) == tmp_path / "demo" / "telemetry.db"


def test_db_path_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_TELEMETRY_DB", str(tmp_path / "x.db"))
    assert db_path(CFG) == tmp_path / "x.db"


def test_record_and_read_back(tmp_path):
    db = tmp_path / "t.db"
    record(
        {"command_path": ["foo"], "exit_code": 0, "duration_ms": 5, "version": "9"},
        CFG,
        path=db,
    )
    conn = connect(db)
    rows = conn.execute("SELECT command_path, exit_code, version FROM calls").fetchall()
    conn.close()
    assert rows == [('["foo"]', 0, "9")]


def test_opt_out_skips_write(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_TELEMETRY_OFF", "1")
    db = tmp_path / "t.db"
    record({"command_path": ["x"]}, CFG, path=db)
    assert not db.exists()


def test_do_not_track_skips_write(tmp_path, monkeypatch):
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    db = tmp_path / "t.db"
    record({"command_path": ["x"]}, CFG, path=db)
    assert not db.exists()


def test_stats_heading_uses_tool_name(tmp_path):
    db = tmp_path / "t.db"
    for _ in range(3):
        record(
            {"command_path": ["build"], "exit_code": 0, "duration_ms": 10}, CFG, path=db
        )
    record(
        {"command_path": ["build"], "exit_code": 2, "duration_ms": 20, "err": "boom"},
        CFG,
        path=db,
    )
    out = stats(CFG, path=db)
    assert "demo telemetry" in out
    assert "4 calls" in out
    assert "build" in out


def test_stats_missing_db_is_friendly(tmp_path):
    out = stats(CFG, path=tmp_path / "none.db")
    assert "no telemetry yet" in out


def test_run_instrumented_records_and_stamps_version(tmp_path):
    typer = pytest.importorskip("typer")
    app = typer.Typer()

    @app.command()
    def hello() -> None:
        print("hi")

    @app.command()
    def bye() -> None:  # a 2nd command makes typer build a subcommand group
        print("bye")

    db = tmp_path / "t.db"
    code = run_instrumented(app, ["hello"], CFG, path=db)
    assert code == 0
    conn = connect(db)
    command_path, version, out_bytes = conn.execute(
        "SELECT command_path, version, out_bytes FROM calls"
    ).fetchone()
    conn.close()
    assert json.loads(command_path) == ["hello"]
    assert version == "1.2.3"
    assert out_bytes >= 3  # "hi\n" observed through the Tee


# ---- v2 new tests ----


def test_v1_migration(tmp_path):
    """A v1 ledger (has 'verb', no 'command_path') is migrated on connect()."""
    db = tmp_path / "v1.db"
    # Create a v1-style schema manually
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE calls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL DEFAULT '',
            pid         INTEGER NOT NULL DEFAULT 0,
            verb        TEXT    NOT NULL DEFAULT '',
            args        TEXT    NOT NULL DEFAULT '[]',
            exit_code   INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            out_bytes   INTEGER NOT NULL DEFAULT 0,
            stdout      TEXT    NOT NULL DEFAULT '',
            stderr      TEXT    NOT NULL DEFAULT '',
            err         TEXT    NOT NULL DEFAULT '',
            cwd         TEXT    NOT NULL DEFAULT '',
            version     TEXT    NOT NULL DEFAULT '',
            is_tty      INTEGER NOT NULL DEFAULT 0,
            is_ci       INTEGER NOT NULL DEFAULT 0,
            meta        TEXT    NOT NULL DEFAULT '{}'
        );
        PRAGMA user_version=1;
    """)
    conn.execute("INSERT INTO calls (verb, exit_code) VALUES ('recall', 0)")
    conn.execute("INSERT INTO calls (verb, exit_code) VALUES ('stats', 0)")
    conn.commit()
    conn.close()

    # connect() should migrate in-place
    migrated = connect(db)
    rows = migrated.execute(
        "SELECT verb, command_path FROM calls ORDER BY id"
    ).fetchall()
    migrated.close()

    assert len(rows) == 2
    # old verb column preserved
    assert rows[0][0] == "recall"
    assert rows[1][0] == "stats"
    # command_path backfilled from verb
    assert json.loads(rows[0][1]) == ["recall"]
    assert json.loads(rows[1][1]) == ["stats"]


def test_v1_migration_with_preexisting_caller_context(tmp_path):
    """Migration works even when caller/context columns already exist."""
    db = tmp_path / "v1_custom.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE calls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            verb        TEXT    NOT NULL DEFAULT '',
            exit_code   INTEGER NOT NULL DEFAULT 0,
            caller      TEXT    NOT NULL DEFAULT '',
            context     TEXT    NOT NULL DEFAULT '{}'
        );
        PRAGMA user_version=1;
    """)
    conn.execute(
        "INSERT INTO calls (verb, caller, exit_code) VALUES ('deploy', 'agent', 0)"
    )
    conn.execute(
        "INSERT INTO calls (verb, caller, exit_code) VALUES ('stats', 'human', 0)"
    )
    conn.commit()
    conn.close()

    migrated = connect(db)
    rows = migrated.execute(
        "SELECT verb, command_path, caller FROM calls ORDER BY id"
    ).fetchall()
    migrated.close()

    assert len(rows) == 2
    assert json.loads(rows[0][1]) == ["deploy"]
    assert rows[0][2] == "agent"
    assert json.loads(rows[1][1]) == ["stats"]
    assert rows[1][2] == "human"


def test_migration_adds_missing_version_column(tmp_path):
    """connect() adds 'version' col when a schema has all base cols but lacks version."""
    db = tmp_path / "partial.db"
    conn = sqlite3.connect(str(db))
    # Full base schema but without version
    conn.executescript("""
        CREATE TABLE calls (
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
            is_tty       INTEGER NOT NULL DEFAULT 0,
            is_ci        INTEGER NOT NULL DEFAULT 0,
            caller       TEXT    NOT NULL DEFAULT '',
            context      TEXT    NOT NULL DEFAULT '{}',
            meta         TEXT    NOT NULL DEFAULT '{}'
        );
    """)
    conn.commit()
    conn.close()

    connect(db).close()  # trigger _ensure_v2_columns to add version
    # record() must now succeed — version column must be present
    record({"command_path": ["smoke"], "exit_code": 0, "version": "9"}, CFG, path=db)
    verify = connect(db)
    rows = verify.execute(
        "SELECT command_path, version FROM calls ORDER BY id DESC LIMIT 1"
    ).fetchall()
    verify.close()
    assert json.loads(rows[0][0]) == ["smoke"]
    assert rows[0][1] == "9"


def test_record_command_path(tmp_path):
    db = tmp_path / "t.db"
    record(
        {"command_path": ["deploy", "pipeline", "run"], "exit_code": 0},
        CFG,
        path=db,
    )
    conn = connect(db)
    (cp,) = conn.execute("SELECT command_path FROM calls").fetchone()
    conn.close()
    assert json.loads(cp) == ["deploy", "pipeline", "run"]


def test_record_caller_explicit(tmp_path):
    db = tmp_path / "t.db"
    record({"command_path": ["x"], "caller": "agent"}, CFG, path=db)
    conn = connect(db)
    (caller,) = conn.execute("SELECT caller FROM calls").fetchone()
    conn.close()
    assert caller == "agent"


def test_record_caller_auto_detect(tmp_path, monkeypatch):
    """When caller is not in rec, detect_caller() is called automatically."""
    # Force "human" by clearing all agent signals
    for key in (
        "AI_AGENT",
        "CLAUDECODE",
        "CLAUDE_CODE",
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_SANDBOX",
        "CODEX_SESSION_ID",
        "CODEX_THREAD_ID",
        "OPENAI_CODEX",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    db = tmp_path / "t.db"
    record({"command_path": ["x"]}, CFG, path=db)
    conn = connect(db)
    (caller,) = conn.execute("SELECT caller FROM calls").fetchone()
    conn.close()
    # value must be one of the two valid strings (ps scan may still return "agent")
    assert caller in ("agent", "human")


def test_detect_caller_agent_env(monkeypatch):
    for key in (
        "AI_AGENT",
        "CLAUDECODE",
        "CLAUDE_CODE",
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_SANDBOX",
        "CODEX_SESSION_ID",
        "CODEX_THREAD_ID",
        "OPENAI_CODEX",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert detect_caller(env={"CLAUDECODE": "1"}) == "agent"


def test_detect_caller_human():
    result = detect_caller(env={})
    assert result == "human"


def test_detect_caller_term_program():
    assert detect_caller(env={"TERM_PROGRAM": "claude-dev"}) == "agent"
    assert detect_caller(env={"TERM_PROGRAM": "codex"}) == "agent"
    assert detect_caller(env={"TERM_PROGRAM": "iTerm.app"}) == "human"


def test_record_context(tmp_path):
    db = tmp_path / "t.db"
    ctx = {"app": "demo", "unit": "cli", "env": "test"}
    record({"command_path": ["deploy"], "context": ctx}, CFG, path=db)
    conn = connect(db)
    (raw,) = conn.execute("SELECT context FROM calls").fetchone()
    conn.close()
    assert json.loads(raw) == ctx


def test_run_instrumented_command_path_explicit(tmp_path):
    typer = pytest.importorskip("typer")
    app = typer.Typer()

    @app.command()
    def go() -> None:
        pass

    @app.command()
    def other() -> None:
        pass

    db = tmp_path / "t.db"
    run_instrumented(app, ["go"], CFG, command_path=["build", "run"], path=db)
    conn = connect(db)
    (cp,) = conn.execute("SELECT command_path FROM calls").fetchone()
    conn.close()
    assert json.loads(cp) == ["build", "run"]


def test_run_instrumented_command_path_derived(tmp_path):
    """Without explicit command_path, derive from leading non-flag tokens."""
    typer = pytest.importorskip("typer")
    app = typer.Typer()

    @app.command()
    def sub() -> None:
        pass

    @app.command()
    def other() -> None:
        pass

    db = tmp_path / "t.db"
    run_instrumented(app, ["sub"], CFG, path=db)
    conn = connect(db)
    (cp,) = conn.execute("SELECT command_path FROM calls").fetchone()
    conn.close()
    assert json.loads(cp) == ["sub"]


def test_stats_command_path(tmp_path):
    db = tmp_path / "t.db"
    for _ in range(2):
        record(
            {"command_path": ["deploy", "pipeline"], "exit_code": 0, "duration_ms": 50},
            CFG,
            path=db,
        )
    record(
        {"command_path": ["recall"], "exit_code": 0, "duration_ms": 10}, CFG, path=db
    )
    out = stats(CFG, path=db)
    assert "deploy pipeline" in out
    assert "recall" in out
    assert "3 calls" in out
