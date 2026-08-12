from glyff import ArgumentsDigest, DomainId, ExecutionId, ExecutionName

DOMAIN = DomainId("test")


def make(name: str, sequence: int, digest: str, parent=None) -> ExecutionId:
    return ExecutionId(
        parent_id=parent,
        domain_id=DOMAIN,
        name=ExecutionName(name),
        sequence=sequence,
        arguments_digest=ArgumentsDigest(digest),
    )


def test_str_representation_no_parent():
    eid = make("my.func", 0, "hash1")
    assert str(eid) == "ExecutionId(domain_id='test', name='my.func', sequence=0)"


def test_str_representation_with_parent():
    eid = make("child.func", 0, "hash_c", parent=make("root", 1, "hash_p"))
    expected = (
        "ExecutionId(domain_id='test', name='child.func', sequence=0, parent='root#1')"
    )
    assert str(eid) == expected
