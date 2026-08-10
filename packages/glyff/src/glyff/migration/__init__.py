"""Carrying a recorded session across a change of domain version.

This package is the mechanism, split in two: a `SessionMigrator` decides what a
session should become, and a backend's `SessionMigration` makes that the stored
truth atomically. Neither knows the other's half.
"""

from ._arguments import RecordedValue
from ..serialization import Opaque
from ._interfaces import MigratableBackend, SessionMigration, SessionMigrator
from ._models import (
    MigrationReport,
    SessionMetadata,
    SessionMigrationResult,
    StoredSession,
)
from ._remap import (
    ArgumentConversion,
    ExecutionShape,
    RemappingMigrator,
    VersionChange,
)

__all__ = [
    "ArgumentConversion",
    "ExecutionShape",
    "MigratableBackend",
    "MigrationReport",
    "Opaque",
    "RecordedValue",
    "RemappingMigrator",
    "SessionMetadata",
    "SessionMigration",
    "SessionMigrationResult",
    "SessionMigrator",
    "StoredSession",
    "VersionChange",
]
