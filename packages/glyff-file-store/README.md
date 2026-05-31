# glyff-file-store

A file-based `SessionStore` implementation for
[glyff](https://pypi.org/project/glyff/).

Sessions are persisted to disk as a JSON event log. The whole log is loaded
into memory at startup and the file is rewritten atomically on each commit;
suitable for sessions where the log fits comfortably in memory. For very
large or high-throughput sessions, prefer a database-backed store.

## Install

```bash
pip install glyff-file-store
```

This package depends on `glyff>=0.1.0`.

## Public API

| Name                   | Description                                                  |
| ---------------------- | ------------------------------------------------------------ |
| `FileClient`           | Low-level file I/O for a session directory.                  |
| `JsonFileSessionStore` | `SessionStore` writing a pretty-printed JSON event log file. |

## Format

Each session lives under `<base_dir>/<session_id>/` and stores its event log
in a single pretty-printed JSON file (`executions.json`). The whole log is
read into memory on startup and rewritten atomically on every commit.

## Commit atomicity

`FileClient` commits the staged ops for an entire session directory as a unit.
Each commit builds the full new session state in a sibling `.commit-*`
directory and then swaps it into place via two renames (`session` →
`session.bak`, `tmp` → `session`, drop `session.bak`). All staged ops are
visible together or none are, regardless of how many files were touched, and
a writer callback raising mid-commit leaves the on-disk session unchanged.

If a process dies mid-commit, the next `FileClient(...)` instantiation
restores the session from `session.bak` (when the live directory is missing)
or drops the stale backup (when the live directory is already in place), and
removes any orphan `.commit-*` temp directories.

## Status

Early development. APIs may change before v1.0.

## License

MIT
