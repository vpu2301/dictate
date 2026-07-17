"""S11 deployment (ADR-0028) — DSAR download-token helper properties."""

from __future__ import annotations

import time
from uuid import UUID

from core_service.domain import download_tokens

KEY = "ab" * 32
TENANT = UUID("11111111-1111-1111-1111-111111111111")
REQUEST = UUID("99999999-9999-9999-9999-999999999999")


def test_roundtrip_valid() -> None:
    token, exp = download_tokens.mint(KEY, tenant_id=TENANT, request_id=REQUEST, ttl_seconds=900)
    assert exp - time.time() <= 900
    assert download_tokens.verify(KEY, tenant_id=TENANT, request_id=REQUEST, token=token)


def test_expired_token_rejected() -> None:
    token, _ = download_tokens.mint(KEY, tenant_id=TENANT, request_id=REQUEST, ttl_seconds=-1)
    assert not download_tokens.verify(KEY, tenant_id=TENANT, request_id=REQUEST, token=token)


def test_wrong_binding_rejected() -> None:
    token, _ = download_tokens.mint(KEY, tenant_id=TENANT, request_id=REQUEST, ttl_seconds=900)
    other = UUID("22222222-2222-2222-2222-222222222222")
    assert not download_tokens.verify(KEY, tenant_id=other, request_id=REQUEST, token=token)
    assert not download_tokens.verify(KEY, tenant_id=TENANT, request_id=other, token=token)
    assert not download_tokens.verify("cd" * 32, tenant_id=TENANT, request_id=REQUEST, token=token)


def test_expiry_not_forgeable() -> None:
    """Extending exp without the key invalidates the MAC."""
    token, exp = download_tokens.mint(KEY, tenant_id=TENANT, request_id=REQUEST, ttl_seconds=-10)
    _, _, mac = token.partition(".")
    forged = f"{exp + 86400}.{mac}"
    assert not download_tokens.verify(KEY, tenant_id=TENANT, request_id=REQUEST, token=forged)


def test_malformed_tokens_rejected() -> None:
    for bad in ("", ".", "123", "abc.def", "1e9.deadbeef", "123.", f"{2**40}"):
        assert not download_tokens.verify(KEY, tenant_id=TENANT, request_id=REQUEST, token=bad)
