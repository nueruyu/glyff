# glyff-sqlite

SQLite-backed durable `ExecutionRepository` implementation for
[glyff](https://pypi.org/project/glyff/).

This is the **production** backend: one row per execution in a SQLite database,
WAL mode, indexed lookups, and native transaction atomicity.

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

## Per-execution metadata

Persist application data alongside an execution — committed atomically with its
record — from within an engraved call:

```python
ctx = glyff.get_context()
await ctx.set_metadata("my_key", {"any": "json-serializable value"})
value = await ctx.get_metadata("my_key", dict)
```

Metadata is a keyed map attached to the current execution and is removed if that
execution's record is deleted.

## Transaction model

- Writes are staged in-memory per transaction and flushed on commit.
- Nested transactions (child scopes) commit independently of their parent.
- `BEGIN IMMEDIATE` prevents writer contention; WAL mode keeps reads fast.
- A per-database asyncio write lock serialises concurrent write transactions.

## Status

Early development. APIs may change before v1.0.

## License

MIT
