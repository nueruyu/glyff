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

- Marked function calls are recorded in a session-scoped execution repository, keyed by
  function identity, arguments, and call position.
- Re-invoking the same completed call within the same session returns the
  recorded result instead of re-executing.
- An exception persists nothing: the interrupted call stays `STARTED` (retried
  on resume), completed descendant work remains committed, and the original
  exception propagates.
- To pause a session intentionally, raise an application-owned exception and
  catch it outside the `Session` block.

## Public API

| Name                  | Description                                                     |
| --------------------- | --------------------------------------------------------------- |
| `engrave`             | Decorator that marks an async function for recording.           |
| `Session`             | Async context manager that scopes a sequence of engraved calls. |
| `ExecutionId`         | Identifier for a recorded function execution.                   |
| `Execution`           | Aggregate Root for a recorded function execution.               |
| `ExecutionStatus`     | Enum: `STARTED`, `COMPLETED`.                                   |
| `SerializedValue`     | Serializer-neutral persisted value.                             |
| `Metadata`            | Metadata entry owned by an `Execution`.                         |
| `ExecutionRepository` | Repository for execution aggregates.                            |
| `Transaction`         | Active transaction boundary.                                    |
| `TransactionProvider` | Provider used by `TransactionScope`.                            |
| `Serializer`          | Protocol for value serialization.                               |
| `ArgsHasher`          | Protocol for argument hashing.                                  |

## Extending

- For persistent storage, see [`glyff-file-store`](https://pypi.org/project/glyff-file-store/).
- For Pydantic-typed serialization, see [`glyff-pydantic`](https://pypi.org/project/glyff-pydantic/).
- Custom backends should provide separate `ExecutionRepository` and
  `TransactionProvider` objects, often through a small backend bundle.

## Status

Early development. APIs may change before v1.0.

## License

MIT
