# Migration & versioning

"Migration" is three problems with different owners. glyff's stance is
**mechanism, not policy**: it migrates its own store, defines the contract for
your payloads, and turns code-change divergence into a loud, typed error with
userland primitives to act on — it does not rewrite your in-flight sessions.

> Pre-1.0: this page shows the release-target behavior. Sections marked *Planned*
> link to the tracking issue.

## The three layers

| Layer | Owner | Answer |
| --- | --- | --- |
| glyff's own store schema | glyff | Ordinary sequential store migrations, enabled by format-version stamps. |
| User payload (recorded results, metadata) | The serializer boundary — you | Recorded values must deserialize under the current code. |
| In-flight sessions across code changes | You, with glyff primitives | Detected loudly; never auto-migrated. |

### glyff's store schema

Store formats carry a version stamp. The SQLite backend records it in a
`<table_prefix>_meta` table (alongside `<table_prefix>_executions`, default
prefix `glyff`); the JSON file store writes a `glyff_format.json` marker beside
the session's records. Both are at `FORMAT_VERSION = 1`.

A store written by an incompatible build is refused loudly with
`StoreFormatVersionError` rather than misread. A fresh or pre-versioning store is
stamped on first open. The in-memory backend is ephemeral and carries no stamp.

Stamping is deliberately the first thing that landed: data that cannot be
identified cannot be migrated later, which makes it the one pre-1.0 item that
cannot be retrofitted. Sequential migrations between glyff versions are glyff's
responsibility, and are possible precisely because the stamp now exists.

> **Planned** — a migration runner. The stamp is in place and refuses unknown
> versions; nothing yet converts a store from one format version to the next.

### User payload

Results and metadata are recorded through your `Serializer`, and replayed values
are deserialized by the *current* code. The contract is therefore yours to keep:
**recorded values must deserialize under the code that resumes them**.
Forward-compatible model evolution — the normal Pydantic practice of tolerant
fields and defaults — is exactly the right tool; glyff adds nothing on top.

### In-flight sessions across code changes

glyff does **not** auto-migrate a paused session onto new code. Instead:

- **Detect loudly.** A session records an application-supplied generation marker
  (`Session(app_version=...)`) on first write; resuming with a different value
  raises a typed `SessionVersionMismatch` instead of silently replaying old
  records against new code. The serializer identifier is stamped in the same
  slot, so deserializing with a different serializer also fails early.
- **Userland scripts.** For sessions you decide to carry across, the repository
  surface (including [enumeration](./backends.md#planned-contract-extensions))
  lets you write your own migration script — the same
  mechanism-not-policy escape hatch as [pruning](./events.md#pruning-completed-subtrees).

Those scripts are **forward, offline batches**, not hooks on the resume path.
Boundary arguments are persisted as canonical JSON alongside the execution
record, so a migration reads the old record, maps `old_args -> new_args`,
recomputes the key, and rewrites it — the same direction and shape as payload
migration, and as every schema migration tool. Two consequences worth stating:

- **The application version selects migrations; it is not part of the key.**
  `Session(app_version=...)` stamps the session and decides *which* migrations
  apply — a resume whose stamp trails the code applies the `vN -> vN+1` steps in
  sequence, with unchanged boundaries as the identity. Execution keys stay
  `(parent, name, args_hash, sequence)` and never carry the app version.
- **Failures surface at deploy time**, over every affected record, rather than on
  whichever paused session happens to resume in production. Nothing is added to
  the resume path.

Which key component moved decides the work: renames, `version=` bumps,
re-parenting and repeated-call renumbering never need the old arguments; only an
argument signature or value change does — which is why they are recorded.

> **Planned** — [#41](https://github.com/nueruyu/glyff/issues/41) (generation
> stamp and typed mismatch error — the store format stamp above has landed, this
> is the remaining half), [#47](https://github.com/nueruyu/glyff/issues/47)
> (canonical-JSON argument persistence and the migration shape described here),
> [#42](https://github.com/nueruyu/glyff/issues/42) (repository enumeration).
> Until they land, resuming a session on changed code diverges silently — pin
> paused sessions to the code that started them.

## The supported "no migration" mode

Running without migration is a first-class operating mode, not a workaround:
**paused sessions run to completion on the code version that started them.**
Concretely, route resumes to a worker pinned to the old version (blue-green
style) and let new sessions start on the new version. Combined with
[coarse-grained boundaries](./execution-identity.md#choosing-engrave-boundaries),
most deployments never need anything else.

## Deliberately not planned

A Temporal-style `patched()` API — branching on version markers *inside* recorded
history — is deliberately not planned until real demand appears. It trades a
large permanent API surface for a problem the version stamp plus worker pinning
already covers.
