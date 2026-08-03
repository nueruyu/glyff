# Events

Sessions emit `ExecutionCompleted` and `ExecutionFailed` (`events.py`) to
handlers registered on the session's `EventEmitter`. Typical uses are pruning,
metrics, and projecting executions into an application database.

## Delivery semantics

- **Handler exceptions do not propagate.** Handlers run sequentially in
  registration order; an exception is logged, later handlers still run, and
  nothing reaches the caller of the engraved function (`_event_system.py`).
  Long-running work should be offloaded by the handler.
- **Delivery is at-most-once.** `ExecutionCompleted` is emitted after the
  completion transaction commits (`_executor.py`), so a crash in that window
  loses the event, and it is never re-fired on replay: a completed execution
  short-circuits to its recorded result before reaching the emit path.
- **`ExecutionFailed` reports an attempt, not a persisted change.** It is emitted
  after the transaction is rolled back and before the exception propagates.

Handlers must therefore be idempotent, and anything that must observe *every*
completion needs a reconciliation sweep over the store alongside the event path.

## Pruning completed subtrees

Once a call completes, any resume returns its recorded result and the calls
underneath are never replayed. Those descendant records are dead weight, but when
and whether to delete them is a retention policy glyff does not ship: it exposes
`descendants_of` and `delete_many` on the repository, and you decide the rest.

Drive them from an `ExecutionCompleted` handler, which runs after the completion
is committed and so opens its own transaction:

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
    argument_canonicalizer=argument_canonicalizer,
    event_emitter=EventEmitter([PruneDescendants(backend.repository)]),
)
```

Replay and resume are unaffected — only unreachable records are removed. The
handler fires at every completion, so a nested call is pruned as soon as it
finishes rather than when its top-level ancestor does, and a lost event only
defers cleanup to a later completion.

## Projecting into an application database

Applications often want history at their own units — conversations, messages,
approvals — in their own database, while the glyff store stays an internal
execution record. This needs no engine changes: a `materialize` wrapper outside
the engraved call writes idempotently, keyed by the canonical `ExecutionId`, with
a reconciliation sweep covering the events lost to at-most-once delivery. Where
the backend can [cohabit the application
database](./backends.md#planned-contract-extensions), both writes commit in one
transaction and the idempotency machinery is unnecessary.

> **Planned** — [#43](https://github.com/nueruyu/glyff/issues/43) holds the
> design; it blocks on [#40](https://github.com/nueruyu/glyff/issues/40)
> (canonical id encoding) and [#42](https://github.com/nueruyu/glyff/issues/42)
> (enumeration and transaction enlistment). The wrapper would live outside glyff
> core.
