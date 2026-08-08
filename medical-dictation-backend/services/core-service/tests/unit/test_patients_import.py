"""Behavioural tests for ``POST /patients/import`` — the bulk roster import.

The interesting behaviour is not "a good file lands"; it is what happens to a
file that is *partly* good, which is every file a clinic actually has. These
tests pin the three properties the endpoint promises: a bad row fails alone, a
duplicate never becomes a twin and never overwrites, and ``dry_run`` runs the
same decisions with no writes.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient

from tests.conftest import REQUESTER_SUB, TENANT_ID

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
EXISTING_ID = UUID("44444444-4444-4444-4444-444444444444")


def _row(**over: object) -> dict:
    base: dict = {
        "id": uuid4(),
        "tenant_id": TENANT_ID,
        "name_uk": "Іван Петренко",
        "name_en": "Ivan Petrenko",
        "dob": date(1980, 1, 15),
        "sex": "M",
        "mrn": "",
        "phone": "",
        "email": "",
        "address_street": "",
        "address_house": "",
        "address_zip": "",
        "address_city": "",
        "address_country": "",
        "summary_uk": "",
        "summary_en": "",
        "tags": [],
        "status": "active",
        "last_visit_at": None,
        "created_by": REQUESTER_SUB,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(over)
    return base


class _FakeTx:
    """Stands in for the per-row SAVEPOINT."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False  # never swallow — the handler owns the decision


class _FakeConn:
    def transaction(self) -> _FakeTx:
        return _FakeTx()


