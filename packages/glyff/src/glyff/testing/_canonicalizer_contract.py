"""pytest conformance contract for :class:`~glyff.ArgumentCanonicalizer`.

Re-exported from :mod:`glyff.testing`. A bound mapping in, an encodable canonical
value out, and the determinism a key needs. Which Python value becomes which
canonical form, and what happens to one with no representation, are each
implementation's own.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from .._interfaces import ArgumentCanonicalizer
from .._execution import Opaque, opaque_marker
from ..exceptions import ArgumentCanonicalizationError
from ..serialization._utils import encode_canonical

CanonicalizerFactory = Callable[[], ArgumentCanonicalizer]


class ArgumentCanonicalizerContract:
    """Conformance suite for an `ArgumentCanonicalizer` implementation.

    Subclass it and supply a ``canonicalizer_factory`` fixture, which builds an
    equivalent canonicalizer each time it is called.
    """

    @pytest.fixture
    def canonicalizer_factory(self) -> CanonicalizerFactory:
        raise NotImplementedError

    @pytest.fixture
    def canonicalizer(
        self, canonicalizer_factory: CanonicalizerFactory
    ) -> ArgumentCanonicalizer:
        return canonicalizer_factory()

    def test_a_canonical_form_encodes_into_a_key(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        form = canonicalizer.canonicalize({"a": 1, "b": "two", "c": None})

        assert encode_canonical(form)

    def test_the_bound_name_is_part_of_the_form(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        # One value, two names: a canonicalizer carrying only the values would
        # key `f(a=1)` and `f(b=1)` the same way.
        assert canonicalizer.canonicalize({"a": 1}) != canonicalizer.canonicalize(
            {"b": 1}
        )

    def test_an_extra_bound_argument_changes_the_form(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        assert canonicalizer.canonicalize({"a": 1}) != canonicalizer.canonicalize(
            {"a": 1, "b": 2}
        )

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

    def test_no_arguments_still_encodes_into_a_key(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        form = canonicalizer.canonicalize({})

        assert encode_canonical(form)

    def test_a_canonical_form_canonicalizes_to_itself(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        # A migration reads recorded canonical arguments and hands back the ones
        # the call should be keyed by. An argument it passed through untouched
        # has to come out of here keying the call the way it already did.
        form = canonicalizer.canonicalize(
            {"a": 1, "b": ["x", None], "c": {"d": True}, "e": "text"}
        )
        assert isinstance(form, dict)

        assert canonicalizer.canonicalize(form) == form

    def test_an_opaque_value_canonicalizes_to_the_marker(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        # The marker is the canonical format's, not any one canonicalizer's: a
        # migration reads recorded arguments by it, so a form written another
        # way would not be read back as opaque at all.
        assert canonicalizer.canonicalize({"a": Opaque("com.example.Service")}) == {
            "a": opaque_marker("com.example.Service")
        }

    def test_a_value_claiming_the_marker_is_refused(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        # Reserved for the same reason: a value canonicalizing to the marker
        # would share an opaque value's key, and read back as one.
        with pytest.raises(ArgumentCanonicalizationError):
            canonicalizer.canonicalize(
                {"a": dict(opaque_marker("com.example.Service"))}  # type: ignore[arg-type]
            )

    def test_a_later_instance_agrees_on_the_form(
        self, canonicalizer_factory: CanonicalizerFactory
    ):
        # The form is the execution's key, and a session that resumes a paused
        # run is handed a canonicalizer it built itself. A form that depended on
        # the instance would miss every record the run before it wrote.
        arguments = {"a": 1, "b": "two"}

        assert canonicalizer_factory().canonicalize(arguments) == (
            canonicalizer_factory().canonicalize(arguments)
        )
