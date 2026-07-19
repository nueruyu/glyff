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

Store formats carry a version stamp — `PRAGMA user_version` (or a meta table) in
the SQLite backend, a `format_version` field in the JSON file store. An unknown
or newer version is refused loudly. Sequential migrations between glyff versions
are glyff's responsibility, and are possible precisely because the stamp exists.

> **Planned** — [#41](https://github.com/nueruyu/glyff/issues/41). Released
> stores are unversioned (`_sqlite_client.py` does a bare `CREATE TABLE IF NOT
> EXISTS`), and data that cannot be identified cannot be migrated later — which
> is why stamping is the one pre-1.0 item that cannot be retrofitted. The stamp
> lands first; a migration runner comes later.

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

> **Planned** — [#41](https://github.com/nueruyu/glyff/issues/41) (generation
> stamp and typed mismatch error),
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
