# Events

Sessions emit events (`ExecutionCompleted`, `ExecutionFailed` — `events.py`) that
handlers registered on the session's `EventEmitter` observe. Events are the seam
for userland reactions to execution lifecycle: pruning, metrics, projections.
This page states the delivery semantics precisely — they are weaker than they
look, and the difference matters for anything that must not miss an event.

## Delivery semantics

Two separate facts hide behind "best-effort":

- **Handler exceptions do not propagate.** Handlers run sequentially in
  registration order; an exception is logged, later handlers still run, and
  nothing reaches the caller of the engraved function (`_event_system.py`).
  Long-running work should be explicitly offloaded by the handler.
- **Delivery is at-most-once.** `ExecutionCompleted` is emitted *after* the
  completion transaction commits (`_executor.py`), so a crash in the window
  between commit and emit loses the event. And it is **never re-fired on
  replay**: a completed execution short-circuits to its recorded result before
  the emit path is reached. `ExecutionFailed` is emitted when the engraved
  function body raises — after the transaction is rolled back, before the
  exception propagates — so it observes an attempt, not a persisted state
  change.

Consequences:

- **Handlers must be idempotent** — an execution that is retried after a
  mid-transaction failure can complete (and emit) on a later attempt.
- **Events alone cannot guarantee a projection.** Anything that must observe
  *every* completion needs a reconciliation sweep over the store in addition to
  the event fast path — see
  [projecting into an application database](#projecting-into-an-application-database).

## Pruning completed subtrees

Once a call completes, any resume returns its recorded result directly and the
calls underneath are never replayed. Those descendant records are dead weight,
but *when and whether* to delete them is a retention policy glyff does not ship.
glyff knows only **what** is unreachable — a completed call's strict
descendants; you decide the rest.

The repository exposes `descendants_of` and `delete_many` (in `ExecutionId`
terms). Drive them from an `ExecutionCompleted` handler; the event fires after
the completion is durably committed, so the handler opens its own transaction:

```python
from glyff import EventEmitter, EventHandler, ExecutionRepository, Session
from glyff.events import ExecutionCompleted


class PruneDescendants(EventHandler[ExecutionCompleted]):
    def __init__(self, repository: ExecutionRepository):
        self._repository = repository

    async def handle(self, event: ExecutionCompleted) -> None:
        async with event.context.get_transaction_scope():
            descendants = await self._repository.descendants_of(event.execution_id)
            if descendants:
                await self._repository.delete_many(descendants)


session = Session(
    id=session_id,
    backend=backend,
    serializer=serializer,
    hasher=hasher,
    event_emitter=EventEmitter([PruneDescendants(backend.repository)]),
)
```

Replay and resume are unaffected — only unreachable records are removed. The
handler fires at every completion, so a nested call is pruned as soon as it
finishes, not when its top-level ancestor does. At-most-once delivery is
acceptable here: a lost event just defers the cleanup to a later completion.

## Projecting into an application database

Applications often want history at their own units — conversations, messages,
approvals — in their **own** database, while the glyff store stays an internal
execution record. The design direction, worked out in the
[materialize RFC (#43)](https://github.com/nueruyu/glyff/issues/43), needs no
engine changes; glyff core contributes only canonical execution ids, events, and
the backend-contract extensions. The decorator itself lives in a companion
package, outside glyff core. In brief:

- **At-least-once + idempotency** (any backend): a `materialize` wrapper
  *outside* the engraved call, writing with `put_if_absent` keyed by the
  canonical `ExecutionId`. Replay semantics make it convergent across every
  crash window. The event fast path is backed by a **mandatory** reconciliation
  sweep, because delivery is at-most-once (see above).
- **Cohabited backend, single transaction** (when your store can live in your
  application database): the completion commit and your own writes commit
  together, and the idempotency machinery becomes unnecessary — see
  [backends](./backends.md#planned-contract-extensions).

> **Planned** — [#43](https://github.com/nueruyu/glyff/issues/43), blocked on
> the canonical id encoding
> ([#40](https://github.com/nueruyu/glyff/issues/40)) and repository
> enumeration / transaction enlistment
> ([#42](https://github.com/nueruyu/glyff/issues/42)).
