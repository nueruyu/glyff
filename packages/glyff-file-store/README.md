# glyff-file-store

File-backed `ExecutionRepository` implementation for
[glyff](https://pypi.org/project/glyff/).

This is the human-readable **debug** backend: `JsonFileBackend` bundles a
file-backed execution repository and transaction provider, storing executions
in a single pretty-printed JSON map that is rewritten atomically on each
commit.

For the durable production backend, see
[`glyff-sqlite`](https://pypi.org/project/glyff-sqlite/).

## Install

```bash
pip install glyff-file-store
```

This package depends on `glyff>=0.14.0` and `filelock>=3.15`.

## Public API

| Name                      | Description                                               |
| ------------------------- | --------------------------------------------------------- |
| `JsonFileBackend`         | Bundle exposing the store's collaborators.                |
| `FileExecutionRepository` | Debug repository writing pretty-printed JSON.             |
| `FileTransactionProvider` | Transaction provider for the file backend.                |

Construct it with a `base_dir`; it holds every session written under it:

```python
from glyff_file_store import JsonFileBackend

backend = JsonFileBackend(base_dir=".sessions")
```

The underlying `FileClient` is internal and not part of the public API.

## JSON debug format

Everything the store holds lives in one pretty-printed, key-sorted document at
`<base_dir>/glyff.json`, nested by session id:

```json
{
  "format_version": 1,
  "sessions": {
    "orders": {
      "app_version": "v1",
      "executions": {}
    }
  }
}
```

`format_version` is glyff's own, so a store written by an incompatible build is
refused rather than misread; `app_version` is the application's, claimed by
whichever process opens the session first.

Execution results and metadata are stored as embedded JSON values, so
serializers used with this backend must produce JSON text. Canonical arguments
are stored as a JSON *string* instead: the execution's key is the digest of
those exact bytes, so they are kept verbatim rather than re-encoded.

The whole document is read on access and rewritten on every commit, which is
what keeps it readable and what makes it unsuitable for high-throughput or
large-scale use.

## Commit atomicity

A commit reads the document, applies the transaction's staged updates, and
replaces the file in one `os.replace`, so it is all-or-nothing however many
sessions it touched. A crash can only strand a temporary beside the document,
never leave the document itself half-written; the next open clears it.

## Concurrency

Writers and version claims hold a `.glyff.lock` file beside the document,
because each is a read-modify-write and atomic replacement alone would still let
one process overwrite another's records. An in-process `asyncio.Lock` sits
inside it, because the file lock is re-entrant per handle and so does not
serialize tasks holding the same one.

Readers hold nothing: a replacement is atomic, so a read sees either the whole
old document or the whole new one.

## Status

Pre-1.0 — the API is unstable and will change.

## License

MIT
