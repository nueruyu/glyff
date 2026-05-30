# glyff-file-store

A file-based `SessionStore` implementation for
[glyff](https://pypi.org/project/glyff/).

Sessions are persisted to disk as append-only JSON event logs, with separate
files for large execution results. Designed to survive process restarts
without requiring a database.

## Install

```bash
pip install glyff-file-store
```

This package depends on `glyff>=0.1.0`.

## Public API

| Name                        | Description                                                                |
| --------------------------- | -------------------------------------------------------------------------- |
| `FileClient`                | Low-level file I/O for a session directory.                                |
| `JsonFileSessionStore`      | `SessionStore` writing a pretty-printed JSON log. Intended for debugging.  |
| `JsonLinesFileSessionStore` | `SessionStore` writing a JSON-Lines log with an in-memory index. Scalable. |

## Format

Sessions are stored as event log files under the configured base directory.
Each event is a single JSON line; the format is append-only and
forward-compatible. Large execution results are written to separate files
referenced by the event log.

## Status

Early development. APIs may change before v1.0.

## License

MIT
