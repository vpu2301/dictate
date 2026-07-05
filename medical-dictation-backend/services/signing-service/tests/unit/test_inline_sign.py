"""POST /signing/inline — behavioural tests over faked providers/repo.

Covers: dev happy path (level='dev' persisted, never qualified), wrong
account password → 401 + sec audit, bad key container → 400 + sec
audit, IdP lockout → 423, provider not wired → 503, canonical-hash
binding → 400, and the qualified path persisting the VERIFIER's
``is_qualified`` verdict (truthfulness invariant).
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
    AccountLockedError,
    InvalidCredentialsError,
    ParsedEnvelopeDTO,
    ProviderName,
    SignatureLevel,
    SignedEnvelope,
)

from audit.canonical import canonicalize
from auth import Claims

CANONICAL_JSON = {"canonical_version": "1.0", "report": {"id": "r-1"}}
CANONICAL_BYTES = canonicalize(CANONICAL_JSON)
CANONICAL_HASH_HEX = hashlib.sha256(CANONICAL_BYTES).hexdigest()


def _claims() -> Claims:
    return Claims(
        sub=uuid4(),
        tid=uuid4(),
        roles=["clinician"],
        sid="s",
        iss="https://test/issuer",
        aud="mdx",
        exp=9_999_999_999,
        iat=1_700_000_000,
        preferred_username="clinician@tenant-a.example",
        name="Лікар Тестовий",
    )


def _dev_envelope(**overrides) -> SignedEnvelope:
    parsed = ParsedEnvelopeDTO(
        signer_full_name="Лікар Тестовий",
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
    defaults = {
        "provider": ProviderName.DEV_PASSWORD,
        "provider_envelope_id": "dev-x",
        "signed_bytes": b"%PDF-dev",
        "parsed": parsed,
        "signature_level": SignatureLevel.DEV,
    }
    defaults.update(overrides)
    return SignedEnvelope(**defaults)


class _FakeSigner:
    def __init__(self, result: SignedEnvelope | Exception) -> None:
        self._result = result
        self.calls: list[dict] = []

    async def sign_inline(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from signing_service import deps
    from signing_service.main import create_app
    from signing_service.routers import inline

    audit: list[dict] = []

    async def _write_event(**kwargs):
        audit.append(kwargs)

    inline_signers: dict[ProviderName, object] = {}

    def _get_inline(name: ProviderName):
        try:
            return inline_signers[name]
        except KeyError as exc:
            raise ValueError(f"inline provider {name!r} not configured") from exc

    deps.install_state(  # type: ignore[arg-type]
        SimpleNamespace(
            app_pool=object(),
            trust_store=object(),
            audit_writer=SimpleNamespace(write_event=_write_event),
            providers=SimpleNamespace(get_inline=_get_inline),
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

    monkeypatch.setattr(inline, "tenant_connection", _fake_conn)

    calls: dict = {"transitions": [], "insert": None, "mark": None}
    session_id = uuid4()

    async def _insert_session(conn, **kwargs):
        calls["session"] = kwargs
        return session_id

    async def _transition(conn, *, session_id, expected_from, to, failure_reason=None, signed_envelope_id=None):
        calls["transitions"].append((expected_from, to, failure_reason))
        return True

    async def _insert_envelope(conn, **kwargs):
        calls["insert"] = kwargs
        return uuid4()

    async def _mark(conn, **kwargs):
        calls["mark"] = kwargs
        return "signed"

    monkeypatch.setattr(inline.repo, "insert_session", _insert_session)
    monkeypatch.setattr(inline.repo, "transition_session", _transition)
    monkeypatch.setattr(inline.repo, "insert_envelope", _insert_envelope)
    monkeypatch.setattr(inline.repo, "mark_resource_signed", _mark)

    app = create_app()
    app.dependency_overrides[deps.current_user] = _claims
    client = TestClient(app)
    return SimpleNamespace(
        client=client, audit=audit, calls=calls, signers=inline_signers, inline=inline
    )


def _body(provider: str = "dev_password", **overrides) -> dict:
    body = {
        "resource_type": "report",
        "resource_id": str(uuid4()),
        "resource_version_id": str(uuid4()),
        "provider": provider,
        "canonical_json": CANONICAL_JSON,
        "canonical_hash_hex": CANONICAL_HASH_HEX,
    }
    if provider == "dev_password":
        body["password"] = "dev-password"
    else:
        body["key_container_b64"] = "MIIB"  # valid b64
        body["key_password"] = "pw"
    body.update(overrides)
    return body


def test_dev_password_happy_path(harness) -> None:
    harness.signers[ProviderName.DEV_PASSWORD] = _FakeSigner(_dev_envelope())
    resp = harness.client.post("/signing/inline", json=_body())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["signature_level"] == "dev"
    assert data["is_qualified"] is False
    assert data["report_status"] == "signed"
    assert data["verification_token"]
    # Persisted truthfully.
    assert harness.calls["insert"]["signature_level"] == "dev"
    assert harness.calls["insert"]["is_qualified"] is False
    assert harness.calls["mark"]["resource_type"] == "report"
    assert ("awaiting_user", "verifying", None) in harness.calls["transitions"]
    assert any(t[1] == "signed" for t in harness.calls["transitions"])
    kinds = [a["kind"] for a in harness.audit]
    assert "signing.envelope.persisted" in kinds
    persisted = next(a for a in harness.audit if a["kind"] == "signing.envelope.persisted")
    assert persisted["payload"]["signature_level"] == "dev"


def test_wrong_account_password_401_with_sec_audit(harness) -> None:
    harness.signers[ProviderName.DEV_PASSWORD] = _FakeSigner(
        InvalidCredentialsError("account password rejected")
    )
    resp = harness.client.post("/signing/inline", json=_body())
    assert resp.status_code == 401
    kinds = [a["kind"] for a in harness.audit]
    assert "signing.dev_password_rejected" in kinds
    assert any(t[1] == "failed" for t in harness.calls["transitions"])


def test_bad_key_container_400_with_sec_audit(harness) -> None:
    harness.signers[ProviderName.FILE_KEY] = _FakeSigner(
        InvalidCredentialsError("key container rejected")
    )
    resp = harness.client.post("/signing/inline", json=_body("file_key"))
    assert resp.status_code == 400
    kinds = [a["kind"] for a in harness.audit]
    assert "signing.file_key_rejected" in kinds


def test_account_locked_423(harness) -> None:
    harness.signers[ProviderName.DEV_PASSWORD] = _FakeSigner(AccountLockedError("locked"))
    resp = harness.client.post("/signing/inline", json=_body())
    assert resp.status_code == 423


def test_provider_not_wired_503(harness) -> None:
    resp = harness.client.post("/signing/inline", json=_body("file_key"))
    assert resp.status_code == 503
    assert "provider_unavailable" in resp.text


def test_canonical_hash_mismatch_400(harness) -> None:
    harness.signers[ProviderName.DEV_PASSWORD] = _FakeSigner(_dev_envelope())
    resp = harness.client.post(
        "/signing/inline", json=_body(canonical_hash_hex="0" * 64)
    )
    assert resp.status_code == 400
    assert "canonical_hash_mismatch" in resp.text


def test_missing_password_400(harness) -> None:
    harness.signers[ProviderName.DEV_PASSWORD] = _FakeSigner(_dev_envelope())
    body = _body()
    del body["password"]
    resp = harness.client.post("/signing/inline", json=body)
    assert resp.status_code == 400


def test_qualified_path_persists_verifier_verdict(harness, monkeypatch) -> None:
    """Truthfulness: a test-CA-anchored envelope persists is_qualified
    exactly as the verifier says (False), even if the provider claims
    otherwise."""
    from medical_kep.mock_provider import _default_test_ca_dir, _ensure_test_ca, _sign_with_test_ca

    ca = _ensure_test_ca(_default_test_ca_dir())
    doc_hash = hashlib.sha256(CANONICAL_BYTES).digest()
    cms = _sign_with_test_ca(
        ca=ca, doc_hash=doc_hash, signer_full_name="Dr Q", signer_ipn="1234567890"
    )
    parsed = ParsedEnvelopeDTO(
        signer_full_name="Dr Q",
        signer_ipn="1234567890",
        signer_cert_serial="ab",
        signer_cert_issuer_cn="Test CA",
        cert_chain_pem=ca.chain_pem,
        document_hash_sha256=doc_hash,
        signed_at=datetime(2026, 7, 3, tzinfo=UTC),
        tsa_token_present=False,
        ocsp_responses_present=False,
        signature_algorithm="sha256_rsa",
        is_qualified=True,  # provider self-asserts qualified
        format="CAdES-BES",
    )
    harness.signers[ProviderName.FILE_KEY] = _FakeSigner(
        _dev_envelope(
            provider=ProviderName.FILE_KEY,
            signed_bytes=cms,
            parsed=parsed,
            signature_level=SignatureLevel.QUALIFIED,
        )
    )
    monkeypatch.setattr(
        harness.inline,
        "verify_envelope",
        lambda **kw: SimpleNamespace(valid=True, errors=[], is_qualified=False),
    )
    resp = harness.client.post("/signing/inline", json=_body("file_key"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["signature_level"] == "qualified"
    assert resp.json()["is_qualified"] is False  # verifier's verdict wins
    assert harness.calls["insert"]["is_qualified"] is False
    assert harness.calls["insert"]["signature_level"] == "qualified"


def test_envelope_id_is_uuid(harness) -> None:
    harness.signers[ProviderName.DEV_PASSWORD] = _FakeSigner(_dev_envelope())
    resp = harness.client.post("/signing/inline", json=_body())
    UUID(resp.json()["envelope_id"])  # raises if malformed
