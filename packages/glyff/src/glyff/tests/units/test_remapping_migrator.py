"""What a migration declared as boundaries makes of a recorded session."""

import hashlib
import json

import pytest
from glyff import CanonicalValue, DomainId, Execution, Opaque, SerializedValue
from glyff.exceptions import MigrationError, MigrationOrdinalAmbiguityError
from glyff.migration import (
    DomainVersionTransition,
    ExecutionShape,
    RemappingMigrator,
    SessionMetadata,
    StoredSession,
)
from glyff.serialization import JsonArgumentCanonicalizer
from glyff.testing import canonical_arguments, make_execution_id

PAY = DomainId("com.example.payments")
SHIP = DomainId("com.example.shipping")


def started(
    name: str,
    *,
    arguments: dict[str, CanonicalValue] | None = None,
    parent=None,
    sequence: int = 0,
    domain: DomainId = PAY,
) -> Execution:
    return Execution.start(
        make_execution_id(
            name,
            parent=parent,
            sequence=sequence,
            arguments=arguments,
            domain=domain,
        ),
        canonical_arguments(arguments),
    )


def session(
    *executions: Execution, versions: dict[DomainId, str] | None = None
) -> StoredSession:
    return StoredSession(
        metadata=SessionMetadata(domain_versions=versions or {PAY: "v1"}),
        executions=executions,
    )


def migrator(
    transitions: dict[DomainId | str, DomainVersionTransition] | None = None,
) -> RemappingMigrator:
    return RemappingMigrator(
        canonicalizer=JsonArgumentCanonicalizer(),
        version_transitions=transitions or {PAY: DomainVersionTransition("v1", "v2")},
    )


def recorded(execution: Execution) -> CanonicalValue:
    # Read back what the migration wrote rather than rebuilding it through the
    # same encoder: an expectation built that way agrees with a bug.
    return json.loads(execution.arguments.data)


def migrate(migration: RemappingMigrator, source: StoredSession) -> list[Execution]:
    return list(migration.migrate(source).session.executions)


# -- Leaving records alone ---------------------------------------------------


def test_an_unregistered_boundary_keeps_its_records():
    execution = started("authorize", arguments={"order": "ord_1"})

    [migrated] = migrate(migrator(), session(execution))

    assert migrated.id == execution.id
    assert migrated.arguments.data == execution.arguments.data


def test_a_migrated_execution_keeps_its_result_and_metadata():
    execution = started("authorize", arguments={"order": "ord_1"})
    execution.complete(SerializedValue(b'"receipt"'))
    execution.set_metadata("attempts", SerializedValue(b"2"))

    migration = migrator()
    migration.remap(
        ExecutionShape(PAY, "authorize", "order"),
        ExecutionShape(PAY, "charge", "order"),
    )
    [migrated] = migrate(migration, session(execution))

    assert migrated.id.name.value == "charge"
    assert migrated.result == execution.result
    assert migrated.metadata == execution.metadata


# -- Moving a boundary -------------------------------------------------------


def test_a_renamed_boundary_keeps_everything_but_its_name():
    execution = started("authorize", arguments={"order": "ord_1"})

    migration = migrator()
    migration.remap(
        ExecutionShape(PAY, "authorize", "order"),
        ExecutionShape(PAY, "charge", "order"),
    )
    [migrated] = migrate(migration, session(execution))

    assert migrated.id.name.value == "charge"
    assert migrated.id.domain == PAY
    assert migrated.arguments.data == execution.arguments.data


def test_a_boundary_can_move_to_another_domain():
    execution = started("authorize", arguments={"order": "ord_1"})

    migration = migrator(
        {
            PAY: DomainVersionTransition("v1", "v2"),
            # Unchanged, but named: a rule reaching it has to say which
            # generation it was written against.
            SHIP: DomainVersionTransition("v7", "v7"),
        }
    )
    migration.remap(
        ExecutionShape(PAY, "authorize", "order"),
        ExecutionShape(SHIP, "authorize", "order"),
    )
    result = migration.migrate(session(execution, versions={PAY: "v1", SHIP: "v7"}))

    assert result.session.executions[0].id.domain == SHIP
    # A migration names the domains it owns; one it says nothing about keeps the
    # version it had.
    assert result.report.to_domain_versions == {PAY: "v2", SHIP: "v7"}


