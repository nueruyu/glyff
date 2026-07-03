# Design memo 0001: Descendant ops and store layering

Status: Accepted
Date: 2026-07-02
Scope: `glyff`, `glyff-file-store`, `glyff-sqlite` (and future store implementations)

## Background / problem

The `SessionStore` ABC currently carries, on top of the resumption primitive
(`begin_transaction` / `start_execution` / `get_execution_record`),
`get_descendants` / `delete_executions` as **mandatory abstract methods**.
The only consumer of these is the opt-in `PruningEventHandler`; they exist
solely for pruning ("a completed call's descendant records are never replayed,
so they can be deleted").

Problems with this placement:

1. **scope**: Pruning is not load-bearing for correctness (it is pure GC /
   storage optimization). Baking it into the mandatory store contract implies
   "to be a valid glyff store you must participate in GC" (an ISP violation).
2. **transaction**: `delete_executions` rides the execution's durability
   transaction. There is no reason to couple a GC that only touches unreachable
   records to the correctness-critical commit.
3. **seam**: The three stores share `glyff.store.utils.execution_id_to_path`,
   but that is only because they happened to pick the same path encoding — the
   real addresses differ per store. Promoting the shared helper to a public
   seam would freeze that incidental implementation choice into a contract.
4. **responsibility**: Keeping descendant/delete as public methods on the
   concrete store turns it into a grab-bag of "SessionStore protocol adapter"
   plus "place that holds ExecutionId↔address domain knowledge".

## Facts (from the code)

- The **Client** (`MemoryClient` / `FileClient` / `SQLiteClient`) is explicitly
  a **generic KV**. It is not in `_interfaces.py` (i.e. a backend-private native
  extension point). All three already have `list_keys(prefix)` and
  `stage_delete`.
- **The real address differs completely per store**:

  | store | real address | per execution |
  |---|---|---|
  | memory | `execution::{path}::{part}` (`_make_key`/`_key_to_path` private to `_memory.py`) | 3 keys (status/result/error) |
  | file | **no key**; a key inside the single `executions.json` JSON dict | 0 keys (entry in a blob) |
  | sqlite | one `(namespace="executions", key=path)` row | 1 row |

  Only the `ExecutionId → path` encoding is shared. The file store has no
  per-execution key at all, so a "shared key-conversion helper" was never a
  sound abstraction.
- The only consumer of `get_descendants` / `delete_executions` is
  `PruningEventHandler`.
- The only reason the client is public is to let user-defined metadata be
  co-committed with execution records in the same transaction (glyff-sqlite
  README "External metadata" section). Co-commit happens either (1) inside an
  engraved body via the client's ContextVar staging (body scope), or (2) by
  opening `store.begin_transaction()` explicitly.
- The README's `Session(prune_completed_descendants=True)` flag does not exist
  in the current `_session.py`; in practice you pass
  `EventEmitter([PruningEventHandler()])`. Docs and implementation had drifted.

## Decisions

### 1. scope: move pruning out of glyff's core contract

- glyff should own only the *semantics* of "what is unreachable" (a completed
  call's strict descendants are never replayed again).
- "*When* and *how* to delete" (retention policy / timing / physical-delete
  cadence) is a backend/userland concern that glyff does not own.
- glyff does not bless "delete immediately on every completion" as a policy
  (in production, cascade delete / TTL / offline compaction are often cheaper).

### 2. transaction: GC does not ride the execution's durability transaction

- Because deletion only targets records made unreachable by a completed
  ancestor, it is **idempotent, best-effort, and does not need atomicity**.
  So it is not coupled to the execution commit.
- The client's `stage_delete` still requires staging (a transaction); there is
  no single-shot, non-transactional delete. "Outside the transaction" precisely
  means **not riding the execution's durability transaction / GC opens its own
  transaction**.

### 3. Layering: Store (transaction) and Repository (persistence) over a private client

Neither "grow descendant/delete methods on the concrete store" nor "teach the
generic client about ExecutionId" is chosen. The former makes responsibilities
a grab-bag; the latter fuses "atomic byte staging" (the slow I/O layer) with
"what an execution is" (the domain layer). Instead, split into three:

| Layer | Public | Responsibility | Vocabulary |
|---|---|---|---|
| **Client** (generic KV) | **internal** | staging / atomic commit / `list_keys` | `(namespace,)key` / bytes |
| **ExecutionRepository** (per backend, concrete) | non-core (below) | ExecutionId↔address, record codec, start/get, **per-execution metadata**, **descendant enumeration + deletion** | ExecutionId |
| **Store** (`SessionStore` impl, per backend) | public | **transaction** (`begin_transaction`) owner + resumption/metadata port (delegates to the repository) | protocol |

- **Both the Store and the Repository depend on the client** (for different
  uses). The Store wraps `client.begin_staging` / `commit_staged` into a
  `Transaction`. The Repository reads/writes records via `client.read` /
  `stage_write` / `list_keys`.
- **The two never reference each other directly; they coordinate only through
  the client's ContextVar staging.** The Store opens the transaction (staging)
  and the Repository's writes flow into it. This keeps them co-transactional
  yet loosely coupled (the single transaction owner is the Store).
- Address translation (memory's `::` expansion, file's in-blob dict, sqlite's
  row) is **hidden inside the Repository**. The only vocabulary that leaks out
  is `ExecutionId`. `glyff.store.utils.execution_id_to_path` is demoted to
  internal reuse and **not exposed as a public seam**.

#### The Repository is not a core abstraction yet (Shape A / single port)

The core (executor / `Context`) sees **only the single `SessionStore` port**
(Shape A). `Context` holds only `store`; the executor calls
`ctx.store.start_execution(...)` / `ctx.store.begin_transaction()`. The
Store↔Repository split stays an implementation detail *inside each backend
package*.

- **`ExecutionRepository` is not made a core ABC/Protocol** (with a single
  implementation shape and no need for polymorphism or third-party
  implementers, that would be premature abstraction — YAGNI). Revisit when a
  second implementation shape or an external extension need appears.
- The alternative (Shape B: `Context` holds both `store` and `repository`)
  would make the core reference the repository — needing at least a Protocol —
  contradicting "not a core abstraction". So Shape A is chosen.
- The descendant/delete and per-execution metadata bodies live on the
  Repository. Userland GC reaches them through the concrete store's exposed
  repository (e.g. `store.repository`).

### 4. Where descendant/delete live, and the Protocol (none for now)

- Not on the `SessionStore` port (`get_descendants` / `delete_executions`
  removed).
- Not on the generic client.
- Concrete methods on each backend's `ExecutionRepository`; inputs/outputs in
  `ExecutionId` vocabulary.
- **No store-agnostic abstract `Protocol` for now.** The userland GC handler
  calls the concrete repository's methods directly (there is no core-shipped
  handler today, and userland knows its concrete backend). Add a `Protocol`
  only once you want to type a store-agnostic handler.
- glyff does **not** ship a pruning handler. Userland composes the completion
  event with the repository's descendant/delete in its own transaction.

### 5. glyff keeps the reachability rule as documentation

Policy, handler, and transaction are all given up, but the *rule itself* —
"a completed call's descendants are unreachable" — is glyff's execution
semantics, so it stays on glyff's side as docs (or a single pure-function
helper). If future features (partial results / streaming / retries) can reopen
a subtree, a userland pruner that reinterprets this rule on its own risks
deleting still-reachable records.

