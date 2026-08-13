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
| `Domain`              | Versioned ownership boundary; its `engrave` marks a function.   |
| `Session`             | Async context manager that scopes a sequence of engraved calls. |
| `ExecutionId`         | Identifier for a recorded function execution.                   |
| `DomainId`            | A domain's persistent machine identifier.                       |
| `ExecutionName`       | The recorded name of an engraved function.                      |
| `ArgumentsDigest`     | Digest of a call's canonical arguments.                         |
| `Execution`           | Aggregate Root for a recorded function execution.               |
| `ExecutionStatus`     | Enum: `STARTED`, `COMPLETED`.                                   |
| `SerializedValue`     | Serializer-neutral persisted value (results, metadata).         |
| `CanonicalArguments`     | A call's encoded canonical arguments, the preimage of its key. |
| `CanonicalArgumentValue` | A logical canonical value, including a fallback.              |
| `CanonicalFallback`      | A fallback representation in a canonical argument.            |
| `Metadata`            | Metadata entry owned by an `Execution`.                         |
| `ExecutionRepository` | Repository for execution aggregates.                            |
| `Transaction`         | Active transaction boundary.                                    |
| `TransactionProvider` | Provider used by `TransactionScope`.                            |
| `Serializer`          | Protocol for value serialization.                               |
| `ArgumentCanonicalizer`   | Contract for normalizing bound call arguments into a canonical form. |
| `CanonicalValue`      | A value in the JSON data model.                                 |
| `SessionId`           | The name a session's records are stored under.                  |

`glyff.migration` carries the offline half: `MigratableBackend` and
`SessionMigration` for a store that can replace a session atomically,
`SessionMigrator` for what a session should become, and `DomainMigration` —
declared as the `ExecutionShape` pairs that changed shape — as the one glyff
ships.
See [migration](https://github.com/nueruyu/glyff/blob/main/docs/migration.md).

Every engraved function belongs to a domain, which owns and versions its
records:

```python
engrave = glyff.Domain("com.example.payments", version="3").engrave
```

The first call into a domain records that version for the session, and a later
call under a different one raises `DomainVersionMismatchError` rather than
migrating. See
[migration](https://github.com/nueruyu/glyff/blob/main/docs/migration.md).

Persistent boundaries should declare an identity such as
`@engrave(name="chat.reply")`, so Python function renames do not change recorded
keys. Without `name`, glyff derives it from the function's `__qualname__`.

## Extending

- For persistent storage, see
  [`glyff-sqlite`](https://pypi.org/project/glyff-sqlite/) (production) and
  [`glyff-file-store`](https://pypi.org/project/glyff-file-store/) (debug).
- For Pydantic-typed serialization, see [`glyff-pydantic`](https://pypi.org/project/glyff-pydantic/).
- Custom backends provide an `ExecutionRepository`, a `TransactionProvider` and
  `claim_domain`, usually through a small backend bundle. See
  [the backends doc](https://github.com/nueruyu/glyff/blob/main/docs/backends.md).
- `glyff.testing` carries a conformance suite for each extension point, listed in
  [the backends doc](https://github.com/nueruyu/glyff/blob/main/docs/backends.md#writing-your-own)
  (*planned:* [#36](https://github.com/nueruyu/glyff/issues/36)).

## Status

Pre-1.0 — the API is unstable and will change. See the
[documentation](https://github.com/nueruyu/glyff/tree/main/docs) for the
guarantees and what is planned.

## License

MIT
