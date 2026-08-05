# Migration & versioning

"Migration" is three problems with different owners. glyff migrates its own
store, defines the contract for your recorded payloads, and refuses a session
whose application version has changed — it does not rewrite your in-flight
sessions.

## The three layers

| Layer | Owner | Answer |
| --- | --- | --- |
| glyff's own store schema | glyff | Ordinary sequential store migrations, enabled by format-version stamps. |
| User payload (recorded results, metadata) | The serializer boundary — you | Recorded values must deserialize under the current code. |
| In-flight sessions across code changes | You, with glyff primitives | Raised as a typed error; never auto-migrated. |

The first and last are each guarded by a version, owned by a different party, at
different scopes: glyff's `FORMAT_VERSION` covers the whole store and is stamped
when it first writes one, while your `app_version` is recorded per session, by
whichever process claims it first.

### glyff's store schema

Store formats carry a version stamp, at `FORMAT_VERSION = 1` in both durable
backends.

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

- **Every session records an application-supplied generation marker.** Entering
  a session claims it for `Session(app_version=...)`, and entering one whose
  records were written under a different value raises `AppVersionMismatchError`
  instead of replaying them against code that may no longer mean the same thing.
  The value is opaque to glyff: what counts as a new generation is yours to
  decide.
- **Sessions you decide to carry across** are handled by a forward, offline
  batch that reads records through `ExecutionRepository.executions`, remaps
  them, and writes them back. Nothing is added to the resume path.

What makes such a script possible is that every execution records the
[canonical form of its arguments](./execution-identity.md#canonical-arguments),
byte-for-byte the preimage of its `arguments_digest`. Remapping an argument is
therefore a transformation of recorded JSON, with no dead Python types to keep
alive and no dependence on the canonicalizer that wrote the record.

> **Planned** — the runner and the atomic re-stamp it needs
> ([#39](https://github.com/nueruyu/glyff/issues/39)). Until then, reproducing
> glyff's canonical encoding yourself is not a supported surface, so pin paused
> sessions to the code that started them.

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
