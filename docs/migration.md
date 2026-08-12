# Migration & versioning

"Migration" is three problems with different owners. glyff migrates its own
store, defines the contract for your recorded payloads, and refuses a call into
a domain whose version has changed — it does not rewrite your in-flight
sessions.

## The three layers

| Layer | Owner | Answer |
| --- | --- | --- |
| glyff's own store schema | glyff | Ordinary sequential store migrations, enabled by format-version stamps. |
| User payload (recorded results, metadata) | The serializer boundary — you | Recorded values must deserialize under the current code. |
| In-flight sessions across code changes | You, with glyff primitives | Raised as a typed error; never auto-migrated. |

The first and last are each guarded by a version, owned by a different party, at
different scopes: glyff's `FORMAT_VERSION` covers the whole store and is stamped
when it first writes one, while a **domain's** version is recorded per session and
per domain, by whichever process claims it first. A domain is the ownership
boundary for a set of engraved functions (see
[execution identity](./execution-identity.md#domains)), so a library on glyff
owns the version its records carry and the migration between generations of it,
without needing the application it runs inside to own either. Running that
migration stays the application's: it decides when a session goes offline and
which migrations the run carries.

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
> nothing yet converts a store from one format version to the next
> ([#41](https://github.com/nueruyu/glyff/issues/41)).

### User payload

Results and metadata are recorded through your `Serializer` and deserialized by
the *current* code, so **recorded values must deserialize under the code that
resumes them**. Forward-compatible model evolution — tolerant fields and
defaults, the normal Pydantic practice — is the tool for this; glyff adds nothing
on top.

### In-flight sessions across code changes

- **A session records the version of every domain it has entered.** Calling a
  domain-bound function lazily claims or verifies that domain's version. There
  are three outcomes, and only three:

  | The session records | What happens |
  | --- | --- |
  | nothing for this domain | the current version is recorded, and the call proceeds |
  | the same version | the call proceeds |
  | a different version | `DomainVersionMismatchError`, with nothing changed |

  The version is opaque to glyff: what counts as a new generation is the
  domain owner's to decide. The error carries `domain_id`, `recorded_version`
  and `current_version`, so a caller can route the session to migration without
  reading the message.

  Only a session that already records a version for the domain needs migrating —
  which is not the same as one holding records in it, since a claim lands before
  call identity is resolved.

  The check moved from session entry to first use, which is what makes per-domain
  versions possible: a session has no single version to check on the way in. So a
  mismatch surfaces deeper than it used to, and other domains may already have
  run. What has *not* happened is any write to the mismatched domain's records.
- **Sessions you decide to carry across** are handled by a forward, offline
  batch: a `MigratableBackend`'s `session_migration` takes the session
  exclusively, hands its metadata and executions to a `SessionMigrator`, and
  stores what comes back — the records and the versions they were written under
  in one atomic step, so "migrated but still stamped for the old version" is not
  a state a store can be found in. Nothing is added to the resume path.
  Taking the session offline is yours: the exclusion lasts for the call, and
  glyff does not stop a worker on the old version from resuming afterwards.

  So a mismatch is a *signal*, not a trigger: catch
  `DomainVersionMismatchError`, take the session offline, plan the domain
  migrations it needs, replace the session in one `SessionMigration.run()`, and
  resume on the new version. Migrating one domain in the middle of a running
  session would leave the rest of it recorded under versions that no longer mean
  the same thing — which is exactly the state the atomic replacement exists to
  prevent.

  A `StoredSession` refuses to be built unless it records a version for every
  domain named anywhere in its executions' identity chains, ancestors included.
  Dropping a domain's version therefore means remapping the descendants that
  still name it. The versions themselves are held to what a `Domain` could
  declare — non-empty — so a migration cannot leave behind a version no running
  process could ever match.

What makes such a script possible is that every execution records the
[canonical form of its arguments](./execution-identity.md#canonical-arguments),
byte-for-byte the preimage of its `arguments_digest`. Remapping an argument is
therefore a transformation of recorded JSON, with no dead Python types to keep
alive. What canonicalized the record is not needed to read it back; a migration
is handed the canonicalizer the *resuming* session will use, so the keys it
writes are the keys that session goes looking for.

### Writing one

`ExecutionMigrator` is the `SessionMigrator` to reach for. A migration is the
boundaries that changed shape — what each one was, what it became, and the
conversion between their arguments:

```python
from glyff.migration import DomainVersionTransition, ExecutionMigrator, ExecutionShape

migrator = ExecutionMigrator(
    canonicalizer=canonicalizer,
    version_transitions={
        "com.example.payments": DomainVersionTransition.between("1", "2")
    },
)
migrator.remap(
    ExecutionShape.from_names("com.example.payments", "authorize", "order", "units"),
    ExecutionShape.from_names("com.example.payments", "charge", "order_id", "cents"),
    convert_arguments=lambda order, units: {
        "order_id": order["id"],
        "cents": units * 100,
    },
)

report = await backend.session_migration.run(SessionId("order-42"), migrator)
```

An `ExecutionShape` is a domain, the name records carry, and the names of the
arguments a call is bound to — no Python signature and no version. It is spelled
out rather than read off the function it names, because taking it from a live
signature would let a later, unrelated change there silently reshape records the
migration claims to know.

Every side is then checked against what is actually there:

- `version_transitions` names the generations this migration is *for* — the
  version it reads and the version it writes, per domain. A session recording
  anything else is refused, so a v1→v2 migration cannot be applied to a v3
  session whose boundaries happen to match. Because a shape carries no version,
  these are the only thing tying a rule to a generation, so **every domain a
  rule touches needs one**: a domain that only carries records across unchanged
  declares the same version twice, and one the session has never entered uses
  `DomainVersionTransition.from_unclaimed("1")`. Domains no rule touches keep the
  versions they had, so a library can publish a migration for its own domain
  without knowing what else the session has entered.
- Records whose argument names are not the ones declared are refused, and so is
  a conversion that returns the wrong names. Dropping is held to the same check,
  since it is the destructive one.

An `ExecutionShape`'s `argument_names` are every name a call carries, defaults
included, so a boundary that gained a default has the migration write it out.

`convert_arguments` receives the recorded arguments by their old names and
returns the ones the new shape is keyed by. Leave it out when the names did not
change and the recorded form is kept as it is. It runs once per class of
[identical repeated calls](./execution-identity.md#identical-repeated-calls),
which are recorded with the same arguments by definition. `drop` removes a
boundary's records, and everything recorded beneath them, since a descendant
outlives its parent only as weight no resume can reach.

A remap rebuilds every descendant's chain onto its remapped ancestor.
**Ordinals it leaves alone**: one orders a call among the identical calls the
resumed code will make, and a migration knows nothing about how many of those
there are.

**Values with no value representation** arrive wrapped in `Opaque`, carrying
whatever the [`OpaquePolicy`](./execution-identity.md#canonical-arguments) put in
the key — wherever they sit, since a policy applies at any depth. Return one
unchanged and the argument keys the call exactly as it did; there is nothing to
rebuild, because there was nothing to record.

A conversion computes with the recorded canonical form, never the values that
produced it — see [canonical arguments](./execution-identity.md#canonical-arguments)
for what that form keeps and what it drops.

#### What changing the order does

| What changed | What a migration owes it |
| --- | --- |
| The order of a boundary's arguments | Nothing. A key is a mapping of names, and its encoding sorts them. |
| The order of distinct calls | Nothing. Keys are content-addressed, not positional. |
| The count or relative order of [identical repeated calls](./execution-identity.md#identical-repeated-calls) | This is the one glyff cannot carry. |

Repeated calls that share parent, name and arguments are matched by ordinal, and
nothing records which of them ran first. So a migration that gathers calls
recorded separately into one such class — two boundaries renamed onto one, or a
conversion that maps distinct arguments onto one value — is refused with
`MigrationOrdinalAmbiguityError` rather than given an order glyff invented. Give
those calls arguments that tell them apart, or drop one.

> **Planned** — migration chains, so a session whose stamp trails the code by
> more than one generation applies the steps in sequence, and a published form so
> a library can ship the migrations for its own domain
> ([#39](https://github.com/nueruyu/glyff/issues/39)). Today a migration
> describes one step, and running it is the application's.

## Running without migration

Paused sessions run to completion on the code version that started them: route
resumes to a worker pinned to the old domain versions and start new sessions on
the new ones. Combined with
[coarse-grained boundaries](./execution-identity.md#choosing-engrave-boundaries),
most deployments need nothing else. This is a supported mode, not a workaround.

## Deliberately not planned

A Temporal-style `patched()` API — branching on version markers inside recorded
history — is not planned until real demand appears. It adds a large permanent API
for a problem the version stamp plus worker pinning already covers.
