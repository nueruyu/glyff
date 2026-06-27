# glyff-sqlite

SQLite-backed durable `SessionStore` implementation for
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
from glyff.serialization import JsonSerializer
from glyff_sqlite import SQLiteSessionStore

store = SQLiteSessionStore("executions.sqlite3", JsonSerializer())
```

## Public API

| Name                 | Description                                                   |
| -------------------- | ------------------------------------------------------------- |
| `SQLiteSessionStore` | Durable `SessionStore` backed by a local SQLite database.     |
| `SQLiteClient`       | Low-level SQLite key/value store (generic, reusable).         |

## External metadata

The underlying ``SQLiteClient`` exposes ``stage_write`` / ``stage_delete`` /
``stage_update`` so application code can persist its own rows alongside execution
records and commit or roll back atomically together:

```python
client = SQLiteClient("session.sqlite3")
store = SQLiteSessionStore(client=client, serializer=JsonSerializer())

tx = await store.begin_transaction()
execution = await store.start_execution(some_id)
await execution.complete("ok", str)

client.stage_write("metadata", "my_key", b"my_value")
client.stage_delete("metadata", "old_key")

await tx.commit()   # execution record + metadata commit atomically
```

## Transaction model

- Writes are staged in-memory per transaction and flushed on commit.
- Nested transactions (child scopes) commit independently of their parent.
- `BEGIN IMMEDIATE` prevents writer contention; WAL mode keeps reads fast.
- A per-database asyncio write lock serialises concurrent write transactions.

## Status

Early development. APIs may change before v1.0.

## License

MIT
