"""POST /auth/reauth — the step-up that gates break-glass.

The hotfix folds TOTP into this flow: enrolled principals must present a
current code alongside the password, unenrolled ones still get through on
the password alone, and the ticket records which of the two happened.

Real crypto and real TOTP maths; only Keycloak and the DB are doubled.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from auth import Claims
from crypto import Envelope, FileMasterKeyProvider

TENANT = UUID("00000000-0000-0000-0000-00000000000a")
USER = UUID("0a000000-0000-0000-0000-0000000000aa")

PASSWORD = "correct-horse-battery-staple"


def _claims(*, roles: list[str] | None = None, mfa: bool = False) -> Claims:
    return Claims(
        sub=USER,
        tid=TENANT,
        roles=roles or ["tenant_admin"],
        scope="",
        mfa=mfa,
        sid="sid-1",
        iss="https://test/issuer",
        aud="mdx",
        exp=9_999_999_999,
        iat=1_700_000_000,
        preferred_username="admin@tenant-a.example",
    )


class FakeKekRepo:
    def __init__(self) -> None:
        self._keks: dict[UUID, bytes] = {}

    async def get_or_create(self, tenant_id: UUID) -> bytes:
        return self._keks.setdefault(tenant_id, os.urandom(32))

    def master_key_id_for(self, tenant_id: UUID) -> str:
        return "file-v1"


@pytest.fixture
async def envelope(tmp_path: Path) -> Envelope:
    key = tmp_path / "master.key"
    key.write_bytes(os.urandom(32))
    os.chmod(key, 0o400)
    master = FileMasterKeyProvider(path=key)
    await master.startup_self_check()
    return Envelope(master_key_provider=master, kek_repository=FakeKekRepo())


class FakeKeycloak:
    """Password grant + attribute store. `password_ok` steers the grant."""

    def __init__(self) -> None:
        self.password_ok = True
        self.attributes: dict[str, list[str]] = {"tenant_id": [str(TENANT)]}
        self.grants: list[str] = []
        self.raise_on_get = False

    async def password_grant(self, *, username: str, password: str) -> dict[str, str]:
        from auth_service.keycloak_client import KeycloakError

        self.grants.append(username)
        if not self.password_ok or password != PASSWORD:
            raise KeycloakError(status=401, body="invalid_grant")
        return {"access_token": "t"}

    async def get_user(self, sub: UUID) -> dict[str, Any]:
        from auth_service.keycloak_client import KeycloakError

        if self.raise_on_get:
            raise KeycloakError(status=500, body="boom")
        return {"id": str(sub), "attributes": self.attributes}


@pytest.fixture
async def env(monkeypatch: pytest.MonkeyPatch, envelope: Envelope):
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from auth_service import deps
    from auth_service.main import create_app
    from auth_service.routers import reauth as reauth_router

    audit_calls: list[dict[str, Any]] = []
    inserts: list[tuple] = []

    async def _write_event(**kwargs: Any) -> None:
        audit_calls.append(kwargs)

    kc = FakeKeycloak()

    async def _get_envelope() -> Envelope:
        return envelope

    class _FakeConn:
        async def execute(self, sql: str, *args: Any) -> None:
            if "INSERT INTO auth_reauth_tickets" in sql:
                inserts.append(args)

        async def fetchval(self, sql: str, *args: Any) -> Any:
            return None

    @contextlib.asynccontextmanager
    async def _fake_tenant_conn(pool, tenant_id):  # noqa: ANN001
        yield _FakeConn()

    monkeypatch.setattr(reauth_router, "tenant_connection", _fake_tenant_conn)

    state = SimpleNamespace(
        keycloak=kc,
        audit_writer=SimpleNamespace(write_event=_write_event),
        app_pool=object(),
        tenant_writer_pool=object(),
        get_envelope=_get_envelope,
    )
    deps.install_state(state)  # type: ignore[arg-type]

    app = create_app()
    app.dependency_overrides[deps.current_user] = lambda: _claims()

    async def enrol(secret: str) -> None:
        """Give the fake user a real, envelope-encrypted TOTP secret."""
        from auth_service import totp

        packed = await totp.encrypt_secret(
            envelope, secret=secret, tenant_id=TENANT, sub=USER
        )
        kc.attributes["totp_secret_enc"] = [packed]

    return SimpleNamespace(
        client=TestClient(app),
        kc=kc,
        audit_calls=audit_calls,
        inserts=inserts,
        enrol=enrol,
    )


def _post(env, **body: Any):
    return env.client.post("/auth/reauth", json={"password": PASSWORD, **body})


# ── Unenrolled: password alone still works ──────────────────────────────


async def test_unenrolled_user_mints_a_password_only_ticket(env) -> None:
    """MFA is off in the pilot and most accounts have no enrolment. If the
    step-up demanded TOTP unconditionally, break-glass would be unusable
    for exactly the people it exists for."""
    resp = _post(env)
    assert resp.status_code == 200
    body = resp.json()
    assert body["factors"] == ["password"]
    assert body["reauth_ticket"]
    # The ticket row records it, so the grant it backs is auditable.
    assert env.inserts[-1][-1] == ["password"]


async def test_wrong_password_mints_nothing(env) -> None:
    env.kc.password_ok = False
    resp = env.client.post("/auth/reauth", json={"password": "nope"})
    assert resp.status_code == 401
    assert env.inserts == []
    kinds = [c["kind"] for c in env.audit_calls]
    assert "auth.reauth_failed" in kinds


# ── Enrolled: the second factor is required ─────────────────────────────


async def test_enrolled_user_must_supply_a_totp_code(env) -> None:
    """The defect-closure branch: a correct password is no longer enough
    for a principal who has a second factor."""
    from auth_service import totp

    await env.enrol(totp.generate_secret())
    resp = _post(env)
    assert resp.status_code == 401
    assert resp.json()["code"] == "totp_required"
    assert env.inserts == []


async def test_enrolled_user_with_a_valid_code_gets_a_two_factor_ticket(env) -> None:
    from auth_service import totp

    secret = totp.generate_secret()
    await env.enrol(secret)
    resp = _post(env, totp_code=totp.totp_at(secret))
    assert resp.status_code == 200
    assert resp.json()["factors"] == ["password", "totp"]
    assert env.inserts[-1][-1] == ["password", "totp"]
    succeeded = [
        c for c in env.audit_calls if c["kind"] == "auth.reauth_succeeded"
    ]
    assert succeeded[-1]["payload"]["factors"] == ["password", "totp"]


async def test_enrolled_user_with_a_wrong_code_is_refused_and_audited(env) -> None:
    from auth_service import totp

    secret = totp.generate_secret()
    await env.enrol(secret)
    # A code from a different secret — valid-looking, wrong.
    wrong = totp.totp_at(totp.generate_secret())
    resp = _post(env, totp_code=wrong)
    assert resp.status_code == 401
    assert env.inserts == []
    failed = [c for c in env.audit_calls if c["kind"] == "auth.reauth_failed"]
    assert failed and failed[-1]["payload"]["factor"] == "totp"


async def test_stale_totp_code_is_refused(env) -> None:
    """Freshness is the point of a step-up: a code from ten minutes ago
    proves the phone was in reach then, not now."""
    from auth_service import totp

    secret = totp.generate_secret()
    await env.enrol(secret)
    import time

    stale = totp.totp_at(secret, at_unix=time.time() - 600)
    resp = _post(env, totp_code=stale)
    assert resp.status_code == 401


# ── Fail-safe, not fail-open ────────────────────────────────────────────


async def test_enrolment_lookup_failure_refuses_rather_than_downgrading(env) -> None:
    """If Keycloak is unreachable we cannot tell whether this principal
    has a second factor. Answering "assume not" would turn an outage into
    a silent downgrade of the strongest control in the system."""
    env.kc.raise_on_get = True
    resp = _post(env)
    assert resp.status_code == 503
    assert env.inserts == []


async def test_token_mfa_claim_does_not_decide_the_requirement(env) -> None:
    """A session opened BEFORE the user enrolled carries mfa=false. The
    requirement is read from the enrolment attribute, not the token, or
    an old token would be a way to skip the second factor."""
    from auth_service import totp

    secret = totp.generate_secret()
    await env.enrol(secret)

    from auth_service import deps

    env.client.app.dependency_overrides[deps.current_user] = lambda: _claims(mfa=False)
    resp = _post(env)
    assert resp.status_code == 401
    assert resp.json()["code"] == "totp_required"


# ── Ticket properties the consumer relies on ────────────────────────────


async def test_ticket_is_never_stored_in_plaintext(env) -> None:
    resp = _post(env)
    ticket = resp.json()["reauth_ticket"]
    stored = env.inserts[-1]
    assert ticket not in [a for a in stored if isinstance(a, str)]
    # ticket_hash is the sha256 digest, positionally third.
    import hashlib

    assert stored[2] == hashlib.sha256(ticket.encode()).digest()


async def test_purpose_is_bound_to_the_ticket(env) -> None:
    """A step-up minted to open a chart must not be redeemable against
    some future high-risk action."""
    resp = _post(env)
    assert resp.json()["purpose"] == "phi_access_request"
    assert env.inserts[-1][3] == "phi_access_request"
