"""Token + HMAC helper tests — defence at the route layer."""

from __future__ import annotations

from signing_service.security import (
    ip_hmac,
    ipn_hmac,
    is_well_formed_token,
    new_verification_token,
)


def test_new_token_22_chars_url_safe():
    t = new_verification_token()
    assert len(t) == 22
    assert all(ch.isalnum() or ch in "-_" for ch in t)
    assert is_well_formed_token(t)


def test_well_formed_token_rejects_traversal():
    assert not is_well_formed_token("../../etc/passwd")
    assert not is_well_formed_token("' OR 1=1 --")
    assert not is_well_formed_token("%n%n%n%n")
    assert not is_well_formed_token("")
    assert not is_well_formed_token("a" * 23)
    assert not is_well_formed_token("a" * 21)


def test_well_formed_token_accepts_valid_shapes():
    assert is_well_formed_token("Abcdef0123_-XYZqwertyU")
    assert is_well_formed_token("a" * 22)


def test_ipn_hmac_deterministic():
    a = ipn_hmac("1234567890", "00" * 32)
    b = ipn_hmac("1234567890", "00" * 32)
    assert a == b
    assert len(a) == 32


def test_ipn_hmac_changes_with_key():
    a = ipn_hmac("1234567890", "00" * 32)
    b = ipn_hmac("1234567890", "11" * 32)
    assert a != b


def test_ip_hmac_changes_with_ip():
    a = ip_hmac("1.1.1.1", "00" * 32)
    b = ip_hmac("2.2.2.2", "00" * 32)
    assert a != b
