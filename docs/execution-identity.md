# Execution identity

Every engraved call is recorded under an `ExecutionId`, and on resume a call is
matched to its record by that key. This page covers what the key is made of,
which code changes preserve recorded history, and how to choose engrave
boundaries.

## What the key is made of

An `ExecutionId` (`_models.py`) has four components:

| Component | Where it comes from |
| --- | --- |
| `parent_id` | The nearest engraved ancestor on the call stack, forming a chain up to the session root. |
| `name` | The engraved function's name — explicit `name=`/`version=` when given, derived from the function otherwise. |
| `args_hash` | A digest over the canonical form of the bound arguments, produced by the session's `ArgsCanonicalizer`. |
| `sequence` | An ordinal from an independent counter per `(parent_id, name, args_hash)` (`_sequencer.py`). |

Keys are **content-addressed, not positional**: the sequence counter is scoped to
the exact `(parent, name, args)` identity, not to a session-wide step number.

## Refactor compatibility guarantees

Because keys are content-addressed rather than positional:

- **Inserting** a call between existing ones shifts no existing keys; the new call
  executes fresh.
- **Deleting** a call leaves every other key untouched. The orphaned record is dead
  weight (see [pruning](./events.md#pruning-completed-subtrees)), not corruption.
- **Reordering distinct calls** — different name or different arguments — is
  compatible.

### Identical repeated calls

Calls that share parent, name, *and* arguments are matched by ordinal within that
identity class, so changing their count or relative order across a resume
mismatches silently.

- **Give repeated calls distinguishing arguments** (the loop element, an index),
  which removes the ordinal dependence.
- **Concurrent identical-args calls** are numbered in scheduling order, so their
  recorded results must be treated as interchangeable.

### Refactoring guide

- Extracting code into a **non-engraved** helper preserves all keys: the parent is
  the nearest *engraved* ancestor, and non-engraved frames are invisible to it.
- Wrapping code in a **new engraved** function rewrites the keys of everything
  beneath it — the new function becomes the parent in every descendant's chain.

## Explicit names and versions

Identity should come from your declaration, not from code shape. `engrave`
accepts an explicit name and version:

```python
@glyff.engrave
async def step(...) -> ...: ...          # name derived from the function

@glyff.engrave(name="chat.reply", version=2)
async def reply(...) -> ...: ...         # stable across renames and moves
```

The pair is canonicalized into the stored name (e.g. `"chat.reply@2"`); the key
stays a `str`. Duplicate explicit names are rejected at decoration time. The
resolved name is also what the canonicalizer sees, so a rename with a stable
`name=` invalidates nothing.

`ExecutionId` also has a public canonical string encoding, stable across resumes,
for use as an idempotency key when
[projecting into an application database](./events.md#projecting-into-an-application-database).

> **Planned** — [#40](https://github.com/nueruyu/glyff/issues/40), covering both.
> Today the name is derived from `__qualname__` (`_engrave.py`), so renaming an
> engraved function invalidates the history of paused sessions, and
> `ExecutionId.__str__` is a debug representation that must not be persisted
> (`_models.py`).

## Canonical arguments

Identity runs through a **canonical form**, not directly through a hash. The
session's `ArgsCanonicalizer` normalizes the bound arguments into the JSON data
model; glyff encodes that once, and those bytes are both digested into
`args_hash` and recorded on the execution. So for every recorded execution:

```
id.args_hash == execution.args.digest
```

`Execution.args` is an `EncodedArguments`, distinct from the `SerializedValue`
that carries results and metadata: only one of the two is a key's preimage.
`Execution` enforces the invariant on construction, and stores keep the bytes
verbatim — anything that re-encoded them would break the key. It is what lets a
[migration](./migration.md#in-flight-sessions-across-code-changes) rewrite an
argument and recompute the key from the record alone.

Canonicalizing is **not** serializing. It is one-way and deliberately lossy,
keeping only what identity depends on:

| Value | Canonical form |
| --- | --- |
| `bytes` | hex string |
| `set` / `frozenset` | list, ordered so the form is stable across processes |
| `tuple` | list |
| dataclass | only the fields it compares by — a `field(compare=False)` never distinguished two calls |
| type, named function | qualified name |
| `functools.partial` | its function and bound arguments |
| mapping key | coerced to a string; two keys that would collide are rejected rather than collapsed |

A value with no value representation raises rather than being approximated.

For values that are deliberately opaque — a service object passed as `self`, a
client handle — glyff owns the canonicalization contract, not the taxonomy of
what counts as opaque in your application.
`JsonArgsCanonicalizer(opaque_policy=...)` takes an `OpaquePolicy`, and
`glyff.serialization` ships two:

| Policy | Behavior |
| --- | --- |
| `RaiseOnOpaque` (default) | Rejects the value with `UnserializableArgumentError`, so distinct instances never silently collide. |
| `QualnameOpaque` (opt-in) | Identifies the value by its class' qualified name, collapsing every instance of a class to one representation. Correct only when the value carries no identity that should distinguish calls — a stateless client handle, not a per-user session. |

A policy receives an `OpaqueContext` rather than the bare value, so the signature
can grow without breaking implementations. Policy return values are namespaced,
so a policy that returns `"pkg.Cls"` cannot collide with a plain string argument
of the same text. The same classification governs what is *stored*, not just what
is hashed — an opaque value the policy rejects never reaches the store.

> **Planned** — [#37](https://github.com/nueruyu/glyff/issues/37): standard
> composable policies (marker attribute, type list, predicate). Today anything
> beyond `QualnameOpaque` means implementing `OpaquePolicy` yourself.

## Choosing engrave boundaries

The recommended pattern is **coarse-grained boundaries**: engrave a small number
of flat, explicitly-named, application-meaningful calls (a chat turn, a document
build), not every helper. Fewer boundaries means fewer signatures whose
compatibility you have to maintain.

- **Boundary arguments must be explicit and deterministically derived** from
  session inputs or recorded results. Non-engraved code re-runs live on resume, so
  a nondeterministic input (a timestamp, a random id) changes `args_hash` and
  cache-misses a completed boundary.
- **Everything inside a boundary re-executes on resume**, so in-boundary code must
  be pure or idempotent. Engrave non-idempotent side effects and pause points
  individually, at the grain you need them recorded.
- **Keep boundary arguments small and free of raw secrets** — pass an id or a
  reference, not a blob or a credential. Canonical argument values are recorded
  with the execution and are what a
  [migration](./migration.md#in-flight-sessions-across-code-changes) reads back.
