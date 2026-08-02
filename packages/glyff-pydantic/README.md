# glyff-pydantic

Pydantic-based `Serializer` and `ArgsCanonicalizer` implementations for
[glyff](https://pypi.org/project/glyff/).

Enables glyff sessions to record arguments and results that are Pydantic
models, or any type Pydantic's `TypeAdapter` can handle.

## Install

```bash
pip install glyff-pydantic
```

This package depends on `glyff>=0.1.0` and `pydantic>=2.0`.

## Public API

| Name                 | Description                                                                                      |
| -------------------- | ------------------------------------------------------------------------------------------------ |
| `PydanticSerializer` | Serializes values to JSON using `TypeAdapter`. Restores typed values on read.                    |
| `PydanticArgsCanonicalizer` | Canonicalizes function arguments through Pydantic's own dump, for stable, type-aware identity. |

`PydanticSerializer` works with any type `TypeAdapter` handles. The
canonicalizer's reach is narrower by design: it dumps `BaseModel` instances and
represents the scalars pydantic knows (`datetime`, `UUID`, `Decimal`), while
every container is walked by glyff's shared canonicalization so mappings, sets
and opaque values follow one set of rules.

Canonical arguments are part of an execution's identity, so they must stay stable
across code changes — see
[execution identity](https://github.com/nueruyu/glyff/blob/main/docs/execution-identity.md)
for what the canonicalizer may and may not see.

## Status

Pre-1.0 — the API is unstable and will change.

## License

MIT
