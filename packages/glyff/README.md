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

- Marked function calls are recorded in a session-scoped execution repository,
  keyed by function identity, arguments, and an ordinal among identical calls.
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
| `SerializedValue`     | Serializer-neutral persisted value (results, metadata).         |
| `EncodedArguments`    | A call's canonical arguments, the preimage of its key.          |
| `Metadata`            | Metadata entry owned by an `Execution`.                         |
| `ExecutionRepository` | Repository for execution aggregates.                            |
| `Transaction`         | Active transaction boundary.                                    |
| `TransactionProvider` | Provider used by `TransactionScope`.                            |
| `Serializer`          | Protocol for value serialization.                               |
| `ArgsCanonicalizer`   | Contract for normalizing call arguments into a canonical form.  |
| `CanonicalValue`      | The JSON data model value a canonicalizer returns.              |

`engrave` also takes an explicit identity —
`@engrave(name="chat.reply", version=2)` — so recorded histories survive
renames. *Planned:*
[#40](https://github.com/nueruyu/glyff/issues/40); released versions derive the
name from the function's `__qualname__`.

## Extending

- For persistent storage, see
  [`glyff-sqlite`](https://pypi.org/project/glyff-sqlite/) (production) and
  [`glyff-file-store`](https://pypi.org/project/glyff-file-store/) (debug).
- For Pydantic-typed serialization, see [`glyff-pydantic`](https://pypi.org/project/glyff-pydantic/).
- Custom backends provide separate `ExecutionRepository` and
  `TransactionProvider` objects, usually through a small backend bundle, and are
  verified against the shared contract suite in `glyff.testing` (*planned:*
  [#36](https://github.com/nueruyu/glyff/issues/36)). See
  [the backends doc](https://github.com/nueruyu/glyff/blob/main/docs/backends.md).

## Status

Pre-1.0 — the API is unstable and will change. See the
[documentation](https://github.com/nueruyu/glyff/tree/main/docs) for the
guarantees and what is planned.

## License

MIT
