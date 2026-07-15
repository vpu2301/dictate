"""S11 step 04 — two-person erasure workflow (service layer)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from audit import Severity
from tests.conftest import REQUESTER_SUB, TENANT_ID

PATIENT_ID = UUID("33333333-3333-3333-3333-333333333333")
REQUEST_ID = UUID("77777777-7777-7777-7777-777777777777")
OTHER_ADMIN = UUID("88888888-8888-8888-8888-888888888888")
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _request_row(**over: object) -> dict:
    base: dict = {
        "id": REQUEST_ID,
        "tenant_id": TENANT_ID,
        "patient_id": PATIENT_ID,
        "kind": "erasure",
        "reason": "patient asked",
        "status": "requested",
        "requested_by": OTHER_ADMIN,
        "requested_at": NOW,
        "scheduled_for": None,
        "reviewed_by": None,
        "reviewed_at": None,
        "rejection_reason": None,
        "executing_at": None,
        "completed_at": None,
        "report_of_execution": None,
    }
    base.update(over)
    return base


def _stub(monkeypatch: pytest.MonkeyPatch, name: str, result: dict | None) -> dict:
    from core_service.domain import privacy_repository

    captured: dict = {}

    async def _fn(conn, **kwargs):  # noqa: ANN001, ANN003
        captured.update(kwargs)
        return result

    monkeypatch.setattr(privacy_repository, name, _fn)
    return captured


def _stub_get(monkeypatch: pytest.MonkeyPatch, row: dict | None) -> None:
    from core_service.domain import privacy_repository

    async def _get(conn, *, request_id):  # noqa: ANN001
        return row

    monkeypatch.setattr(privacy_repository, "get_request", _get)


# ── request creation ────────────────────────────────────────────────


def test_erasure_request_starts_at_requested(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import patients_repository

    async def _get_patient(conn, *, patient_id):  # noqa: ANN001
        return {"id": patient_id}

    monkeypatch.setattr(patients_repository, "get_patient", _get_patient)
    captured = _stub(
        monkeypatch, "create_request", _request_row(requested_by=REQUESTER_SUB)
    )

    resp = client.post(f"/patients/{PATIENT_ID}/erasure", json={"reason": "GDPR art 17"})
    assert resp.status_code == 201, resp.text
    assert captured["status"] == "requested"
    assert captured["scheduled_for"] is None
    kinds = [c["kind"] for c in client.audit_calls]  # type: ignore[attr-defined]
    assert "privacy.erasure_requested" in kinds


# ── two-person rule ─────────────────────────────────────────────────


def test_requester_cannot_approve_own_request(
    make_client: Callable[[list[str]], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = make_client(["tenant_admin"])
    # The request was filed by the SAME sub the client authenticates as.
    _stub_get(monkeypatch, _request_row(requested_by=REQUESTER_SUB))

    resp = admin.post(f"/privacy-requests/{REQUEST_ID}/approve")
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "two_person_rule"


def test_requester_cannot_reject_own_request(
    make_client: Callable[[list[str]], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = make_client(["tenant_admin"])
    _stub_get(monkeypatch, _request_row(requested_by=REQUESTER_SUB))
    resp = admin.post(
        f"/privacy-requests/{REQUEST_ID}/reject", json={"rejection_reason": "dup"}
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "two_person_rule"


def test_second_admin_approves_with_grace(
    make_client: Callable[[list[str]], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.config import settings

    admin = make_client(["tenant_admin"])
    _stub_get(monkeypatch, _request_row(requested_by=OTHER_ADMIN))
    approved = _request_row(
        requested_by=OTHER_ADMIN,
        status="approved",
        reviewed_by=REQUESTER_SUB,
        reviewed_at=NOW,
        scheduled_for=NOW + timedelta(days=7),
    )
    captured = _stub(monkeypatch, "approve", approved)

    resp = admin.post(f"/privacy-requests/{REQUEST_ID}/approve")
    assert resp.status_code == 200, resp.text
    # Grace: scheduled_for ≈ now + ERASURE_GRACE_DAYS (config).
    delta = captured["scheduled_for"] - datetime.now(UTC)
    assert timedelta(days=settings.erasure_grace_days, minutes=-5) < delta
    assert delta <= timedelta(days=settings.erasure_grace_days)
    # SEC-severity audit with the schedule in the payload.
    approvals = [c for c in admin.audit_calls if c["kind"] == "privacy.erasure_approved"]  # type: ignore[attr-defined]
    assert approvals and approvals[-1]["severity"] is Severity.SEC
    assert approvals[-1]["payload"]["grace_days"] == settings.erasure_grace_days


# ── permission gate ─────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["clinician", "nurse", "auditor"])
def test_approve_requires_privacy_approve_permission(
    make_client: Callable[[list[str]], TestClient],
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    non_admin = make_client([role])
    _stub_get(monkeypatch, _request_row(requested_by=OTHER_ADMIN))
    resp = non_admin.post(f"/privacy-requests/{REQUEST_ID}/approve")
    assert resp.status_code == 403, resp.text


# ── transition matrix ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "endpoint, from_status",
    [
        ("review", "approved"),
        ("review", "completed"),
        ("review", "rejected"),
        ("approve", "executing"),
        ("approve", "completed"),
        ("approve", "rejected"),
        ("reject", "executing"),
        ("reject", "completed"),
        ("reject", "rejected"),
    ],
)
def test_illegal_transitions_409(
    make_client: Callable[[list[str]], TestClient],
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    from_status: str,
) -> None:
    admin = make_client(["tenant_admin"])
    _stub_get(monkeypatch, _request_row(requested_by=OTHER_ADMIN, status=from_status))
    for name in ("mark_review", "approve", "reject"):
        _stub(monkeypatch, name, None)  # repo refuses: not in the legal from-set

    body = {"rejection_reason": "r"} if endpoint == "reject" else None
    resp = admin.post(f"/privacy-requests/{REQUEST_ID}/{endpoint}", json=body)
    assert resp.status_code == 409, resp.text
    payload = resp.json()
    assert payload["code"] == "invalid_transition"
    assert payload["current_status"] == from_status


def test_reject_requires_written_reason(
    make_client: Callable[[list[str]], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = make_client(["tenant_admin"])
    _stub_get(monkeypatch, _request_row(requested_by=OTHER_ADMIN))
    resp = admin.post(f"/privacy-requests/{REQUEST_ID}/reject", json={"rejection_reason": "  "})
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "rejection_reason_required"


def test_reject_during_grace_cancels_approved(
    make_client: Callable[[list[str]], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = make_client(["tenant_admin"])
    _stub_get(
        monkeypatch,
        _request_row(
            requested_by=OTHER_ADMIN,
            status="approved",
            reviewed_by=uuid4(),
            reviewed_at=NOW,
            scheduled_for=NOW + timedelta(days=7),
        ),
    )
    rejected = _request_row(
        requested_by=OTHER_ADMIN,
        status="rejected",
        reviewed_by=REQUESTER_SUB,
        reviewed_at=NOW,
        rejection_reason="patient changed their mind",
    )
    _stub(monkeypatch, "reject", rejected)
    resp = admin.post(
        f"/privacy-requests/{REQUEST_ID}/reject",
        json={"rejection_reason": "patient changed their mind"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"


# ── engine states are closed to HTTP ────────────────────────────────


@pytest.mark.parametrize("engine_state", ["executing", "completed", "failed"])
def test_engine_transitions_have_no_http_surface(
    client: TestClient, engine_state: str
) -> None:
    resp = client.post(f"/privacy-requests/{REQUEST_ID}/{engine_state}")
    assert resp.status_code in (404, 405), resp.text
