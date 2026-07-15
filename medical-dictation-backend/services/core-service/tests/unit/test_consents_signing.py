"""S11 step 03 — digital consent capture, canonical binding, sign flow."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from core_service.domain.consent_canonical import canonical_consent
from tests.conftest import REQUESTER_SUB, TENANT_ID

PATIENT_ID = UUID("33333333-3333-3333-3333-333333333333")
CONSENT_ID = UUID("55555555-5555-5555-5555-555555555555")
GRANTED_AT = datetime(2026, 7, 10, 9, 0, tzinfo=UTC)
TEXT_SHA = b"\x11" * 32


def _patient_row() -> dict:
    return {"name_uk": "Іван Петренко"}


def _consent_row(**over: object) -> dict:
    base: dict = {
        "id": CONSENT_ID,
        "tenant_id": TENANT_ID,
        "patient_id": PATIENT_ID,
        "encounter_id": None,
        "type": "ai_scribe",
        "method": "digital",
        "version": "v1",
        "status": "granted",
        "granted_at": GRANTED_AT,
        "withdrawn_at": None,
        "created_by": REQUESTER_SUB,
        "signed_envelope_id": None,
        "canonical_hash": None,
    }
    base.update(over)
    return base


def _expected_hash_hex() -> str:
    _, _, hash_hex = canonical_consent(
        consent_id=CONSENT_ID,
        tenant_id=TENANT_ID,
        patient_id=PATIENT_ID,
        patient_name_uk="Іван Петренко",
        consent_type="ai_scribe",
        text_version="v1",
        text_sha256=TEXT_SHA,
        granted_at=GRANTED_AT,
        attested_by_sub=REQUESTER_SUB,
        attested_by_name="Dev Clinician",
    )
    return hash_hex


def _install_canonical_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    from core_service.domain import consents_repository, patients_repository
    from core_service.routers import consents as consents_router

    async def _get_patient(conn, *, patient_id):  # noqa: ANN001
        return _patient_row()

    async def _display(conn, *, sub):  # noqa: ANN001
        return "Dev Clinician"

    monkeypatch.setattr(patients_repository, "get_patient", _get_patient)
    monkeypatch.setattr(consents_repository, "fetch_user_display_name", _display)
    monkeypatch.setattr(
        consents_router, "consent_text_sha256", lambda t, v: TEXT_SHA if v == "v1" else None
    )


# ── canonical document ──────────────────────────────────────────────


def test_canonical_is_deterministic() -> None:
    kwargs = {
        "consent_id": CONSENT_ID,
        "tenant_id": TENANT_ID,
        "patient_id": PATIENT_ID,
        "patient_name_uk": "Іван Петренко",
        "consent_type": "ai_scribe",
        "text_version": "v1",
        "text_sha256": TEXT_SHA,
        "granted_at": GRANTED_AT,
        "attested_by_sub": REQUESTER_SUB,
        "attested_by_name": "Dev Clinician",
    }
    _, bytes_a, hash_a = canonical_consent(**kwargs)
    _, bytes_b, hash_b = canonical_consent(**kwargs)
    assert bytes_a == bytes_b
    assert hash_a == hash_b


def test_canonical_binds_text_bytes() -> None:
    """A one-byte change in the approved text changes the signed document."""
    base = {
        "consent_id": CONSENT_ID,
        "tenant_id": TENANT_ID,
        "patient_id": PATIENT_ID,
        "patient_name_uk": "Іван Петренко",
        "consent_type": "ai_scribe",
        "text_version": "v1",
        "granted_at": GRANTED_AT,
        "attested_by_sub": REQUESTER_SUB,
        "attested_by_name": "Dev Clinician",
    }
    _, _, hash_a = canonical_consent(**base, text_sha256=TEXT_SHA)
    _, _, hash_b = canonical_consent(**base, text_sha256=b"\x12" + b"\x11" * 31)
    assert hash_a != hash_b


def test_text_registry_hashes_file_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The registry hashes the exact file bytes; editing the file changes
    the hash and an unknown (type, version) is None."""
    import hashlib

    from core_service.config import settings
    from core_service.domain.consent_texts import consent_text_sha256

    monkeypatch.setattr(settings, "consent_texts_dir", str(tmp_path))
    text = tmp_path / "ai_scribe-v1.md"
    text.write_text("Згода v1")
    assert consent_text_sha256("ai_scribe", "v1") == hashlib.sha256(
        "Згода v1".encode()
    ).digest()

    text.write_text("Згода v1 (edited)")
    assert consent_text_sha256("ai_scribe", "v1") == hashlib.sha256(
        "Згода v1 (edited)".encode()
    ).digest()

    assert consent_text_sha256("ai_scribe", "v9") is None
    assert consent_text_sha256("../evil", "v1") is None


# ── capture ─────────────────────────────────────────────────────────


