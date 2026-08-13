"""Carrying a recorded session across a change of domain version.

This package is the mechanism, split in two: a `SessionMigrator` decides what a
session should become, and a backend's `SessionMigration` makes that the stored
truth atomically. Neither knows the other's half.
"""

from ._interfaces import MigratableBackend, SessionMigration, SessionMigrator
from ._models import (
    MigrationReport,
    SessionMetadata,
    StoredSession,
)
from ._migrator import DomainMigration, DomainVersionTransition, ExecutionShape

__all__ = [
    "DomainMigration",
    "DomainVersionTransition",
    "ExecutionShape",
    "MigratableBackend",
    "MigrationReport",
    "SessionMetadata",
    "SessionMigration",
    "SessionMigrator",
    "StoredSession",
]
