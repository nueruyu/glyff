"""What a migration declared as boundaries makes of a recorded session."""

import hashlib
import itertools
import json

import pytest
from glyff import CanonicalValue, DomainId, Execution, Opaque, SerializedValue
from glyff.exceptions import MigrationError, MigrationOrdinalAmbiguityError
from glyff.migration import (
    DomainVersionTransition,
    ExecutionMigrator,
    ExecutionShape,
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
) -> ExecutionMigrator:
    return ExecutionMigrator(
        canonicalizer=JsonArgumentCanonicalizer(),
        version_transitions=transitions or {PAY: DomainVersionTransition("v1", "v2")},
    )


def recorded(execution: Execution) -> CanonicalValue:
    # Read back what the migration wrote rather than rebuilding it through the
    # same encoder: an expectation built that way agrees with a bug.
    return json.loads(execution.arguments.data)


def migrate(migration: ExecutionMigrator, source: StoredSession) -> list[Execution]:
    return list(migration.migrate(source).executions)


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
        ExecutionShape.from_names(PAY, "authorize", "order"),
        ExecutionShape.from_names(PAY, "charge", "order"),
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
        ExecutionShape.from_names(PAY, "authorize", "order"),
        ExecutionShape.from_names(PAY, "charge", "order"),
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
            SHIP: DomainVersionTransition("v7", "v7"),
        }
    )
    migration.remap(
        ExecutionShape.from_names(PAY, "authorize", "order"),
        ExecutionShape.from_names(SHIP, "authorize", "order"),
    )
    replacement = migration.migrate(
        session(execution, versions={PAY: "v1", SHIP: "v7"})
    )

    assert replacement.executions[0].id.domain == SHIP
    assert replacement.metadata.domain_versions == {PAY: "v2", SHIP: "v7"}


