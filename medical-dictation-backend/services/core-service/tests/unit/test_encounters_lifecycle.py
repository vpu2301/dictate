"""Encounter (visit) lifecycle — state machine + the /encounters verbs.

The gap these cover: before 0058 a visit opened by the SPA could never be
closed, so the pipeline filled with visits that were long over.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from core_service.domain import encounter_state
from tests.conftest import REQUESTER_SUB, TENANT_ID

PATIENT_ID = UUID("33333333-3333-3333-3333-333333333333")
ENCOUNTER_ID = UUID("55555555-5555-5555-5555-555555555555")
NOW = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)


def _enc_row(**over: object) -> dict:
    base: dict = {
        "id": ENCOUNTER_ID,
        "tenant_id": TENANT_ID,
        "patient_id": PATIENT_ID,
        "kind": "visit",
        "reason": "cough",
        "occurred_at": NOW,
        "status": "in_progress",
        "created_by": REQUESTER_SUB,
        "created_at": NOW,
        "started_at": NOW,
        "ended_at": None,
        "updated_at": NOW,
    }
    base.update(over)
    return base


def _queue_row(**over: object) -> dict:
    """A worklist row — the encounter plus the joined patient."""
    return _enc_row(
        patient_name_uk="Іваненко Іван",
        patient_name_en="Ivan Ivanenko",
        patient_mrn="MRN-7",
        patient_dob=None,
        patient_sex="M",
        **over,
    )


# ── state machine ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("current", "action"),
    [
        ("scheduled", "start"),
        ("scheduled", "cancel"),
        ("in_progress", "pause"),
        ("in_progress", "complete"),
        ("in_progress", "cancel"),
        ("paused", "resume"),
        ("paused", "complete"),
        ("paused", "cancel"),
    ],
)
def test_legal_transitions(current: str, action: str) -> None:
    assert encounter_state.transition_error(current, action) is None


@pytest.mark.parametrize(
    ("current", "action"),
    [
        ("completed", "complete"),
        ("completed", "pause"),
        ("completed", "resume"),
        ("cancelled", "start"),
        ("cancelled", "complete"),
        ("scheduled", "pause"),
        ("scheduled", "resume"),
        ("in_progress", "resume"),
        ("paused", "pause"),
        ("in_progress", "start"),
    ],
)
def test_illegal_transitions_are_refused_with_a_reason(current: str, action: str) -> None:
    problem = encounter_state.transition_error(current, action)
    assert problem, f"{current} --{action}--> should be refused"
    assert current in problem


def test_terminal_states_have_no_way_out() -> None:
    for terminal in ("completed", "cancelled"):
        assert encounter_state.is_terminal(terminal)
        for target in ("scheduled", "in_progress", "paused", "completed", "cancelled"):
            assert not encounter_state.can_transition(terminal, target)


def test_open_statuses_are_exactly_the_pipeline_holders() -> None:
    assert {"in_progress", "paused"} == encounter_state.OPEN_STATUSES


# ── endpoints ───────────────────────────────────────────────────────


def _patch_repo(
    monkeypatch: pytest.MonkeyPatch,
    *,
    row: dict,
    live_sessions: int = 0,
    captured: dict | None = None,
) -> None:
    from core_service.domain import encounters_repository, patients_repository

    async def _get(conn, *, encounter_id):  # noqa: ANN001
        return row

    async def _count(conn, *, encounter_id, stale_after_seconds):  # noqa: ANN001
        return live_sessions

    async def _update(conn, *, encounter_id, expected_status, new_status, now):  # noqa: ANN001
        if captured is not None:
            captured["expected_status"] = expected_status
            captured["new_status"] = new_status
        if expected_status != row["status"]:
            return None
        return _enc_row(
            status=new_status,
            ended_at=now if new_status in ("completed", "cancelled") else None,
            updated_at=now,
        )

    async def _bump(conn, *, patient_id, when):  # noqa: ANN001
        if captured is not None:
            captured["bumped"] = patient_id

    monkeypatch.setattr(encounters_repository, "get_encounter", _get)
    monkeypatch.setattr(encounters_repository, "count_live_sessions", _count)
    monkeypatch.setattr(encounters_repository, "update_lifecycle", _update)
    monkeypatch.setattr(patients_repository, "bump_last_visit", _bump)


def test_complete_ends_the_visit_bumps_last_visit_and_audits(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}
    _patch_repo(monkeypatch, row=_enc_row(), captured=captured)

    resp = client.post(f"/encounters/{ENCOUNTER_ID}/complete", json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["ended_at"] is not None
    # The visit being over is what stamps last-visit, not the moment it
    # was created.
    assert captured["bumped"] == PATIENT_ID
    kinds = [c["kind"] for c in client.audit_calls]  # type: ignore[attr-defined]
    assert "encounter.completed" in kinds


def test_pause_then_resume_round_trip(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_repo(monkeypatch, row=_enc_row(status="in_progress"))
    paused = client.post(f"/encounters/{ENCOUNTER_ID}/pause", json={})
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    # A paused visit is still open: ended_at stays clear.
    assert paused.json()["ended_at"] is None

    _patch_repo(monkeypatch, row=_enc_row(status="paused"))
    resumed = client.post(f"/encounters/{ENCOUNTER_ID}/resume", json={})
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "in_progress"


def test_completing_a_completed_visit_is_409_not_a_second_audit_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_repo(monkeypatch, row=_enc_row(status="completed", ended_at=NOW))
    resp = client.post(f"/encounters/{ENCOUNTER_ID}/complete", json={})
    assert resp.status_code == 409
    assert "completed" in resp.json()["detail"]


def test_live_recording_blocks_ending_the_visit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_repo(monkeypatch, row=_enc_row(), live_sessions=1)
    resp = client.post(f"/encounters/{ENCOUNTER_ID}/complete", json={})
    assert resp.status_code == 409
    assert "still live" in resp.json()["detail"]


def test_force_ends_the_visit_over_a_live_recording_and_records_the_override(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_repo(monkeypatch, row=_enc_row(), live_sessions=2)
    resp = client.post(f"/encounters/{ENCOUNTER_ID}/complete", json={"force": True})
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    forced = [
        c
        for c in client.audit_calls  # type: ignore[attr-defined]
        if c["kind"] == "encounter.completed"
    ]
    assert forced[-1]["payload"]["forced_over_live_sessions"] == 2


def test_a_stale_session_does_not_wedge_the_visit_open(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """count_live_sessions windows on last_active_at, so a session stranded
    by a dead worker reports zero and the clinician can still close up."""
    _patch_repo(monkeypatch, row=_enc_row(), live_sessions=0)
    resp = client.post(f"/encounters/{ENCOUNTER_ID}/complete", json={})
    assert resp.status_code == 200


def test_cancel_does_not_bump_last_visit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}
    _patch_repo(monkeypatch, row=_enc_row(), captured=captured)
    resp = client.post(f"/encounters/{ENCOUNTER_ID}/cancel", json={"reason": "no-show"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert "bumped" not in captured
    payloads = [
        c["payload"]
        for c in client.audit_calls  # type: ignore[attr-defined]
        if c["kind"] == "encounter.cancelled"
    ]
    assert payloads[-1]["reason"] == "no-show"


def test_lost_cas_is_a_409(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two clinicians ending the same visit: one winner, one 409."""
    from core_service.domain import encounters_repository

    _patch_repo(monkeypatch, row=_enc_row())

    async def _update_loses(conn, **kwargs):  # noqa: ANN001, ANN003
        return None

    monkeypatch.setattr(encounters_repository, "update_lifecycle", _update_loses)
    resp = client.post(f"/encounters/{ENCOUNTER_ID}/complete", json={})
    assert resp.status_code == 409
    assert "concurrently" in resp.json()["detail"]


