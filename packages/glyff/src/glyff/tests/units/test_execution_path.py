"""The path an ExecutionId is stored under, and what it refuses to read back."""

import pytest
from glyff import ArgumentsDigest, DomainId, ExecutionId, ExecutionName
from glyff.store.utils import execution_id_to_path, path_to_execution_id

DOMAIN = DomainId("com.example.payments")


def make(
    name: str = "task",
    *,
    domain: DomainId = DOMAIN,
    digest: str = "d" * 64,
    sequence: int = 0,
    parent: ExecutionId | None = None,
) -> ExecutionId:
    return ExecutionId(
        parent_id=parent,
        domain=domain,
        name=ExecutionName(name),
        sequence=sequence,
        arguments_digest=ArgumentsDigest(digest),
    )


def test_a_path_round_trips():
    child = make("child", parent=make("root", sequence=10))

    assert path_to_execution_id(execution_id_to_path(child)) == child


@pytest.mark.parametrize(
    "awkward", ["a#b", "a/b", "a:b", "a%b", "%2E", "Outer.<locals>.task", "決済", " "]
)
def test_every_component_round_trips_whatever_it_holds(awkward: str):
    # The value objects decide what an identity may say; the codec's job is to
    # carry it, so no character can end an identity early or start a new frame.
    execution_id = make(awkward, digest=awkward)

    assert path_to_execution_id(execution_id_to_path(execution_id)) == execution_id


def test_a_readable_identifier_stays_readable():
    # Percent encoding leaves the unreserved set alone, so the file store's
    # document is still something to read.
    assert execution_id_to_path(make(digest="abc")).startswith(
        "com.example.payments:task#0:abc"
    )


def test_a_domain_prefix_keeps_ancestors_before_descendants():
    root = make("root")
    child = make("child", parent=root)

    assert execution_id_to_path(child).startswith(execution_id_to_path(root) + "/")


@pytest.mark.parametrize(
    "path",
    [
        "",
        "not-a-frame",
        "domain:name#0",  # no digest
        "domain:name:0:digest",  # no ordinal
        "domain-name#0:digest",  # no domain separator
        "domain:name#zero:digest",
    ],
)
def test_a_path_that_is_not_a_frame_is_refused(path: str):
    with pytest.raises(ValueError):
        path_to_execution_id(path)


@pytest.mark.parametrize(
    "path",
    [
        "domain:%zzname#0:digest",  # not an escape at all
        "domain:%2Ename#0:digest",  # an escape that need not have been one
        "domain:na%2fme#0:digest",  # lower-case hex where quote() writes upper
    ],
)
def test_a_non_canonical_encoding_is_refused(path: str):
    # unquote() would take all of these, and two spellings of one identity would
    # become two identities — or worse, silently the same one.
    with pytest.raises(ValueError):
        path_to_execution_id(path)
