# Documentation

The [project README](../README.md) is the pitch and the minimal example; this page
is the map of everything else. A reasonable first path:
**[README](../README.md) → [Execution identity](./execution-identity.md) →
[Events](./events.md)**.

## Understand the model

- **[Execution identity](./execution-identity.md)** — how calls are keyed, which
  refactors preserve recorded history, where the hazards are, and how to choose
  engrave boundaries.
- **[Events](./events.md)** — delivery semantics, pruning completed subtrees, and
  projecting executions into your own database.
- **[Backends](./backends.md)** — the backend contract, the shared contract test
  suite, and how to write your own store.

## Operate

- **[Migration & versioning](./migration.md)** — what glyff migrates, what it
  refuses, and what stays in your hands.

## Contribute

- **[Contributing](../CONTRIBUTING.md)** — setup, commands, the module map,
  conventions, and the "keep the docs in sync" rule.
- **[AGENTS.md](../AGENTS.md)** — the same guidance, addressed to AI coding
  agents.

> **Pre-1.0.** These pages describe the release-target API. A section marked
> *Planned* is not in a released version and links its tracking issue; everything
> else describes what the code does today. This note covers the whole docs tree —
> the individual pages do not repeat it.
