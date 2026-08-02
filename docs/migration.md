# Migration & versioning

"Migration" is three problems with different owners. glyff migrates its own
store, defines the contract for your recorded payloads, and refuses a session
whose code generation has changed — it does not rewrite your in-flight sessions.

## The three layers

| Layer | Owner | Answer |
| --- | --- | --- |
| glyff's own store schema | glyff | Ordinary sequential store migrations, enabled by format-version stamps. |
| User payload (recorded results, metadata) | The serializer boundary — you | Recorded values must deserialize under the current code. |
| In-flight sessions across code changes | You, with glyff primitives | Raised as a typed error; never auto-migrated. |

### glyff's store schema

Store formats carry a version stamp. The SQLite backend records it in a
`<table_prefix>_meta` table (alongside `<table_prefix>_executions`, default
prefix `glyff`); the JSON file store writes a `glyff_format.json` marker beside
the session's records. Both are at `FORMAT_VERSION = 2`.

A store written by an incompatible build raises `StoreFormatVersionError` rather
than being misread. A fresh or pre-versioning store is stamped on first open. The
in-memory backend is ephemeral and carries no stamp.

Stamping landed first because data that cannot be identified cannot be migrated
later; sequential migrations between glyff versions are glyff's responsibility
and are possible because the stamp exists.

> **Planned** — a migration runner. The stamp refuses unknown versions, but
> nothing yet converts a store from one format version to the next.

### User payload

Results and metadata are recorded through your `Serializer` and deserialized by
the *current* code, so **recorded values must deserialize under the code that
resumes them**. Forward-compatible model evolution — tolerant fields and
defaults, the normal Pydantic practice — is the tool for this; glyff adds nothing
on top.

### In-flight sessions across code changes

glyff does not auto-migrate a paused session onto new code. Instead:

- **A session records an application-supplied generation marker**
  (`Session(app_version=...)`) on first write, and resuming with a different
  value raises `SessionVersionMismatch` instead of replaying old records against
  new code. The serializer identifier is stamped in the same slot, so switching
  serializers also fails early.
- **Sessions you decide to carry across** are handled by your own migration
  script: a forward, offline batch that reads records through the repository,
  remaps them, and writes them back — the same userland escape hatch as
  [pruning](./events.md#pruning-completed-subtrees). Nothing is added to the
  resume path.

What makes such a script possible is that every execution records the
[canonical form of its arguments](./execution-identity.md#canonical-arguments),
byte-for-byte the preimage of its `args_hash`. Remapping an argument is therefore
a transformation of recorded JSON, with no dead Python types to keep alive and no
dependence on the canonicalizer that wrote the record.

> **Planned** — [#41](https://github.com/nueruyu/glyff/issues/41) (generation
> stamp and typed mismatch error; the store format stamp above has landed),
> [#42](https://github.com/nueruyu/glyff/issues/42) (repository enumeration, so
> a script can walk a session's records). The encoder and digest that recompute a
> rewritten key ship with the runner; until then, reproducing glyff's canonical
> encoding yourself is not a supported surface. Resuming a session on changed
> code diverges silently in the meantime, so pin paused sessions to the code that
> started them.

## Running without migration

Paused sessions run to completion on the code version that started them: route
resumes to a worker pinned to the old version and start new sessions on the new
one. Combined with
[coarse-grained boundaries](./execution-identity.md#choosing-engrave-boundaries),
most deployments need nothing else. This is a supported mode, not a workaround.

## Deliberately not planned

A Temporal-style `patched()` API — branching on version markers inside recorded
history — is not planned until real demand appears. It adds a large permanent API
for a problem the version stamp plus worker pinning already covers.