def test_a_converted_argument_becomes_the_one_the_record_is_keyed_by():
    execution = started("authorize", arguments={"order": {"id": "ord_1"}, "units": 12})

    migration = migrator()
    migration.remap(
        ExecutionShape(PAY, "authorize", "order", "units"),
        ExecutionShape(PAY, "charge", "order_id", "cents"),
        convert_arguments=lambda order, units: {
            "order_id": order["id"],
            "cents": units * 100,
        },
    )
    [migrated] = migrate(migration, session(execution))

    assert recorded(migrated) == {"order_id": "ord_1", "cents": 1200}
    assert migrated.id.arguments_digest.value == (
        hashlib.sha256(migrated.arguments.data).hexdigest()
    )


def test_a_default_the_new_boundary_gained_is_written_out_by_the_migration():
    # Defaults are part of a bound call, so a migration states them rather than
    # having them applied from a signature it deliberately does not read.
    execution = started("authorize", arguments={"order": "ord_1"})

    migration = migrator()
    migration.remap(
        ExecutionShape(PAY, "authorize", "order"),
        ExecutionShape(PAY, "authorize", "order", "currency"),
        convert_arguments=lambda order: {"order": order, "currency": "JPY"},
    )
    [migrated] = migrate(migration, session(execution))

    assert recorded(migrated) == {"order": "ord_1", "currency": "JPY"}


# -- Opaque arguments --------------------------------------------------------


def test_an_opaque_argument_reaches_the_conversion_as_itself():
    seen: list[object] = []
    execution = started(
        "authorize",
        arguments={"client": {"__glyff_opaque__": "com.example.PaymentClient"}},
    )

    migration = migrator()
    migration.remap(
        ExecutionShape(PAY, "authorize", "client"),
        ExecutionShape(PAY, "charge", "client"),
        convert_arguments=lambda client: seen.append(client) or {"client": client},
    )
    migrate(migration, session(execution))

    assert seen == [Opaque("com.example.PaymentClient")]


def test_an_opaque_argument_passed_through_keys_the_call_as_it_did_before():
    execution = started(
        "authorize",
        arguments={
            "client": {"__glyff_opaque__": "com.example.PaymentClient"},
            "order": "ord_1",
        },
    )

    migration = migrator()
    migration.remap(
        ExecutionShape(PAY, "authorize", "client", "order"),
        ExecutionShape(PAY, "charge", "client", "order"),
        convert_arguments=lambda client, order: {"client": client, "order": order},
    )
    [migrated] = migrate(migration, session(execution))

    # Byte-for-byte: the marker is glyff's own, so a migration that never
    # touched the argument must not be able to change how it is recorded.
    assert migrated.arguments.data == execution.arguments.data


# -- Chains ------------------------------------------------------------------


def test_a_child_follows_its_remapped_parent():
    parent = started("authorize", arguments={"order": "ord_1"})
    child = started("capture", parent=parent.id)

    migration = migrator()
    migration.remap(
        ExecutionShape(PAY, "authorize", "order"),
        ExecutionShape(PAY, "charge", "order"),
    )
    migrated = migrate(migration, session(parent, child))
    by_name = {execution.id.name.value: execution for execution in migrated}

    assert by_name["capture"].id.parent_id == by_name["charge"].id


def test_a_grandchild_follows_too():
    root = started("authorize", arguments={"order": "ord_1"})
    child = started("capture", parent=root.id)
    grandchild = started("settle", parent=child.id)

    migration = migrator()
    migration.remap(
        ExecutionShape(PAY, "authorize", "order"),
        ExecutionShape(PAY, "charge", "order"),
    )
    migrated = migrate(migration, session(root, child, grandchild))
    by_name = {execution.id.name.value: execution for execution in migrated}

    assert by_name["settle"].id.parent_id == by_name["capture"].id
    assert by_name["capture"].id.parent_id == by_name["charge"].id


def test_an_execution_whose_parent_is_missing_is_refused():
    absent = started("authorize", arguments={"order": "ord_1"})
    orphan = started("capture", parent=absent.id)

    with pytest.raises(MigrationError, match="names a parent"):
        migrate(migrator(), session(orphan))


# -- Dropping ----------------------------------------------------------------


