# glyff

**G**uaranteed **L**ightweight **Y**ieldable **F**unction **F**oundation.

A primitive for pausing async functions across process and request boundaries,
and resuming them later from the same point.

## Example

```python
import glyff
import glyff.exceptions
import glyff_file_store
from glyff_pydantic import PydanticArgsHasher, PydanticSerializer


@glyff.engrave
async def ask_user(question: str, answer: str | None = None) -> str:
    """Ask the user a question. Yields if no answer has been provided."""
    if answer is None:
        print(f"[USER_INPUT_REQUIRED] {question}")
        raise glyff.exceptions.YieldException()
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
    store = glyff_file_store.FileSessionStore(
        client=client,
        serializer=serializer,
        format="json",
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
    except glyff.exceptions.YieldException:
        print(
            f"Session paused. Resume with: --session-id {session_id} --answer <value>"
        )


if __name__ == "__main__":
    # First run: yields when ask_user is reached.
    # asyncio.run(main(session_id=str(uuid.uuid4())))

    # Resume run: provide the same session id and the user's answer.
    # asyncio.run(main(session_id="<prior-session-id>", answer="Ali"))
    ...
```

On the resume run, `greet` re-executes from the top, but `ask_user`'s recorded
input on the second invocation returns the previously provided answer instead
of yielding again.

## Behavior

- Marked function calls are recorded in a session-scoped store, keyed by
  function identity, arguments, and call position.
- Re-invoking the same call within the same session returns the recorded
  result instead of re-executing.
- A call's outcome — success or failure — is permanent once recorded.
- `YieldException` suspends execution at a function boundary; the session
  can be resumed later by entering it again with the same session id.

## Streaming

A marked function annotated to return an `AsyncIterator` (or `AsyncGenerator`)
is treated as a stream: the wrapper transparently yields each item to the
caller while recording the run.

```python
from collections.abc import AsyncIterator


@glyff.engrave
async def tokens(prompt: str) -> AsyncIterator[str]:
    async for chunk in some_llm_stream(prompt):
        yield chunk


async for chunk in tokens("hello"):
    print(chunk, end="")
```

- A stream is recorded as a single value (the list of all yielded items) and
  only when it completes naturally. Once recorded, re-invoking the same call
  replays the stored items without re-running the function.
- An unfinished stream — interrupted by `YieldException`, broken out of early,
  or lost to a crash — records nothing and re-runs from scratch on the next
  invocation. Streams are never resumed mid-iteration.

Because the full result is buffered in memory and persisted to the store,
**do not return large or unbounded streams**. For heavy payloads, write the
data to a file (or external store) inside the function and return only a
reference, or split the work into cursor-addressable batches:

```python
@glyff.engrave
async def batch(cursor: int) -> list[Item]:
    ...  # each batch is recorded independently and replays on resume
```

## Status

Early development. APIs may change before v1.0.

## Packages

| Package                                           | Description                                                    |
| ------------------------------------------------- | -------------------------------------------------------------- |
| [`glyff`](./packages/glyff)                       | Core primitive. In-memory only, standard library dependencies. |
| [`glyff-file-store`](./packages/glyff-file-store) | Append-only file-backed session store.                         |
| [`glyff-pydantic`](./packages/glyff-pydantic)     | Pydantic-typed serialization and arg hashing.                  |

```bash
pip install glyff
pip install glyff-file-store
pip install glyff-pydantic
```

## License

MIT
