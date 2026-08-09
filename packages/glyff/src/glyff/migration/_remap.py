"""A migrator declared as the boundaries that changed shape."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .._execution import CanonicalArguments, Execution
from .._identity import DomainId, ExecutionId, ExecutionName
from .._interfaces import ArgumentCanonicalizer
from ..exceptions import MigrationError, MigrationOrderError
from ..serialization._utils import encode_canonical
from ._arguments import from_recorded, restore
from ._interfaces import SessionMigrator
from ._models import (
    MigrationReport,
    SessionMetadata,
    SessionMigrationResult,
    StoredSession,
)

ArgumentConversion = Callable[..., Mapping[str, Any]]
"""Old bound arguments, by name, to the ones the new boundary is keyed by."""


@dataclass(frozen=True)
class Boundary:
    """One engraved function, as a migration names it.

    Spelled out rather than read off the function, so a migration keeps
    describing the generation it was written for once the code has moved on.
    Reading a live signature would let an unrelated change to it silently
    reshape records this migration claims to know.

    ``parameters`` is every name a call of it carries — the ones with defaults
    too, since a recorded call names those as well.
    """

    domain: DomainId
    name: ExecutionName
    parameters: frozenset[str]

    def __init__(
        self, domain: DomainId | str, name: str | ExecutionName, *parameters: str
    ) -> None:
        object.__setattr__(
            self, "domain", DomainId(domain) if isinstance(domain, str) else domain
        )
        object.__setattr__(
            self, "name", ExecutionName(name) if isinstance(name, str) else name
        )
        object.__setattr__(self, "parameters", frozenset(parameters))
        if len(self.parameters) != len(parameters):
            raise ValueError(f"{self.name} names a parameter more than once.")

    @property
    def key(self) -> tuple[DomainId, ExecutionName]:
        return (self.domain, self.name)


@dataclass(frozen=True)
class _Rewrite:
    old: Boundary
    to: Boundary | None
    arguments: ArgumentConversion | None


# What a class of repeated calls was before a rewrite. A `sequence` is a call
# order only within the class that assigned it, so renumbering one is sound only
# while its members all come from a single class.
_SourceClass = tuple["ExecutionId | None", DomainId, ExecutionName, str]
_Class = tuple["ExecutionId | None", DomainId, ExecutionName, str]


class RemappingMigrator(SessionMigrator):
    """A `SessionMigrator` declared as pairs of boundaries.

    Each boundary that changed shape is registered as what it was, what it
    became, and the conversion between their arguments. Boundaries left
    unregistered keep their records untouched.

    This owns what a caller cannot reproduce: the canonical argument encoding,
    the parent chains a rewrite invalidates, and the ordinals a live `Sequencer`
    would assign on resume.
    """

    def __init__(
        self,
        *,
        canonicalizer: ArgumentCanonicalizer,
        to_domain_versions: Mapping[DomainId, str] | Mapping[str, str],
    ) -> None:
        # The same canonicalizer the resuming session builds. Unlike the
        # application's own types, this one is glyff's identity codec: a change
        # to it moves every paused session's keys, migration or not.
        self._canonicalizer = canonicalizer
        self._to_versions = {
            DomainId(domain) if isinstance(domain, str) else domain: version
            for domain, version in to_domain_versions.items()
        }
        self._rewrites: dict[tuple[DomainId, ExecutionName], _Rewrite] = {}

    def migrate_function(
        self,
        old: Boundary,
        to: Boundary,
        *,
        arguments: ArgumentConversion | None = None,
    ) -> None:
        """Moves the records of ``old`` onto ``to``.

        ``arguments`` receives the recorded arguments by their old names and
        returns the ones ``to`` is keyed by; leave it out when the parameters
        did not change, and the recorded form is kept as it is.
        """
        if arguments is None and old.parameters != to.parameters:
            self._refuse_silent_rename(old, to)
        self._register(old.key, _Rewrite(old=old, to=to, arguments=arguments))

    def drop_function(self, old: Boundary) -> None:
        """Removes the records of ``old``, and everything recorded beneath them.

        A descendant outlives its parent only as weight no resume can reach, so
        the subtree goes with it.
        """
        self._register(old.key, _Rewrite(old=old, to=None, arguments=None))

    def migrate(self, source: StoredSession) -> SessionMigrationResult:
        return SessionMigrationResult(
            session=StoredSession(
                metadata=SessionMetadata(domain_versions=self._to_versions),
                executions=self._rebuild(source),
            ),
            report=MigrationReport(
                from_domain_versions=source.metadata.domain_versions,
                to_domain_versions=self._to_versions,
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

    @staticmethod
    def _refuse_silent_rename(old: Boundary, to: Boundary) -> None:
        raise MigrationError(
            f"{old.name} takes {_named(old.parameters)} and {to.name} takes "
            f"{_named(to.parameters)}, so the recorded arguments cannot carry "
            "over unchanged. Give the migration a conversion between them."
        )

    # -- Rebuilding ----------------------------------------------------------

    def _rebuild(self, source: StoredSession) -> tuple[Execution, ...]:
        children = _children_by_parent(source.executions)

        migrated: list[Execution] = []
        ordinals: dict[_Class, int] = {}
        sources: dict[_Class, set[_SourceClass]] = {}

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
                    continue

                arguments = self._arguments(execution, rewrite)
                target = rewrite.to if rewrite is not None else None
                domain = target.domain if target is not None else execution.id.domain
                name = target.name if target is not None else execution.id.name

                cls: _Class = (new_parent, domain, name, arguments.digest.value)
                _record_source(sources, cls, execution.id)
                sequence = ordinals.get(cls, 0)
                ordinals[cls] = sequence + 1

                new_id = ExecutionId(
                    parent_id=new_parent,
                    domain=domain,
                    name=name,
                    sequence=sequence,
                    arguments_digest=arguments.digest,
                )
                migrated.append(replace(execution, id=new_id, arguments=arguments))
                stack.append((execution.id, new_id))

        return tuple(migrated)

    def _arguments(
        self, execution: Execution, rewrite: _Rewrite | None
    ) -> CanonicalArguments:
        if rewrite is None or rewrite.arguments is None:
            # The recorded bytes are the digest's preimage, so a boundary whose
            # arguments did not change keeps its key by keeping them.
            if rewrite is not None:
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

        if set(converted) != rewrite.to.parameters:
            raise MigrationError(
                f"The conversion onto {rewrite.to.name} returned "
                f"{_named(set(converted))}, but it takes "
                f"{_named(rewrite.to.parameters)}. A key is made of every "
                "argument a call is bound to, defaults included."
            )
        canonical = self._canonicalizer.canonicalize(restore(converted))
        return CanonicalArguments(encode_canonical(canonical))

    @staticmethod
    def _recorded(execution: Execution, old: Boundary) -> dict[str, Any]:
        stored = json.loads(execution.arguments.data)
        if set(stored) != old.parameters:
            raise MigrationError(
                f"{execution.id.name} in {execution.id.domain} recorded "
                f"{_named(set(stored))}, but the migration describes it as "
                f"taking {_named(old.parameters)}. These records belong to "
                "another generation of this boundary."
            )
        return {name: from_recorded(value) for name, value in stored.items()}


def _named(parameters: set[str] | frozenset[str]) -> str:
    return ", ".join(sorted(parameters)) or "no arguments"


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
    sources: dict[_Class, set[_SourceClass]], cls: _Class, old_id: ExecutionId
) -> None:
    source: _SourceClass = (
        old_id.parent_id,
        old_id.domain,
        old_id.name,
        old_id.arguments_digest.value,
    )
    seen = sources.setdefault(cls, set())
    seen.add(source)
    if len(seen) > 1:
        _, domain, name, _ = cls
        raise MigrationOrderError(
            f"Migrating onto {name} in {domain} gathers calls that were "
            "recorded separately into one class of repeated calls, which are "
            "matched by ordinal. Nothing records which of them ran first. Give "
            "them arguments that tell them apart, or drop one."
        )