### 6. Metadata: per-execution only, typed × keyed

- **Granularity**: after checking sefia's actual usage, **session-scoped
  storage is unnecessary; per-execution only is enough** (see the rubric at the
  end). So metadata lives as **part of the execution record owned by
  `ExecutionRepository`**, and **no separate `SessionMetadata` facet is
  created**. The earlier concerns (per-execution restriction / grab-bag /
  lifecycle coupling) are exactly what the per-execution requirement wants, so
  they dissolve.
- **Type and shape**: **typed (through the session's `Serializer`) × keyed map**
  (multiple keys per execution). Like results, a type hint is passed on read.
  Keyed means multiple `set` calls within a body do not clobber each other.
- **Write**: current execution only (`ctx.current_execution_id`).
  `ctx.set_metadata(key, value)`. Staged into the open transaction scope (the
  body scope while the body runs) so it co-commits.
- **Read**: `ctx.get_metadata` / repository, addressed by execution_id.
- `complete` / `fail` must **not overwrite** (must preserve) the execution
  record's metadata.

### 7. Make the client internal

The only reason to expose the client was the metadata seam; with that provided
as a first-class `ctx.set_metadata` / `get_metadata` in (6), the client can be
made internal.

- Drop the clients (`SQLiteClient` / `FileClient` / `MemoryClient`) from the
  public API.
- Store constructors take backend config directly and build the client
  internally (the file store no longer requires an externally built
  `FileClient`; sqlite already accepted `database_path`).
- Remove the `SQLiteSessionStore.client` property and the sqlite README
  "External metadata" section (raw client usage), replacing it with a
  per-execution metadata example.
- **Do not expose arbitrary SQL** (`read_sql` / `execute` are not public API).

## Public surface (after the decisions)

`Session` / store constructor (backend config, client not passed in) / `ctx`
(resumption + per-execution metadata `ctx.set_metadata` / `get_metadata`) /
serializer & hasher / the concrete repository for writing GC (`store.repository`
descendant/delete methods).
**The client, a standalone metadata facet, and a store-agnostic Protocol are
not public.**

## Rejected alternatives

- **status quo (keep as mandatory `SessionStore` methods)**: ISP violation,
  transaction coupling, forces the implementation on non-pruning stores.
- **teach the generic client about ExecutionId**: pollutes a generic KV with
  the domain; fuses the I/O layer and the domain layer's reasons to change.
- **keep descendant/delete as public methods on the concrete store**: makes
  responsibilities a grab-bag.
- **expose `execution_id_to_path` as a public seam**: freezes an incidentally
  matching path encoding into a contract; breaks for stores like the file store
  that have no key.
- **make metadata a standalone `SessionMetadata` facet**: overkill now that
  session-scoped storage is unnecessary (confirmed via sefia). Per-execution
  metadata belongs on the execution record.
- **Shape B (`Context` holds store and repository separately)**: makes the
  repository a core-referenced type, contradicting "not a core abstraction"
  (YAGNI).
- **make metadata bytes × single blob**: simpler than typed × keyed, but worse
  for consistency with results and for non-clobbering of multiple annotations.

## Impact / migration outline

1. Remove `get_descendants` / `delete_executions` from the `SessionStore` ABC;
   shape it into the resumption primitive + metadata (`set_metadata` /
   `get_metadata`).
2. Extract a concrete, non-core `ExecutionRepository` per backend. Move each
   store's `_to_record` / `_make_stored` / `_STATUS_NAMES` /
   `execution_id_to_path` usage / `get_descendants` / `delete_executions` into
   it. It holds the client and owns persistence.
3. Thin the `Store` down to "transaction owner + port (delegating to the
   repository)" (Shape A). Expose `store.repository` for userland GC.
