# Backends

glyff core is store-agnostic: everything durable goes through the `Backend`
contract. This page covers that contract, how to verify an implementation of it,
and what is being added.

## The contract

A `Backend` (`_interfaces.py`) is:

| Piece | Role |
| --- | --- |
| `ExecutionRepository` | Aggregate persistence: `get`, `save`, `executions`, `delete_many`. |
| `TransactionProvider` | Owns transaction boundaries; `begin_transaction` returns a `Transaction` with `commit`/`rollback`. |
| `claim_session(session_id, app_version)` | Records the application version behind a session's records, and reports the one it carries. |

Every repository method takes the `SessionId` it acts on. A store is not bound
to a session, so which session a record belongs to is never implied by which
object you happen to be holding — `Session.id` is the only place a session is
named.

`executions(session_id, *, status=None, under=None)` yields whole aggregates,
each after its own ancestors, including records staged in the caller's open
transaction. It returns `Execution`s rather than ids because its consumers —
reconciliation sweeps, [pruning](./events.md#pruning-completed-subtrees), and
[migration](./migration.md) — need arguments and results, and because the path
encoding an id is rebuilt from is a backend's internal business. It is an async
iterator so a backend that can stream does: the SQLite one pulls its range scan
a batch of rows at a time rather than materializing the table.

`claim_session` is one atomic step and its own transaction, and it has to hold
across *processes*: two workers starting the same paused session is the shape of
the hazard. Reading the version and then writing it would let both find the
session unclaimed and both start, mixing two generations of records. SQLite gets
this from `BEGIN IMMEDIATE`; the file store takes a lock file beside its session
directories. It returns the version the session carries afterwards — the
caller's if it took the session, the incumbent's if it did not — and `Session`
decides what a difference means (see [migration](./migration.md)). glyff only
records the value; what it means is the application's.

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
backend factory — the factory names a *store*, and the same name reopens it. The
shipped backends run the same suite. It also exports the reference
`PruningEventHandler` and the helpers `save_execution`, `serialized_value`, and
the pair `make_execution_id` / `canonical_arguments`, which build an execution
that satisfies the `arguments_digest` invariant.

## Session scope

One store holds any number of sessions: the SQLite backend keys records by
`(session_id, path)`, the file store gives each session a directory under
`base_dir`. Backends are constructed over the store, never over a session, so
there is no second place a session can be named and nothing to reconcile.

A `SessionId` is a name rather than a path, and rejects anything that could
reach outside the store it is given to.

A transaction covers one session. SQLite could commit more, but a backend is
free to hold to the narrower rule and the file store does, since its unit of
atomicity is a directory swap. `Session` never spans sessions, so this
constrains only code driving a repository directly.

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
