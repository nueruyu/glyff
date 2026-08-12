"""Domain migration paths declared as execution-shape changes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, TypeAlias

from .._domain import Domain
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
class ExecutionShape:
    """What one generation of an engraved function is recorded as.

    Spelled out rather than read off a live function, so a later change to that
    function cannot quietly reshape records a migration claims to know.
    """

    name: ExecutionName
    argument_names: frozenset[str]

    def __post_init__(self) -> None:
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
        name: str | ExecutionName,
        *argument_names: str,
    ) -> ExecutionShape:
        """A shape spelled out at a call site, from plain strings."""
        if len(set(argument_names)) != len(argument_names):
            raise ValueError(f"{name} names an argument more than once.")
        return cls(
            name=ExecutionName(name) if isinstance(name, str) else name,
            argument_names=frozenset(argument_names),
        )

    @property
    def key(self) -> ExecutionName:
        return self.name


@dataclass(frozen=True)
class _Remap:
    source: ExecutionShape
    target: ExecutionShape
    convert_arguments: _ArgumentConverter | None


@dataclass(frozen=True)
class _Drop:
    source: ExecutionShape


_Rule: TypeAlias = "_Remap | _Drop"


class DomainVersionTransition:
    """The execution changes made by one direct domain-version transition."""

    def __init__(
        self,
        domain_id: DomainId,
        source_version: DomainVersion,
        target_version: DomainVersion,
    ) -> None:
        self._domain_id = domain_id
        self._source_version = source_version
        self._target_version = target_version
        self._rules: dict[ExecutionName, _Rule] = {}

    @property
    def source_version(self) -> DomainVersion:
        return self._source_version

    @property
    def target_version(self) -> DomainVersion:
        return self._target_version

    def remap(
        self,
        source: ExecutionShape,
        target: ExecutionShape,
        *,
        convert_arguments: _ArgumentConverter | None = None,
    ) -> DomainVersionTransition:
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
        self._register(
            source.key,
            _Remap(source=source, target=target, convert_arguments=convert_arguments),
        )
        return self

    def drop(self, source: ExecutionShape) -> DomainVersionTransition:
        """Registers the removal of ``source``'s records, and their descendants."""
        self._register(source.key, _Drop(source=source))
        return self

    def _register(self, key: ExecutionName, rule: _Rule) -> None:
        if key in self._rules:
            raise MigrationError(
                f"{key} in {self._domain_id} is already registered. One boundary "
                "changes shape once."
            )
        self._rules[key] = rule

    def _plan(self) -> _ExecutionMigrationPlan:
        return _ExecutionMigrationPlan(
            domain_id=self._domain_id,
            source_version=self._source_version,
            target_version=self._target_version,
            rules=self._rules,
        )


class DomainMigration(SessionMigrator):
    """Migration definitions and execution for one domain."""

    def __init__(
        self,
        domain: Domain,
        *,
        canonicalizer: ArgumentCanonicalizer,
    ) -> None:
        self._domain = domain
        self._canonicalizer = canonicalizer
        self._transitions: dict[DomainVersion, DomainVersionTransition] = {}

    def transition(
        self,
        source: DomainVersion | str,
        target: DomainVersion | str,
    ) -> DomainVersionTransition:
        source_version = (
            source if isinstance(source, DomainVersion) else DomainVersion(source)
        )
        target_version = (
            target if isinstance(target, DomainVersion) else DomainVersion(target)
        )
        if source_version == target_version:
            raise MigrationError("A domain-version transition must change the version.")
        if source_version in self._transitions:
            raise MigrationError(
                f"{self._domain.id} already has a migration from "
                f"{source_version.value!r}."
            )
        self._require_acyclic(source_version, target_version)
        transition = DomainVersionTransition(
            self._domain.id, source_version, target_version
        )
        self._transitions[source_version] = transition
        return transition

    def execute(self, source: StoredSession) -> StoredSession:
        versions = source.metadata.domain_versions
        if self._domain.id not in versions:
            raise MigrationError(
                f"Cannot migrate {self._domain.id}: the session records no version "
                "for that domain."
            )

        plans = {
            source_version: transition._plan()
            for source_version, transition in self._transitions.items()
        }
        migrated = source
        version = versions[self._domain.id]
        visited: set[DomainVersion] = set()
        while version != self._domain.version:
            if version in visited:
                raise MigrationError(
                    f"The migration path for {self._domain.id} contains a cycle."
                )
            visited.add(version)
            plan = plans.get(version)
            if plan is None:
                raise MigrationError(
                    f"No migration path takes {self._domain.id} from "
                    f"{version.value!r} to {self._domain.version.value!r}."
                )
            migrated = _ExecutionMigrationRunner(plan, self._canonicalizer).migrate(
                migrated
            )
            version = plan.target_version
        return migrated

    def migrate(self, source: StoredSession) -> StoredSession:
        return self.execute(source)

    def _require_acyclic(self, source: DomainVersion, target: DomainVersion) -> None:
        version = target
        while True:
            if version == source:
                raise MigrationError(
                    f"Adding {source.value!r} to {target.value!r} would create "
                    f"a migration cycle for {self._domain.id}."
                )
            if version not in self._transitions:
                return
            version = self._transitions[version].target_version


class _ExecutionMigrationPlan:
    def __init__(
        self,
        *,
        domain_id: DomainId,
        source_version: DomainVersion,
        target_version: DomainVersion,
        rules: Mapping[ExecutionName, _Rule],
    ) -> None:
        self.domain_id = domain_id
        self.source_version = source_version
        self.target_version = target_version
        self._rules = dict(rules)

    def rule_for(self, domain_id: DomainId, name: ExecutionName) -> _Rule | None:
        if domain_id != self.domain_id:
            return None
        return self._rules.get(name)


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
            {self._plan.domain_id: self._plan.target_version}
        )
        return StoredSession(
            metadata=SessionMetadata(domain_versions=migrated_versions),
            executions=self._rebuild(source),
        )

    def _require_source_versions(self, recorded: DomainVersionMap) -> None:
        domain_id = self._plan.domain_id
        if domain_id not in recorded:
            raise MigrationError(
                f"This migration reads {domain_id} at version "
                f"{self._plan.source_version.value!r}, but the session records no "
                "version for that domain."
            )
        if recorded[domain_id] != self._plan.source_version:
            raise MigrationError(
                f"This migration reads {domain_id} at version "
                f"{self._plan.source_version.value!r}, but the session records "
                f"{recorded[domain_id].value!r}. It describes another generation "
                "of these records."
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
                    domain_id=execution.id.domain_id,
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
