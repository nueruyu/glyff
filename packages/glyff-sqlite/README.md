# glyff-sqlite

SQLite-backed durable `ExecutionRepository` implementation for
[glyff](https://pypi.org/project/glyff/).

This is the **production** backend: one row per execution in a SQLite
`glyff_executions` table, WAL mode, indexed lookups, and native transaction
atomicity. Execution `result` and `metadata` columns are stored as readable JSON
text, so serializers used with this backend must produce JSON text.

`table_prefix` (default `glyff`) names the two tables the store owns:
`<prefix>_executions` and `<prefix>_meta`, which records the format version so a
store written by an incompatible build is refused. Pass `table_prefix=` to
cohabit an application's own database; `PRAGMA user_version` is left to the
application.

## Install

```bash
pip install glyff-sqlite
```

This package depends on `glyff>=0.1.0` (no additional runtime dependencies —
`sqlite3` is part of the standard library).

## Usage

```python
from glyff_sqlite import SQLiteBackend

backend = SQLiteBackend("executions.sqlite3")
```

## Public API

| Name                        | Description                                          |
| --------------------------- | ---------------------------------------------------- |
| `SQLiteBackend`             | Bundle exposing repository and transaction provider. |
| `SQLiteExecutionRepository` | Durable repository backed by local SQLite.           |
| `SQLiteTransactionProvider` | Transaction provider for the SQLite backend.         |

The underlying `SQLiteClient` is internal and not part of the public API.

## Storage model

- One row per execution; `result` and `metadata` are JSON text columns, readable
  in place with any SQLite client.
- Per-execution metadata (see the
  [glyff README](https://pypi.org/project/glyff/)) commits atomically with the
  execution's `COMPLETED` status and result, and is removed with the execution's
  row when the record is deleted.

## Transaction model

- Writes are staged in-memory per transaction and flushed on commit.
- Nested transactions (child scopes) commit independently of their parent.
- `BEGIN IMMEDIATE` prevents writer contention; WAL mode keeps reads fast.
- A per-database asyncio write lock serialises concurrent write transactions.

## Planned

- **Store migrations** — the format-version stamp is in place and an
  incompatible store is refused, but nothing yet converts a store from one
  format version to the next
  ([#41](https://github.com/nueruyu/glyff/issues/41)).
- **External transaction enlistment and enumeration** — constructing the backend
  on an externally supplied connection so completion commits join the
  application's transaction, plus a public execution-enumeration primitive
  ([#42](https://github.com/nueruyu/glyff/issues/42)).

## Status

Pre-1.0 — the API is unstable and will change. Items under *Planned* track the
linked issues and are not released yet.

## License

MIT