def test_transition_cas_uses_the_status_we_validated(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}
    _patch_repo(monkeypatch, row=_enc_row(status="paused"), captured=captured)
    client.post(f"/encounters/{ENCOUNTER_ID}/complete", json={})
    assert captured["expected_status"] == "paused"
    assert captured["new_status"] == "completed"


def test_missing_encounter_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.domain import encounters_repository

    async def _none(conn, *, encounter_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(encounters_repository, "get_encounter", _none)
    resp = client.post(f"/encounters/{ENCOUNTER_ID}/complete", json={})
    assert resp.status_code == 404


def test_open_list_is_not_shadowed_by_the_id_route(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/encounters/open`` must not be parsed as an encounter UUID — route
    declaration order is the only thing keeping that true."""
    from core_service.domain import encounters_repository

    seen: dict = {}

    async def _list_open(conn, *, created_by, limit):  # noqa: ANN001
        seen["created_by"] = created_by
        return [_queue_row(status="paused")]

    monkeypatch.setattr(encounters_repository, "list_open", _list_open)

    resp = client.get("/encounters/open")
    assert resp.status_code == 200
    assert [e["status"] for e in resp.json()] == ["paused"]
    assert seen["created_by"] == REQUESTER_SUB

    resp_all = client.get("/encounters/open?mine=false")
    assert resp_all.status_code == 200
    assert seen["created_by"] is None


def test_worklist_rows_carry_the_patient_inline(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nameless row is not a worklist a clinician can act on, and one
    fetch per row is an N+1 the roster already avoids."""
    from core_service.domain import encounters_repository

    async def _list_open(conn, *, created_by, limit):  # noqa: ANN001
        return [_queue_row()]

    async def _list_schedule(conn, *, day_start, day_end):  # noqa: ANN001
        return [_queue_row(status="scheduled", started_at=None)]

    monkeypatch.setattr(encounters_repository, "list_open", _list_open)
    monkeypatch.setattr(encounters_repository, "list_schedule", _list_schedule)

    for path in ("/encounters/open", "/schedule"):
        row = client.get(path).json()[0]
        assert row["patient"]["id"] == str(PATIENT_ID)
        assert row["patient"]["name"] == {"uk": "Іваненко Іван", "en": "Ivan Ivanenko"}
        assert row["patient"]["mrn"] == "MRN-7"


def test_reader_role_cannot_end_a_visit(
    make_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ending a visit is a write; a read-only role must be refused."""
    _patch_repo(monkeypatch, row=_enc_row())
    viewer = make_client(["auditor"])
    resp = viewer.post(f"/encounters/{ENCOUNTER_ID}/complete", json={})
    assert resp.status_code == 403
