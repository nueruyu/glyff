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

This package depends on `glyff>=0.14.0`.

## Public API

| Name                      | Description                                               |
| ------------------------- | --------------------------------------------------------- |
| `JsonFileBackend`         | Bundle exposing the store's collaborators.                |
| `FileExecutionRepository` | Debug repository writing pretty-printed JSON.             |
| `FileTransactionProvider` | Transaction provider for the file backend.                |
| `FileAppVersionStore`     | Reads and writes the recorded application version.        |

Construct it with a `base_dir` and `session_id`:

```python
from glyff_file_store import JsonFileBackend

backend = JsonFileBackend(base_dir=".sessions", session_id="my-session")
```

The underlying `FileClient` is internal and not part of the public API.

## JSON debug format

`JsonFileBackend` stores each session under `<base_dir>/<session_id>/` in a
single pretty-printed JSON file (`executions.json`). The execution map is read
from that file on access and rewritten atomically on every commit.
Execution results and metadata are stored as embedded JSON values, so
serializers used with this backend must produce JSON text. Canonical arguments
are stored as a JSON *string* instead: the execution's key is the digest of
those exact bytes, so they are kept verbatim rather than re-encoded.

A `glyff_format.json` marker beside it carries both versions the store knows
about: glyff's own `format_version`, so a session written by an incompatible
build is refused rather than misread, and the `app_version` its records were
written under. The latter is written through the same staged directory swap as
the records, so a migration cannot rewrite one without the other.

This format is intended for debugging and manual inspection, not as the durable
or high-throughput backend.

## Commit atomicity

The store commits the staged ops for an entire session directory as a unit.
Each commit builds the full new session state in a sibling `.commit-*`
directory and then swaps it into place using directory renames. All staged ops
are visible together or none are, regardless of how many files were touched,
and a writer callback raising mid-commit leaves the on-disk session unchanged.

If a process dies mid-commit, the next time the session directory is opened it
is restored from `session.bak` when the live directory is missing, or the stale
backup is removed when the live directory is already in place. Any orphan
`.commit-*` temp directories are also removed.

## Status

Pre-1.0 — the API is unstable and will change.

## License

MIT
