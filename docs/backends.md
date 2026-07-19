# Backends

A backend is the persistence seam: glyff core is store-agnostic, and everything
durable goes through one narrow contract. This page states that contract, how to
verify a custom backend against it, and where the contract is growing.

> Pre-1.0: this page shows the release-target surface. Sections marked *Planned*
> link to the tracking issue.

## The contract

A `Backend` (`_interfaces.py`) is a bundle of two objects:

| Piece | Role |
| --- | --- |
| `ExecutionRepository` | Aggregate persistence: `get`, `save`, `descendants_of`, `delete_many`. |
| `TransactionProvider` | Owns transaction boundaries; `begin_transaction` returns a `Transaction` with `commit`/`rollback`. |

The repository stores `Execution` aggregates whole — status, result, metadata —
and glyff core never assumes anything about the medium underneath. Tables,
files, key prefixes are implementation details of a backend, which is why there
is no schema-customization interface (see [non-goals](#non-goal-a-schema-customization-interface)).

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

Implement the two objects and expose them as a bundle. Verify the implementation
against the shared contract suite in `glyff.testing`: subclass the contract
classes (`ExecutionBackendContract`, `DurableBackendContract`, and the
text/binary-safety variants), provide your backend factory, and the suite runs
the semantics every backend must satisfy — the same suite the shipped backends
run. `glyff.testing` also exports the reference `PruningEventHandler` and small
test helpers (`eid`, `value`, `save_execution`).

> **Planned** — the contract suite is being promoted from glyff's internal test
> tree to the public `glyff.testing` module
> ([#36](https://github.com/nueruyu/glyff/issues/36)). Until that lands, the
> contracts live under `glyff.tests.contracts` and are not a public surface.

## Planned contract extensions

Two capabilities are being added to the contract, both serving the same end
state: projecting executions into an application database without a second
consistency domain ([#42](https://github.com/nueruyu/glyff/issues/42)).

- **External transaction enlistment.** Construct a backend on an externally
  supplied connection/session (e.g. a SQLAlchemy `AsyncSession`), so glyff's
  transaction scope *participates in* the application's transaction instead of
  opening its own. A backend cohabiting the application database then commits an
  engrave completion and the application's own writes in **one transaction** —
  exactly-once projection falls out, with no idempotency machinery. Concrete
  `glyff-sqlalchemy`/`glyff-postgres` backends follow as separate packages once
  the interface exists.
- **Repository enumeration.** An iteration/query primitive (filterable by status
  and session scope), promoting what backends already do internally into the
  public contract. Consumers: outbox reconciliation sweeps (see
  [events](./events.md#projecting-into-an-application-database)) and userland
  [migration scripts](./migration.md#in-flight-sessions-across-code-changes).

The contract test suite grows with both, so custom backends can verify the new
surface the same way.

## Non-goal: a schema-customization interface

Considered and rejected. glyff core does not know tables exist — the `Backend`
protocol is already the correct customization boundary, and `glyff-sqlite` is
just one implementation of it. "User-defined schema" is served by writing a
custom backend and verifying it against the contract suite, not by
parameterizing shipped schemas.
