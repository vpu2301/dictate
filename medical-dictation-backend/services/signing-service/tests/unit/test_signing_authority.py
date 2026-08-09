"""HOTFIX — signing is clinician-only, proven at the provider boundary.

The defect: every route in this service gated on `report.write`, which
`nurse` holds. A nurse could apply a qualified electronic signature to a
clinical report — a clinician's personal legal act under Law 2155-VIII.

These are the service-layer half of the closure (the matrix half lives in
``libs/auth/tests/unit/test_perms.py``). Each non-clinician role is
driven at every signing entry point and must get 403, and the KEP
provider must never be reached — the spy below is the point of the whole
file. A 403 that arrives *after* a provider has been handed a document
hash is not the control anyone thinks it is.
"""

from __future__ import annotations

import contextlib
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from medical_kep import (
    ParsedEnvelopeDTO,
    ProviderName,
    SignatureLevel,
    SignedEnvelope,
    SigningSessionInit,
)

from audit.canonical import canonicalize
from auth import Claims

CANONICAL_JSON = {"canonical_version": "1.0", "report": {"id": "r-1"}}
CANONICAL_BYTES = canonicalize(CANONICAL_JSON)
CANONICAL_HASH_HEX = hashlib.sha256(CANONICAL_BYTES).hexdigest()
PDF_HASH_HEX = hashlib.sha256(b"pdf").hexdigest()

TENANT = UUID("22222222-2222-2222-2222-222222222222")

# Every role that is NOT a clinician. `knowledge_admin` is included even
# though it has no signing UI: the point is that the matrix denies it, so
# a future route wiring cannot quietly admit it.
NON_SIGNING_ROLES = ["nurse", "tenant_admin", "auditor", "service", "knowledge_admin"]


def _claims(role: str) -> Claims:
    return Claims(
        sub=uuid4(),
        tid=TENANT,
        roles=[role],
        sid="s",
        iss="https://test/issuer",
        aud="mdx",
        exp=9_999_999_999,
        iat=1_700_000_000,
        preferred_username=f"{role}@tenant-a.example",
        name="Тест Тестовий",
    )


class SpyProvider:
    """Records every call. Any entry here means the guard let something
    through — the assertion is always that it stayed empty."""

    def __init__(self) -> None:
        self.initiated: list[dict] = []
        self.signed_inline: list[dict] = []

    async def initiate(self, **kwargs):
        self.initiated.append(kwargs)
        return SigningSessionInit(
            provider=ProviderName.DEV_PASSWORD,
            provider_session_id="spy-1",
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
            redirect_url="https://example.invalid/sign",
            qr_payload=None,
            local_helper_payload=None,
        )

    async def sign_inline(self, **kwargs):
        self.signed_inline.append(kwargs)
        parsed = ParsedEnvelopeDTO(
            signer_full_name="Тест Тестовий",
            signer_ipn=None,
            signer_cert_serial="",
            signer_cert_issuer_cn="",
            cert_chain_pem=[],
            document_hash_sha256=hashlib.sha256(CANONICAL_BYTES).digest(),
            signed_at=datetime(2026, 7, 3, tzinfo=UTC),
            tsa_token_present=False,
            ocsp_responses_present=False,
            signature_algorithm="none-dev-password",
            is_qualified=False,
            format="dev",
        )
        return SignedEnvelope(
            provider=ProviderName.DEV_PASSWORD,
            provider_envelope_id="spy-env",
            signed_bytes=b"%PDF-spy",
            parsed=parsed,
            signature_level=SignatureLevel.DEV,
        )

    @property
    def touched(self) -> bool:
        return bool(self.initiated or self.signed_inline)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from signing_service import deps
    from signing_service.main import create_app
    from signing_service.routers import inline, sessions, uploads

    audit: list[dict] = []

    async def _write_event(**kwargs):
        audit.append(kwargs)

    spy = SpyProvider()

    deps.install_state(  # type: ignore[arg-type]
        SimpleNamespace(
            app_pool=object(),
            trust_store=object(),
            audit_writer=SimpleNamespace(write_event=_write_event),
            providers=SimpleNamespace(
                # MOCK for the session flow (dev_password is inline-only,
                # so provider selection would never choose it) and the
                # same spy for the inline flow.
                providers={ProviderName.MOCK: spy},
                get=lambda name: spy,
                get_inline=lambda name: spy,
            ),
        )
    )

    class _FakeConn:
        @contextlib.asynccontextmanager
        async def _tx(self):
            yield None

        def transaction(self):
            return self._tx()

    @contextlib.asynccontextmanager
    async def _fake_conn(pool, tenant_id):
        yield _FakeConn()

    for mod in (sessions, inline, uploads):
        monkeypatch.setattr(mod, "tenant_connection", _fake_conn)

    async def _health(conn):
        return {"mock": True}

    monkeypatch.setattr(sessions.repo, "fetch_all_provider_health", _health)

    async def _insert_session(conn, **kwargs):
        return uuid4()

    monkeypatch.setattr(sessions.repo, "insert_session", _insert_session)

    app = create_app()
    state = SimpleNamespace(role="clinician")
    app.dependency_overrides[deps.current_user] = lambda: _claims(state.role)

    return SimpleNamespace(
        client=TestClient(app), audit=audit, spy=spy, state=state
    )


def _as(harness, role: str) -> TestClient:
    harness.state.role = role
    return harness.client


