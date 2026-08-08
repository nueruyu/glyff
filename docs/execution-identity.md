# Execution identity

Every engraved call is recorded under an `ExecutionId`, and on resume a call is
matched to its record by that key. This page covers what the key is made of,
which code changes preserve recorded history, and how to choose engrave
boundaries.

## What the key is made of

An `ExecutionId` (`_identity.py`) has five components, each a value object rather
than a bare string:

| Component | Where it comes from |
| --- | --- |
| `parent_id` | The nearest engraved ancestor on the call stack, forming a chain up to the session root. |
| `domain` | The `DomainId` of the [domain](#domains) whose `engrave` decorated the function. |
| `name` | An `ExecutionName`, derived from the engraved function's `__qualname__` ([explicit names are planned](#explicit-names-and-versions)). |
| `arguments_digest` | An `ArgumentsDigest` over the canonical form of the bound arguments, produced by the session's `ArgumentCanonicalizer`. |
| `sequence` | An ordinal from an independent counter per `(parent_id, domain, name, arguments_digest)` (`_sequencer.py`). |

Keys are **content-addressed, not positional**: the sequence counter is scoped to
the exact `(parent, domain, name, args)` identity, not to a session-wide step
number.

`DomainId` is a machine identifier that outlives the code declaring it, so it is
held to a reverse-DNS shape: lowercase ASCII segments of letters, digits,
underscores and hyphens, joined by dots. `ExecutionName` is deliberately
permissive — an inferred name is a `__qualname__` and looks like
`Outer.<locals>.task`, and a migration has to hold whatever an older version
wrote. `ArgumentsDigest` is opaque: nothing in glyff reads it. `sequence` is a
non-negative `int`, which is what keeps the [path codec](#how-a-key-is-stored)
closed: every identity that can be constructed has a path that reads back as the
same identity.

## Domains

Every engraved function belongs to exactly one domain, fixed where it is
decorated:

```python
from glyff import Domain

domain = Domain("com.example.payment-library", version="3")
engrave = domain.engrave

@engrave
async def authorize(...) -> ...: ...
```

The domain identifier is part of an execution's identity, so a library's records
are recognizable as its own and two libraries may name a function the same thing
without colliding or sharing an ordinal counter. The domain *version* is not part
of identity — it says which generation of the owner's code the records belong to,
and is [claimed per session](./migration.md#in-flight-sessions-across-code-changes)
the first time one of the domain's functions is entered.

## How a key is stored

Backends key records by a path built from the identity chain
(`store/utils.py`), frames joined by `/`:

```
{domain}:{name}#{sequence}:{arguments_digest}
```

Each string component is percent-encoded, so any identifier round-trips and no
character can end a frame early or fabricate a new one. Decoding accepts only
what encoding writes — canonical escapes, and the ordinal's plain decimal
spelling — so one identity has exactly one path. Lexicographic order on
these paths is ancestor-first, which is what makes
`ExecutionRepository.executions` yield parents before children.

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

Today a name is derived from the function's `__qualname__` (`_function.py`), so
renaming an engraved function invalidates the history of paused sessions. And
`ExecutionId.__str__` is a debug representation (`_identity.py`) — not a key, and
not to be persisted.

> **Planned** — [#40](https://github.com/nueruyu/glyff/issues/40), covering
> both. Identity should come from your declaration, not from code shape, so a
> domain's `engrave` is to accept an explicit name and version:
>
> ```python
> @engrave(name="chat.reply", version=2)
> async def reply(...) -> ...: ...
> ```
>
> The pair would be canonicalized into the stored name (e.g. `"chat.reply@2"`)
> through `ExecutionName.explicit()` — the strict counterpart to the permissive
> constructor, allowing letters, digits, dots, underscores and hyphens — with
> duplicates rejected at decoration time. That version is the *function's* and
> would be part of identity — unlike a [domain's version](#domains), which is
> not. The same issue covers a public canonical string encoding of an
> `ExecutionId`, stable across resumes, for use as an idempotency key when
> [projecting into an application database](./events.md#projecting-into-an-application-database).

## Canonical arguments

Identity runs through a **canonical form**, not directly through a hash. A call
is bound to a name-to-value mapping first (`_function.py`, the one place that
reads Python's reflection API), and the session's `ArgumentCanonicalizer`
normalizes that mapping into the JSON data model — it never sees the callable or
its signature. glyff encodes the result once, and those bytes are both digested into
`arguments_digest` and recorded on the execution. So for every recorded
execution:

```
id.arguments_digest == execution.arguments.digest
```

`Execution.arguments` is a `CanonicalArguments`, distinct from the
`SerializedValue` that carries results and metadata: only one of the two is a
key's preimage.
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
`JsonArgumentCanonicalizer(opaque_policy=...)` takes an `OpaquePolicy`, and
`glyff.serialization` ships two:

| Policy | Behavior |
| --- | --- |
| `RejectOpaque` (default) | Rejects the value with `ArgumentCanonicalizationError`, so distinct instances never silently collide. |
| `OpaqueByTypeQualname` (opt-in) | Identifies the value by its class' qualified name, collapsing every instance of a class to one representation. Correct only when the value carries no identity that should distinguish calls — a stateless client handle, not a per-user session. |

Policy return values are namespaced, so a policy that returns `"pkg.Cls"` cannot
collide with a plain string argument of the same text. The same classification governs what is *stored*, not just what
is hashed — an opaque value the policy rejects never reaches the store.

> **Planned** — [#37](https://github.com/nueruyu/glyff/issues/37): standard
> composable policies (marker attribute, type list, predicate). Today anything
> beyond `OpaqueByTypeQualname` means implementing `OpaquePolicy` yourself.

## Choosing engrave boundaries

The recommended pattern is **coarse-grained boundaries**: engrave a small number
of flat, explicitly-named, application-meaningful calls (a chat turn, a document
build), not every helper. Fewer boundaries means fewer signatures whose
compatibility you have to maintain.

- **Boundary arguments must be explicit and deterministically derived** from
  session inputs or recorded results. Non-engraved code re-runs live on resume, so
  a nondeterministic input (a timestamp, a random id) changes the
  `arguments_digest` and cache-misses a completed boundary.
- **Everything inside a boundary re-executes on resume**, so in-boundary code must
  be pure or idempotent. Engrave non-idempotent side effects and pause points
  individually, at the grain you need them recorded.
- **Keep boundary arguments small and free of raw secrets** — pass an id or a
  reference, not a blob or a credential. Canonical argument values are recorded
  with the execution and are what a
  [migration](./migration.md#in-flight-sessions-across-code-changes) reads back.
