# Backends

glyff core is store-agnostic: everything durable goes through the `Backend`
contract. This page covers that contract, how to verify an implementation of it,
and what is being added.

## The contract

A `Backend` (`_interfaces.py`) is an ABC you subclass — glyff's extension seams
are nominal, so a backend states what it implements rather than being taken for
one because its attributes happen to line up. It is:

| Piece | Role |
| --- | --- |
| `ExecutionRepository` | Aggregate persistence: `get`, `save`, `executions`, `delete_many`. |
| `TransactionProvider` | Owns transaction boundaries; `begin_transaction` returns a `Transaction` with `commit`/`rollback`. |
| `claim_session(session_id, app_version)` | Atomically establishes the application version behind a session's records, and returns the recorded one. |

- **Every repository operation names its `SessionId`.** A backend is scoped to a
  store, not a session, so which session a record belongs to is never implied by
  which object you happen to be holding — `Session.id` is the only place a
  session is named. A `SessionId` is any non-empty string; what a store can
  safely put in a key or a column is that store's problem.
- **A transaction may carry changes for any number of sessions**, and the
  shipped stores commit all of them together.
- **`executions(session_id, *, status=None, under=None)`** yields whole
  aggregates, each after its own ancestors, including changes staged in the
  caller's open transaction. It returns `Execution`s rather than ids because its
  consumers — reconciliation sweeps,
  [pruning](./events.md#pruning-completed-subtrees), and
  [migration](./migration.md) — need arguments and results, and because the path
  an id is rebuilt from is a backend's internal business. It is an async
  iterator so a backend that can stream does.
- **`claim_session`** takes an unclaimed session's version or reports the one
  already recorded, in one step that holds across processes. `Session` decides
  what a difference means (see [migration](./migration.md)); glyff only records
  the value.

Entering a `Session` atomically claims it with a concrete application version
before execution begins. Using an `ExecutionRepository` directly is the
lower-level persistence API and bypasses the `Session` lifecycle, including that
claim.

The repository stores `Execution` aggregates whole — args, status, result, metadata —
and core assumes nothing about the medium underneath. Tables, files, and key
prefixes are a backend's own business, which is why there is
[no schema-customization interface](#non-goal-a-schema-customization-interface).

Serialized values pass through as bytes; the shipped file and SQLite backends
store them as readable JSON text, so serializers used with them must produce
JSON text.

## Migration, an optional capability

A durable backend also offers `session_migration`, which replaces one session's
recorded state wholesale:

```python
report = await backend.session_migration.run(session_id, migrator)
```

It is deliberately not part of `Backend`. Running a session needs none of it,
and an ephemeral store has no old records to carry forward — the in-memory
backend is a plain `Backend` and does not provide it. A backend that offers
migration subclasses `MigratableBackend` (`glyff.migration`), which is itself a
`Backend`, so the capability is claimed rather than inferred.

The split is mechanism against policy. `SessionMigration` takes the session
exclusively, reads its `SessionMetadata` and executions into a `StoredSession`,
hands that to a `SessionMigrator`, and stores what comes back — metadata and
executions in one atomic step, or neither. It knows nothing about
transformations. `SessionMigrator.migrate` decides what the session should
become and is pure and synchronous by contract, because it runs while the
backend holds the session: anything that waits there holds a lock, and anything
with a side effect happens inside a transaction that may yet be undone.

Exclusion is the store's own, and each backend has one primitive that every
write goes through: SQLite's `run_immediate` runs an operation inside a single
`BEGIN IMMEDIATE`, and the file store's `update_document` reads, changes and
replaces the document with the store held — including past a cancellation, so a
cancelled caller cannot hand the store to the next writer while its own worker
is still replacing it. Either way no other writer can act on the state being
replaced. A `StoredSession` refuses to hold two executions on one id, so a
migrator that merges two histories is stopped before a store can silently keep
whichever it wrote last.

## Shipped backends

| Package | Backend | Intended use |
| --- | --- | --- |
| [`glyff`](../packages/glyff) | in-memory | Tests and ephemeral runs. |
| [`glyff-file-store`](../packages/glyff-file-store) | pretty-printed JSON files | Debugging and manual inspection. |
| [`glyff-sqlite`](../packages/glyff-sqlite) | SQLite, WAL mode | Production. |

## Writing your own

Subclass `Backend` — or `MigratableBackend` if you offer migration — and expose
the pieces above. Verify the
implementation against the shared contract suite in `glyff.testing`: subclass
the contract classes (`ExecutionBackendContract`, `DurableBackendContract`,
`AppVersionContract`, and the text/binary-safety variants) and provide your
backend factory — the factory names a *store*, and the same name reopens it. The
shipped backends run the same suite. It also exports the reference
`PruningEventHandler` and the helpers `save_execution`, `serialized_value`, and
the pair `make_execution_id` / `canonical_arguments`, which build an execution
that satisfies the `arguments_digest` invariant.

`glyff.store.staging` is there if you want it. A backend holds one
`ExecutionStaging`; a transaction calls `begin()` for its own `ExecutionStage`,
and the repository reaches the open one through `current()` /
`require_current()`, so neither has to hold a reference to the other:

```python
stage = staging.begin()
stage.save(session_id, execution)     # visible to reads in this transaction only
stage.close()                         # finalizes the batch, restores any parent
await client.commit_mutations(stage.batch)
```

Stages nest, and a rollback is a `close()` whose batch is discarded. A closed
stage is never the open one, including in a context copied while it was still
open, so a read there cannot overlay a batch that was never persisted. Every
shipped backend uses one. Nothing in the contract mentions it — a backend that
stages differently is free to ignore it.

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
