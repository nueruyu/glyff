# glyff-pydantic

Pydantic-based `Serializer` and `ArgumentCanonicalizer` implementations for
[glyff](https://pypi.org/project/glyff/).

Lets a glyff session record results of any type Pydantic's `TypeAdapter` can
handle, and key executions on arguments that are Pydantic models.

## Install

```bash
pip install glyff-pydantic
```

This package depends on `glyff>=0.14.0` and `pydantic>=2.0`.

## Public API

| Name                 | Description                                                                                      |
| -------------------- | ------------------------------------------------------------------------------------------------ |
| `PydanticSerializer` | Serializes values to JSON using `TypeAdapter`. Restores typed values on read.                    |
| `PydanticArgumentCanonicalizer` | Canonicalizes function arguments through Pydantic's own dump, for stable, type-aware identity. |

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