def test_create_digital_consent_stores_hash_and_returns_hint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import consents_repository

    _install_canonical_stubs(monkeypatch)
    captured: dict = {}

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        captured.update(kwargs)
        return _consent_row(
            id=kwargs["consent_id"], canonical_hash=kwargs["canonical_hash"]
        )

    monkeypatch.setattr(consents_repository, "create_consent", _create)

    resp = client.post(
        f"/patients/{PATIENT_ID}/consents",
        json={"type": "ai_scribe", "method": "digital", "version": "v1"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["signing"]["resource_type"] == "consent"
    assert body["signing"]["resource_id"] == body["signing"]["resource_version_id"]
    assert captured["canonical_hash"] is not None
    assert body["signing"]["canonical_hash_hex"] == captured["canonical_hash"].hex()


def test_create_digital_unknown_text_version_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_canonical_stubs(monkeypatch)
    resp = client.post(
        f"/patients/{PATIENT_ID}/consents",
        json={"type": "ai_scribe", "method": "digital", "version": "v9"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "consent_text_version_unknown"


def test_create_verbal_consent_has_no_hint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import consents_repository, patients_repository

    async def _get_patient(conn, *, patient_id):  # noqa: ANN001
        return _patient_row()

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        assert kwargs["canonical_hash"] is None
        return _consent_row(method="verbal", version="")

    monkeypatch.setattr(patients_repository, "get_patient", _get_patient)
    monkeypatch.setattr(consents_repository, "create_consent", _create)

    resp = client.post(f"/patients/{PATIENT_ID}/consents", json={"method": "verbal"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["signing"] is None


# ── sign flow ───────────────────────────────────────────────────────


def _install_sign_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    row: dict,
    post_result: dict | None = None,
) -> dict:
    from core_service.domain import consents_repository
    from core_service.routers import consents as consents_router

    _install_canonical_stubs(monkeypatch)
    captured: dict = {"posts": []}
    reads = {"n": 0}

    async def _get_consent(conn, *, consent_id, patient_id):  # noqa: ANN001
        reads["n"] += 1
        if reads["n"] > 1 and post_result is not None:
            return {**row, "signed_envelope_id": UUID(post_result["envelope_id"])}
        return row

    async def _post(path, body, *, auth_header, expected):  # noqa: ANN001
        captured["posts"].append((path, body))
        return post_result or {}

    monkeypatch.setattr(consents_repository, "get_consent", _get_consent)
    monkeypatch.setattr(consents_router, "_post_signing", _post)
    return captured


def test_sign_digital_consent_inline_links_and_audits(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope_id = str(uuid4())
    row = _consent_row(canonical_hash=bytes.fromhex(_expected_hash_hex()))
    captured = _install_sign_stubs(
        monkeypatch,
        row=row,
        post_result={
            "envelope_id": envelope_id,
            "signature_level": "dev",
            "verification_token": "tok-123",
            "signed_at": "2026-07-15T10:00:00+00:00",
            "signer_full_name": "Dev Clinician",
            "is_qualified": False,
        },
    )

    resp = client.post(
        f"/patients/{PATIENT_ID}/consents/{CONSENT_ID}/sign",
        json={"provider": "dev_password", "password": "dev-password"},
    )
    assert resp.status_code == 200, resp.text
    path, posted = captured["posts"][0]
    assert path == "/signing/inline"
    assert posted["resource_type"] == "consent"
    assert posted["resource_id"] == posted["resource_version_id"] == str(CONSENT_ID)
    assert posted["canonical_hash_hex"] == _expected_hash_hex()
    assert posted["canonical_json"]["kind"] == "patient_consent"

    body = resp.json()
    assert body["consent"]["signed_envelope_id"] == envelope_id
    signed = [c for c in client.audit_calls if c["kind"] == "consent.signed"]  # type: ignore[attr-defined]
    assert signed and signed[-1]["payload"]["envelope_id"] == envelope_id


def test_sign_diia_returns_202_session(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _consent_row(canonical_hash=bytes.fromhex(_expected_hash_hex()))
    captured = _install_sign_stubs(
        monkeypatch,
        row=row,
        post_result={"session_id": str(uuid4()), "provider": "diia", "expires_at": "x"},
    )
    resp = client.post(
        f"/patients/{PATIENT_ID}/consents/{CONSENT_ID}/sign", json={"provider": "diia"}
    )
    assert resp.status_code == 202, resp.text
    path, posted = captured["posts"][0]
    assert path == "/signing/sessions"
    assert posted["document_pdf_hash_hex"] == _expected_hash_hex()


def test_sign_already_signed_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _consent_row(signed_envelope_id=uuid4())
    _install_sign_stubs(monkeypatch, row=row)
    resp = client.post(
        f"/patients/{PATIENT_ID}/consents/{CONSENT_ID}/sign",
        json={"provider": "dev_password", "password": "x"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "consent_already_signed"


def test_sign_non_digital_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _consent_row(method="verbal")
    _install_sign_stubs(monkeypatch, row=row)
    resp = client.post(
        f"/patients/{PATIENT_ID}/consents/{CONSENT_ID}/sign",
        json={"provider": "dev_password", "password": "x"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "consent_not_digital"


def test_sign_canonical_drift_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row mutated (or patient renamed) since capture → no signing."""
    row = _consent_row(canonical_hash=b"\x99" * 32)
    _install_sign_stubs(monkeypatch, row=row)
    resp = client.post(
        f"/patients/{PATIENT_ID}/consents/{CONSENT_ID}/sign",
        json={"provider": "dev_password", "password": "x"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "consent_canonical_changed"


# ── withdrawal keeps the envelope ───────────────────────────────────


def test_withdraw_after_sign_preserves_envelope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import consents_repository

    envelope_id = uuid4()

    async def _withdraw(conn, *, consent_id, patient_id, when):  # noqa: ANN001
        return _consent_row(
            status="withdrawn",
            withdrawn_at=when,
            signed_envelope_id=envelope_id,
        )

    monkeypatch.setattr(consents_repository, "withdraw_consent", _withdraw)

    resp = client.post(f"/patients/{PATIENT_ID}/consents/{CONSENT_ID}/withdraw")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Both facts: granted-and-signed AND withdrawn.
    assert body["status"] == "withdrawn"
    assert body["withdrawn_at"] is not None
    assert body["granted_at"] is not None
    assert body["signed_envelope_id"] == str(envelope_id)
