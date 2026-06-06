"""RFC 8785 JCS canonicalization — key-ordering, number form, error path."""

from __future__ import annotations

import math

import pytest

from audit.canonical import canonicalize
from audit.exceptions import CanonicalizationError


def test_key_insertion_order_does_not_affect_output() -> None:
    """Two dicts that differ only in key insertion order canonicalise identically.

    This is *the* invariant the audit chain relies on: writer and verifier
    must produce identical bytes for the same logical event.
    """
    a = {"z": 1, "a": 2, "m": 3}
    b = {"a": 2, "m": 3, "z": 1}
    assert canonicalize(a) == canonicalize(b)


def test_keys_are_lexicographically_sorted() -> None:
    out = canonicalize({"b": 1, "a": 2}).decode("utf-8")
    assert out.index('"a"') < out.index('"b"')


def test_nested_objects_canonicalize_recursively() -> None:
    a = {"outer": {"z": 1, "a": 2}}
    b = {"outer": {"a": 2, "z": 1}}
    assert canonicalize(a) == canonicalize(b)


def test_arrays_preserve_order() -> None:
    """Arrays are NOT sorted — JCS preserves the caller-supplied order."""
    assert canonicalize([3, 1, 2]) != canonicalize([1, 2, 3])
    assert canonicalize([1, 2, 3]) == b"[1,2,3]"


def test_unicode_strings_canonicalize_stably() -> None:
    out = canonicalize({"hello": "Привіт"})
    # The rfc8785 library may emit the string either with raw UTF-8 bytes
    # or as \u-escaped — we just need stability across calls.
    assert canonicalize({"hello": "Привіт"}) == out


def test_nan_raises_canonicalization_error() -> None:
    with pytest.raises(CanonicalizationError):
        canonicalize({"x": float("nan")})


def test_infinity_raises_canonicalization_error() -> None:
    with pytest.raises(CanonicalizationError):
        canonicalize({"x": math.inf})


def test_non_serialisable_type_raises() -> None:
    class Foo:
        pass

    with pytest.raises(CanonicalizationError):
        canonicalize({"x": Foo()})


def test_empty_dict_and_array() -> None:
    assert canonicalize({}) == b"{}"
    assert canonicalize([]) == b"[]"


def test_null_and_bools() -> None:
    out = canonicalize({"a": None, "b": True, "c": False}).decode("utf-8")
    assert '"a":null' in out
    assert '"b":true' in out
    assert '"c":false' in out


def test_typical_event_record_shape() -> None:
    """Sanity: the shape our writer constructs canonicalises without error."""
    rec = {
        "tenant_id": "00000000-0000-0000-0000-00000000000a",
        "seq": 1,
        "created_at": "2026-05-11T12:34:56.789+00:00",
        "actor_sub": "11111111-1111-1111-1111-111111111111",
        "actor_role": "clinician",
        "kind": "auth.login",
        "target_kind": None,
        "target_id": None,
        "payload": {"ip": "10.0.0.1", "user_agent": "Mozilla/5.0"},
        "severity": "info",
    }
    out = canonicalize(rec)
    assert b'"seq":1' in out
    assert b'"kind":"auth.login"' in out