4. Add per-execution metadata (typed × keyed) to the execution record in
   `ExecutionRepository`, and expose `ctx.set_metadata` (current execution) /
   `ctx.get_metadata` (by execution_id). Ensure `complete` / `fail` preserve
   metadata.
5. Remove `PruningEventHandler` from core. Drop the README's
   `prune_completed_descendants` / `PruningEventHandler` text and replace it
   with an example of writing your own GC using the completion event plus the
   repository's descendant/delete.
6. Demote `glyff.store.utils.execution_id_to_path`'s public treatment to
   internal reuse (underscore, or duplicate into each repository).
7. Make the client internal (drop `SQLiteClient` / `FileClient` / `MemoryClient`
   from the public API). Store constructors take backend config directly.
   Remove `SQLiteSessionStore.client` and the sqlite README "External metadata"
   section, replacing it with a per-execution metadata example. `read_sql` /
   `execute` are not public.
8. **Do not add a store-agnostic Protocol** (for now).

## Summary

The only universal vocabulary is `ExecutionId`. Over a **private client**, place
a **Store = transaction owner + SessionStore port** and a **Repository =
persistence (start/get, per-execution metadata, descendants, delete)**; the two
coordinate only through the client's ContextVar staging. The Repository is not a
core abstraction — it is concrete per backend. Descendant/delete live on the
Repository, not on the protocol or the client; there is no Protocol for now.
Metadata is per-execution only, typed × keyed, stored on the execution record.
Policy and handler are userland; GC uses its own transaction. glyff owns only
"what is unreachable".

## Appendix: metadata granularity rubric (applied to sefia)

**Result (2026-07-02): per-execution only is enough (no session-scoped
storage).** The rubric used:

- **Per-execution suffices**: the data is "this call's inputs/outputs/incidental
  info" and its lifetime matches the execution (fine to disappear on
  completion/pruning) → store it on the `ExecutionRepository` record.
- **Session scope needed**: whole-session state not tied to any execution
  (user_id / trace_id / session config / aggregate counters), especially if you
  want to write it before any execution exists, or keep it after an execution is
  deleted → a standalone facet is required (not the case here, so not adopted).
- **Both**: if there is both session state and per-call annotation, support
  both. Not needed here.
