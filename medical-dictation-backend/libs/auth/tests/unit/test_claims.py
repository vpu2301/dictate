"""Claims schema: strict, frozen, extra=forbid."""

from __future__ import annotations

import time
from uuid import UUID

import pytest
from pydantic import ValidationError

from auth.claims import Claims


def _good() -> dict[str, object]:
    now = int(time.time())
    return {
        "sub": "11111111-1111-1111-1111-111111111111",
        "tid": "00000000-0000-0000-0000-00000000000a",
        "roles": ["clinician"],
        "scope": "openid",
        "sid": "session-1",
        "iss": "https://issuer.test/realms/medical-dictation",
        "aud": "mdx-api",
        "exp": now + 900,
        "iat": now,
    }


def test_valid_payload_parses() -> None:
    c = Claims(**_good())
    assert isinstance(c.sub, UUID)
    assert isinstance(c.tid, UUID)
    assert c.roles == ["clinician"]
    assert c.mfa is False  # default
    assert c.nbf is None


def test_aud_can_be_list() -> None:
    payload = _good() | {"aud": ["mdx-api", "another-svc"]}
    c = Claims(**payload)
    assert c.aud == ["mdx-api", "another-svc"]


def test_extra_field_rejected() -> None:
    """An attacker-injected extra claim like is_admin must be rejected."""
    payload = _good() | {"is_admin": True}
    with pytest.raises(ValidationError):
        Claims(**payload)


def test_missing_tid_rejected() -> None:
    payload = _good()
    del payload["tid"]
    with pytest.raises(ValidationError):
        Claims(**payload)


def test_missing_sub_rejected() -> None:
    payload = _good()
    del payload["sub"]
    with pytest.raises(ValidationError):
        Claims(**payload)


def test_non_uuid_tid_rejected() -> None:
    payload = _good() | {"tid": "not-a-uuid"}
    with pytest.raises(ValidationError):
        Claims(**payload)


def test_frozen_model_cannot_be_mutated() -> None:
    c = Claims(**_good())
    with pytest.raises(ValidationError):
        c.roles = ["tenant_admin"]  # type: ignore[misc]


def test_roles_must_be_list_of_strings() -> None:
    payload = _good() | {"roles": "clinician"}  # bare string, not list
    with pytest.raises(ValidationError):
        Claims(**payload)
