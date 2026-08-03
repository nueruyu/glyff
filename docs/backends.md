# Backends

glyff core is store-agnostic: everything durable goes through the `Backend`
contract. This page covers that contract, how to verify an implementation of it,
and what is being added.

## The contract

A `Backend` (`_interfaces.py`) is a bundle of three pieces:

| Piece | Role |
| --- | --- |
| `ExecutionRepository` | Aggregate persistence: `get`, `save`, `executions`, `delete_many`. |
| `TransactionProvider` | Owns transaction boundaries; `begin_transaction` returns a `Transaction` with `commit`/`rollback`. |
| `AppVersionStore \| None` | The application version behind the store's records, or `None` for a store too ephemeral to outlive the code that wrote it. |

`executions(*, status=None, under=None)` yields whole aggregates, each after its
own ancestors, including records staged in the caller's open transaction. It
returns `Execution`s rather than ids because its consumers — reconciliation
sweeps, [pruning](./events.md#pruning-completed-subtrees), and
[migration](./migration.md) — need arguments and results, and because the path
encoding an id is rebuilt from is a backend's internal business.

The version store is written through the same staging as the records, so a
migration's rewrites and the version it rewrote them to commit together. glyff
only records that version and refuses a mismatch; what it means is the
application's (see [migration](./migration.md)).

The repository stores `Execution` aggregates whole — args, status, result, metadata —
and core assumes nothing about the medium underneath. Tables, files, and key
prefixes are a backend's own business, which is why there is
[no schema-customization interface](#non-goal-a-schema-customization-interface).

Serialized values pass through as bytes; the shipped file and SQLite backends
store them as readable JSON text, so serializers used with them must produce
JSON text.

## Shipped backends

| Package | Backend | Intended use |
| --- | --- | --- |
| [`glyff`](../packages/glyff) | in-memory | Tests and ephemeral runs. |
| [`glyff-file-store`](../packages/glyff-file-store) | pretty-printed JSON files | Debugging and manual inspection. |
| [`glyff-sqlite`](../packages/glyff-sqlite) | SQLite, WAL mode | Production. |

## Writing your own

Implement the pieces above and expose them as a bundle. Verify the
implementation against the shared contract suite in `glyff.testing`: subclass
the contract classes (`ExecutionBackendContract`, `DurableBackendContract`,
`AppVersionContract`, and the text/binary-safety variants) and provide your
backend factory. A backend that records no application version exposes `None`
and skips `AppVersionContract`. The shipped backends run the same suite. It also
exports the reference `PruningEventHandler` and the helpers `save_execution`,
`serialized_value`, and the pair `make_execution_id` / `canonical_arguments`,
which build an execution that satisfies the `arguments_digest` invariant.

## Session scope

A backend instance covers one session — a file store directory, one set of
SQLite tables — so nothing in the contract takes a session argument. The SQLite
backend enforces it: execution paths carry no session component, so opening the
same tables under a different `session_id` raises `StoreSessionMismatchError`
rather than interleaving two histories.

## Planned contract extensions

> **Planned** — **external transaction enlistment**
> ([#42](https://github.com/nueruyu/glyff/issues/42)): construct a backend on an
> externally supplied connection or session (e.g. a SQLAlchemy `AsyncSession`)
> so glyff's transaction scope joins the application's. Execution completion and
> the application's own writes then commit atomically. Concrete
> `glyff-sqlalchemy`/`glyff-postgres` backends follow once the interface exists,
> and the contract suite grows with it.

## Non-goal: a schema-customization interface

glyff core does not know tables exist; the `Backend` protocol is the
customization boundary, and `glyff-sqlite` is one implementation of it. A
user-defined schema means writing a backend and verifying it against the
contract suite, not parameterizing a shipped one.
