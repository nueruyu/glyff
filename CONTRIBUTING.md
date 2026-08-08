# Contributing

Thanks for looking at glyff. This is the human-facing development guide; the model
behind the code is explained in the [docs tree](./docs/README.md), starting with
[`docs/execution-identity.md`](./docs/execution-identity.md).

## Prerequisites

- Python 3.11+
- [`uv`](https://github.com/astral-sh/uv) (the repo is a `uv` workspace)

## Setup & commands

```bash
uv sync --all-packages        # install the workspace
uv run pytest                 # run all tests (asyncio auto-mode)
uv run pytest packages/glyff  # run one package's tests
uv run ruff check .           # lint
uv run ruff format --check .  # formatting (CI enforces this; drop --check to fix)
uv run pyright                # type-check
uv run pytest --cov           # tests with branch coverage (what CI runs)
```

Each package's tests are split into `units/` (per-module) and `scenarios/`
(behavioral); backend packages also run the shared contract suites from
`glyff.testing`.

**Prove a behavior once, at the narrowest thing that owns it.** A caller tests
only what its own composition adds. So a value's canonical form belongs to
`to_canonical`'s tests, not to every canonicalizer that walks with it; a store's
semantics belong to the contract suite, not to each backend restating them. A
useful check: if breaking something already fails its own focused test, a second
test failing for exactly the same reason is earning nothing.

Three things still deserve their own tests even when they look like a
restatement, and each says so where it lives: a store's *mechanism* (file
replacement, locking, transaction atomicity, schema), anything that has to go
through a store's own representation (the format-version stamp), and a
regression for a failure mode that was distinct in practice.

Never build an expected value by running the same production adapter or codec
the system under test runs — the same bug then lands on both sides of the
assertion. Observe what was persisted instead.

> **Planned** — [#36](https://github.com/nueruyu/glyff/issues/36) moves the test
> trees from inside the source packages (`packages/*/src/*/tests/`) to plain
> `packages/*/tests/` directories and promotes the shared contracts to
> `glyff.testing`. Until then, the in-src layout is what you will see.

## Where to make a change

| Area | Where |
| --- | --- |
| Domains, the `engrave` decorator, version claims | `packages/glyff/src/glyff/_domain.py`, `_domain_claims.py` |
| Call identity and sequencing | `packages/glyff/src/glyff/_identity.py`, `_sequencer.py`, `store/utils.py` |
| The execution aggregate | `packages/glyff/src/glyff/_execution.py` |
| Reading an engraved Python function (signatures, hints, binding) | `packages/glyff/src/glyff/_function.py` |
| Execution orchestration (cache check, transactions, event emission) | `packages/glyff/src/glyff/_executor.py` |
| Session lifecycle and context | `packages/glyff/src/glyff/_session.py`, `_context.py` |
| Events and handler dispatch | `packages/glyff/src/glyff/events.py`, `_event_system.py` |
| The backend / serializer / canonicalizer contracts | `packages/glyff/src/glyff/_interfaces.py` |
| Built-in JSON hashing and serialization | `packages/glyff/src/glyff/serialization/` |
| In-memory store | `packages/glyff/src/glyff/store/` |
| File backend (debug) | `packages/glyff-file-store/` |
| SQLite backend (production) | `packages/glyff-sqlite/` |
| Pydantic serializer / canonicalizer | `packages/glyff-pydantic/` |

## Conventions & guardrails

- **Keep dependency arrows one-way.** `glyff` core depends on nothing beyond the
  standard library; backends and serializers depend on `glyff`, never the
  reverse. Anything Pydantic-, file-, or SQLite-shaped lives in its satellite
  package behind the `_interfaces.py` contracts.
- **Execution identity is a persistence contract.** Anything that feeds
  `ExecutionId` — name derivation, argument hashing, sequencing — changes the
  keys of recorded histories. Treat such changes as compatibility changes, not
  refactors; read [`docs/execution-identity.md`](./docs/execution-identity.md)
  first.
- **Mechanism, not policy.** Core knows *what* (unreachable records, version
  divergence); *when and whether* to act on it — retention, migration — stays in
  userland via events and repository primitives. Don't ship policy in core.
- **Keep persistence and observation separate.** Persistence goes through the
  `Backend` contract (repository + transaction provider); event handlers run
  after commit, observe only, and must not carry control flow.
- **Underscore = internal.** Import public names from a package's `__init__.py`.
- Match the surrounding code's style and density.

## Keep the docs in sync

Several docs describe the code at a level that **drifts when the code changes** —
treat updating them as part of the change, not a follow-up. The mapping:

| If you change… | Update… |
| --- | --- |
| The public API, exports, or quickstart-level usage | `README.md`, the package READMEs |
| Name derivation, argument canonicalization, or sequencing | `docs/execution-identity.md` (it states guarantees with source references) |
| Event emission points or delivery semantics | `docs/events.md`, the events section of `README.md` |
| The backend contract or a store's on-disk behavior | `docs/backends.md`, that backend's README |
| Store formats, stamps, or version-mismatch behavior | `docs/migration.md`, the backend READMEs |

When in doubt, grep the docs for the symbol or filename you touched. A change that
makes a doc's example or file reference wrong is incomplete until the doc is fixed.

## Pre-1.0

The API is unstable; see the [docs index](./docs/README.md) for how the docs mark
what is planned versus released. Execution identity, store formats, and the
backend contract are in flight and coordinated, so check the
[open issues](https://github.com/nueruyu/glyff/issues) before working on them.

## Pull requests

Keep PRs focused, include tests for behavior changes, and make sure
`pytest` / `ruff` / `pyright` pass. Describe the change and the reasoning; link any
relevant issue.
