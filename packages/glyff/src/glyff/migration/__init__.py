"""Carrying a recorded session across a change in application version.

This package is the mechanism, split in two: a `SessionMigrator` decides what a
session should become, and a backend's `SessionMigration` makes that the stored
truth atomically. Neither knows the other's half.
"""

from ._arguments import Opaque
from ._interfaces import MigratableBackend, SessionMigration, SessionMigrator
from ._models import (
    MigrationReport,
    SessionMetadata,
    SessionMigrationResult,
    StoredSession,
)
from ._remap import ArgumentConversion, Boundary, RemappingMigrator

__all__ = [
    "ArgumentConversion",
    "Boundary",
    "MigratableBackend",
    "MigrationReport",
    "Opaque",
    "RemappingMigrator",
    "SessionMetadata",
    "SessionMigration",
    "SessionMigrationResult",
    "SessionMigrator",
    "StoredSession",
]