def test_a_converted_argument_becomes_the_one_the_record_is_keyed_by():
    execution = started("authorize", arguments={"order": {"id": "ord_1"}, "units": 12})

    migration = migrator()
    migration.remap(
        ExecutionShape.from_names(PAY, "authorize", "order", "units"),
        ExecutionShape.from_names(PAY, "charge", "order_id", "cents"),
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
    execution = started("authorize", arguments={"order": "ord_1"})

    migration = migrator()
    migration.remap(
        ExecutionShape.from_names(PAY, "authorize", "order"),
        ExecutionShape.from_names(PAY, "authorize", "order", "currency"),
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
        ExecutionShape.from_names(PAY, "authorize", "client"),
        ExecutionShape.from_names(PAY, "charge", "client"),
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
        ExecutionShape.from_names(PAY, "authorize", "client", "order"),
        ExecutionShape.from_names(PAY, "charge", "client", "order"),
        convert_arguments=lambda client, order: {"client": client, "order": order},
    )
    [migrated] = migrate(migration, session(execution))

    assert migrated.arguments.data == execution.arguments.data


# -- Chains ------------------------------------------------------------------


def test_a_child_follows_its_remapped_parent():
    parent = started("authorize", arguments={"order": "ord_1"})
    child = started("capture", parent=parent.id)

    migration = migrator()
    migration.remap(
        ExecutionShape.from_names(PAY, "authorize", "order"),
        ExecutionShape.from_names(PAY, "charge", "order"),
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
        ExecutionShape.from_names(PAY, "authorize", "order"),
        ExecutionShape.from_names(PAY, "charge", "order"),
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
    migration.drop(ExecutionShape.from_names(PAY, "authorize", "order"))
    migrated = migrate(migration, session(root, child, kept))

    assert [execution.id.name.value for execution in migrated] == ["notify"]
    assert migrated[0].id == kept.id


def test_repeated_calls_keep_the_ordinals_that_tell_them_apart():
    first = started("retry", sequence=0)
    second = started("retry", sequence=1)

    migration = migrator()
    migration.remap(
        ExecutionShape.from_names(PAY, "retry"),
        ExecutionShape.from_names(PAY, "retried"),
    )
    migrated = migrate(migration, session(first, second))

    assert sorted(execution.id.sequence for execution in migrated) == [0, 1]
    assert {execution.id.name.value for execution in migrated} == {"retried"}


def test_an_ordinal_is_carried_over_rather_than_re_derived():
    survivor = started("retry", sequence=1)

    migrated = migrate(migrator(), session(survivor))

    assert migrated[0].id.sequence == 1


def test_a_migration_leaves_another_domains_ordinals_alone():
    elsewhere = started("label", sequence=1, domain=SHIP)

    migrated = migrate(migrator(), session(elsewhere, versions={PAY: "v1", SHIP: "v7"}))

    assert migrated[0].id == elsewhere.id


def test_identical_repeated_calls_are_converted_once_between_them():
    # They are recorded with the same bytes, so a second conversion could only
    # differ by being nondeterministic — and would scatter one class of repeated
    # calls across scopes that each start counting from 0 again.
    conversions = itertools.count()
    migration = migrator()
    migration.remap(
        ExecutionShape.from_names(PAY, "retry", "order"),
        ExecutionShape.from_names(PAY, "retried", "order"),
        convert_arguments=lambda order: {"order": f"{order}-{next(conversions)}"},
    )

    migrated = migrate(
        migration,
        session(
            started("retry", arguments={"order": "ord_1"}, sequence=0),
            started("retry", arguments={"order": "ord_1"}, sequence=1),
        ),
    )

    assert [recorded(execution) for execution in migrated] == [
        {"order": "ord_1-0"},
        {"order": "ord_1-0"},
    ]
    assert sorted(execution.id.sequence for execution in migrated) == [0, 1]


# -- Ordinals a migration cannot recover -------------------------------------


def test_gathering_separate_calls_into_one_class_is_refused():
    first = started("authorize", arguments={"order": "ord_1"})
    second = started("authorize", arguments={"order": "ord_2"})

    migration = migrator()
    migration.remap(
        ExecutionShape.from_names(PAY, "authorize", "order"),
        ExecutionShape.from_names(PAY, "charge", "at_all"),
        convert_arguments=lambda order: {"at_all": True},
    )

    with pytest.raises(MigrationOrdinalAmbiguityError, match="which of them ran first"):
        migrate(migration, session(first, second))


def test_two_boundaries_may_share_a_name_while_their_calls_stay_apart():
    authorize = started("authorize", arguments={"order": "ord_1"})
    capture = started("capture", arguments={"order": "ord_2"})

    migration = migrator()
    migration.remap(
        ExecutionShape.from_names(PAY, "authorize", "order"),
        ExecutionShape.from_names(PAY, "charge", "order"),
    )
    migration.remap(
        ExecutionShape.from_names(PAY, "capture", "order"),
        ExecutionShape.from_names(PAY, "charge", "order"),
    )
    migrated = migrate(migration, session(authorize, capture))

    assert {execution.id.name.value for execution in migrated} == {"charge"}
    assert [execution.id.sequence for execution in migrated] == [0, 0]


# -- Refusing a migration that does not describe these records ---------------


def test_records_of_another_generation_are_refused():
    execution = started("authorize", arguments={"order": "ord_1"})

    migration = migrator()
    migration.remap(
        ExecutionShape.from_names(PAY, "authorize", "order", "currency"),
        ExecutionShape.from_names(PAY, "charge", "order"),
        convert_arguments=lambda order, currency: {"order": order},
    )

    with pytest.raises(MigrationError, match="another generation"):
        migrate(migration, session(execution))


def test_a_conversion_that_misses_an_argument_is_refused():
    execution = started("authorize", arguments={"order": "ord_1"})

    migration = migrator()
    migration.remap(
        ExecutionShape.from_names(PAY, "authorize", "order"),
        ExecutionShape.from_names(PAY, "charge", "order_id", "cents"),
        convert_arguments=lambda order: {"order_id": order},
    )

    with pytest.raises(MigrationError, match="but it is keyed by"):
        migrate(migration, session(execution))


def test_a_rename_that_changes_the_parameters_needs_a_conversion():
    migration = migrator()

    with pytest.raises(MigrationError, match="conversion between them"):
        migration.remap(
            ExecutionShape.from_names(PAY, "authorize", "order"),
            ExecutionShape.from_names(PAY, "charge", "order_id"),
        )


def test_a_rule_reaching_an_undeclared_domain_is_refused():
    migration = migrator()

    with pytest.raises(MigrationError, match="declares no version transition"):
        migration.remap(
            ExecutionShape.from_names(SHIP, "authorize", "order"),
            ExecutionShape.from_names(SHIP, "charge", "order"),
        )

    with pytest.raises(MigrationError, match="declares no version transition"):
        migration.drop(ExecutionShape.from_names(SHIP, "authorize", "order"))


def test_a_rewrite_into_an_undeclared_domain_is_refused():
    migration = migrator()

    with pytest.raises(MigrationError, match="declares no version transition"):
        migration.remap(
            ExecutionShape.from_names(PAY, "authorize", "order"),
            ExecutionShape.from_names(SHIP, "authorize", "order"),
        )


def test_a_boundary_registered_twice_is_refused():
    migration = migrator()
    migration.remap(
        ExecutionShape.from_names(PAY, "authorize", "order"),
        ExecutionShape.from_names(PAY, "charge", "order"),
    )

    with pytest.raises(MigrationError, match="already registered"):
        migration.drop(ExecutionShape.from_names(PAY, "authorize", "order"))


@pytest.mark.parametrize(
    "build",
    [
        lambda: DomainVersionTransition("v1", ""),
        lambda: DomainVersionTransition("", "v2"),
        lambda: DomainVersionTransition.from_unclaimed(""),
    ],
)
def test_a_transition_to_or_from_an_empty_version_is_refused(build):
    with pytest.raises(ValueError, match="cannot be empty"):
        build()


def test_a_boundary_naming_one_parameter_twice_is_refused():
    with pytest.raises(ValueError, match="more than once"):
        ExecutionShape.from_names(PAY, "authorize", "order", "order")


def test_one_domain_declared_twice_is_refused():
    with pytest.raises(MigrationError, match="more than one version transition"):
        migrator(
            {
                PAY: DomainVersionTransition("v1", "v2"),
                PAY.value: DomainVersionTransition("v1", "v3"),
            }
        )


def test_an_opaque_nested_in_a_container_survives_a_conversion():
    execution = started(
        "authorize",
        arguments={
            "clients": [{"__glyff_opaque__": "com.example.PaymentClient"}],
            "units": 12,
        },
    )

    migration = migrator()
    migration.remap(
        ExecutionShape.from_names(PAY, "authorize", "clients", "units"),
        ExecutionShape.from_names(PAY, "charge", "clients", "cents"),
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
            SHIP: DomainVersionTransition.from_unclaimed("v1"),
        }
    )
    migration.remap(
        ExecutionShape.from_names(PAY, "authorize", "order"),
        ExecutionShape.from_names(SHIP, "authorize", "order"),
    )
    replacement = migration.migrate(session(execution))

    assert replacement.executions[0].id.domain == SHIP
    assert replacement.metadata.domain_versions == {PAY: "v2", SHIP: "v1"}


def test_claiming_a_domain_the_session_already_records_is_refused():
    execution = started("authorize", arguments={"order": "ord_1"})

    migration = migrator(
        {
            PAY: DomainVersionTransition("v1", "v2"),
            SHIP: DomainVersionTransition.from_unclaimed("v1"),
        }
    )

    with pytest.raises(MigrationError, match="has not entered"):
        migrate(migration, session(execution, versions={PAY: "v1", SHIP: "v7"}))


def test_a_session_that_never_entered_the_domain_is_refused():
    execution = started("authorize", arguments={"order": "ord_1"}, domain=SHIP)

    with pytest.raises(MigrationError, match="records no version"):
        migrate(migrator(), session(execution, versions={SHIP: "v1"}))


def test_a_drop_is_held_to_the_shape_it_declares():
    execution = started("authorize", arguments={"order": "ord_1"})

    migration = migrator()
    migration.drop(ExecutionShape.from_names(PAY, "authorize", "order", "currency"))

    with pytest.raises(MigrationError, match="another generation"):
        migrate(migration, session(execution))


# -- The report --------------------------------------------------------------


def test_the_replacement_carries_the_versions_it_was_migrated_to():
    execution = started("authorize", arguments={"order": "ord_1"})

    replacement = migrator().migrate(session(execution))

    assert replacement.metadata.domain_versions == {PAY: "v2"}
