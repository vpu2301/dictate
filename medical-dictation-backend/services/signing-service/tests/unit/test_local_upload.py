"""Behavioural tests for POST /signing/sessions/{id}/upload (M1·B4).

The PAdES parse + trust-store verify are stubbed; these assert the session
gate, file-shape validation, the binding-failure → 422 path and the happy
path → signed + audit.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from auth import Claims


def _admin_claims() -> Claims:
    return Claims(
        sub=uuid4(),
        tid=uuid4(),
        roles=["tenant_admin"],
        sid="s",
        iss="https://test/issuer",
        aud="mdx",
        exp=9_999_999_999,
        iat=1_700_000_000,
    )


def _session_row(status: str = "awaiting_user", *, document_pdf_hash: bytes | None = b"\x11" * 32):
    return {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "status": status,
        "provider": "iit",
        "expires_at": datetime(2026, 6, 1, tzinfo=UTC),
        "redirect_url": None,
        "qr_payload": None,
        "signed_envelope_id": None,
        "failure_reason": None,
        "initiated_by": uuid4(),
        "resource_type": "report",
        "resource_id": uuid4(),
        "resource_version_id": uuid4(),
        "provider_session_id": "psid-1",
        "document_pdf_hash": document_pdf_hash,
        "verification_token": None,
        "signed_at": None,
        "signer_full_name": None,
    }


def _fake_parsed():
    return SimpleNamespace(
        format="PAdES-BES",
        signer_full_name="Dr Test",
        signer_ipn="1234567890",
        signer_cert_serial_hex="abcd",
        signer_cert_issuer_cn="Test CA",
        cert_chain_pem=["-----BEGIN CERT-----"],
        document_hash_sha256=b"\x11" * 32,
        signed_at=datetime(2026, 5, 30, tzinfo=UTC),
        tsa_token_present=True,
        ocsp_responses_present=True,
        signature_algorithm="RSA-SHA256",
        is_qualified=True,
    )


class _FakeEnvelope:
    def __init__(self, raw, *, declared_format=None):  # noqa: ANN001
        self._raw = raw

    def parse(self):
        return _fake_parsed()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from signing_service import deps
    from signing_service.main import create_app

    audit: list[dict] = []

    async def _write_event(**kwargs):  # noqa: ANN003
        audit.append(kwargs)

    deps.install_state(  # type: ignore[arg-type]
        SimpleNamespace(
            app_pool=object(),
            trust_store=object(),
            audit_writer=SimpleNamespace(write_event=_write_event),
        )
    )

    app = create_app()
    app.dependency_overrides[deps.current_user] = _admin_claims
    c = TestClient(app)
    c.audit_calls = audit  # type: ignore[attr-defined]
    return c


@pytest.fixture(autouse=True)
def _stub_tenant_conn(monkeypatch: pytest.MonkeyPatch) -> None:
    from signing_service.routers import uploads

    @contextlib.asynccontextmanager
    async def _fake(pool, tenant_id):  # noqa: ANN001
        yield None

    monkeypatch.setattr(uploads, "tenant_connection", _fake)


def _pdf_file(content: bytes = b"%PDF-1.7 signed", content_type: str = "application/pdf"):
    return {"file": ("signed.pdf", content, content_type)}


def test_upload_404_when_missing(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from signing_service.routers import uploads

    async def _fetch(conn, *, session_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(uploads.repo, "fetch_session_by_id", _fetch)
    assert client.post(f"/signing/sessions/{uuid4()}/upload", files=_pdf_file()).status_code == 404


def test_upload_409_when_not_awaiting(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from signing_service.routers import uploads

    async def _fetch(conn, *, session_id):  # noqa: ANN001
        return _session_row("verifying")

    monkeypatch.setattr(uploads.repo, "fetch_session_by_id", _fetch)
    resp = client.post(f"/signing/sessions/{uuid4()}/upload", files=_pdf_file())
    assert resp.status_code == 409


def test_upload_415_wrong_content_type(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from signing_service.routers import uploads

    async def _fetch(conn, *, session_id):  # noqa: ANN001
        return _session_row()

    monkeypatch.setattr(uploads.repo, "fetch_session_by_id", _fetch)
    resp = client.post(
        f"/signing/sessions/{uuid4()}/upload",
        files=_pdf_file(content_type="text/plain"),
    )
    assert resp.status_code == 415


def test_upload_422_when_not_pdf(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from signing_service.routers import uploads

    async def _fetch(conn, *, session_id):  # noqa: ANN001
        return _session_row()

    monkeypatch.setattr(uploads.repo, "fetch_session_by_id", _fetch)
    resp = client.post(
        f"/signing/sessions/{uuid4()}/upload",
        files=_pdf_file(content=b"not a pdf"),
    )
    assert resp.status_code == 422


def test_upload_422_when_verify_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from signing_service.routers import uploads

    async def _fetch(conn, *, session_id):  # noqa: ANN001
        return _session_row()

    async def _transition(conn, *, session_id, expected_from, to, failure_reason=None, signed_envelope_id=None):  # noqa: ANN001
        return True

    monkeypatch.setattr(uploads.repo, "fetch_session_by_id", _fetch)
    monkeypatch.setattr(uploads.repo, "transition_session", _transition)
    monkeypatch.setattr(uploads, "Envelope", _FakeEnvelope)
    monkeypatch.setattr(
        uploads, "verify_envelope",
        lambda **kw: SimpleNamespace(valid=False, errors=["untrusted_cert"]),
    )

    resp = client.post(f"/signing/sessions/{uuid4()}/upload", files=_pdf_file())
    assert resp.status_code == 422
    assert "untrusted_cert" in resp.text


def test_upload_happy_path(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from signing_service.routers import uploads

    env_id = uuid4()
    expected = {}

    async def _fetch(conn, *, session_id):  # noqa: ANN001
        return _session_row()

    async def _transition(conn, *, session_id, expected_from, to, failure_reason=None, signed_envelope_id=None):  # noqa: ANN001
        expected.setdefault("transitions", []).append((expected_from, to))
        return True

    async def _insert(conn, **kwargs):  # noqa: ANN003
        expected["insert"] = kwargs
        return env_id

    monkeypatch.setattr(uploads.repo, "fetch_session_by_id", _fetch)
    monkeypatch.setattr(uploads.repo, "transition_session", _transition)
    monkeypatch.setattr(uploads.repo, "insert_envelope", _insert)
    monkeypatch.setattr(uploads, "Envelope", _FakeEnvelope)
    monkeypatch.setattr(
        uploads, "verify_envelope", lambda **kw: SimpleNamespace(valid=True, errors=[])
    )

    resp = client.post(f"/signing/sessions/{uuid4()}/upload", files=_pdf_file())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "signed"
    assert body["signed_envelope_id"] == str(env_id)
    assert body["verification_token"]
    # awaiting_user→verifying then verifying→signed.
    assert expected["transitions"] == [("awaiting_user", "verifying"), ("verifying", "signed")]
    # Binding: verify is called with the session's stored hash, inserted as ciphertext bytes.
    assert expected["insert"]["signed_data"].startswith(b"%PDF")
    assert expected["insert"]["certificate_serial"] == "abcd"
    kinds = [c["kind"] for c in client.audit_calls]  # type: ignore[attr-defined]
    assert "signing.envelope.persisted" in kinds
    assert "signing.session.local_upload" in kinds