@pytest.fixture
def client(make_client: Callable[[list[str]], TestClient], monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Clinician client whose tenant_connection yields a connection that can
    open (fake) savepoints — the shared fixture yields ``None``."""
    from core_service.routers import patients

    @contextlib.asynccontextmanager
    async def _conn(pool, tenant_id):  # noqa: ANN001
        yield _FakeConn()

    monkeypatch.setattr(patients, "tenant_connection", _conn)
    return make_client(["clinician"])


@pytest.fixture(autouse=True)
def _no_duplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: an empty roster. Individual tests override the lookups."""
    from core_service.domain import patients_repository

    async def _none(conn, **kwargs):  # noqa: ANN001, ANN003
        return None

    monkeypatch.setattr(patients_repository, "find_patient_id_by_mrn", _none)
    monkeypatch.setattr(patients_repository, "find_patient_id_by_ipn_hmac", _none)


def _stub_create(monkeypatch: pytest.MonkeyPatch, calls: list[dict]) -> None:
    from core_service.domain import patients_repository

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        calls.append(kwargs)
        return _row(
            id=kwargs["patient_id"], name_uk=kwargs["name_uk"], name_en=kwargs["name_en"]
        )

    monkeypatch.setattr(patients_repository, "create_patient", _create)


def _body(*items: dict, **opts: object) -> dict:
    return {"items": list(items), **opts}


def _person(name: str, **over: object) -> dict:
    item: dict = {"name": {"uk": name, "en": name}}
    item.update(over)
    return item


# ── happy path ──────────────────────────────────────────────────────


def test_import_creates_every_valid_row_and_audits(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    _stub_create(monkeypatch, calls)

    resp = client.post(
        "/patients/import",
        json=_body(
            _person("Іван Петренко", mrn="MRN-1", dob="1980-01-15", sex="M"),
            _person("Олена Ковальчук", phone="+380 (67) 123-45-67"),
        ),
    )

    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert (out["total"], out["created"], out["skipped"], out["failed"]) == (2, 2, 0, 0)
    assert [r["status"] for r in out["rows"]] == ["created", "created"]
    assert all(r["patient_id"] for r in out["rows"])
    assert len(calls) == 2
    # The phone is stored normalized, exactly as the single-create path does.
    assert calls[1]["phone"] == "+380671234567"

    kinds = [c["kind"] for c in client.audit_calls]  # type: ignore[attr-defined]
    # Per-record trail AND one event for the run.
    assert kinds.count("patient.created") == 2
    assert kinds.count("patient.imported") == 1
    run = next(c for c in client.audit_calls if c["kind"] == "patient.imported")  # type: ignore[attr-defined]
    assert run["payload"] == {
        "total": 2,
        "created": 2,
        "skipped": 0,
        "failed": 0,
        "dry_run": False,
    }


def test_import_audit_payload_carries_no_phi(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Names, MRNs and contact details must never reach the audit chain —
    only presence flags, same as the single create."""
    _stub_create(monkeypatch, [])

    client.post(
        "/patients/import",
        json=_body(_person("Іван Петренко", mrn="MRN-9", email="ivan@example.com")),
    )

    for call in client.audit_calls:  # type: ignore[attr-defined]
        blob = repr(call["payload"])
        assert "Іван" not in blob
        assert "MRN-9" not in blob
        assert "ivan@example.com" not in blob


# ── partial success ─────────────────────────────────────────────────


def test_a_bad_row_fails_alone(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    _stub_create(monkeypatch, calls)

    resp = client.post(
        "/patients/import",
        json=_body(
            _person("Добрий Рядок"),
            _person("Поганий Телефон", phone="not-a-number"),
            {"name": {"uk": "  ", "en": ""}},
            _person("Погана Пошта", email="broken.example.com"),
            _person("Ще Один Добрий"),
        ),
    )

    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert (out["created"], out["failed"]) == (2, 3)
    assert [r["status"] for r in out["rows"]] == [
        "created",
        "failed",
        "failed",
        "failed",
        "created",
    ]
    # The row index points at the spreadsheet line, and the code is the same
    # vocabulary the single-create 422 uses.
    assert [(r["index"], r["code"]) for r in out["rows"] if r["status"] == "failed"] == [
        (1, "phone_invalid"),
        (2, "name_required"),
        (3, "email_invalid"),
    ]
    # Nothing was written for the failures.
    assert len(calls) == 2


# ── duplicates ──────────────────────────────────────────────────────


def test_existing_mrn_is_skipped_and_points_at_the_record(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    calls: list[dict] = []
    _stub_create(monkeypatch, calls)

    async def _by_mrn(conn, *, mrn):  # noqa: ANN001
        return EXISTING_ID if mrn == "MRN-1" else None

    monkeypatch.setattr(patients_repository, "find_patient_id_by_mrn", _by_mrn)

    resp = client.post(
        "/patients/import",
        json=_body(_person("Вже У Базі", mrn="MRN-1"), _person("Новий", mrn="MRN-2")),
    )

    out = resp.json()
    assert (out["created"], out["skipped"], out["failed"]) == (1, 1, 0)
    dup = out["rows"][0]
    assert dup["status"] == "skipped"
    assert dup["code"] == "mrn_exists"
    assert dup["existing_patient_id"] == str(EXISTING_ID)
    # The existing record is never touched: only the second row was written.
    assert [c["mrn"] for c in calls] == ["MRN-2"]


def test_on_duplicate_fail_reports_the_row_as_failed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    _stub_create(monkeypatch, [])

    async def _by_mrn(conn, *, mrn):  # noqa: ANN001
        return EXISTING_ID

    monkeypatch.setattr(patients_repository, "find_patient_id_by_mrn", _by_mrn)

    resp = client.post(
        "/patients/import",
        json=_body(_person("Вже У Базі", mrn="MRN-1"), on_duplicate="fail"),
    )

    out = resp.json()
    assert (out["created"], out["skipped"], out["failed"]) == (0, 0, 1)
    assert out["rows"][0]["status"] == "failed"
    assert out["rows"][0]["code"] == "mrn_exists"


def test_duplicate_inside_the_file_is_caught_before_the_index(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two copies of one patient in the same upload: the second is reported
    against the first row, not written and then rejected by the DB."""
    calls: list[dict] = []
    _stub_create(monkeypatch, calls)

    resp = client.post(
        "/patients/import",
        json=_body(
            _person("Іван Петренко", mrn="MRN-7"),
            _person("Іван Петренко", mrn="MRN-7"),
        ),
    )

    out = resp.json()
    assert (out["created"], out["skipped"]) == (1, 1)
    assert out["rows"][1]["code"] == "duplicate_in_batch"
    assert "row 0" in out["rows"][1]["message"]
    assert len(calls) == 1


def test_unique_violation_races_are_absorbed_per_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicate the pre-flight lookup could not see (a concurrent import)
    rolls back its own SAVEPOINT and leaves the rest of the batch alive."""
    from core_service.domain import patients_repository

    written: list[str] = []

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        if kwargs["mrn"] == "RACED":
            raise asyncpg.UniqueViolationError("duplicate key")
        written.append(kwargs["mrn"])
        return _row(id=kwargs["patient_id"])

    monkeypatch.setattr(patients_repository, "create_patient", _create)

    resp = client.post(
        "/patients/import",
        json=_body(
            _person("A", mrn="A-1"),
            _person("B", mrn="RACED"),
            _person("C", mrn="C-1"),
        ),
    )

    out = resp.json()
    assert (out["created"], out["skipped"]) == (2, 1)
    assert out["rows"][1]["code"] == "mrn_exists"
    assert written == ["A-1", "C-1"]


# ── dry run ─────────────────────────────────────────────────────────


def test_dry_run_decides_everything_and_writes_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    calls: list[dict] = []
    _stub_create(monkeypatch, calls)

    async def _by_mrn(conn, *, mrn):  # noqa: ANN001
        return EXISTING_ID if mrn == "MRN-1" else None

    monkeypatch.setattr(patients_repository, "find_patient_id_by_mrn", _by_mrn)

    resp = client.post(
        "/patients/import",
        json=_body(
            _person("Новий"),
            _person("Вже У Базі", mrn="MRN-1"),
            _person("Поганий", phone="nope"),
            dry_run=True,
        ),
    )

    out = resp.json()
    assert out["dry_run"] is True
    assert (out["created"], out["skipped"], out["failed"]) == (0, 1, 1)
    assert [r["status"] for r in out["rows"]] == ["valid", "skipped", "failed"]
    assert calls == []
    # The preview is still recorded — an import that was rehearsed against
    # the roster read patient data.
    run = next(c for c in client.audit_calls if c["kind"] == "patient.imported")  # type: ignore[attr-defined]
    assert run["payload"]["dry_run"] is True


# ── guards ──────────────────────────────────────────────────────────


def test_batch_over_the_ceiling_is_rejected_whole(client: TestClient) -> None:
    from core_service.config import settings

    resp = client.post(
        "/patients/import",
        json=_body(*[_person(f"P{i}") for i in range(settings.patient_import_max_rows + 1)]),
    )

    assert resp.status_code == 422
    problem = resp.json()
    assert problem["code"] == "import_too_large"
    assert problem["max_rows"] == settings.patient_import_max_rows


def test_empty_batch_is_rejected(client: TestClient) -> None:
    assert client.post("/patients/import", json={"items": []}).status_code == 422


def test_unknown_field_in_a_row_is_rejected(client: TestClient) -> None:
    """extra="forbid" is inherited: a spreadsheet column nobody mapped must
    not be silently dropped on the floor."""
    resp = client.post(
        "/patients/import",
        json=_body({"name": {"uk": "Іван", "en": "Ivan"}, "insurance_no": "X"}),
    )
    assert resp.status_code == 422


def test_import_requires_patient_write(
    make_client: Callable[[list[str]], TestClient],
) -> None:
    auditor = make_client(["auditor"])
    resp = auditor.post("/patients/import", json=_body(_person("Іван")))
    assert resp.status_code == 403
