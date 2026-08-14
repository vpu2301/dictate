"""Sprint-16 MFA: TOTP math, envelope packing, enrolment surface, grace flow.

The envelope round-trips use REAL libs/crypto (file master key in tmp,
in-memory KEK repo) — only Keycloak and the DB are doubled.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from auth import Claims
from crypto import Envelope, FileMasterKeyProvider

TENANT = UUID("00000000-0000-0000-0000-00000000000a")
USER = UUID("0a000000-0000-0000-0000-0000000000aa")
OTHER_USER = UUID("0a000000-0000-0000-0000-0000000000bb")

# RFC 4226 appendix D vectors (secret "12345678901234567890", 6 digits).
RFC4226_SECRET_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
RFC4226_CODES = [
    "755224", "287082", "359152", "969429", "338314",
    "254676", "287922", "162583", "399871", "520489",
]


def _claims(
    *,
    roles: list[str],
    sub: UUID = USER,
    mfa: bool = False,
    mfa_enrolled: bool = False,
) -> Claims:
    return Claims(
        sub=sub,
        tid=TENANT,
        roles=roles,
        scope="",
        mfa=mfa,
        mfa_enrolled=mfa_enrolled,
        sid="sid-1",
        iss="https://test/issuer",
        aud="mdx",
        exp=9_999_999_999,
        iat=1_700_000_000,
    )


# ── TOTP math ───────────────────────────────────────────────────────────


def test_hotp_matches_rfc4226_vectors() -> None:
    from auth_service import totp

    for counter, expected in enumerate(RFC4226_CODES):
        at = counter * totp.TOTP_PERIOD_SECONDS + 1
        assert totp.totp_at(RFC4226_SECRET_B32, at_unix=at) == expected


def test_verify_code_accepts_drift_window_only() -> None:
    from auth_service import totp

    secret = totp.generate_secret()
    now = 1_700_000_000.0
    good = totp.totp_at(secret, at_unix=now)
    prev = totp.totp_at(secret, at_unix=now - totp.TOTP_PERIOD_SECONDS)
    stale = totp.totp_at(secret, at_unix=now - 3 * totp.TOTP_PERIOD_SECONDS)
    assert totp.verify_code(secret, good, at_unix=now)
    assert totp.verify_code(secret, prev, at_unix=now)
    if stale not in (good, prev):  # astronomically likely
        assert not totp.verify_code(secret, stale, at_unix=now)


def test_verify_code_rejects_malformed() -> None:
    from auth_service import totp

    secret = totp.generate_secret()
    assert not totp.verify_code(secret, "")
    assert not totp.verify_code(secret, "12345")
    assert not totp.verify_code(secret, "abcdef")


# ── Envelope packing (real crypto, no DB) ───────────────────────────────


class FakeKekRepo:
    """In-memory TenantKekRepository double: fixed KEK per tenant."""

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


async def test_secret_envelope_round_trip(envelope: Envelope) -> None:
    from auth_service import totp

    secret = totp.generate_secret()
    packed = await totp.encrypt_secret(
        envelope, secret=secret, tenant_id=TENANT, sub=USER
    )
    assert secret not in packed  # never plaintext at rest
    out = await totp.decrypt_secret(envelope, packed=packed, tenant_id=TENANT, sub=USER)
    assert out == secret


async def test_secret_envelope_bound_to_sub(envelope: Envelope) -> None:
    """The AAD binds the blob to the user — a copied attribute fails."""
    from auth_service import totp
    from crypto import CryptoError, DecryptError

    packed = await totp.encrypt_secret(
        envelope, secret=totp.generate_secret(), tenant_id=TENANT, sub=USER
    )
    with pytest.raises((CryptoError, DecryptError)):
        await totp.decrypt_secret(
            envelope, packed=packed, tenant_id=TENANT, sub=OTHER_USER
        )


# ── Router surface ──────────────────────────────────────────────────────


class FakeKeycloak:
    def __init__(self) -> None:
        self.users: dict[UUID, dict[str, Any]] = {}
        self.logged_out: list[UUID] = []

    def _rep(self, sub: UUID) -> dict[str, Any]:
        return self.users.setdefault(
            sub, {"id": str(sub), "attributes": {"tenant_id": [str(TENANT)]}}
        )

    async def get_user(self, sub: UUID) -> dict[str, Any]:
        return self._rep(sub)

    async def set_user_attributes(self, sub: UUID, updates: dict[str, list[str]]) -> None:
        attrs = self._rep(sub)["attributes"]
        for k, v in updates.items():
            if v:
                attrs[k] = v
            else:
                attrs.pop(k, None)

    async def logout_user(self, sub: UUID) -> None:
        self.logged_out.append(sub)


@pytest.fixture
def make_client(monkeypatch: pytest.MonkeyPatch, envelope: Envelope):
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from auth_service import deps
    from auth_service.config import settings
    from auth_service.main import create_app
    from auth_service.routers import mfa as mfa_router

    monkeypatch.setattr(settings, "mfa_enrolment_enabled", True)

    audit_calls: list[dict[str, Any]] = []

    async def _write_event(**kwargs: Any) -> None:
        audit_calls.append(kwargs)

    kc = FakeKeycloak()

    async def _get_envelope() -> Envelope:
        return envelope

    state = SimpleNamespace(
        keycloak=kc,
        audit_writer=SimpleNamespace(write_event=_write_event),
        app_pool=object(),
        tenant_writer_pool=object(),
        get_envelope=_get_envelope,
    )
    deps.install_state(state)  # type: ignore[arg-type]

    # Statements the router ran against the doubled DB, so a test can
    # assert the S21 reminder-resolving UPDATE actually fires rather than
    # being swallowed by the best-effort try/except around it.
    executed: list[str] = []

    @contextlib.asynccontextmanager
    async def _fake_conn(pool: Any, tenant_id: Any):
        async def _execute(query: str, *a: Any, **k: Any) -> None:
            executed.append(query)

        async def _fetchrow(query: str, *a: Any) -> Any:
            # users-table lookups resolve any known FakeKeycloak user.
            sub = a[0] if a else None
            return {"sub": sub} if sub in kc.users else None

        @contextlib.asynccontextmanager
        async def _transaction():
            yield None

        yield SimpleNamespace(
            execute=_execute, fetchrow=_fetchrow, transaction=_transaction
        )

    monkeypatch.setattr(mfa_router, "tenant_connection", _fake_conn)

    def _build(claims: Claims) -> TestClient:
        app = create_app()
        app.dependency_overrides[deps.current_user] = lambda: claims
        c = TestClient(app)
        c.audit_calls = audit_calls  # type: ignore[attr-defined]
        c.kc = kc  # type: ignore[attr-defined]
        c.executed = executed  # type: ignore[attr-defined]
        return c

    return _build


def test_enrol_disabled_by_default(
    make_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from auth_service.config import settings

    monkeypatch.setattr(settings, "mfa_enrolment_enabled", False)
    client = make_client(_claims(roles=["clinician"]))
    assert client.post("/auth/mfa/enrol").status_code == 403


def test_enrol_verify_flow(make_client: Any) -> None:
    from auth_service import totp

    client = make_client(_claims(roles=["clinician"]))
    r = client.post("/auth/mfa/enrol")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provisioning_uri"].startswith("otpauth://totp/")
    attrs = client.kc.users[USER]["attributes"]
    assert "totp_secret_enc_pending" in attrs
    assert body["secret"] not in attrs["totp_secret_enc_pending"][0]

    # Wrong code → 400, still pending.
    bad = client.post("/auth/mfa/verify", json={"code": "000000"})
    assert bad.status_code == 400
    assert "totp_secret_enc" not in {
        k for k in attrs if k != "totp_secret_enc_pending"
    }

    # Right code → enrolled, attribute promoted, audited.
    good = client.post(
        "/auth/mfa/verify", json={"code": totp.totp_at(body["secret"])}
    )
    assert good.status_code == 200, good.text
    assert attrs["mfa_enrolled"] == ["true"]
    assert "totp_secret_enc" in attrs
    assert "totp_secret_enc_pending" not in attrs
    assert any(c["kind"] == "auth.mfa.enrolled" for c in client.audit_calls)


def test_enrolment_closes_a_standing_reminder(make_client: Any) -> None:
    """S21: enrolling is the ONLY way an access-review reminder closes.

    There is no dismiss button anywhere in the product, so if this UPDATE
    stops firing the banner never goes away for anyone who was reminded —
    a failure that looks like a UI bug and is a backend one.
    """
    from auth_service import totp

    client = make_client(_claims(roles=["clinician"]))
    body = client.post("/auth/mfa/enrol").json()
    client.post("/auth/mfa/verify", json={"code": totp.totp_at(body["secret"])})

    resolves = [q for q in client.executed if "mfa_reminders" in q]
    assert resolves, "verify did not resolve the standing reminder"
    assert "resolved_at = now()" in resolves[0]
    assert "resolved_at IS NULL" in resolves[0]


def test_enrol_conflict_when_already_enrolled(make_client: Any) -> None:
    from auth_service import totp

    client = make_client(_claims(roles=["clinician"]))
    body = client.post("/auth/mfa/enrol").json()
    client.post("/auth/mfa/verify", json={"code": totp.totp_at(body["secret"])})
    assert client.post("/auth/mfa/enrol").status_code == 409


def test_admin_reset_clears_and_audits_sec(make_client: Any) -> None:
    from audit import Severity
    from auth_service import totp

    client = make_client(_claims(roles=["clinician"]))
    body = client.post("/auth/mfa/enrol").json()
    client.post("/auth/mfa/verify", json={"code": totp.totp_at(body["secret"])})

    admin = make_client(_claims(roles=["tenant_admin"], sub=OTHER_USER))
    r = admin.delete(f"/auth/mfa/{USER}")
    assert r.status_code == 204, r.text
    attrs = admin.kc.users[USER]["attributes"]
    assert "totp_secret_enc" not in attrs
    assert "mfa_enrolled" not in attrs
    assert USER in admin.kc.logged_out
    reset_events = [c for c in admin.audit_calls if c["kind"] == "user.reset_mfa"]
    assert reset_events and reset_events[0]["severity"] == Severity.SEC


def test_reset_denied_to_clinician(make_client: Any) -> None:
    client = make_client(_claims(roles=["clinician"]))
    assert client.delete(f"/auth/mfa/{uuid4()}").status_code == 403


# ── requires_mfa grace flow ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mfa", "mfa_enrolled", "expected"),
    [
        (True, True, 200),   # MFA satisfied
        (False, False, 403),  # unenrolled → grace: route to enrolment
        (False, True, 401),   # enrolled, pre-enrolment token → re-login
    ],
)
def test_requires_mfa_grace_flow(
    make_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    mfa: bool,
    mfa_enrolled: bool,
    expected: int,
) -> None:
    from auth_service.config import settings

    monkeypatch.setattr(settings, "require_mfa", True)
    client = make_client(
        _claims(roles=["tenant_admin"], mfa=mfa, mfa_enrolled=mfa_enrolled)
    )
    # Reset is both perm-gated and MFA-gated — a natural gated probe.
    r = client.delete(f"/auth/mfa/{USER}")
    if expected == 200:
        # Past the MFA gate; the request itself proceeds (target exists).
        assert r.status_code in (204, 404)
    else:
        assert r.status_code == expected
        if expected == 403:
            assert "enrolment" in r.json()["detail"].lower()


def test_requires_mfa_noop_when_flag_off(make_client: Any) -> None:
    client = make_client(_claims(roles=["tenant_admin"]))
    # Flag off (default): the gate is a no-op; request reaches the handler.
    assert client.delete(f"/auth/mfa/{USER}").status_code in (204, 404)


# ── Login OTP enforcement ───────────────────────────────────────────────


@pytest.fixture
def login_env(monkeypatch: pytest.MonkeyPatch, envelope: Envelope):
    """Wire a fake Keycloak + real envelope behind the login route."""
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from auth_service import deps
    from auth_service.keycloak_client import TokenResponse
    from auth_service.main import create_app
    from auth_service.routers import login as login_router

    kc = FakeKeycloak()
    enrolled_claims = {"value": _claims(roles=["clinician"], mfa_enrolled=True)}

    async def _password_grant(*, username: str, password: str) -> TokenResponse:
        if password != "pw":
            from auth_service.keycloak_client import KeycloakError

            raise KeycloakError(status=401, body={"error": "invalid_grant"})
        return TokenResponse(
            access_token="tok",
            refresh_token="rt",
            expires_in=900,
            refresh_expires_in=1800,
            token_type="Bearer",
        )

    kc.password_grant = _password_grant  # type: ignore[attr-defined]

    async def _get_envelope() -> Envelope:
        return envelope

    async def _write_event(**kwargs: Any) -> None:
        return None

    state = SimpleNamespace(
        keycloak=kc,
        jwks_cache=object(),
        audit_writer=SimpleNamespace(write_event=_write_event),
        app_pool=object(),
        tenant_writer_pool=object(),
        get_envelope=_get_envelope,
    )
    deps.install_state(state)  # type: ignore[arg-type]

    async def _fake_verify(token: str, **kwargs: Any) -> Claims:
        return enrolled_claims["value"]

    monkeypatch.setattr(login_router, "verify_token", _fake_verify)

    client = TestClient(create_app())
    return SimpleNamespace(client=client, kc=kc, claims=enrolled_claims)


async def _store_secret(env: Any, envelope: Envelope) -> str:
    from auth_service import totp

    secret = totp.generate_secret()
    packed = await totp.encrypt_secret(
        envelope, secret=secret, tenant_id=TENANT, sub=USER
    )
    await env.kc.set_user_attributes(USER, {"totp_secret_enc": [packed]})
    return secret


async def test_login_enrolled_without_otp_is_refused(
    login_env: Any, envelope: Envelope
) -> None:
    await _store_secret(login_env, envelope)
    r = login_env.client.post(
        "/auth/login", json={"email": "a@b.c", "password": "pw"}
    )
    assert r.status_code == 401
    assert "TOTP" in r.json()["detail"]


async def test_login_enrolled_with_wrong_otp_is_refused(
    login_env: Any, envelope: Envelope
) -> None:
    await _store_secret(login_env, envelope)
    r = login_env.client.post(
        "/auth/login", json={"email": "a@b.c", "password": "pw", "otp": "000000"}
    )
    assert r.status_code == 401


async def test_login_enrolled_with_valid_otp_succeeds(
    login_env: Any, envelope: Envelope
) -> None:
    from auth_service import totp

    secret = await _store_secret(login_env, envelope)
    r = login_env.client.post(
        "/auth/login",
        json={"email": "a@b.c", "password": "pw", "otp": totp.totp_at(secret)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["access_token"] == "tok"


async def test_login_unenrolled_needs_no_otp(login_env: Any) -> None:
    login_env.claims["value"] = _claims(roles=["clinician"], mfa_enrolled=False)
    r = login_env.client.post(
        "/auth/login", json={"email": "a@b.c", "password": "pw"}
    )
    assert r.status_code == 200, r.text
