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

Construct it with a `base_dir`; it holds every session written under it, each in
its own directory:

```python
from glyff_file_store import JsonFileBackend

backend = JsonFileBackend(base_dir=".sessions")
```

The underlying `FileClient` is internal and not part of the public API.

## JSON debug format

`JsonFileBackend` stores each session in a single pretty-printed JSON file
(`executions.json`) under its own directory in `base_dir`. The directory name is
the session id percent-escaped — everything outside `A-Za-z0-9-_` is escaped,
including `.`, so an id can never be a path segment, escape `base_dir`, or
collide with the dot-prefixed names the store keeps for itself. Ordinary ids
(`chat-42`, a UUID) come through unchanged.

Directory names fold case on macOS and Windows, so two sessions whose ids differ
only in case share a directory there.

The execution map is read from that file on access and rewritten atomically on
every commit. Execution results and metadata are stored as embedded JSON values,
so serializers used with this backend must produce JSON text. Canonical
arguments are stored as a JSON *string* instead: the execution's key is the
digest of those exact bytes, so they are kept verbatim rather than re-encoded.

A `glyff_format.json` marker beside the session directories records the store's
`format_version`, so a store written by an incompatible build is refused rather
than misread. Each session directory carries a `session.json` with the
`app_version` its records were written under.

A commit swaps one session directory into place. A transaction that writes to a
second session raises `RuntimeError` rather than committing as two swaps: the
unit of atomicity here is the swap, and a `Session` only ever touches its own
records.

## Concurrency

Every access to committed state — commits, version claims, format stamping,
recovery from a crashed commit, *and reads* — is serialized by a `.glyff.lock`
file beside the session directories, so processes sharing a `base_dir` do not
race. An in-process `asyncio.Lock` sits inside it, because the file lock is
re-entrant per handle and so does not serialize tasks holding the same one.

Reads are held under the lock because a swap renames the session directory away
before renaming the replacement in: a read landing between the two would find
nothing and report a recorded execution as missing, which core reads as "never
ran".

`session.json` is replaced atomically (write a temporary in the same directory,
`fsync`, then `os.replace`), so a crash mid-write leaves the previous version
rather than half of the new one.

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
