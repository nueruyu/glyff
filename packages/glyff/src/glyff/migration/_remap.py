"""A migrator declared as the boundaries that changed shape."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .._execution import CanonicalArguments, CanonicalValue, Execution
from .._identity import DomainId, ExecutionId, ExecutionName, SequenceScope
from .._interfaces import ArgumentCanonicalizer
from ..exceptions import MigrationError, MigrationOrderError
from ..serialization._utils import as_opaque, encode_canonical
from ._arguments import Opaque, RecordedValue, from_recorded
from ._interfaces import SessionMigrator
from ._models import (
    MigrationReport,
    SessionMetadata,
    SessionMigrationResult,
    StoredSession,
)

ArgumentConversion = Callable[..., Mapping[str, Any]]
"""What a boundary's arguments become.

Called with the recorded arguments as keywords, each a `RecordedValue`. What it
returns is canonicalized, so those may be ordinary Python values — or
`RecordedValue`s handed straight back.
"""

VersionChange = tuple[str, str]
"""The version a migration reads, and the one it writes."""


@dataclass(frozen=True)
class ExecutionShape:
    """What one generation of an engraved function is recorded as.

    Spelled out rather than read off a live function, so a later change to that
    function cannot quietly reshape records a migration claims to know.
    """

    domain: DomainId
    name: ExecutionName
    argument_names: frozenset[str]

    def __init__(
        self, domain: DomainId | str, name: str | ExecutionName, *argument_names: str
    ) -> None:
        object.__setattr__(
            self, "domain", DomainId(domain) if isinstance(domain, str) else domain
        )
        object.__setattr__(
            self, "name", ExecutionName(name) if isinstance(name, str) else name
        )
        object.__setattr__(self, "argument_names", frozenset(argument_names))
        if len(self.argument_names) != len(argument_names):
            raise ValueError(f"{self.name} names an argument more than once.")

    @property
    def key(self) -> tuple[DomainId, ExecutionName]:
        return (self.domain, self.name)


@dataclass(frozen=True)
class _Rewrite:
    old: ExecutionShape
    to: ExecutionShape | None
    arguments: ArgumentConversion | None


class RemappingMigrator(SessionMigrator):
    """A `SessionMigrator` declared as pairs of execution shapes.

    Each boundary that changed shape is registered as what it was, what it
    became, and the conversion between their arguments; boundaries left
    unregistered keep their records untouched. See `docs/migration.md`.
    """

    def __init__(
        self,
        *,
        canonicalizer: ArgumentCanonicalizer,
        domain_versions: (
            Mapping[DomainId, VersionChange] | Mapping[str, VersionChange]
        ),
    ) -> None:
        # The canonicalizer the resuming session will build, not the one that
        # wrote the records: the keys this computes have to be the keys that
        # session goes looking for.
        self._canonicalizer = canonicalizer
        self._versions: dict[DomainId, VersionChange] = {
            DomainId(domain) if isinstance(domain, str) else domain: change
            for domain, change in domain_versions.items()
        }
        self._rewrites: dict[tuple[DomainId, ExecutionName], _Rewrite] = {}

    def rewrite(
        self,
        old: ExecutionShape,
        to: ExecutionShape,
        *,
        arguments: ArgumentConversion | None = None,
    ) -> None:
        """Registers a move of ``old``'s records onto ``to``.

        ``arguments`` receives the recorded arguments by their old names and
        returns the ones ``to`` is keyed by; leave it out when the argument
        names did not change and the recorded form is kept as it is.
        """
        if arguments is None and old.argument_names != to.argument_names:
            raise MigrationError(
                f"{old.name} is recorded with {_named(old.argument_names)} and "
                f"{to.name} takes {_named(to.argument_names)}, so the recorded "
                "arguments cannot carry over unchanged. Give the migration a "
                "conversion between them."
            )
        self._register(old.key, _Rewrite(old=old, to=to, arguments=arguments))

    def drop(self, old: ExecutionShape) -> None:
        """Registers the removal of ``old``'s records, and their descendants."""
        self._register(old.key, _Rewrite(old=old, to=None, arguments=None))

    def migrate(self, source: StoredSession) -> SessionMigrationResult:
        recorded = source.metadata.domain_versions
        self._require_source_versions(recorded)
        migrated = {
            **recorded,
            **{domain: change[1] for domain, change in self._versions.items()},
        }
        return SessionMigrationResult(
            session=StoredSession(
                metadata=SessionMetadata(domain_versions=migrated),
                executions=self._rebuild(source),
            ),
            report=MigrationReport(
                from_domain_versions=recorded, to_domain_versions=migrated
            ),
        )

    # -- Registration --------------------------------------------------------

    def _register(
        self, boundary: tuple[DomainId, ExecutionName], rewrite: _Rewrite
    ) -> None:
        if boundary in self._rewrites:
            domain, name = boundary
            raise MigrationError(
                f"{name} in {domain} is already registered. One boundary "
                "changes shape once."
            )
        self._rewrites[boundary] = rewrite

    def _require_source_versions(self, recorded: Mapping[DomainId, str]) -> None:
        for domain, (reads, _) in self._versions.items():
            if domain not in recorded:
                raise MigrationError(
                    f"This migration reads {domain} at version {reads!r}, but "
                    "the session records no version for that domain."
                )
            if recorded[domain] != reads:
                raise MigrationError(
                    f"This migration reads {domain} at version {reads!r}, but "
                    f"the session records {recorded[domain]!r}. It describes "
                    "another generation of these records."
                )

    # -- Rebuilding ----------------------------------------------------------

    def _rebuild(self, source: StoredSession) -> tuple[Execution, ...]:
        children = _children_by_parent(source.executions)

        migrated: list[Execution] = []
        ordinals: dict[SequenceScope, int] = {}
        sources: dict[SequenceScope, set[SequenceScope]] = {}

        # Ancestors first, so a parent's own key is settled before anything that
        # names it. Depth-first only because that keeps the walk iterative.
        stack: list[tuple[ExecutionId | None, ExecutionId | None]] = [(None, None)]
        while stack:
            old_parent, new_parent = stack.pop()
            for execution in sorted(
                children.get(old_parent, ()), key=lambda e: e.id.sequence
            ):
                rewrite = self._rewrites.get((execution.id.domain, execution.id.name))
                if rewrite is not None and rewrite.to is None:
                    # Checked before deleting, not only before rewriting: this
                    # is the destructive half.
                    self._recorded(execution, rewrite.old)
                    continue

                arguments = self._arguments(execution, rewrite)
                target = rewrite.to if rewrite is not None else None
                scope = SequenceScope(
                    parent_id=new_parent,
                    domain=target.domain if target else execution.id.domain,
                    name=target.name if target else execution.id.name,
                    arguments_digest=arguments.digest,
                )
                _record_source(sources, scope, execution.id)
                sequence = ordinals.get(scope, 0)
                ordinals[scope] = sequence + 1

                new_id = ExecutionId(
                    parent_id=scope.parent_id,
                    domain=scope.domain,
                    name=scope.name,
                    sequence=sequence,
                    arguments_digest=scope.arguments_digest,
                )
                migrated.append(replace(execution, id=new_id, arguments=arguments))
                stack.append((execution.id, new_id))

        return tuple(migrated)

    def _arguments(
        self, execution: Execution, rewrite: _Rewrite | None
    ) -> CanonicalArguments:
        if rewrite is None:
            return execution.arguments
        if rewrite.arguments is None:
            # The recorded bytes are the digest's preimage, so an argument that
            # did not change keeps its key by keeping them.
            self._recorded(execution, rewrite.old)
            return execution.arguments

        assert rewrite.to is not None
        recorded = self._recorded(execution, rewrite.old)
        try:
            converted = dict(rewrite.arguments(**recorded))
        except Exception as e:
            raise MigrationError(
                f"Converting the arguments of {execution.id.name} in "
                f"{execution.id.domain} failed: {e}"
            ) from e

        if set(converted) != rewrite.to.argument_names:
            raise MigrationError(
                f"The conversion onto {rewrite.to.name} returned "
                f"{_named(set(converted))}, but it is keyed by "
                f"{_named(rewrite.to.argument_names)}."
            )
        return CanonicalArguments(encode_canonical(self._canonical(converted)))

    def _canonical(self, converted: dict[str, Any]) -> dict[str, CanonicalValue]:
        """The converted arguments as a key, markers kept as recorded.

        A marker never goes back through the canonicalizer, which refuses the
        reserved key it is written under.
        """
        derived = {
            name: value
            for name, value in converted.items()
            if not isinstance(value, Opaque)
        }
        for name, value in derived.items():
            _refuse_nested_opaque(name, value)

        canonical = dict(self._canonicalizer.canonicalize(derived))
        for name, value in converted.items():
            if isinstance(value, Opaque):
                canonical[name] = as_opaque(value.value)
        return canonical

    @staticmethod
    def _recorded(
        execution: Execution, old: ExecutionShape
    ) -> dict[str, RecordedValue]:
        stored = json.loads(execution.arguments.data)
        if set(stored) != old.argument_names:
            raise MigrationError(
                f"{execution.id.name} in {execution.id.domain} is recorded with "
                f"{_named(set(stored))}, but the migration describes it as "
                f"{_named(old.argument_names)}. It describes another generation "
                "of these records."
            )
        return {name: from_recorded(value) for name, value in stored.items()}


