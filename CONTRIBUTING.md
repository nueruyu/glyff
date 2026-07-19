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
(behavioral); backend packages also run the shared backend contract suite. Add
tests next to the layer you change, and run new backends against the contract
suite rather than hand-writing store semantics tests.

> **Planned** — [#36](https://github.com/nueruyu/glyff/issues/36): the test trees
> currently live *inside* the source packages (`packages/*/src/*/tests/`, with the
> shared contracts under `glyff.tests.contracts`) and are moving to plain
> `packages/*/tests/` directories, with the contracts promoted to the public
> `glyff.testing` module. Until then, the in-src layout is the one you'll see.

## Where to make a change

| Area | Where |
| --- | --- |
| `engrave` decorator, call identity, sequencing | `packages/glyff/src/glyff/_engrave.py`, `_sequencer.py`, `_models.py` |
| Execution orchestration (cache check, transactions, event emission) | `packages/glyff/src/glyff/_executor.py` |
| Session lifecycle and context | `packages/glyff/src/glyff/_session.py`, `_context.py` |
| Events and handler dispatch | `packages/glyff/src/glyff/events.py`, `_event_system.py` |
| The backend / serializer / hasher contracts | `packages/glyff/src/glyff/_interfaces.py` |
| Built-in JSON hashing and serialization | `packages/glyff/src/glyff/serialization/` |
| In-memory store | `packages/glyff/src/glyff/store/` |
| File backend (debug) | `packages/glyff-file-store/` |
| SQLite backend (production) | `packages/glyff-sqlite/` |
| Pydantic serializer / hasher | `packages/glyff-pydantic/` |

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
- **Respect the seams.** Persistence goes through the `Backend` contract
  (repository + transaction provider); event handlers observe after commit and
  must not carry control flow.
- **Underscore = internal.** Import public names from a package's `__init__.py`.
- Match the surrounding code's style and density.

## Keep the docs in sync

Several docs describe the code at a level that **drifts when the code changes** —
treat updating them as part of the change, not a follow-up. The mapping:

| If you change… | Update… |
| --- | --- |
| The public API, exports, or quickstart-level usage | `README.md`, the package READMEs |
| Name derivation, argument hashing, or sequencing | `docs/execution-identity.md` (it states guarantees with source references) |
| Event emission points or delivery semantics | `docs/events.md`, the events section of `README.md` |
| The backend contract or a store's on-disk behavior | `docs/backends.md`, that backend's README |
| Store formats, stamps, or version-mismatch behavior | `docs/migration.md`, the backend READMEs' *Planned* sections |

When in doubt, grep the docs for the symbol or filename you touched. A change that
makes a doc's example or file reference wrong is incomplete until the doc is fixed.

## Pre-1.0

The API is unstable, and the docs describe the **release-target** surface — some
of it intentionally ahead of the code, marked *Planned* with a tracking issue.
Before working on execution identity, store formats, or the backend contract,
check the [open issues](https://github.com/nueruyu/glyff/issues) — the
[#40](https://github.com/nueruyu/glyff/issues/40)–[#43](https://github.com/nueruyu/glyff/issues/43)
cluster is in flight and coordinated.

## Pull requests

Keep PRs focused, include tests for behavior changes, and make sure
`pytest` / `ruff` / `pyright` pass. Describe the change and the reasoning; link any
relevant issue.
