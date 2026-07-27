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

## Per-execution metadata

Persist application data alongside an execution from within an engraved call:

```python
ctx = glyff.get_context()
await ctx.metadata.set("my_key", {"any": "json-serializable value"})
value = await ctx.metadata.get("my_key", dict)
```

Metadata is a keyed map owned by the current `Execution` aggregate.
`ctx.metadata.set(...)` stages metadata into the currently active transaction.
During normal engraved execution, metadata set in the function body commits
atomically with the execution's `COMPLETED` status and result. If completing
the current execution fails, metadata staged through `ctx.metadata.set(...)` in
that function body is rolled back with the completion write.

Metadata is removed if that execution's record is deleted.

## Transaction model

- Writes are staged in-memory per transaction and flushed on commit.
- Nested transactions (child scopes) commit independently of their parent.
- `BEGIN IMMEDIATE` prevents writer contention; WAL mode keeps reads fast.
- A per-database asyncio write lock serialises concurrent write transactions.

## Status

Early development. APIs may change before v1.0.

## License

MIT
