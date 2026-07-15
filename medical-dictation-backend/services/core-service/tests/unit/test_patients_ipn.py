"""S11 step 01 — ІПН capture, lookup dispatch, erased-status guards.

All ІПН values are synthetic (constructed to satisfy the public РНОКПП
control-digit formula); none belong to a person.
"""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Callable
from uuid import UUID

import asyncpg
import pytest
from fastapi.testclient import TestClient

from crypto.ipn import ipn_hmac as compute_ipn_hmac
from tests.unit.test_patients import PATIENT_ID, _patient_row

VALID_IPN = "1759013776"
VALID_IPN_SPACED = "175 901 37 76"
OTHER_VALID_IPN = "2874309631"
BAD_CHECKSUM_IPN = "1759013775"

# Matches the config default ("00" * 32).
EXPECTED_HMAC = compute_ipn_hmac(VALID_IPN, "00" * 32)


class _FakeIpnUnique(asyncpg.UniqueViolationError):
    """UniqueViolationError pinned to the ІПН partial-unique constraint."""

    def __init__(self) -> None:
        Exception.__init__(self, "duplicate key value")

    @property
    def constraint_name(self) -> str:
        return "uq_patients_tenant_ipn"


# ── capture ─────────────────────────────────────────────────────────


def test_create_with_ipn_stores_hmac_never_raw(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from core_service.domain import patients_repository

    captured: dict = {}

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        captured.update(kwargs)
        return _patient_row(has_ipn=kwargs["ipn_hmac"] is not None)

    monkeypatch.setattr(patients_repository, "create_patient", _create)

    with caplog.at_level(logging.DEBUG):
        resp = client.post(
            "/patients",
            json={"name": {"uk": "Іван Петренко"}, "ipn": VALID_IPN_SPACED},
        )
    assert resp.status_code == 201, resp.text

    # Stored: the hmac of the *normalized* ІПН; raw retention off by default.
    assert captured["ipn_hmac"] == EXPECTED_HMAC
    assert captured["ipn_encrypted"] is None
    assert captured["ipn_dek"] is None
    # The service, not the DB, generates the row id (AAD binding).
    assert isinstance(captured["patient_id"], UUID)

    # Raw ІПН appears nowhere: not in the response, not in any log record.
    assert resp.json()["has_ipn"] is True
    for leak_surface in (resp.text, caplog.text):
        assert VALID_IPN not in leak_surface
        assert VALID_IPN_SPACED not in leak_surface

    # Audit payload carries presence only.
    created = [c for c in client.audit_calls if c["kind"] == "patient.created"]  # type: ignore[attr-defined]
    assert created and created[-1]["payload"]["has_ipn"] is True
    assert VALID_IPN not in str(created[-1]["payload"])


def test_create_without_ipn_has_ipn_false(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        return _patient_row(has_ipn=kwargs["ipn_hmac"] is not None)

    monkeypatch.setattr(patients_repository, "create_patient", _create)
    resp = client.post("/patients", json={"name": {"uk": "Іван Петренко"}})
    assert resp.status_code == 201, resp.text
    assert resp.json()["has_ipn"] is False


@pytest.mark.parametrize("bad", ["123", "175901377a", BAD_CHECKSUM_IPN])
def test_create_invalid_ipn_422(client: TestClient, bad: str) -> None:
    resp = client.post("/patients", json={"name": {"uk": "Іван"}, "ipn": bad})
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "ipn_invalid"


def test_create_duplicate_ipn_409_with_existing_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        raise _FakeIpnUnique()

    async def _find(conn, *, ipn_hmac):  # noqa: ANN001
        assert ipn_hmac == EXPECTED_HMAC
        return PATIENT_ID

    monkeypatch.setattr(patients_repository, "create_patient", _create)
    monkeypatch.setattr(patients_repository, "find_patient_id_by_ipn_hmac", _find)

    resp = client.post("/patients", json={"name": {"uk": "Іван"}, "ipn": VALID_IPN})
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == "patient_ipn_exists"
    assert body["existing_patient_id"] == str(PATIENT_ID)
    assert VALID_IPN not in resp.text


def test_create_duplicate_mrn_still_plain_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        raise asyncpg.UniqueViolationError("duplicate key value")

    monkeypatch.setattr(patients_repository, "create_patient", _create)
    resp = client.post("/patients", json={"name": {"uk": "Іван"}, "mrn": "MRN-1"})
    assert resp.status_code == 409
    assert "MRN" in resp.json()["detail"]


# ── raw retention (flag on) ─────────────────────────────────────────


def test_create_with_raw_retention_encrypts_via_envelope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service import deps
    from core_service.config import settings
    from core_service.domain import patients_repository
    from crypto import EnvelopeBlob

    captured: dict = {}

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        captured.update(kwargs)
        return _patient_row(has_ipn=True)

    monkeypatch.setattr(patients_repository, "create_patient", _create)
    monkeypatch.setattr(settings, "patient_ipn_raw_enabled", True)

    class _FakeEnvelope:
        async def encrypt(self, plaintext: bytes, *, tenant_id: UUID, aad: bytes) -> EnvelopeBlob:
            captured["plaintext"] = plaintext
            captured["aad"] = aad
            return EnvelopeBlob(
                ciphertext=b"c" * 10,
                iv=b"i" * 12,
                tag=b"t" * 16,
                wrapped_dek=b"w" * 32,
                dek_iv=b"j" * 12,
                dek_tag=b"u" * 16,
                tenant_id=tenant_id,
                master_key_id="dev",
            )

    deps.get_state().envelope = _FakeEnvelope()  # type: ignore[attr-defined]

    resp = client.post("/patients", json={"name": {"uk": "Іван"}, "ipn": VALID_IPN})
    assert resp.status_code == 201, resp.text
    # AAD binds tenant ‖ generated row id.
    assert captured["plaintext"] == VALID_IPN.encode()
    assert captured["aad"] == captured["patient_id"].bytes
    assert captured["ipn_encrypted"] == b"i" * 12 + b"t" * 16 + b"c" * 10
    assert captured["ipn_dek"] == b"j" * 12 + b"u" * 16 + b"w" * 32
    assert VALID_IPN not in resp.text


# ── roster search dispatch ──────────────────────────────────────────


def _capture_list(monkeypatch: pytest.MonkeyPatch) -> dict:
    from core_service.domain import patients_repository

    captured: dict = {}

    async def _list(conn, **kwargs):  # noqa: ANN001, ANN003
        captured.update(kwargs)
        return []

    monkeypatch.setattr(patients_repository, "list_patients", _list)
    return captured


def test_search_ipn_shaped_dispatches_to_hmac(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _capture_list(monkeypatch)
    resp = client.get("/patients", params={"query": VALID_IPN})
    assert resp.status_code == 200
    assert captured["ipn_hmac"] == EXPECTED_HMAC


def test_search_ipn_with_spaces_still_dispatches(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _capture_list(monkeypatch)
    resp = client.get("/patients", params={"query": VALID_IPN_SPACED})
    assert resp.status_code == 200
    assert captured["ipn_hmac"] == EXPECTED_HMAC


@pytest.mark.parametrize("q", ["Іван Петренко", "MRN-1", BAD_CHECKSUM_IPN])
def test_search_non_ipn_takes_text_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, q: str
) -> None:
    captured = _capture_list(monkeypatch)
    resp = client.get("/patients", params={"query": q})
    assert resp.status_code == 200
    assert captured["ipn_hmac"] is None
    assert captured["query"] == q


# ── erased status ───────────────────────────────────────────────────


def test_list_excludes_erased_by_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _capture_list(monkeypatch)
    resp = client.get("/patients")
    assert resp.status_code == 200
    assert captured["include_erased"] is False


def test_include_erased_requires_tenant_admin(
    make_client: Callable[[list[str]], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    clinician = make_client(["clinician"])
    resp = clinician.get("/patients", params={"include_erased": "true"})
    assert resp.status_code == 403

    captured = _capture_list(monkeypatch)
    admin = make_client(["tenant_admin"])
    resp = admin.get("/patients", params={"include_erased": "true"})
    assert resp.status_code == 200, resp.text
    assert captured["include_erased"] is True


def test_put_cannot_set_erased(client: TestClient) -> None:
    resp = client.put(f"/patients/{PATIENT_ID}", json={"status": "erased"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "status_immutable_erased"


def test_put_on_erased_patient_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from core_service.domain import patients_repository

    async def _get(conn, *, patient_id):  # noqa: ANN001
        return _patient_row(status="erased", erased_at=datetime(2026, 7, 1, tzinfo=UTC))

    monkeypatch.setattr(patients_repository, "get_patient", _get)
    resp = client.put(f"/patients/{PATIENT_ID}", json={"name": {"uk": "Новe Ім'я"}})
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "status_immutable_erased"


def test_put_empty_ipn_clears_columns(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    captured: dict = {}

    async def _get(conn, *, patient_id):  # noqa: ANN001
        return _patient_row()

    async def _update(conn, *, patient_id, fields):  # noqa: ANN001
        captured.update(fields)
        return _patient_row()

    monkeypatch.setattr(patients_repository, "get_patient", _get)
    monkeypatch.setattr(patients_repository, "update_patient", _update)

    resp = client.put(f"/patients/{PATIENT_ID}", json={"ipn": ""})
    assert resp.status_code == 200, resp.text
    assert captured == {"ipn_hmac": None, "ipn_encrypted": None, "ipn_dek": None}
    # Audit sees a single "ipn" marker, not the three column names.
    updated = [c for c in client.audit_calls if c["kind"] == "patient.updated"]  # type: ignore[attr-defined]
    assert updated[-1]["payload"]["fields"] == ["ipn"]


def test_put_set_ipn_updates_hmac(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    captured: dict = {}

    async def _get(conn, *, patient_id):  # noqa: ANN001
        return _patient_row()

    async def _update(conn, *, patient_id, fields):  # noqa: ANN001
        captured.update(fields)
        return _patient_row(has_ipn=True)

    monkeypatch.setattr(patients_repository, "get_patient", _get)
    monkeypatch.setattr(patients_repository, "update_patient", _update)

    resp = client.put(f"/patients/{PATIENT_ID}", json={"ipn": OTHER_VALID_IPN})
    assert resp.status_code == 200, resp.text
    assert captured["ipn_hmac"] == compute_ipn_hmac(OTHER_VALID_IPN, "00" * 32)
    assert resp.json()["has_ipn"] is True
    assert OTHER_VALID_IPN not in resp.text


# ── the repository never touches raw ІПН ────────────────────────────


def test_repository_sql_has_no_raw_ipn_predicate() -> None:
    """The acceptance-criteria grep: every ІПН reference in the repository
    is one of the derived columns (ipn_hmac / ipn_encrypted / ipn_dek) —
    a bare ``ipn`` column name appears in no SQL string or identifier."""
    from core_service.domain import patients_repository

    src = inspect.getsource(patients_repository)
    assert "ipn_hmac" in src  # the sanctioned lookup token is used
    bare_ipn = re.compile(r"(?<![\w])ipn(?![\w])", re.IGNORECASE)
    assert not bare_ipn.search(src), "raw `ipn` reference found in repository source"
