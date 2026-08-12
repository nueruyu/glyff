"""A migrator declared as the boundaries that changed shape."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Any, TypeAlias

from .._execution import (
    CanonicalArguments,
    Execution,
    RecordedArgumentValue,
)
from .._identity import (
    DomainId,
    DomainVersion,
    DomainVersionMap,
    ExecutionId,
    ExecutionName,
    ExecutionSequenceScope,
)
from .._interfaces import ArgumentCanonicalizer
from ..exceptions import (
    InvalidExecutionError,
    MigrationError,
    MigrationOrdinalAmbiguityError,
)
from ._interfaces import SessionMigrator
from ._models import SessionMetadata, StoredSession

_ArgumentConverter = Callable[..., Mapping[str, Any]]


@dataclass(frozen=True)
class DomainVersionTransition:
    """The generation a migration reads for one domain, and the one it writes.

    Every domain a migration touches needs one, so a rewrite can never run
    against a generation nobody said it was for. A domain that only carries
    records across unchanged declares the same version twice.
    """

    source: DomainVersion | None
    target: DomainVersion

    def __post_init__(self) -> None:
        if self.source is not None and not isinstance(self.source, DomainVersion):
            raise TypeError("A transition source must be a DomainVersion or None.")
        if not isinstance(self.target, DomainVersion):
            raise TypeError("A transition target must be a DomainVersion.")

    @classmethod
    def between(cls, source: str, target: str) -> DomainVersionTransition:
        return cls(DomainVersion(source), DomainVersion(target))

    @classmethod
    def from_unclaimed(cls, target: str) -> DomainVersionTransition:
        """A domain the session has not entered, which this migration claims."""
        return cls(source=None, target=DomainVersion(target))


@dataclass(frozen=True)
class ExecutionShape:
    """What one generation of an engraved function is recorded as.

    Spelled out rather than read off a live function, so a later change to that
    function cannot quietly reshape records a migration claims to know.
    """

    domain_id: DomainId
    name: ExecutionName
    argument_names: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.domain_id, DomainId):
            raise TypeError("ExecutionShape.domain_id must be a DomainId.")
        if not isinstance(self.name, ExecutionName):
            raise TypeError("ExecutionShape.name must be an ExecutionName.")
        if not isinstance(self.argument_names, frozenset) or not all(
            isinstance(name, str) for name in self.argument_names
        ):
            raise TypeError(
                "ExecutionShape.argument_names must be a frozenset of strings."
            )

    @classmethod
    def from_names(
        cls,
        domain_id: DomainId | str,
        name: str | ExecutionName,
        *argument_names: str,
    ) -> ExecutionShape:
        """A shape spelled out at a call site, from plain strings."""
        if len(set(argument_names)) != len(argument_names):
            raise ValueError(f"{name} names an argument more than once.")
        return cls(
            domain_id=(
                DomainId(domain_id) if isinstance(domain_id, str) else domain_id
            ),
            name=ExecutionName(name) if isinstance(name, str) else name,
            argument_names=frozenset(argument_names),
        )

    @property
    def key(self) -> tuple[DomainId, ExecutionName]:
        return (self.domain_id, self.name)


@dataclass(frozen=True)
class _Remap:
    source: ExecutionShape
    target: ExecutionShape
    convert_arguments: _ArgumentConverter | None


@dataclass(frozen=True)
class _Drop:
    source: ExecutionShape


_Rule: TypeAlias = "_Remap | _Drop"


class ExecutionMigrator(SessionMigrator):
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
        self._transitions = _normalized_transitions(version_transitions)
        self._rules: dict[tuple[DomainId, ExecutionName], _Rule] = {}

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
        self._require_transition(source.domain_id)
        self._require_transition(target.domain_id)
        self._register(
            source.key,
            _Remap(source=source, target=target, convert_arguments=convert_arguments),
        )

    def drop(self, source: ExecutionShape) -> None:
        """Registers the removal of ``source``'s records, and their descendants."""
        self._require_transition(source.domain_id)
        self._register(source.key, _Drop(source=source))

    def migrate(self, source: StoredSession) -> StoredSession:
        plan = _ExecutionMigrationPlan(self._transitions, self._rules)
        return _ExecutionMigrationRunner(plan, self._canonicalizer).migrate(source)

    # -- Registration --------------------------------------------------------

    def _require_transition(self, domain_id: DomainId) -> None:
        # The shapes carry no version, so the transitions are the only thing
        # saying which generation a rule was written against. A rule reaching a
        # domain none of them names would run whatever the session records.
        if domain_id not in self._transitions:
            raise MigrationError(
                f"This migration touches {domain_id} but declares no version "
                "transition for it, so nothing says which generation it is "
                "written against. Declare one, repeating the version if the "
                "domain only carries records across unchanged."
            )

    def _register(self, key: tuple[DomainId, ExecutionName], rule: _Rule) -> None:
        if key in self._rules:
            domain_id, name = key
            raise MigrationError(
                f"{name} in {domain_id} is already registered. One boundary "
                "changes shape once."
            )
        self._rules[key] = rule


