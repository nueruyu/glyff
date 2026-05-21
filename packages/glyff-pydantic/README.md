# glyff-pydantic

Pydantic-based `Serializer` and `ArgsHasher` implementations for
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
| `PydanticArgsHasher` | Hashes function arguments by dumping them through `TypeAdapter` for stable, type-aware identity. |

Both work with arbitrary types supported by Pydantic v2: models, dataclasses,
unions, generics, and standard library types.

## Status

Early development. APIs may change before v1.0.

## License

MIT