def _refuse_nested_opaque(name: str, value: Any) -> None:
    if isinstance(value, Opaque):
        raise MigrationError(
            f"The conversion put a recorded opaque value inside {name}. One "
            "stands for a whole argument, so it can be returned as one or not "
            "at all."
        )
    if isinstance(value, dict):
        for item in value.values():
            _refuse_nested_opaque(name, item)
    elif isinstance(value, list):
        for item in value:
            _refuse_nested_opaque(name, item)


def _named(names: set[str] | frozenset[str]) -> str:
    return ", ".join(sorted(names)) or "no arguments"


def _children_by_parent(
    executions: tuple[Execution, ...],
) -> dict[ExecutionId | None, list[Execution]]:
    known = {execution.id for execution in executions}
    children: dict[ExecutionId | None, list[Execution]] = {}
    for execution in executions:
        parent = execution.id.parent_id
        if parent is not None and parent not in known:
            raise MigrationError(
                f"{execution.id.name} in {execution.id.domain} names a parent "
                "the session does not hold, so there is no chain to rebuild."
            )
        children.setdefault(parent, []).append(execution)
    return children


def _record_source(
    sources: dict[SequenceScope, set[SequenceScope]],
    scope: SequenceScope,
    old_id: ExecutionId,
) -> None:
    # An ordinal orders calls within one scope and means nothing across two, so
    # renumbering is sound only while a scope's members all came from one.
    seen = sources.setdefault(scope, set())
    seen.add(SequenceScope.of(old_id))
    if len(seen) > 1:
        raise MigrationOrderError(
            f"Migrating onto {scope.name} in {scope.domain} gathers calls that "
            "were recorded separately into one class of repeated calls, which "
            "are matched by ordinal. Nothing records which of them ran first. "
            "Give them arguments that tell them apart, or drop one."
        )
