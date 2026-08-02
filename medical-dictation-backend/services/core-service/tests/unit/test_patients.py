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
        "phone": "+380671234567",
        "email": "ivan@example.com",
        "address_street": "вул. Шевченка",
        "address_house": "12, кв. 5",
        "address_zip": "01001",
        "address_city": "Київ",
        "address_country": "Україна",
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

    async def _list(conn, *, query, limit, cursor, **kwargs):  # noqa: ANN001, ANN003
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

    async def _list(conn, *, query, limit, cursor, **kwargs):  # noqa: ANN001, ANN003
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

    async def _get(conn, *, patient_id):  # noqa: ANN001
        return _patient_row()

    monkeypatch.setattr(patients_repository, "update_patient", _update)
    monkeypatch.setattr(patients_repository, "get_patient", _get)
    resp = client.put(
        f"/patients/{PATIENT_ID}",
        json={"status": "inactive", "tags": ["htn"]},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "inactive"
    assert seen["status"] == "inactive"
    assert seen["tags"] == ["htn"]


# ── contact details (0060) ──────────────────────────────────────────


ADDRESS = {
    "street": "вул. Шевченка",
    "house": "12, кв. 5",
    "zip": "01001",
    "city": "Київ",
    "country": "Україна",
}


def test_create_captures_contact_details(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    captured: dict = {}

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        captured.update(kwargs)
        return _patient_row()

    monkeypatch.setattr(patients_repository, "create_patient", _create)

    resp = client.post(
        "/patients",
        json={
            "name": {"uk": "Іван Петренко", "en": "Ivan Petrenko"},
            "sex": "M",
            "phone": "  +380 (67) 123-45-67 ",
            "email": "  Ivan@Example.COM ",
            "address": {**ADDRESS, "street": " вул. Шевченка "},
        },
    )
    assert resp.status_code == 201, resp.text
    # Trimmed on the way in; the phone loses its separators and the e-mail
    # is case-folded.
    assert captured["phone"] == "+380671234567"
    assert captured["email"] == "ivan@example.com"
    assert captured["address_street"] == "вул. Шевченка"
    assert captured["address_house"] == "12, кв. 5"
    assert captured["address_zip"] == "01001"
    assert captured["address_city"] == "Київ"
    assert captured["address_country"] == "Україна"

    body = resp.json()
    assert body["phone"] == "+380671234567"
    assert body["email"] == "ivan@example.com"
    assert body["address"] == ADDRESS


def test_create_contact_details_are_optional(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    captured: dict = {}

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        captured.update(kwargs)
        return _patient_row(
            phone="",
            email="",
            address_street="",
            address_house="",
            address_zip="",
            address_city="",
            address_country="",
        )

    monkeypatch.setattr(patients_repository, "create_patient", _create)
    resp = client.post("/patients", json={"name": {"uk": "X"}, "sex": "U"})
    assert resp.status_code == 201
    # "not captured" is the empty string, never NULL — matches the column default.
    assert captured["phone"] == ""
    assert captured["email"] == ""
    assert captured["address_street"] == ""
    assert captured["address_country"] == ""
    assert resp.json()["email"] == ""
    assert resp.json()["address"] == {
        "street": "",
        "house": "",
        "zip": "",
        "city": "",
        "country": "",
    }


def test_create_accepts_a_partial_address(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A city with no street is a legitimate half-captured address."""
    from core_service.domain import patients_repository

    captured: dict = {}

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        captured.update(kwargs)
        return _patient_row()

    monkeypatch.setattr(patients_repository, "create_patient", _create)
    resp = client.post(
        "/patients",
        json={"name": {"uk": "X"}, "sex": "U", "address": {"city": "Львів"}},
    )
    assert resp.status_code == 201
    assert captured["address_city"] == "Львів"
    assert captured["address_street"] == ""


@pytest.mark.parametrize(
    "bad", ["not-an-email", "no@domain", "@example.com", "user@", "a b@example.com"]
)
def test_create_rejects_malformed_email(client: TestClient, bad: str) -> None:
    resp = client.post(
        "/patients", json={"name": {"uk": "X"}, "sex": "U", "email": bad}
    )
    assert resp.status_code == 422
    assert resp.json().get("code") == "email_invalid"


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("+380671234567", "+380671234567"),
        ("+380 (67) 123-45-67", "+380671234567"),   # separators dropped
        ("+38 067 123 45 67", "+380671234567"),
        ("0671234567", "0671234567"),               # national form kept as typed
        ("044 123-45-67", "0441234567"),
        ("+48 22 123 45 67", "+48221234567"),       # a foreign number dials fine
    ],
)
def test_phone_is_normalized_to_e164_shape(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, raw: str, stored: str
) -> None:
    from core_service.domain import patients_repository

    captured: dict = {}

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        captured.update(kwargs)
        return _patient_row(phone=stored)

    monkeypatch.setattr(patients_repository, "create_patient", _create)
    resp = client.post(
        "/patients", json={"name": {"uk": "X"}, "sex": "U", "phone": raw}
    )
    assert resp.status_code == 201, resp.text
    assert captured["phone"] == stored


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-phone",
        "+380 67 ABC 45 67",
        "12345",                 # under the 7-digit floor
        "+1234567890123456",     # over the E.164 15-digit ceiling
        "+",
        "()-",
        "+380-67-123-45-6x",
    ],
)
def test_create_rejects_undiallable_phone(client: TestClient, bad: str) -> None:
    resp = client.post(
        "/patients", json={"name": {"uk": "X"}, "sex": "U", "phone": bad}
    )
    assert resp.status_code == 422
    assert resp.json().get("code") == "phone_invalid"


def test_create_audit_records_presence_not_contact_material(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contact details are PII: the audit payload carries flags, never values."""
    from core_service.domain import patients_repository

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        return _patient_row()

    monkeypatch.setattr(patients_repository, "create_patient", _create)
    resp = client.post(
        "/patients",
        json={
            "name": {"uk": "X"},
            "sex": "U",
            "phone": "+380671234567",
            "email": "ivan@example.com",
        },
    )
    assert resp.status_code == 201
    created = next(
        c for c in client.audit_calls if c["kind"] == "patient.created"  # type: ignore[attr-defined]
    )
    payload = created["payload"]
    assert payload["has_phone"] is True
    assert payload["has_email"] is True
    assert payload["has_address"] is False
    assert "+380671234567" not in str(payload)
    assert "ivan@example.com" not in str(payload)


def test_audit_address_flag_is_true_for_a_partial_address(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        return _patient_row()

    monkeypatch.setattr(patients_repository, "create_patient", _create)
    resp = client.post(
        "/patients",
        json={"name": {"uk": "X"}, "sex": "U", "address": {"city": "Львів"}},
    )
    assert resp.status_code == 201
    created = next(
        c for c in client.audit_calls if c["kind"] == "patient.created"  # type: ignore[attr-defined]
    )
    assert created["payload"]["has_address"] is True
    assert "Львів" not in str(created["payload"])


def test_update_contact_details_and_clearing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    seen: dict = {}

    async def _update(conn, *, patient_id, fields):  # noqa: ANN001
        seen.update(fields)
        return _patient_row(phone="+380509998877", email="")

    async def _get(conn, *, patient_id):  # noqa: ANN001
        return _patient_row()

    monkeypatch.setattr(patients_repository, "update_patient", _update)
    monkeypatch.setattr(patients_repository, "get_patient", _get)

    resp = client.put(
        f"/patients/{PATIENT_ID}",
        # phone replaced; email and the whole address cleared; mrn untouched.
        json={"phone": "+380 50 999 88 77", "email": "", "address": {}},
    )
    assert resp.status_code == 200
    assert seen["phone"] == "+380509998877"
    assert seen["email"] == ""
    # An address object always writes all five columns — a blank component
    # clears it, which is how the form removes a house number.
    assert seen["address_street"] == ""
    assert seen["address_house"] == ""
    assert seen["address_zip"] == ""
    assert seen["address_city"] == ""
    assert seen["address_country"] == ""
    assert "mrn" not in seen


def test_update_omitting_contact_leaves_columns_alone(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    seen: dict = {}

    async def _update(conn, *, patient_id, fields):  # noqa: ANN001
        seen.update(fields)
        return _patient_row()

    async def _get(conn, *, patient_id):  # noqa: ANN001
        return _patient_row()

    monkeypatch.setattr(patients_repository, "update_patient", _update)
    monkeypatch.setattr(patients_repository, "get_patient", _get)

    resp = client.put(f"/patients/{PATIENT_ID}", json={"tags": ["htn"]})
    assert resp.status_code == 200
    # None means "unchanged" — the columns must not appear in the SET list.
    assert "phone" not in seen
    assert "email" not in seen
    assert not [k for k in seen if k.startswith("address_")]


def test_update_rejects_malformed_email(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    async def _get(conn, *, patient_id):  # noqa: ANN001
        return _patient_row()

    monkeypatch.setattr(patients_repository, "get_patient", _get)
    resp = client.put(f"/patients/{PATIENT_ID}", json={"email": "bogus"})
    assert resp.status_code == 422
    assert resp.json().get("code") == "email_invalid"


def test_update_rejects_undiallable_phone(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    async def _get(conn, *, patient_id):  # noqa: ANN001
        return _patient_row()

    monkeypatch.setattr(patients_repository, "get_patient", _get)
    resp = client.put(f"/patients/{PATIENT_ID}", json={"phone": "call me"})
    assert resp.status_code == 422
    assert resp.json().get("code") == "phone_invalid"


def test_contact_fields_are_length_capped(client: TestClient) -> None:
    resp = client.post(
        "/patients",
        json={"name": {"uk": "X"}, "sex": "U", "phone": "0" * 33},
    )
    assert resp.status_code == 422
    resp = client.post(
        "/patients",
        json={"name": {"uk": "X"}, "sex": "U", "address": {"city": "К" * 121}},
    )
    assert resp.status_code == 422


def test_address_rejects_unknown_components(client: TestClient) -> None:
    """The address model is extra="forbid" like every other request model."""
    resp = client.post(
        "/patients",
        json={"name": {"uk": "X"}, "sex": "U", "address": {"region": "Київська"}},
    )
    assert resp.status_code == 422


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

    async def _recordings(conn, *, patient_id, limit=200):  # noqa: ANN001
        return []

    async def _conversations(conn, *, patient_id, limit=200):  # noqa: ANN001
        return []

    monkeypatch.setattr(patients_repository, "get_patient", _get)
    monkeypatch.setattr(timeline_repository, "list_patient_reports", _reports)
    monkeypatch.setattr(timeline_repository, "list_patient_recordings", _recordings)
    monkeypatch.setattr(timeline_repository, "list_patient_conversations", _conversations)

    resp = client.get(f"/patients/{PATIENT_ID}/timeline")
    assert resp.status_code == 200
    items = resp.json()["items"]
    # The SPA keys reports off kind == "dictate".
    assert items[0]["kind"] == "dictate"
    assert items[0]["title"] == "Chest CT"


def test_timeline_includes_recordings_metadata_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S11 step 02: encounter-linked recordings appear newest-first with
    metadata only — never a media URL."""
    from core_service.domain import patients_repository, timeline_repository

    encounter_id = UUID(int=99)

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
                "updated_at": datetime(2026, 6, 2, tzinfo=UTC),
            }
        ]

    async def _recordings(conn, *, patient_id, limit=200):  # noqa: ANN001
        return [
            {
                "id": UUID(int=8),
                "encounter_id": encounter_id,
                "duration_ms": 12_500,
                "status": "stored",
                "created_at": datetime(2026, 6, 3, tzinfo=UTC),
            }
        ]

    async def _conversations(conn, *, patient_id, limit=200):  # noqa: ANN001
        return []

    monkeypatch.setattr(patients_repository, "get_patient", _get)
    monkeypatch.setattr(timeline_repository, "list_patient_reports", _reports)
    monkeypatch.setattr(timeline_repository, "list_patient_recordings", _recordings)
    monkeypatch.setattr(timeline_repository, "list_patient_conversations", _conversations)

    items = client.get(f"/patients/{PATIENT_ID}/timeline").json()["items"]
    # Newest first: the recording (Jun 3) precedes the report (Jun 2).
    assert [i["kind"] for i in items] == ["recording", "dictate"]
    rec = items[0]
    assert rec["encounter_id"] == str(encounter_id)
    assert rec["duration_s"] == 12.5
    # Metadata only — no media/storage reference in the payload.
    assert not any("uri" in k or "url" in k for k in rec)


def test_timeline_includes_conversation_sessions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S14: a finished conversation must be reachable from the patient card.

    Before this, a conversation-mode consultation left only an opaque
    ``kind='recording'`` audio row — the transcript was persisted and
    there was no way to get to it from the record it belongs to.
    """
    from core_service.domain import patients_repository, timeline_repository

    encounter_id = UUID(int=99)
    session_id = UUID(int=11)

    async def _get(conn, *, patient_id):  # noqa: ANN001
        return _patient_row()

    async def _empty(conn, *, patient_id, limit=200):  # noqa: ANN001
        return []

    async def _conversations(conn, *, patient_id, limit=200):  # noqa: ANN001
        return [
            {
                "id": session_id,
                "encounter_id": encounter_id,
                "status": "finalized",
                "language": "uk",
                "total_audio_ms": 29_460,
                "segments": 11,
                "finalized_at": datetime(2026, 6, 5, tzinfo=UTC),
                "created_at": datetime(2026, 6, 5, tzinfo=UTC),
            }
        ]

    monkeypatch.setattr(patients_repository, "get_patient", _get)
    monkeypatch.setattr(timeline_repository, "list_patient_reports", _empty)
    monkeypatch.setattr(timeline_repository, "list_patient_recordings", _empty)
    monkeypatch.setattr(timeline_repository, "list_patient_conversations", _conversations)

    items = client.get(f"/patients/{PATIENT_ID}/timeline").json()["items"]
    assert [i["kind"] for i in items] == ["scribe"]
    conv = items[0]
    assert conv["id"] == str(session_id)
    assert conv["encounter_id"] == str(encounter_id)
    assert conv["duration_s"] == 29.46
    # A segment COUNT, never the transcript itself — the text stays on
    # dictation-service behind its own authz + audit.
    assert conv["segments"] == 11
    assert not any("transcript" in k or "text" in k for k in conv)


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
