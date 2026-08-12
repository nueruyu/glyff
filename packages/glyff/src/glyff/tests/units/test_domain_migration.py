"""What a migration declared as boundaries makes of a recorded session."""

import hashlib
import itertools
import json

import pytest
from glyff import (
    CanonicalArgumentValue,
    CanonicalValue,
    Domain,
    DomainId,
    DomainVersionMap,
    Execution,
    CanonicalFallback,
    SerializedValue,
)
from glyff.exceptions import MigrationError, MigrationOrdinalAmbiguityError
from glyff.migration import (
    DomainMigration,
    DomainVersionTransition,
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
    arguments: dict[str, CanonicalArgumentValue] | None = None,
    parent=None,
    sequence: int = 0,
    domain_id: DomainId = PAY,
) -> Execution:
    return Execution.start(
        make_execution_id(
            name,
            parent=parent,
            sequence=sequence,
            arguments=arguments,
            domain_id=domain_id,
        ),
        canonical_arguments(arguments),
    )


def session(
    *executions: Execution, versions: dict[DomainId, str] | None = None
) -> StoredSession:
    return StoredSession(
        metadata=SessionMetadata(
            domain_versions=DomainVersionMap(versions or {PAY: "v1"})
        ),
        executions=executions,
    )


class MigrationFixture:
    def __init__(self, source: str = "v1", target: str = "v2") -> None:
        self.migration = DomainMigration(
            Domain(PAY, version=target),
            canonicalizer=JsonArgumentCanonicalizer(),
        )
        self.transition = self.migration.transition(source, target)

    def remap(self, *args, **kwargs) -> DomainVersionTransition:
        return self.transition.remap(*args, **kwargs)

    def drop(self, *args, **kwargs) -> DomainVersionTransition:
        return self.transition.drop(*args, **kwargs)

    def migrate(self, source: StoredSession) -> StoredSession:
        return self.migration.migrate(source)


def migrator() -> MigrationFixture:
    return MigrationFixture()


def recorded(execution: Execution) -> CanonicalValue:
    # Read back what the migration wrote rather than rebuilding it through the
    # same encoder: an expectation built that way agrees with a bug.
    return json.loads(execution.arguments.data)


def migrate(migration: MigrationFixture, source: StoredSession) -> list[Execution]:
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
        ExecutionShape.from_names("authorize", "order"),
        ExecutionShape.from_names("charge", "order"),
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
        ExecutionShape.from_names("authorize", "order"),
        ExecutionShape.from_names("charge", "order"),
    )
    [migrated] = migrate(migration, session(execution))

    assert migrated.id.name.value == "charge"
    assert migrated.id.domain_id == PAY
    assert migrated.arguments.data == execution.arguments.data


def test_a_converted_argument_becomes_the_one_the_record_is_keyed_by():
    execution = started("authorize", arguments={"order": {"id": "ord_1"}, "units": 12})

    migration = migrator()
    migration.remap(
        ExecutionShape.from_names("authorize", "order", "units"),
        ExecutionShape.from_names("charge", "order_id", "cents"),
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
        ExecutionShape.from_names("authorize", "order"),
        ExecutionShape.from_names("authorize", "order", "currency"),
        convert_arguments=lambda order: {"order": order, "currency": "JPY"},
    )
    [migrated] = migrate(migration, session(execution))

    assert recorded(migrated) == {"order": "ord_1", "currency": "JPY"}


# -- Canonical fallback arguments --------------------------------------------


def test_a_fallback_argument_reaches_the_conversion_as_itself():
    seen: list[object] = []
    execution = started(
        "authorize",
        arguments={"client": CanonicalFallback("com.example.PaymentClient")},
    )

    migration = migrator()
    migration.remap(
        ExecutionShape.from_names("authorize", "client"),
        ExecutionShape.from_names("charge", "client"),
        convert_arguments=lambda client: seen.append(client) or {"client": client},
    )
    migrate(migration, session(execution))

    assert seen == [CanonicalFallback("com.example.PaymentClient")]


