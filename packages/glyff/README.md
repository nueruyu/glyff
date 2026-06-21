# glyff

**G**uaranteed **L**ightweight **Y**ieldable **F**unction **F**oundation.

A primitive for pausing async functions across process and request boundaries,
and resuming them later from the same point.

## Install

```bash
pip install glyff
```

`glyff` has no dependencies beyond the Python standard library.

## Behavior

- Marked function calls are recorded in a session-scoped store, keyed by
  function identity, arguments, and call position.
- Re-invoking the same call within the same session returns the recorded
  result instead of re-executing.
- A call's outcome — success or failure — is permanent once recorded.
- Exception types configured with `Session(yield_on=...)` suspend execution at a
  function boundary; the session can be resumed later by entering it again with
  the same session id.

## Public API

| Name              | Description                                                     |
| ----------------- | --------------------------------------------------------------- |
| `engrave`         | Decorator that marks an async function for recording.           |
| `Session`         | Async context manager that scopes a sequence of engraved calls. |
| `ExecutionId`     | Identifier for a recorded function execution.                   |
| `ExecutionRecord` | Persisted execution state and result.                           |
| `ExecutionStatus` | Enum: `STARTED`, `COMPLETED`, `FAILED`.                         |
| `SessionStore`    | Protocol for storage backends.                                  |
| `Serializer`      | Protocol for value serialization.                               |
| `ArgsHasher`      | Protocol for argument hashing.                                  |

`Session(yield_on=(...))` registers application exception types that should be
treated as yield signals without making those exceptions inherit from glyff.

## Extending

- For persistent storage, see [`glyff-file-store`](https://pypi.org/project/glyff-file-store/).
- For Pydantic-typed serialization, see [`glyff-pydantic`](https://pypi.org/project/glyff-pydantic/).
- Custom backends can be written by implementing the `SessionStore`,
  `Serializer`, and `ArgsHasher` protocols.

## Status

Early development. APIs may change before v1.0.

## License

MIT