def _session_body() -> dict:
    return {
        "resource_type": "report",
        "resource_id": str(uuid4()),
        "resource_version_id": str(uuid4()),
        "document_pdf_hash_hex": PDF_HASH_HEX,
        "display": {"title": "Chest CT", "code": "REP-2026-00001"},
    }


def _inline_body() -> dict:
    return {
        "resource_type": "report",
        "resource_id": str(uuid4()),
        "resource_version_id": str(uuid4()),
        "provider": "dev_password",
        "canonical_json": CANONICAL_JSON,
        "canonical_hash_hex": CANONICAL_HASH_HEX,
        "password": "dev-password",
    }


# ── The closure: every non-clinician role, every entry point ───────────


@pytest.mark.parametrize("role", NON_SIGNING_ROLES)
def test_session_initiation_is_403_for_non_clinicians(harness, role: str) -> None:
    resp = _as(harness, role).post("/signing/sessions", json=_session_body())
    assert resp.status_code == 403, f"{role} reached session initiation"
    assert not harness.spy.touched, f"{role} reached a KEP provider"


@pytest.mark.parametrize("role", NON_SIGNING_ROLES)
def test_inline_signing_is_403_for_non_clinicians(harness, role: str) -> None:
    resp = _as(harness, role).post("/signing/inline", json=_inline_body())
    assert resp.status_code == 403, f"{role} reached the inline signer"
    assert not harness.spy.touched, f"{role} reached a KEP provider"


@pytest.mark.parametrize("role", NON_SIGNING_ROLES)
def test_local_upload_is_403_for_non_clinicians(harness, role: str) -> None:
    """The local-KEP upload COMPLETES a signature — externally produced,
    but bound to the report here."""
    resp = _as(harness, role).post(
        f"/signing/sessions/{uuid4()}/upload",
        files={"file": ("signed.pdf", b"%PDF-1.7", "application/pdf")},
    )
    assert resp.status_code == 403, f"{role} reached the local-KEP upload"


@pytest.mark.parametrize("role", NON_SIGNING_ROLES)
def test_certificate_listing_is_403_for_non_clinicians(harness, role: str) -> None:
    """Signing-adjacent: enumerating the signing certificates available to
    you is only meaningful if you may sign."""
    resp = _as(harness, role).get("/signing/certificates")
    assert resp.status_code == 403


@pytest.mark.parametrize("role", NON_SIGNING_ROLES)
def test_session_cancel_is_403_for_non_clinicians(harness, role: str) -> None:
    """Cancelling someone else's in-flight signature is part of the
    signing workflow, not report authorship."""
    resp = _as(harness, role).delete(f"/signing/sessions/{uuid4()}")
    assert resp.status_code == 403


# ── The clinician still gets through ───────────────────────────────────


def test_clinician_reaches_the_provider(harness) -> None:
    """The other half of a closure test: proving the door still opens.
    A guard that refuses everyone is not a fix."""
    resp = _as(harness, "clinician").post("/signing/sessions", json=_session_body())
    assert resp.status_code == 200, resp.text
    assert harness.spy.initiated, "clinician did not reach the provider"


def test_dual_role_admin_clinician_reaches_the_provider(harness) -> None:
    """A practising doctor who also administers the tenant holds both
    roles; `check()` passes on any granting role."""
    harness.client.app.dependency_overrides[
        __import__("signing_service.deps", fromlist=["deps"]).current_user
    ] = lambda: Claims(
        sub=uuid4(),
        tid=TENANT,
        roles=["tenant_admin", "clinician"],
        sid="s",
        iss="https://test/issuer",
        aud="mdx",
        exp=9_999_999_999,
        iat=1_700_000_000,
    )
    resp = harness.client.post("/signing/sessions", json=_session_body())
    assert resp.status_code == 200, resp.text


# ── The refusal is visible ─────────────────────────────────────────────


def test_refused_signing_emits_a_sec_audit_naming_the_role(harness) -> None:
    """An admin repeatedly trying to sign charts must be visible. A silent
    403 does not produce that."""
    import anyio
    from fastapi import HTTPException

    # Bypass the route dependency so the in-body assertion is what runs —
    # this is the defence-in-depth layer, and it is what a future route
    # that forgets the dependency would fall back on.
    from signing_service.signing_authority import assert_may_sign

    from audit import Severity

    claims = _claims("tenant_admin")
    with pytest.raises(HTTPException) as excinfo:
        anyio.run(lambda: assert_may_sign(claims, resource_kind="report"))
    assert excinfo.value.status_code == 403
    assert excinfo.value.problem_extras["code"] == "signing_not_permitted"

    denied = [e for e in harness.audit if e["kind"] == "signing.denied_role"]
    assert denied, "no signing.denied_role audit event was written"
    event = denied[-1]
    assert event["severity"] is Severity.SEC
    assert event["actor_sub"] == claims.sub
    assert event["actor_role"] == "tenant_admin"
    # The FULL role list, not just the primary: "which of this person's
    # roles did they think authorised this" is the first review question.
    assert event["payload"]["roles"] == ["tenant_admin"]
    assert event["payload"]["required_permission"] == "report.sign"


def test_assert_may_sign_is_silent_for_a_clinician(harness) -> None:
    import anyio
    from signing_service.signing_authority import assert_may_sign, may_sign

    claims = _claims("clinician")
    assert may_sign(claims) is True
    anyio.run(lambda: assert_may_sign(claims, resource_kind="report"))
    assert not [e for e in harness.audit if e["kind"] == "signing.denied_role"]