def test_a_fallback_argument_passed_through_keys_the_call_as_it_did_before():
    execution = started(
        "authorize",
        arguments={
            "client": CanonicalFallback("com.example.PaymentClient"),
            "order": "ord_1",
        },
    )

    migration = migrator()
    migration.remap(
        ExecutionShape.from_names("authorize", "client", "order"),
        ExecutionShape.from_names("charge", "client", "order"),
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
        ExecutionShape.from_names("authorize", "order"),
        ExecutionShape.from_names("charge", "order"),
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
        ExecutionShape.from_names("authorize", "order"),
        ExecutionShape.from_names("charge", "order"),
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
    migration.drop(ExecutionShape.from_names("authorize", "order"))
    migrated = migrate(migration, session(root, child, kept))

    assert [execution.id.name.value for execution in migrated] == ["notify"]
    assert migrated[0].id == kept.id


def test_repeated_calls_keep_the_ordinals_that_tell_them_apart():
    first = started("retry", sequence=0)
    second = started("retry", sequence=1)

    migration = migrator()
    migration.remap(
        ExecutionShape.from_names("retry"),
        ExecutionShape.from_names("retried"),
    )
    migrated = migrate(migration, session(first, second))

    assert sorted(execution.id.sequence for execution in migrated) == [0, 1]
    assert {execution.id.name.value for execution in migrated} == {"retried"}


def test_an_ordinal_is_carried_over_rather_than_re_derived():
    survivor = started("retry", sequence=1)

    migrated = migrate(migrator(), session(survivor))

    assert migrated[0].id.sequence == 1


def test_a_migration_leaves_another_domains_ordinals_alone():
    elsewhere = started("label", sequence=1, domain_id=SHIP)

    migrated = migrate(migrator(), session(elsewhere, versions={PAY: "v1", SHIP: "v7"}))

    assert migrated[0].id == elsewhere.id


def test_identical_repeated_calls_are_converted_once_between_them():
    # They are recorded with the same bytes, so a second conversion could only
    # differ by being nondeterministic — and would scatter one class of repeated
    # calls across scopes that each start counting from 0 again.
    conversions = itertools.count()
    migration = migrator()
    migration.remap(
        ExecutionShape.from_names("retry", "order"),
        ExecutionShape.from_names("retried", "order"),
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
        ExecutionShape.from_names("authorize", "order"),
        ExecutionShape.from_names("charge", "at_all"),
        convert_arguments=lambda order: {"at_all": True},
    )

    with pytest.raises(MigrationOrdinalAmbiguityError, match="which of them ran first"):
        migrate(migration, session(first, second))


def test_two_boundaries_may_share_a_name_while_their_calls_stay_apart():
    authorize = started("authorize", arguments={"order": "ord_1"})
    capture = started("capture", arguments={"order": "ord_2"})

    migration = migrator()
    migration.remap(
        ExecutionShape.from_names("authorize", "order"),
        ExecutionShape.from_names("charge", "order"),
    )
    migration.remap(
        ExecutionShape.from_names("capture", "order"),
        ExecutionShape.from_names("charge", "order"),
    )
    migrated = migrate(migration, session(authorize, capture))

    assert {execution.id.name.value for execution in migrated} == {"charge"}
    assert [execution.id.sequence for execution in migrated] == [0, 0]


# -- Refusing a migration that does not describe these records ---------------


def test_records_of_another_generation_are_refused():
    execution = started("authorize", arguments={"order": "ord_1"})

    migration = migrator()
    migration.remap(
        ExecutionShape.from_names("authorize", "order", "currency"),
        ExecutionShape.from_names("charge", "order"),
        convert_arguments=lambda order, currency: {"order": order},
    )

    with pytest.raises(MigrationError, match="another generation"):
        migrate(migration, session(execution))


def test_a_conversion_that_misses_an_argument_is_refused():
    execution = started("authorize", arguments={"order": "ord_1"})

    migration = migrator()
    migration.remap(
        ExecutionShape.from_names("authorize", "order"),
        ExecutionShape.from_names("charge", "order_id", "cents"),
        convert_arguments=lambda order: {"order_id": order},
    )

    with pytest.raises(MigrationError, match="but it is keyed by"):
        migrate(migration, session(execution))


def test_a_rename_that_changes_the_parameters_needs_a_conversion():
    migration = migrator()

    with pytest.raises(MigrationError, match="conversion between them"):
        migration.remap(
            ExecutionShape.from_names("authorize", "order"),
            ExecutionShape.from_names("charge", "order_id"),
        )


def test_a_boundary_registered_twice_is_refused():
    migration = migrator()
    migration.remap(
        ExecutionShape.from_names("authorize", "order"),
        ExecutionShape.from_names("charge", "order"),
    )

    with pytest.raises(MigrationError, match="already registered"):
        migration.drop(ExecutionShape.from_names("authorize", "order"))


def test_a_boundary_naming_one_parameter_twice_is_refused():
    with pytest.raises(ValueError, match="more than once"):
        ExecutionShape.from_names("authorize", "order", "order")


def test_an_execution_shape_constructor_requires_value_objects():
    with pytest.raises(TypeError):
        ExecutionShape("authorize", frozenset({"order"}))  # type: ignore[arg-type]


def test_a_fallback_nested_in_a_container_survives_a_conversion():
    execution = started(
        "authorize",
        arguments={
            "clients": [CanonicalFallback("com.example.PaymentClient")],
            "units": 12,
        },
    )

    migration = migrator()
    migration.remap(
        ExecutionShape.from_names("authorize", "clients", "units"),
        ExecutionShape.from_names("charge", "clients", "cents"),
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

    with pytest.raises(MigrationError, match="No migration path"):
        migrate(migrator(), session(execution, versions={PAY: "v0"}))


def test_a_session_that_never_entered_the_domain_is_refused():
    execution = started("authorize", arguments={"order": "ord_1"}, domain_id=SHIP)

    with pytest.raises(MigrationError, match="records no version"):
        migrate(migrator(), session(execution, versions={SHIP: "v1"}))


def test_a_drop_is_held_to_the_shape_it_declares():
    execution = started("authorize", arguments={"order": "ord_1"})

    migration = migrator()
    migration.drop(ExecutionShape.from_names("authorize", "order", "currency"))

    with pytest.raises(MigrationError, match="another generation"):
        migrate(migration, session(execution))


# -- The report --------------------------------------------------------------


def test_the_replacement_carries_the_versions_it_was_migrated_to():
    execution = started("authorize", arguments={"order": "ord_1"})

    replacement = migrator().migrate(session(execution))

    assert replacement.metadata.domain_versions == DomainVersionMap({PAY: "v2"})


# -- Facade and migration paths ---------------------------------------------


def test_registered_transitions_run_to_the_domains_current_version():
    migration = DomainMigration(
        Domain(PAY, version="v3"), canonicalizer=JsonArgumentCanonicalizer()
    )
    migration.transition("v1", "v2").remap(
        ExecutionShape.from_names("authorize", "order"),
        ExecutionShape.from_names("charge", "order"),
    )
    migration.transition("v2", "v3").remap(
        ExecutionShape.from_names("charge", "order"),
        ExecutionShape.from_names("capture", "order"),
    )

    replacement = migration.execute(
        session(started("authorize", arguments={"order": "ord_1"}))
    )

    assert (
        replacement.executions[0].id.name
        == ExecutionShape.from_names("capture", "order").name
    )
    assert replacement.metadata.domain_versions[PAY].value == "v3"


def test_a_session_already_at_the_current_version_is_unchanged():
    source = session(versions={PAY: "v2"})
    migration = DomainMigration(
        Domain(PAY, version="v2"), canonicalizer=JsonArgumentCanonicalizer()
    )

    assert migration.execute(source) is source


def test_a_missing_transition_to_the_current_version_is_refused():
    migration = DomainMigration(
        Domain(PAY, version="v3"), canonicalizer=JsonArgumentCanonicalizer()
    )
    migration.transition("v1", "v2")

    with pytest.raises(MigrationError, match="No migration path"):
        migration.execute(session())


def test_two_transitions_cannot_leave_the_same_version():
    migration = DomainMigration(
        Domain(PAY, version="v3"), canonicalizer=JsonArgumentCanonicalizer()
    )
    migration.transition("v1", "v2")

    with pytest.raises(MigrationError, match="already has a migration"):
        migration.transition("v1", "v3")


def test_a_cycle_is_refused_when_registered():
    migration = DomainMigration(
        Domain(PAY, version="v3"), canonicalizer=JsonArgumentCanonicalizer()
    )
    migration.transition("v1", "v2")

    with pytest.raises(MigrationError, match="cycle"):
        migration.transition("v2", "v1")
