# glyff

**G**uaranteed **L**ightweight **Y**ieldable **F**unction **F**oundation.

A primitive for pausing async functions across process and request boundaries,
and resuming them later from the same point.

## Example

```python
import glyff
import glyff_file_store
from glyff_pydantic import PydanticArgsHasher, PydanticSerializer


class UserInputRequired(Exception):
    pass


@glyff.engrave
async def ask_user(question: str, answer: str | None = None) -> str:
    """Ask the user a question. Pauses if no answer has been provided."""
    if answer is None:
        print(f"[USER_INPUT_REQUIRED] {question}")
        raise UserInputRequired()
    return answer


@glyff.engrave
async def greet(name: str, answer: str | None = None) -> str:
    nickname = await ask_user(f"What should I call you, {name}?", answer=answer)
    return f"Hello, {nickname}!"


async def main(session_id: str, answer: str | None = None):
    serializer = PydanticSerializer()
    backend = glyff_file_store.JsonFileBackend(
        base_dir=".sessions",
        session_id=session_id,
    )

    session = glyff.Session(
        id=session_id,
        executions=backend.executions,
        transactions=backend.transactions,
        serializer=serializer,
        hasher=PydanticArgsHasher(),
    )

    try:
        async with session:
            result = await greet("Alice", answer=answer)
            print(result)
    except UserInputRequired:
        print(
            f"Session paused. Resume with: --session-id {session_id} --answer <value>"
        )


if __name__ == "__main__":
    # First run: pauses when ask_user is reached.
    # asyncio.run(main(session_id=str(uuid.uuid4())))

    # Resume run: provide the same session id and the user's answer.
    # asyncio.run(main(session_id="<prior-session-id>", answer="Ali"))
    ...
```

On the resume run, `greet` re-executes from the top, but `ask_user` receives
the provided answer instead of pausing again.

## Behavior

- Marked function calls are recorded in a session-scoped execution repository, keyed by
  function identity, arguments, and call position.
- Re-invoking the same completed call within the same session returns the
  recorded result instead of re-executing.
- An exception persists nothing: the interrupted call stays `STARTED` (retried
  on resume), completed descendant work remains committed, and the original
  exception propagates.
- To pause a session intentionally, raise an application-owned exception and
  catch it outside the `Session` block.

## Per-execution metadata

Attach application data to the running call; it commits atomically with the
call's own record. Metadata is a keyed map, serialized with the session's
serializer, and lives as long as the execution's record.

```python
@glyff.engrave
async def step() -> str:
    ctx = glyff.get_context()
    await ctx.set_metadata("trace_id", "abc-123")
    ...
    return await ctx.get_metadata("trace_id", str)  # "abc-123"
```

Reads default to the current execution; pass `execution_id=` to read another
call's metadata.

## Pruning completed subtrees (userland)

Once a call completes, any resume returns its recorded result directly and the
calls underneath are never replayed. Those descendant records are dead weight,
but *when and whether* to delete them is a retention policy glyff does not ship.
glyff knows only **what** is unreachable — a completed call's strict
descendants; you decide the rest.

The context execution repository exposes `descendants_of` and `delete_many` (in
`ExecutionId` terms). Drive them from an `ExecutionCompleted` handler. The event
fires *after* the completion is durably committed, so the handler opens its own
transaction — GC is decoupled from the completion, and a prune failure never
rolls it back:

```python
from glyff import EventEmitter, EventHandler, ExecutionRepository, Session
from glyff.events import ExecutionCompleted


class PruneDescendants(EventHandler[ExecutionCompleted]):
    def __init__(self, executions: ExecutionRepository):
        self._executions = executions

    async def handle(self, event: ExecutionCompleted) -> None:
        async with event.context.get_transaction_scope():
            descendants = await self._executions.descendants_of(event.execution_id)
            if descendants:
                await self._executions.delete_many(descendants)


backend = glyff_file_store.JsonFileBackend(
    base_dir=".sessions",
    session_id=session_id,
)
session = Session(
    id=session_id,
    executions=backend.executions,
    transactions=backend.transactions,
    serializer=serializer,
    hasher=hasher,
    event_emitter=EventEmitter([PruneDescendants(backend.executions)]),
)
```

Replay and resume are unaffected — only unreachable records are removed. The
handler fires at every completion, so a nested call is pruned as soon as it
finishes, not when its top-level ancestor does.

## Status

Early development. APIs may change before v1.0.

## Packages

| Package                                           | Description                                                    |
| ------------------------------------------------- | -------------------------------------------------------------- |
| [`glyff`](./packages/glyff)                       | Core primitive. In-memory only, standard library dependencies. |
| [`glyff-file-store`](./packages/glyff-file-store) | File-backed execution repository (debug).                      |
| [`glyff-sqlite`](./packages/glyff-sqlite)         | SQLite-backed durable execution repository (production).       |
| [`glyff-pydantic`](./packages/glyff-pydantic)     | Pydantic-typed serialization and arg hashing.                  |

```bash
pip install glyff
pip install glyff-file-store
pip install glyff-sqlite
pip install glyff-pydantic
```

## License

MIT
