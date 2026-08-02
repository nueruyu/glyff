# Backends

glyff core is store-agnostic: everything durable goes through the `Backend`
contract. This page covers that contract, how to verify an implementation of it,
and what is being added.

## The contract

A `Backend` (`_interfaces.py`) is a bundle of two objects:

| Piece | Role |
| --- | --- |
| `ExecutionRepository` | Aggregate persistence: `get`, `save`, `descendants_of`, `delete_many`. |
| `TransactionProvider` | Owns transaction boundaries; `begin_transaction` returns a `Transaction` with `commit`/`rollback`. |

The repository stores `Execution` aggregates whole — status, result, metadata —
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

Implement the two objects and expose them as a bundle. Verify the implementation
against the shared contract suite in `glyff.testing`: subclass the contract
classes (`ExecutionBackendContract`, `DurableBackendContract`, and the
text/binary-safety variants) and provide your backend factory. The shipped
backends run the same suite. It also exports the reference `PruningEventHandler`
and the helpers `save_execution`, `value`, and the pair `eid` / `encoded_args`,
which build an execution that satisfies the `args_hash` invariant.

## Planned contract extensions

Two additions, both serving projection into an application database
([#42](https://github.com/nueruyu/glyff/issues/42)):

- **External transaction enlistment** — construct a backend on an externally
  supplied connection or session (e.g. a SQLAlchemy `AsyncSession`) so glyff's
  transaction scope joins the application's. Execution completion and the
  application's own writes then commit atomically. Concrete
  `glyff-sqlalchemy`/`glyff-postgres` backends follow once the interface exists.
- **Repository enumeration** — iteration filterable by status and session scope,
  which backends already do internally. Consumers are reconciliation sweeps (see
  [events](./events.md#projecting-into-an-application-database)) and userland
  [migration scripts](./migration.md#in-flight-sessions-across-code-changes).

The contract suite grows with both.

## Non-goal: a schema-customization interface

glyff core does not know tables exist; the `Backend` protocol is the
customization boundary, and `glyff-sqlite` is one implementation of it. A
user-defined schema means writing a backend and verifying it against the
contract suite, not parameterizing a shipped one.