def test_dropping_a_boundary_takes_what_was_recorded_beneath_it():
    root = started("authorize", arguments={"order": "ord_1"})
    child = started("capture", parent=root.id)
    kept = started("notify", arguments={"order": "ord_1"})

    migration = migrator()
    migration.drop(ExecutionShape(PAY, "authorize", "order"))
    migrated = migrate(migration, session(root, child, kept))

    assert [execution.id.name.value for execution in migrated] == ["notify"]
    assert migrated[0].id == kept.id


def test_each_class_of_repeated_calls_counts_from_zero():
    # One counter per (parent, domain, name, arguments), the way a live
    # `Sequencer` keeps them: a shared one would push a neighbour off its key.
    first = started("retry", sequence=0)
    second = started("retry", sequence=1)
    other = started("notify", arguments={"order": "ord_1"})

    migrated = migrate(migrator(), session(other, first, second))

    ordinals: dict[str, list[int]] = {}
    for execution in migrated:
        ordinals.setdefault(execution.id.name.value, []).append(execution.id.sequence)

    assert sorted(ordinals["retry"]) == [0, 1]
    assert ordinals["notify"] == [0]


def test_an_ordinal_no_resume_could_reach_is_closed_up():
    # Whatever left the hole — an earlier migration, a deletion — a live
    # `Sequencer` counts from zero, so the record has to move down to it.
    survivor = started("retry", sequence=1)

    migrated = migrate(migrator(), session(survivor))

    assert migrated[0].id.sequence == 0


# -- Ordinals a migration cannot recover -------------------------------------


def test_gathering_separate_calls_into_one_class_is_refused():
    first = started("authorize", arguments={"order": "ord_1"})
    second = started("authorize", arguments={"order": "ord_2"})

    migration = migrator()
    migration.remap(
        ExecutionShape(PAY, "authorize", "order"),
        ExecutionShape(PAY, "charge", "at_all"),
        convert_arguments=lambda order: {"at_all": True},
    )

    with pytest.raises(MigrationOrdinalAmbiguityError, match="which of them ran first"):
        migrate(migration, session(first, second))


def test_two_boundaries_may_share_a_name_while_their_calls_stay_apart():
    authorize = started("authorize", arguments={"order": "ord_1"})
    capture = started("capture", arguments={"order": "ord_2"})

    migration = migrator()
    migration.remap(
        ExecutionShape(PAY, "authorize", "order"),
        ExecutionShape(PAY, "charge", "order"),
    )
    migration.remap(
        ExecutionShape(PAY, "capture", "order"), ExecutionShape(PAY, "charge", "order")
    )
    migrated = migrate(migration, session(authorize, capture))

    assert {execution.id.name.value for execution in migrated} == {"charge"}
    assert [execution.id.sequence for execution in migrated] == [0, 0]


# -- Refusing a migration that does not describe these records ---------------


def test_records_of_another_generation_are_refused():
    execution = started("authorize", arguments={"order": "ord_1"})

    migration = migrator()
    migration.remap(
        ExecutionShape(PAY, "authorize", "order", "currency"),
        ExecutionShape(PAY, "charge", "order"),
        convert_arguments=lambda order, currency: {"order": order},
    )

    with pytest.raises(MigrationError, match="another generation"):
        migrate(migration, session(execution))


def test_a_conversion_that_misses_an_argument_is_refused():
    execution = started("authorize", arguments={"order": "ord_1"})

    migration = migrator()
    migration.remap(
        ExecutionShape(PAY, "authorize", "order"),
        ExecutionShape(PAY, "charge", "order_id", "cents"),
        convert_arguments=lambda order: {"order_id": order},
    )

    with pytest.raises(MigrationError, match="but it is keyed by"):
        migrate(migration, session(execution))


def test_a_rename_that_changes_the_parameters_needs_a_conversion():
    migration = migrator()

    with pytest.raises(MigrationError, match="conversion between them"):
        migration.remap(
            ExecutionShape(PAY, "authorize", "order"),
            ExecutionShape(PAY, "charge", "order_id"),
        )


def test_a_rule_reaching_an_undeclared_domain_is_refused():
    # Shapes carry no version, so the transitions are the only thing saying
    # which generation a rule was written against.
    migration = migrator()

    with pytest.raises(MigrationError, match="declares no version transition"):
        migration.remap(
            ExecutionShape(SHIP, "authorize", "order"),
            ExecutionShape(SHIP, "charge", "order"),
        )

    with pytest.raises(MigrationError, match="declares no version transition"):
        migration.drop(ExecutionShape(SHIP, "authorize", "order"))


