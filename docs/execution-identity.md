# Execution identity

Every engraved call is recorded under an `ExecutionId`, and on resume a call is
matched to its record by that key. This page states what the key is made of, which
code changes preserve recorded history and which invalidate it, and how to place
engrave boundaries so the answer stays boring.

> Pre-1.0: this page shows the release-target API. Sections marked *Planned* link
> to the tracking issue.

## What the key is made of

An `ExecutionId` (`_models.py`) has four components:

| Component | Where it comes from |
| --- | --- |
| `parent_id` | The nearest engraved ancestor on the call stack, forming a chain up to the session root. |
| `name` | The engraved function's name — explicit `name=`/`version=` when given, derived from the function otherwise. |
| `args_hash` | The bound arguments, hashed by the session's `ArgsHasher`. |
| `sequence` | An ordinal from an independent counter per `(parent_id, name, args_hash)` (`_sequencer.py`). |

Keys are **content-addressed, not positional**: the sequence counter is scoped to
the exact `(parent, name, args)` identity, not to a session-wide step number.

## Refactor compatibility guarantees

Because keys are content-addressed, these hold — unlike in positional-replay
systems, where any of them silently corrupts history:

- **Inserting** a call between existing ones shifts no existing keys; the new call
  simply executes fresh.
- **Deleting** a call leaves every other key untouched. The orphaned record is dead
  weight (see [pruning](./events.md#pruning-completed-subtrees)), not corruption.
- **Reordering distinct calls** — different name or different arguments — is fully
  compatible.

### The residual hazard: identical repeated calls

Calls that share parent, name, *and* arguments are matched by ordinal within that
identity class. Changing their count or relative order across a resume mismatches
silently. Two consequences:

- **Give repeated calls distinguishing arguments** (the loop element, an index).
  This dissolves the ordinal dependence entirely and is the recommended default.
- **Concurrent identical-args calls** get their ordinals in scheduling order, so
  their recorded results must be treated as interchangeable — if they are not,
  pass distinguishing arguments.

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

The pair is canonicalized deterministically into the stored name (e.g.
`"chat.reply@2"`); the key stays a `str`. Duplicate explicit names are rejected at
decoration time by a process-wide registry, since name uniqueness is an invariant
of definitions, not calls. The resolved name is also what the args hasher sees, so
a rename with a stable `name=` invalidates nothing.

> **Planned** — [#40](https://github.com/nueruyu/glyff/issues/40). Released
> versions derive the name from `__qualname__` only (`_engrave.py`), which is not
> module-qualified — `Service.run` in two modules collides — and any rename
> invalidates the history of paused sessions. Until `name=` lands, treat engraved
> function names as part of your persistence contract and avoid renaming them
> while sessions are in flight. [#40](https://github.com/nueruyu/glyff/issues/40)
> also covers an explicit per-call key for repeated calls that cannot take
> distinguishing arguments, and whether default-valued parameters participate in
> `args_hash` (today they do, via `apply_defaults()` — so *adding* a defaulted
> parameter changes the hash of every historical call).

## A canonical, persistence-grade encoding

Applications that project executions into their own database need the execution
id as an idempotency key. `ExecutionId` has a public canonical string encoding,
stable across resumes and releases, and the current execution's id is available
from the context.

> **Planned** — [#40](https://github.com/nueruyu/glyff/issues/40). Today
> `ExecutionId.__str__` is explicitly documented as a debug representation, not a
> persistence key (`_models.py`), and the backends' path encoding is internal. Do
> not persist either. Note the stakes once this lands: the encoding will appear in
> application-DB UNIQUE constraints, so key stability becomes an application-data
> compatibility contract — which is why the naming discipline above is a
> prerequisite, not a recommendation.

## Argument hashing and opaque values

The hashing contract: a value participates in `args_hash` by its *value*
representation; a value the hasher cannot represent is an **error**, never a
silent guess. Types and named functions are identified by reference (qualified
name), which is the correct identity for code objects, and `functools.partial`
decomposes into its function and bound arguments.

For values that are deliberately opaque — a service object passed as `self`, a
client handle — opacity is *your* call, expressed through an injectable policy:
glyff owns the hashing contract, not the taxonomy of what counts as opaque in
your application. `JsonArgsHasher(opaque_policy=...)` takes an `OpaquePolicy`,
and `glyff.serialization` ships two:

| Policy | Behavior |
| --- | --- |
| `RaiseOnOpaque` (default) | Rejects the value with `UnserializableArgumentError`, so distinct instances never silently collide. |
| `QualnameOpaque` (opt-in) | Identifies the value by its class' qualified name, collapsing every instance of a class to one hash. Correct only when the value carries no identity that should distinguish calls — a stateless client handle, not a per-user session. |

A policy receives an `OpaqueContext` rather than the bare value, so the signature
can grow without breaking implementations. Policy return values are namespaced
before hashing, so a policy that returns `"pkg.Cls"` cannot hash-equal a plain
string argument of the same text.

Erroring by default is the important half: an unrepresentable value that is
quietly hashed by class name makes two distinct instances share a key, and a
later call then receives an earlier call's recorded result.

> **Planned** — [#37](https://github.com/nueruyu/glyff/issues/37). Standard
> composable policies (match by marker attribute, by type list, by predicate;
> chain them, with *raise* as the fallback) will make the common cases
> one-liners. Today, anything beyond `QualnameOpaque` means implementing
> `OpaquePolicy` yourself — which is a small class, but not a one-liner.

## Choosing engrave boundaries

The recommended pattern is **coarse-grained boundaries**: engrave a small number
of flat, explicitly-named, application-meaningful calls (a chat turn, a document
build), not every helper. This concentrates the compatibility surface — the
things migration has to care about — into a handful of versioned signatures.

The discipline that makes it work:

- **Boundary arguments must be explicit and deterministically derived** from
  session inputs or recorded results. Non-engraved code re-runs live on resume, so
  a nondeterministic input (a timestamp, a random id) changes `args_hash` and
  cache-misses a completed boundary.
- **Everything inside a boundary re-executes on resume**, so in-boundary code must
  be pure or idempotent. The exceptions are non-idempotent side effects and pause
  points — engrave those individually and finely, so they are recorded (or
  resumed) at exactly the right grain.
- **Keep boundary arguments small and free of raw secrets** — pass an id or a
  reference, not a blob or a credential. Beyond the hashing cost, argument values
  are what a [migration](./migration.md#in-flight-sessions-across-code-changes)
  reads back, and are being persisted for exactly that reason
  ([#47](https://github.com/nueruyu/glyff/issues/47)); the same opacity policy
  that governs hashing will govern what is stored.
