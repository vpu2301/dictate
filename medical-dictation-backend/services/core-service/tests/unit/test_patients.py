"""Behavioural tests for the /patients surface and the unified timeline."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from uuid import UUID

import asyncpg
import pytest
from fastapi.testclient import TestClient

from tests.conftest import REQUESTER_SUB, TENANT_ID

PATIENT_ID = UUID("33333333-3333-3333-3333-333333333333")
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _patient_row(**over: object) -> dict:
    base: dict = {
        "id": PATIENT_ID,
        "tenant_id": TENANT_ID,
        "name_uk": "Іван Петренко",
        "name_en": "Ivan Petrenko",
        "dob": date(1980, 1, 15),
        "sex": "M",
        "mrn": "MRN-1",
        "summary_uk": "",
        "summary_en": "",
        "tags": ["diabetes"],
        "status": "active",
        "last_visit_at": NOW,
        "created_by": REQUESTER_SUB,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(over)
    return base


# ── create ──────────────────────────────────────────────────────────


def test_create_patient_201_and_audit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    captured: dict = {}

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        captured.update(kwargs)
        return _patient_row(name_uk=kwargs["name_uk"], name_en=kwargs["name_en"])

    monkeypatch.setattr(patients_repository, "create_patient", _create)

    resp = client.post(
        "/patients",
        json={
            "name": {"uk": "Іван Петренко", "en": "Ivan Petrenko"},
            "dob": "1980-01-15",
            "sex": "M",
            "mrn": "MRN-1",
            "tags": ["diabetes", " "],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"]["uk"] == "Іван Петренко"
    assert body["sex"] == "M"
    # blank tag dropped server-side
    assert captured["tags"] == ["diabetes"]
    assert captured["dob"] == date(1980, 1, 15)
    assert any(c["kind"] == "patient.created" for c in client.audit_calls)  # type: ignore[attr-defined]


def test_create_falls_back_to_en_name(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        # UA name was blank → service backfills it from EN.
        assert kwargs["name_uk"] == "Ivan"
        return _patient_row(name_uk="Ivan", name_en="Ivan")

    monkeypatch.setattr(patients_repository, "create_patient", _create)
    resp = client.post(
        "/patients", json={"name": {"uk": "", "en": "Ivan"}, "sex": "M"}
    )
    assert resp.status_code == 201


def test_create_requires_a_name(client: TestClient) -> None:
    resp = client.post("/patients", json={"name": {"uk": "", "en": ""}, "sex": "M"})
    assert resp.status_code == 422


def test_create_duplicate_mrn_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        raise asyncpg.UniqueViolationError("duplicate key value")

    monkeypatch.setattr(patients_repository, "create_patient", _create)
    resp = client.post(
        "/patients", json={"name": {"uk": "X", "en": "X"}, "mrn": "MRN-1", "sex": "M"}
    )
    assert resp.status_code == 409


def test_create_rejects_unknown_field(client: TestClient) -> None:
    # extra="forbid" on the wire model.
    resp = client.post(
        "/patients", json={"name": {"uk": "X"}, "sex": "M", "ssn": "123"}
    )
    assert resp.status_code == 422


# ── list / search ───────────────────────────────────────────────────


def test_list_paginates_with_cursor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    async def _list(conn, *, query, limit, cursor):  # noqa: ANN001
        # Repository fetches limit+1 to signal a next page.
        return [
            _patient_row(id=UUID(int=i), last_visit_at=datetime(2026, 6, i + 1, tzinfo=UTC))
            for i in range(limit + 1)
        ]

    monkeypatch.setattr(patients_repository, "list_patients", _list)
    resp = client.get("/patients?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"]


def test_list_no_next_cursor_when_exhausted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    async def _list(conn, *, query, limit, cursor):  # noqa: ANN001
        return [_patient_row()]

    monkeypatch.setattr(patients_repository, "list_patients", _list)
    body = client.get("/patients?limit=50").json()
    assert len(body["items"]) == 1
    assert body["next_cursor"] is None


# ── read / update ───────────────────────────────────────────────────


def test_get_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from core_service.domain import patients_repository

    async def _get(conn, *, patient_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(patients_repository, "get_patient", _get)
    assert client.get(f"/patients/{PATIENT_ID}").status_code == 404


def test_get_ok_audits_view(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    async def _get(conn, *, patient_id):  # noqa: ANN001
        return _patient_row()

    monkeypatch.setattr(patients_repository, "get_patient", _get)
    resp = client.get(f"/patients/{PATIENT_ID}")
    assert resp.status_code == 200
    assert resp.json()["mrn"] == "MRN-1"
    assert any(c["kind"] == "patient.viewed" for c in client.audit_calls)  # type: ignore[attr-defined]


def test_update_patient(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    seen: dict = {}

    async def _update(conn, *, patient_id, fields):  # noqa: ANN001
        seen.update(fields)
        return _patient_row(status="inactive", tags=["htn"])

    monkeypatch.setattr(patients_repository, "update_patient", _update)
    resp = client.put(
        f"/patients/{PATIENT_ID}",
        json={"status": "inactive", "tags": ["htn"]},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "inactive"
    assert seen["status"] == "inactive"
    assert seen["tags"] == ["htn"]


# ── timeline ────────────────────────────────────────────────────────


def test_timeline_returns_patient_reports(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository, timeline_repository

    async def _get(conn, *, patient_id):  # noqa: ANN001
        return _patient_row()

    async def _reports(conn, *, patient_id, limit=200):  # noqa: ANN001
        return [
            {
                "id": UUID(int=7),
                "title": "Chest CT",
                "code": "REP-2026-0007",
                "status": "finalized",
                "encounter_date": None,
                "created_at": datetime(2026, 6, 2, tzinfo=UTC),
                "updated_at": datetime(2026, 6, 4, tzinfo=UTC),
            }
        ]

    monkeypatch.setattr(patients_repository, "get_patient", _get)
    monkeypatch.setattr(timeline_repository, "list_patient_reports", _reports)

    resp = client.get(f"/patients/{PATIENT_ID}/timeline")
    assert resp.status_code == 200
    items = resp.json()["items"]
    # The SPA keys reports off kind == "dictate".
    assert items[0]["kind"] == "dictate"
    assert items[0]["title"] == "Chest CT"


def test_timeline_404_when_patient_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    async def _get(conn, *, patient_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(patients_repository, "get_patient", _get)
    assert client.get(f"/patients/{PATIENT_ID}/timeline").status_code == 404


# ── authz ───────────────────────────────────────────────────────────


def test_auditor_cannot_create(
    make_client: Callable[[list[str]], TestClient],
) -> None:
    auditor = make_client(["auditor"])
    resp = auditor.post(
        "/patients", json={"name": {"uk": "X", "en": "X"}, "sex": "M"}
    )
    assert resp.status_code == 403


def test_auditor_cannot_read(
    make_client: Callable[[list[str]], TestClient],
) -> None:
    auditor = make_client(["auditor"])
    assert auditor.get(f"/patients/{PATIENT_ID}").status_code == 403
