# glyff

**G**uaranteed **L**ightweight **Y**ieldable **F**unction **F**oundation.

A primitive for pausing async functions across process and request boundaries,
and resuming them later from the same point.

## Example

```python
import glyff
import glyff_file_store
from glyff_pydantic import PydanticArgumentCanonicalizer, PydanticSerializer


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
        backend=backend,
        serializer=serializer,
        argument_canonicalizer=PydanticArgumentCanonicalizer(),
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

- Marked function calls are recorded in a session-scoped execution repository,
  keyed by function identity, arguments, and an ordinal among identical calls.
- Re-invoking the same completed call within the same session returns the
  recorded result instead of re-executing.
- An exception persists nothing: the interrupted call stays `STARTED` (retried
  on resume), completed descendant work remains committed, and the original
  exception propagates.
- To pause a session intentionally, raise an application-owned exception and
  catch it outside the `Session` block.

Keys are content-addressed, not positional, so inserting, deleting, and
reordering distinct calls leave existing keys intact. See
**[Execution identity](./docs/execution-identity.md)** for the guarantees, the
hazards, and how to choose engrave boundaries.

## Per-execution metadata

Attach application data to the running call. Metadata is owned by the
`Execution` aggregate: `ctx.metadata.set(...)` stages it into the active
transaction, serialized with the session's serializer, so metadata set in an
engraved function body commits — or rolls back — together with that execution's
`COMPLETED` status and result.

```python
@glyff.engrave
async def step() -> str:
    ctx = glyff.get_context()
    await ctx.metadata.set("trace_id", "abc-123")
    ...
    return await ctx.metadata.get("trace_id", str)  # "abc-123"
```

Reads default to the current execution; pass `execution_id=` to read another
call's metadata.

## Events and pruning

Sessions emit `ExecutionCompleted` and `ExecutionFailed` to handlers registered
on the session. The reference use is pruning: a completed call's descendants are
never replayed, and a handler can delete them. See **[Events](./docs/events.md)**
for delivery semantics, the pruning handler, and projecting executions into your
own database.

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

## Documentation

- **[Execution identity](./docs/execution-identity.md)** — how calls are keyed.
- **[Events](./docs/events.md)** — delivery semantics, pruning, projections.
- **[Backends](./docs/backends.md)** — the backend contract and writing your own.
- **[Migration & versioning](./docs/migration.md)** — store stamps and in-flight
  sessions.
- **[Docs index](./docs/README.md)** — the map, with a suggested reading path.

## Status

Pre-1.0 — the API is unstable and will change. The
[docs](./docs/README.md) mark what is planned and not yet released.

## License

MIT
