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

## Status

Early development. APIs may change before v1.0.

## License

MIT
