"""Carrying a recorded session across a change in application version.

This package is the mechanism, split in two: a `SessionMigrator` decides what a
session should become, and a backend's `SessionMigration` makes that the stored
truth atomically. Neither knows the other's half.
"""

from ._interfaces import MigratableBackend, SessionMigration, SessionMigrator
from ._models import (
    MigrationReport,
    SessionMetadata,
    SessionMigrationResult,
    StoredSession,
)

__all__ = [
    "MigratableBackend",
    "MigrationReport",
    "SessionMetadata",
    "SessionMigration",
    "SessionMigrationResult",
    "SessionMigrator",
    "StoredSession",
]
