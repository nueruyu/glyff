"""What a session snapshot refuses to be."""

import pytest
from glyff import DomainId, Execution
from glyff.exceptions import MigrationCollisionError, MigrationError
from glyff.migration import MigrationReport, SessionMetadata, StoredSession
from glyff.testing import canonical_arguments, make_execution_id

PAYMENTS = DomainId("com.example.payments")
SHIPPING = DomainId("com.example.shipping")


def started(name: str, *, domain: DomainId = PAYMENTS, parent=None) -> Execution:
    return Execution.start(
        make_execution_id(name, domain=domain, parent=parent), canonical_arguments()
    )


def session(versions: dict[DomainId, str], *executions: Execution) -> StoredSession:
    return StoredSession(
        metadata=SessionMetadata(domain_versions=versions), executions=executions
    )


def test_a_session_holding_its_domains_versions_is_accepted():
    assert session({PAYMENTS: "v1"}, started("task")).executions


def test_two_executions_on_one_id_are_refused():
    execution = started("task")

    with pytest.raises(MigrationCollisionError):
        session({PAYMENTS: "v1"}, execution, started("task"))


def test_an_execution_whose_domain_has_no_version_is_refused():
    with pytest.raises(MigrationError, match="com.example.payments"):
        session({}, started("task"))


def test_a_domain_that_only_appears_as_an_ancestor_is_still_required():
    parent = started("parent", domain=PAYMENTS)
    child = started("child", domain=SHIPPING, parent=parent.id)

    with pytest.raises(MigrationError, match="com.example.payments"):
        session({SHIPPING: "v1"}, child)


def test_a_version_with_no_execution_is_allowed():
    # Migration and pruning both remove executions; a version once agreed is
    # still worth keeping.
    assert session({PAYMENTS: "v1", SHIPPING: "v2"}, started("task")).metadata


def test_the_recorded_versions_are_not_the_callers_mapping():
    versions = {PAYMENTS: "v1"}
    stored = session(versions, started("task"))

    versions[PAYMENTS] = "v2"

    assert stored.metadata.domain_versions == {PAYMENTS: "v1"}


def test_the_recorded_versions_cannot_be_written_through():
    stored = session({PAYMENTS: "v1"}, started("task"))

    with pytest.raises(TypeError):
        stored.metadata.domain_versions[SHIPPING] = "v2"  # type: ignore[index]


def test_a_reports_versions_are_not_the_callers_mappings():
    before, after = {PAYMENTS: "v1"}, {PAYMENTS: "v2"}
    report = MigrationReport(from_domain_versions=before, to_domain_versions=after)

    before[PAYMENTS], after[PAYMENTS] = "changed", "changed"

    assert report.from_domain_versions == {PAYMENTS: "v1"}
    assert report.to_domain_versions == {PAYMENTS: "v2"}
