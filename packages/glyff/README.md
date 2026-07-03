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
- Re-invoking the same completed call within the same session returns the
  recorded result instead of re-executing.
- Exceptions raised by a call are recorded as `FAILED`; completed descendant
  work remains committed, and the original exception propagates.
- To pause a session intentionally, raise an application-owned exception and
  catch it outside the `Session` block.

## Public API

| Name              | Description                                                     |
| ----------------- | --------------------------------------------------------------- |
| `engrave`         | Decorator that marks an async function for recording.           |
| `Session`         | Async context manager that scopes a sequence of engraved calls. |
| `ExecutionId`     | Identifier for a recorded function execution.                   |
| `Execution`       | Aggregate Root for a recorded function execution.               |
| `ExecutionRecord` | Read DTO for execution state and result.                        |
| `ExecutionStatus` | Enum: `STARTED`, `COMPLETED`, `FAILED`.                         |
| `ExecutionRepository` | Repository for execution aggregates.                       |
| `TransactionProvider` | Provider used by `TransactionScope`.                       |
| `Serializer`      | Protocol for value serialization.                               |
| `ArgsHasher`      | Protocol for argument hashing.                                  |

## Extending

- For persistent storage, see [`glyff-file-store`](https://pypi.org/project/glyff-file-store/).
- For Pydantic-typed serialization, see [`glyff-pydantic`](https://pypi.org/project/glyff-pydantic/).
- Custom backends can be written by implementing `ExecutionRepository` and
  `TransactionProvider`.

## Status

Early development. APIs may change before v1.0.

## License

MIT
