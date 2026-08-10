"""A migrator declared as the boundaries that changed shape."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .._execution import CanonicalArguments, Execution
from .._identity import DomainId, ExecutionId, ExecutionName, SequenceScope
from .._interfaces import ArgumentCanonicalizer
from ..exceptions import MigrationError, MigrationOrdinalAmbiguityError
from ..serialization._utils import encode_canonical
from ._arguments import RecordedArgumentValue, from_recorded
from ._interfaces import SessionMigrator
from ._models import (
    MigrationReport,
    SessionMetadata,
    SessionMigrationResult,
    StoredSession,
)

_ArgumentConverter = Callable[..., Mapping[str, Any]]


@dataclass(frozen=True)
class DomainVersionTransition:
    """The generation a migration reads for one domain, and the one it writes.

    Every domain a migration touches needs one, so a rewrite can never run
    against a generation nobody said it was for. A domain that only carries
    records across unchanged declares the same version twice.
    """

    source: str | None
    target: str

    @classmethod
    def claiming(cls, target: str) -> DomainVersionTransition:
        """A domain the session has not entered, which this migration claims."""
        return cls(source=None, target=target)


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
class _Remap:
    source: ExecutionShape
    target: ExecutionShape | None
    convert_arguments: _ArgumentConverter | None


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
        version_transitions: Mapping[DomainId | str, DomainVersionTransition],
    ) -> None:
        # The canonicalizer the resuming session will build, not the one that
        # wrote the records: the keys this computes have to be the keys that
        # session goes looking for.
        self._canonicalizer = canonicalizer
        self._transitions: dict[DomainId, DomainVersionTransition] = {
            DomainId(domain) if isinstance(domain, str) else domain: transition
            for domain, transition in version_transitions.items()
        }
        self._remaps: dict[tuple[DomainId, ExecutionName], _Remap] = {}

    def remap(
        self,
        source: ExecutionShape,
        target: ExecutionShape,
        *,
        convert_arguments: _ArgumentConverter | None = None,
    ) -> None:
        """Registers a move of ``source``'s records onto ``target``.

        ``convert_arguments`` receives the recorded arguments by their old names
        and returns the ones ``target`` is keyed by; leave it out when the
        argument names did not change and the recorded form is kept as it is.
        """
        if convert_arguments is None and source.argument_names != target.argument_names:
            raise MigrationError(
                f"{source.name} is recorded with {_format_argument_names(source.argument_names)} "
                f"and {target.name} takes {_format_argument_names(target.argument_names)}, "
                "so the recorded arguments cannot carry over unchanged. Give the "
                "migration a conversion between them."
            )
        self._require_transition(source.domain)
        self._require_transition(target.domain)
        self._register(
            source.key,
            _Remap(source=source, target=target, convert_arguments=convert_arguments),
        )

    def drop(self, source: ExecutionShape) -> None:
        """Registers the removal of ``source``'s records, and their descendants."""
        self._require_transition(source.domain)
        self._register(
            source.key, _Remap(source=source, target=None, convert_arguments=None)
        )

    def migrate(self, source: StoredSession) -> SessionMigrationResult:
        recorded_versions = source.metadata.domain_versions
        self._require_source_versions(recorded_versions)
        migrated_versions = {
            **recorded_versions,
            **{
                domain: transition.target
                for domain, transition in self._transitions.items()
            },
        }
        return SessionMigrationResult(
            session=StoredSession(
                metadata=SessionMetadata(domain_versions=migrated_versions),
                executions=self._rebuild(source),
            ),
            report=MigrationReport(
                from_domain_versions=recorded_versions,
                to_domain_versions=migrated_versions,
            ),
        )

    # -- Registration --------------------------------------------------------

    def _require_transition(self, domain: DomainId) -> None:
        # The shapes carry no version, so the transitions are the only thing
        # saying which generation a rule was written against. A rule reaching a
        # domain none of them names would run whatever the session records.
        if domain not in self._transitions:
            raise MigrationError(
                f"This migration touches {domain} but declares no version "
                "transition for it, so nothing says which generation it is "
                "written against. Declare one, repeating the version if the "
                "domain only carries records across unchanged."
            )

    def _register(self, key: tuple[DomainId, ExecutionName], remap: _Remap) -> None:
        if key in self._remaps:
            domain, name = key
            raise MigrationError(
                f"{name} in {domain} is already registered. One boundary "
                "changes shape once."
            )
        self._remaps[key] = remap

    def _require_source_versions(self, recorded: Mapping[DomainId, str]) -> None:
        for domain, transition in self._transitions.items():
            if transition.source is None:
                if domain in recorded:
                    raise MigrationError(
                        f"This migration claims {domain} as a domain the "
                        f"session has not entered, but it records "
                        f"{recorded[domain]!r} for it."
                    )
                continue
            if domain not in recorded:
                raise MigrationError(
                    f"This migration reads {domain} at version "
                    f"{transition.source!r}, but the session records no version "
                    "for that domain."
                )
            if recorded[domain] != transition.source:
                raise MigrationError(
                    f"This migration reads {domain} at version "
                    f"{transition.source!r}, but the session records "
                    f"{recorded[domain]!r}. It describes another generation of "
                    "these records."
                )

    # -- Rebuilding ----------------------------------------------------------

    def _rebuild(self, source: StoredSession) -> tuple[Execution, ...]:
        children = _children_by_parent(source.executions)

        migrated: list[Execution] = []
        ordinals: dict[SequenceScope, int] = {}
        source_scopes_by_target: dict[SequenceScope, set[SequenceScope]] = {}

        # Ancestors first, so a parent's own key is settled before anything that
        # names it. Depth-first only because that keeps the walk iterative.
        stack: list[tuple[ExecutionId | None, ExecutionId | None]] = [(None, None)]
        while stack:
            old_parent, new_parent = stack.pop()
            for execution in sorted(
                children.get(old_parent, ()), key=lambda e: e.id.sequence
            ):
                remap = self._remaps.get((execution.id.domain, execution.id.name))
                if remap is not None and remap.target is None:
                    # Checked before deleting, not only before rewriting: this
                    # is the destructive half.
                    self._recorded(execution, remap.source)
                    continue

                arguments = self._arguments(execution, remap)
                target = remap.target if remap is not None else None
                scope = SequenceScope(
                    parent_id=new_parent,
                    domain=target.domain if target else execution.id.domain,
                    name=target.name if target else execution.id.name,
                    arguments_digest=arguments.digest,
                )
                _require_one_source_scope(source_scopes_by_target, scope, execution.id)
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
        self, execution: Execution, remap: _Remap | None
    ) -> CanonicalArguments:
        if remap is None:
            return execution.arguments
        if remap.convert_arguments is None:
            # The recorded bytes are the digest's preimage, so an argument that
            # did not change keeps its key by keeping them.
            self._recorded(execution, remap.source)
            return execution.arguments

        assert remap.target is not None
        recorded = self._recorded(execution, remap.source)
        try:
            converted = dict(remap.convert_arguments(**recorded))
        except Exception as e:
            raise MigrationError(
                f"Converting the arguments of {execution.id.name} in "
                f"{execution.id.domain} failed: {e}"
            ) from e

        if set(converted) != remap.target.argument_names:
            raise MigrationError(
                f"The conversion onto {remap.target.name} returned "
                f"{_format_argument_names(set(converted))}, but it is keyed by "
                f"{_format_argument_names(remap.target.argument_names)}."
            )
        return CanonicalArguments(
            encode_canonical(self._canonicalizer.canonicalize(converted))
        )

    @staticmethod
    def _recorded(
        execution: Execution, source: ExecutionShape
    ) -> dict[str, RecordedArgumentValue]:
        stored = json.loads(execution.arguments.data)
        if set(stored) != source.argument_names:
            raise MigrationError(
                f"{execution.id.name} in {execution.id.domain} is recorded with "
                f"{_format_argument_names(set(stored))}, but the migration "
                f"describes it as {_format_argument_names(source.argument_names)}. "
                "It describes another generation of these records."
            )
        return {name: from_recorded(value) for name, value in stored.items()}


def _format_argument_names(names: set[str] | frozenset[str]) -> str:
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


def _require_one_source_scope(
    source_scopes_by_target: dict[SequenceScope, set[SequenceScope]],
    target: SequenceScope,
    old_id: ExecutionId,
) -> None:
    # An ordinal orders calls within one scope and means nothing across two, so
    # renumbering is sound only while a scope's members all came from one.
    seen = source_scopes_by_target.setdefault(target, set())
    seen.add(SequenceScope.from_execution_id(old_id))
    if len(seen) > 1:
        raise MigrationOrdinalAmbiguityError(
            f"Migrating onto {target.name} in {target.domain} gathers calls "
            "that were recorded separately into one class of repeated calls, "
            "which are matched by ordinal. Nothing records which of them ran "
            "first. Give them arguments that tell them apart, or drop one."
        )
