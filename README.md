# gnomon

## Distribution

`gnomon` is a library, not a CLI, so releases publish a platform-independent
wheel, source distribution, and `SHA256SUMS` instead of a fake executable.
Artifacts are available from [GitHub Releases](https://github.com/the-orrery/gnomon/releases).
Pull requests build and install the wheel in an isolated environment; a matching
`v<project.version>` tag publishes immutable assets.

Shared per-invocation telemetry for local CLI tools. `gnomon` writes one row per
CLI run into a local SQLite ledger, then exposes small helper APIs for stats and
tool-specific analysis.

The package is local-only and best-effort: it does not send data over the
network, and telemetry failures must never change the caller's exit code.

## Usage

```python
from gnomon import Cfg, record, stats

cfg = Cfg(tool="demo", version="1.0.0")
record({"command_path": ["build"], "exit_code": 0, "duration_ms": 42}, cfg)
print(stats(cfg))
```

Typer/Click CLIs can use `run_instrumented` for in-process stdout/stderr capture.
Tools that spawn subprocesses can call `record` after they measure a run.

## Development

    uv sync
    uv run poe check
    uv run poe fmt

## Docs

    docs/INDEX.md
    docs/architecture.md
