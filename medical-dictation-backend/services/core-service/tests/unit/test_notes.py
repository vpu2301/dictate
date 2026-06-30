"""Behavioural tests for the /notes surface."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from tests.conftest import REQUESTER_SUB, TENANT_ID

PATIENT_ID = UUID("33333333-3333-3333-3333-333333333333")
NOTE_ID = UUID("44444444-4444-4444-4444-444444444444")
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _note_row(**over: object) -> dict:
    base: dict = {
        "id": NOTE_ID,
        "tenant_id": TENANT_ID,
        "patient_id": PATIENT_ID,
        "encounter_id": None,
        "structure": "soap",
        "title": "Progress note",
        "sections": [{"key": "subjective", "text": "cough"}],
        "status": "draft",
        "author_id": REQUESTER_SUB,
        "source_session_id": None,
        "created_at": NOW,
        "updated_at": NOW,
        "signed_at": None,
    }
    base.update(over)
    return base


def test_note_structures_catalogue(client: TestClient) -> None:
    resp = client.get("/note-structures")
    assert resp.status_code == 200
    codes = {s["code"] for s in resp.json()}
    assert {"soap", "apso", "dap", "free"} <= codes


def test_create_note_bumps_last_visit_and_audits(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import notes_repository, patients_repository

    bumped: dict = {}

    async def _get_patient(conn, *, patient_id):  # noqa: ANN001
        return {"id": patient_id}

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        return _note_row()

    async def _bump(conn, *, patient_id, when):  # noqa: ANN001
        bumped["patient_id"] = patient_id

    monkeypatch.setattr(patients_repository, "get_patient", _get_patient)
    monkeypatch.setattr(notes_repository, "create_note", _create)
    monkeypatch.setattr(patients_repository, "bump_last_visit", _bump)

    resp = client.post(
        "/notes",
        json={
            "patient_id": str(PATIENT_ID),
            "structure": "soap",
            "title": "Progress note",
            "sections": [{"key": "subjective", "text": "cough"}],
        },
    )
    assert resp.status_code == 201, resp.text
    assert bumped["patient_id"] == PATIENT_ID
    assert any(c["kind"] == "note.created" for c in client.audit_calls)  # type: ignore[attr-defined]


def test_create_note_404_when_patient_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    async def _get_patient(conn, *, patient_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(patients_repository, "get_patient", _get_patient)
    resp = client.post("/notes", json={"patient_id": str(PATIENT_ID)})
    assert resp.status_code == 404


def test_sign_note(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from core_service.domain import notes_repository

    async def _sign(conn, *, note_id, when):  # noqa: ANN001
        return _note_row(status="signed", signed_at=when)

    monkeypatch.setattr(notes_repository, "sign_note", _sign)
    resp = client.post(f"/notes/{NOTE_ID}/sign")
    assert resp.status_code == 200
    assert resp.json()["status"] == "signed"
    assert any(c["kind"] == "note.signed" for c in client.audit_calls)  # type: ignore[attr-defined]


def test_sign_already_signed_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import notes_repository

    async def _sign(conn, *, note_id, when):  # noqa: ANN001
        return None  # no draft row matched

    async def _get(conn, *, note_id):  # noqa: ANN001
        return _note_row(status="signed")

    monkeypatch.setattr(notes_repository, "sign_note", _sign)
    monkeypatch.setattr(notes_repository, "get_note", _get)
    resp = client.post(f"/notes/{NOTE_ID}/sign")
    assert resp.status_code == 409


def test_patch_signed_note_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import notes_repository

    async def _get(conn, *, note_id):  # noqa: ANN001
        return _note_row(status="signed")

    monkeypatch.setattr(notes_repository, "get_note", _get)
    resp = client.patch(f"/notes/{NOTE_ID}", json={"title": "edit"})
    assert resp.status_code == 409
