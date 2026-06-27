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
    client = glyff_file_store.FileClient(
        base_dir=".sessions",
        session_id=session_id,
    )
    store = glyff_file_store.JsonFileSessionStore(
        client=client,
        serializer=serializer,
    )

    session = glyff.Session(
        id=session_id,
        store=store,
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

- Marked function calls are recorded in a session-scoped store, keyed by
  function identity, arguments, and call position.
- Re-invoking the same completed call within the same session returns the
  recorded result instead of re-executing.
- Exceptions raised by a call are non-terminal by default: completed work is
  committed, the interrupted call remains `STARTED`, and the original exception
  propagates so the caller can decide whether to resume later.
- To pause a session intentionally, raise an application-owned exception and
  catch it outside the `Session` block.

## Pruning completed subtrees

Once a marked call completes, its result is recorded and any resume returns
that result directly — the calls it made underneath are never replayed. Their
records are therefore dead weight. Passing `prune_completed_descendants=True`
to `Session` deletes a call's descendant records the moment it completes:

```python
session = glyff.Session(
    id=session_id,
    store=store,
    hasher=hasher,
    prune_completed_descendants=True,
)
```

This is opt-in (default off) because it discards history you might otherwise
keep for inspection. The detection of which records are unreachable lives in
the executor; the store only answers `get_descendants` and deletes the ids it
is handed (in one batched `delete_executions` call), so the policy applies
uniformly across stores. Replay and resume are unaffected — only records that
can no longer be reached are removed.

Pruning fires at every completion, so a completed nested call's descendants are
removed immediately rather than lingering until the top-level call finishes.

## Status

Early development. APIs may change before v1.0.

## Packages

| Package                                           | Description                                                    |
| ------------------------------------------------- | -------------------------------------------------------------- |
| [`glyff`](./packages/glyff)                       | Core primitive. In-memory only, standard library dependencies. |
| [`glyff-file-store`](./packages/glyff-file-store) | Append-only file-backed session store (debug).                 |
| [`glyff-sqlite`](./packages/glyff-sqlite)         | SQLite-backed durable session store (production).              |
| [`glyff-pydantic`](./packages/glyff-pydantic)     | Pydantic-typed serialization and arg hashing.                  |

```bash
pip install glyff
pip install glyff-file-store
pip install glyff-sqlite
pip install glyff-pydantic
```

## License

MIT
