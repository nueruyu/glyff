# glyff-file-store

File-backed `SessionStore` implementation for
[glyff](https://pypi.org/project/glyff/).

This package provides a human-readable debug backend:

- `JsonFileSessionStore` stores a pretty-printed JSON event log, loads the
  whole log into memory at startup, and rewrites the entire file atomically on
  each commit.

For the durable production backend, see
[`glyff-sqlite`](https://pypi.org/project/glyff-sqlite/).

## Install

```bash
pip install glyff-file-store
```

This package depends on `glyff>=0.1.0`.

## Public API

| Name                   | Description                                                    |
| ---------------------- | -------------------------------------------------------------- |
| `FileClient`           | Low-level file I/O for a session directory.                    |
| `JsonFileSessionStore` | Debug `SessionStore` writing a pretty-printed JSON event log.  |

## JSON debug format

`JsonFileSessionStore` stores each session under `<base_dir>/<session_id>/` in a
single pretty-printed JSON file (`executions.json`). The whole log is read into
memory on startup and rewritten atomically on every commit.

This format is intended for debugging and manual inspection, not as the durable
or high-throughput backend.

## Commit atomicity

`FileClient` commits the staged ops for an entire session directory as a unit.
Each commit builds the full new session state in a sibling `.commit-*`
directory and then swaps it into place using directory renames. All staged ops
are visible together or none are, regardless of how many files were touched,
and a writer callback raising mid-commit leaves the on-disk session unchanged.

If a process dies mid-commit, the next `FileClient(...)` instantiation restores
the session from `session.bak` when the live directory is missing, or removes
the stale backup when the live directory is already in place. It also removes
any orphan `.commit-*` temp directories.

## Status

Early development. APIs may change before v1.0.

## License

MIT
