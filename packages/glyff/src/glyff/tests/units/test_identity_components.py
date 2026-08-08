"""What the pieces of an execution identity accept, and what they refuse."""

import pytest
from glyff import ArgumentsDigest, DomainId, ExecutionId, ExecutionName


@pytest.mark.parametrize(
    "value",
    [
        "com.example.payments",
        "com.example.payment-library",
        "my_app",
        "payments2",
        "a",
    ],
)
def test_a_reverse_dns_identifier_is_accepted(value: str):
    assert DomainId(value).value == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "Com.Example",  # upper case
        "com..example",  # empty segment
        ".com.example",
        "com.example.",
        "com example",
        "com/example",
        "com:example",
        "com#example",
        "-leading-hyphen",
        "決済",
    ],
)
def test_anything_else_is_refused(value: str):
    with pytest.raises(ValueError):
        DomainId(value)


@pytest.mark.parametrize(
    "value", ["task", "Outer.<locals>.task", "Service.run", "a b", "%2E", "何か"]
)
def test_an_inferred_name_is_taken_as_it_comes(value: str):
    # __qualname__ produces shapes no grammar would allow, and a migration has
    # to hold whatever an older version wrote.
    assert ExecutionName(value).value == value


def test_an_empty_name_is_refused():
    with pytest.raises(ValueError):
        ExecutionName("")


@pytest.mark.parametrize("value", ["chat.reply", "reply-2", "Reply_2", "a"])
def test_an_explicit_name_follows_the_declared_grammar(value: str):
    assert ExecutionName.explicit(value).value == value


@pytest.mark.parametrize(
    "value", ["", "Outer.<locals>.task", ".leading", "a b", "a/b", "a#b", "何か"]
)
def test_an_explicit_name_outside_the_grammar_is_refused(value: str):
    with pytest.raises(ValueError):
        ExecutionName.explicit(value)


def test_a_digest_is_opaque():
    # Nothing here reads the digest, so nothing here constrains its characters.
    assert ArgumentsDigest("not-a-hex-digest").value == "not-a-hex-digest"


def test_an_empty_digest_is_refused():
    with pytest.raises(ValueError):
        ArgumentsDigest("")


@pytest.mark.parametrize("sequence", [-1, -0.0, 1.0, True, False, "0", None])
def test_a_sequence_no_path_could_carry_is_refused(sequence):
    # The codec is closed over what this accepts: anything constructible here has
    # a path that reads back as the same identity.
    with pytest.raises(ValueError):
        ExecutionId(
            parent_id=None,
            domain=DomainId("com.example.payments"),
            name=ExecutionName("task"),
            sequence=sequence,  # type: ignore[arg-type]
            arguments_digest=ArgumentsDigest("d"),
        )


def test_the_first_sequence_is_accepted():
    assert (
        ExecutionId(
            parent_id=None,
            domain=DomainId("com.example.payments"),
            name=ExecutionName("task"),
            sequence=0,
            arguments_digest=ArgumentsDigest("d"),
        ).sequence
        == 0
    )