class _ExecutionMigrationPlan:
    def __init__(
        self,
        transitions: Mapping[DomainId, DomainVersionTransition],
        rules: Mapping[tuple[DomainId, ExecutionName], _Rule],
    ) -> None:
        self._transitions = dict(transitions)
        self._rules = dict(rules)

    def transitions(self) -> Iterator[tuple[DomainId, DomainVersionTransition]]:
        return iter(self._transitions.items())

    def rule_for(self, domain_id: DomainId, name: ExecutionName) -> _Rule | None:
        return self._rules.get((domain_id, name))


class _ExecutionMigrationRunner:
    def __init__(
        self,
        plan: _ExecutionMigrationPlan,
        canonicalizer: ArgumentCanonicalizer,
    ) -> None:
        self._plan = plan
        self._canonicalizer = canonicalizer

    def migrate(self, source: StoredSession) -> StoredSession:
        recorded_versions = source.metadata.domain_versions
        self._require_source_versions(recorded_versions)
        migrated_versions = recorded_versions.replacing(
            {
                domain_id: transition.target
                for domain_id, transition in self._plan.transitions()
            }
        )
        return StoredSession(
            metadata=SessionMetadata(domain_versions=migrated_versions),
            executions=self._rebuild(source),
        )

    def _require_source_versions(self, recorded: DomainVersionMap) -> None:
        for domain_id, transition in self._plan.transitions():
            if transition.source is None:
                if domain_id in recorded:
                    raise MigrationError(
                        f"This migration claims {domain_id} as a domain the "
                        f"session has not entered, but it records "
                        f"{recorded[domain_id].value!r} for it."
                    )
                continue
            if domain_id not in recorded:
                raise MigrationError(
                    f"This migration reads {domain_id} at version "
                    f"{transition.source.value!r}, but the session records no version "
                    "for that domain."
                )
            if recorded[domain_id] != transition.source:
                raise MigrationError(
                    f"This migration reads {domain_id} at version "
                    f"{transition.source.value!r}, but the session records "
                    f"{recorded[domain_id].value!r}. It describes another generation of "
                    "these records."
                )

    # -- Rebuilding ----------------------------------------------------------

    def _rebuild(self, source: StoredSession) -> tuple[Execution, ...]:
        children = _children_by_parent(source.executions)

        migrated: list[Execution] = []
        arguments_by_source_scope: dict[ExecutionSequenceScope, CanonicalArguments] = {}
        source_scopes_by_target: dict[
            ExecutionSequenceScope, set[ExecutionSequenceScope]
        ] = {}

        # Ancestors first, so a parent's own key is settled before anything that
        # names it. Depth-first only because that keeps the walk iterative.
        stack: list[tuple[ExecutionId | None, ExecutionId | None]] = [(None, None)]
        while stack:
            old_parent, new_parent = stack.pop()
            for execution in sorted(
                children.get(old_parent, ()), key=lambda e: e.id.sequence
            ):
                rule = self._plan.rule_for(execution.id.domain_id, execution.id.name)
                if isinstance(rule, _Drop):
                    self._recorded(execution, rule.source)
                    continue

                # Memoized per source scope: its members are recorded with the
                # same bytes, so one conversion answers for all of them.
                source_scope = ExecutionSequenceScope.from_execution_id(execution.id)
                if source_scope not in arguments_by_source_scope:
                    arguments_by_source_scope[source_scope] = self._arguments(
                        execution, rule
                    )
                arguments = arguments_by_source_scope[source_scope]

                target = rule.target if rule is not None else None
                scope = ExecutionSequenceScope(
                    parent_id=new_parent,
                    domain_id=(target.domain_id if target else execution.id.domain_id),
                    name=target.name if target else execution.id.name,
                    arguments_digest=arguments.digest,
                )
                _require_one_source_scope(source_scopes_by_target, scope, source_scope)

                new_id = ExecutionId(
                    parent_id=scope.parent_id,
                    domain_id=scope.domain_id,
                    name=scope.name,
                    sequence=execution.id.sequence,
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

        recorded = self._recorded(execution, remap.source)
        try:
            converted = dict(remap.convert_arguments(**recorded))
        except Exception as e:
            raise MigrationError(
                f"Converting the arguments of {execution.id.name} in "
                f"{execution.id.domain_id} failed: {e}"
            ) from e

        if set(converted) != remap.target.argument_names:
            raise MigrationError(
                f"The conversion onto {remap.target.name} returned "
                f"{_format_argument_names(set(converted))}, but it is keyed by "
                f"{_format_argument_names(remap.target.argument_names)}."
            )
        return CanonicalArguments.from_canonical(
            self._canonicalizer.canonicalize(converted)
        )

    @staticmethod
    def _recorded(
        execution: Execution, source: ExecutionShape
    ) -> dict[str, RecordedArgumentValue]:
        try:
            stored = execution.arguments.recorded()
        except InvalidExecutionError as error:
            raise MigrationError(
                f"Cannot migrate {execution.id.name} in {execution.id.domain_id}: {error}"
            ) from error
        if set(stored) != source.argument_names:
            raise MigrationError(
                f"{execution.id.name} in {execution.id.domain_id} is recorded with "
                f"{_format_argument_names(set(stored))}, but the migration "
                f"describes it as {_format_argument_names(source.argument_names)}. "
                "It describes another generation of these records."
            )
        return dict(stored)


def _normalized_transitions(
    declared: Mapping[DomainId | str, DomainVersionTransition],
) -> dict[DomainId, DomainVersionTransition]:
    # Two keys spelling one domain are two Python keys but one generation guard,
    # so the later would quietly replace the earlier.
    normalized: dict[DomainId, DomainVersionTransition] = {}
    for declared_domain_id, transition in declared.items():
        domain_id = (
            DomainId(declared_domain_id)
            if isinstance(declared_domain_id, str)
            else declared_domain_id
        )
        if domain_id in normalized:
            raise MigrationError(
                f"This migration declares more than one version transition for "
                f"{domain_id}. A domain moves between one pair of generations."
            )
        normalized[domain_id] = transition
    return normalized


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
                f"{execution.id.name} in {execution.id.domain_id} names a parent "
                "the session does not hold, so there is no chain to rebuild."
            )
        children.setdefault(parent, []).append(execution)
    return children


def _require_one_source_scope(
    source_scopes_by_target: dict[ExecutionSequenceScope, set[ExecutionSequenceScope]],
    target: ExecutionSequenceScope,
    source: ExecutionSequenceScope,
) -> None:
    # An ordinal orders calls within one scope and means nothing across two, so
    # carrying one over is sound only while a scope's members all came from one.
    seen = source_scopes_by_target.setdefault(target, set())
    seen.add(source)
    if len(seen) > 1:
        raise MigrationOrdinalAmbiguityError(
            f"Migrating onto {target.name} in {target.domain_id} gathers calls "
            "that were recorded separately into one class of repeated calls, "
            "which are matched by ordinal. Nothing records which of them ran "
            "first. Give them arguments that tell them apart, or drop one."
        )
