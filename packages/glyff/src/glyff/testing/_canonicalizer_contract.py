"""pytest conformance contract for :class:`~glyff.ArgumentCanonicalizer`.

Re-exported from :mod:`glyff.testing`, the public entry point.

Only what the interface itself promises — a bound mapping in, an encodable
canonical value out, and the determinism that being a key requires. Two things
are deliberately absent because the ABC does not ask for them:

- the *taxonomy*, which Python value becomes which canonical form. Implementations
  disagree there on purpose: the shipped JSON canonicalizer rejects a ``datetime``
  as opaque where the Pydantic one represents it by value.
- the ``OpaquePolicy`` mechanism. That is how the shipped family handles a value
  it has no representation for; another canonicalizer may handle one some other
  way, or never meet one.

Both are proved beside the implementations that promise them.
"""

from __future__ import annotations

import pytest

from .._interfaces import ArgumentCanonicalizer
from ..serialization._utils import encode_canonical


class ArgumentCanonicalizerContract:
    """Conformance suite for an `ArgumentCanonicalizer` implementation.

    Subclass it and supply a ``canonicalizer`` fixture.
    """

    @pytest.fixture
    def canonicalizer(self) -> ArgumentCanonicalizer:
        raise NotImplementedError

    def test_a_canonical_form_is_in_the_json_data_model(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        # The encoder is what turns the form into an execution's key, so a form
        # it cannot take is not a canonical form.
        encode_canonical(canonicalizer.canonicalize({"a": 1, "b": "two", "c": None}))

    def test_every_bound_name_reaches_the_canonical_form(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        # A name that vanished would let two different calls share one key.
        assert canonicalizer.canonicalize(
            {"a": 1, "b": 2}
        ) != canonicalizer.canonicalize({"a": 1})

    def test_the_same_arguments_canonicalize_the_same_way(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        assert canonicalizer.canonicalize(
            {"a": 42, "b": "hello"}
        ) == canonicalizer.canonicalize({"a": 42, "b": "hello"})

    def test_different_values_under_one_name_differ(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        assert canonicalizer.canonicalize({"a": 1}) != canonicalizer.canonicalize(
            {"a": 2}
        )

    def test_no_arguments_canonicalizes(self, canonicalizer: ArgumentCanonicalizer):
        encode_canonical(canonicalizer.canonicalize({}))
