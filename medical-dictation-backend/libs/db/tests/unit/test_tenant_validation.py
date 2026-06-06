"""Unit-level validation of tenant_connection input handling.

Integration tests that exercise the actual RLS round-trip against Postgres
live in tests/integration/test_tenant_isolation.py and require the dev
Compose stack to be up.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from db.tenant import _coerce_tenant_id


def test_accepts_uuid_object() -> None:
    u = UUID("11111111-2222-3333-4444-555555555555")
    assert _coerce_tenant_id(u) == u


def test_accepts_uuid_string() -> None:
    s = "11111111-2222-3333-4444-555555555555"
    assert _coerce_tenant_id(s) == UUID(s)


def test_rejects_non_uuid_string() -> None:
    with pytest.raises(ValueError):
        _coerce_tenant_id("not-a-uuid")


def test_rejects_sqli_payload() -> None:
    with pytest.raises(ValueError):
        _coerce_tenant_id("'; DROP TABLE users; --")


def test_rejects_empty_string() -> None:
    with pytest.raises(ValueError):
        _coerce_tenant_id("")


def test_rejects_none() -> None:
    with pytest.raises(TypeError):
        _coerce_tenant_id(None)  # type: ignore[arg-type]


def test_rejects_int() -> None:
    with pytest.raises(TypeError):
        _coerce_tenant_id(12345)  # type: ignore[arg-type]