def test_a_rewrite_into_an_undeclared_domain_is_refused():
    migration = migrator()

    with pytest.raises(MigrationError, match="declares no version transition"):
        migration.remap(
            ExecutionShape(PAY, "authorize", "order"),
            ExecutionShape(SHIP, "authorize", "order"),
        )


def test_a_boundary_registered_twice_is_refused():
    migration = migrator()
    migration.remap(
        ExecutionShape(PAY, "authorize", "order"),
        ExecutionShape(PAY, "charge", "order"),
    )

    with pytest.raises(MigrationError, match="already registered"):
        migration.drop(ExecutionShape(PAY, "authorize", "order"))


def test_a_boundary_naming_one_parameter_twice_is_refused():
    with pytest.raises(ValueError, match="more than once"):
        ExecutionShape(PAY, "authorize", "order", "order")


def test_an_opaque_nested_in_a_container_survives_a_conversion():
    # The canonicalizer applies a policy at any depth, so a recorded marker can
    # sit anywhere. Converting one argument must not cost another its key.
    execution = started(
        "authorize",
        arguments={
            "clients": [{"__glyff_opaque__": "com.example.PaymentClient"}],
            "units": 12,
        },
    )

    migration = migrator()
    migration.remap(
        ExecutionShape(PAY, "authorize", "clients", "units"),
        ExecutionShape(PAY, "charge", "clients", "cents"),
        convert_arguments=lambda clients, units: {
            "clients": clients,
            "cents": units * 100,
        },
    )
    [migrated] = migrate(migration, session(execution))

    assert recorded(migrated) == {
        "clients": [{"__glyff_opaque__": "com.example.PaymentClient"}],
        "cents": 1200,
    }


# -- The generation a migration reads ----------------------------------------


def test_a_session_at_another_version_is_refused():
    execution = started("authorize", arguments={"order": "ord_1"})

    with pytest.raises(MigrationError, match="another generation"):
        migrate(migrator(), session(execution, versions={PAY: "v0"}))


def test_a_rewrite_may_claim_a_domain_the_session_has_not_entered():
    execution = started("authorize", arguments={"order": "ord_1"})

    migration = migrator(
        {
            PAY: DomainVersionTransition("v1", "v2"),
            SHIP: DomainVersionTransition.claiming("v1"),
        }
    )
    migration.remap(
        ExecutionShape(PAY, "authorize", "order"),
        ExecutionShape(SHIP, "authorize", "order"),
    )
    result = migration.migrate(session(execution))

    assert result.session.executions[0].id.domain == SHIP
    assert result.report.to_domain_versions == {PAY: "v2", SHIP: "v1"}


def test_claiming_a_domain_the_session_already_records_is_refused():
    execution = started("authorize", arguments={"order": "ord_1"})

    migration = migrator(
        {
            PAY: DomainVersionTransition("v1", "v2"),
            SHIP: DomainVersionTransition.claiming("v1"),
        }
    )

    with pytest.raises(MigrationError, match="has not entered"):
        migrate(migration, session(execution, versions={PAY: "v1", SHIP: "v7"}))


def test_a_session_that_never_entered_the_domain_is_refused():
    execution = started("authorize", arguments={"order": "ord_1"}, domain=SHIP)

    with pytest.raises(MigrationError, match="records no version"):
        migrate(migrator(), session(execution, versions={SHIP: "v1"}))


def test_a_drop_is_held_to_the_shape_it_declares():
    # The destructive half, so at least as strict as a rewrite: a migration
    # describing another generation must not delete this one's records.
    execution = started("authorize", arguments={"order": "ord_1"})

    migration = migrator()
    migration.drop(ExecutionShape(PAY, "authorize", "order", "currency"))

    with pytest.raises(MigrationError, match="another generation"):
        migrate(migration, session(execution))


# -- The report --------------------------------------------------------------


def test_the_report_carries_the_versions_on_both_sides():
    execution = started("authorize", arguments={"order": "ord_1"})

    report = migrator().migrate(session(execution)).report

    assert report.from_domain_versions == {PAY: "v1"}
    assert report.to_domain_versions == {PAY: "v2"}
