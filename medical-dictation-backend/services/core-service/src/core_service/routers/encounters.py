"""Encounters — per-patient visit history, single-encounter read, the day's
scheduled-visit queue, and the visit lifecycle (start/pause/resume/end).

Until 0058 a visit's status was write-once at INSERT: the SPA opened one as
``in_progress`` and nothing could ever move it out, so the pipeline filled
with visits that were long over. The ``POST /encounters/{id}/{verb}``
surface below is the missing half — one endpoint per clinical action, each
guarded by :mod:`..domain.encounter_state` and each emitting its own audit
kind.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict

from auth import Claims
from db import tenant_connection

from .. import audit_helper, audit_kinds
from ..config import settings
from ..deps import get_state, requires
from ..domain import encounter_state, encounters_repository, patients_repository
from ._phi_access_guard import PatientAccess, patient_record_access

router = APIRouter(tags=["encounters"])

EncounterKind = Literal["visit", "phone", "video", "scribe", "followup", "other"]

EncounterAction = Literal["start", "pause", "resume", "complete", "cancel"]

_ACTION_AUDIT_KIND: dict[str, str] = {
    "start": audit_kinds.ENCOUNTER_STARTED,
    "pause": audit_kinds.ENCOUNTER_PAUSED,
    "resume": audit_kinds.ENCOUNTER_RESUMED,
    "complete": audit_kinds.ENCOUNTER_COMPLETED,
    "cancel": audit_kinds.ENCOUNTER_CANCELLED,
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EncounterCreate(_Strict):
    kind: EncounterKind = "visit"
    datetime: str | None = None  # ISO 8601; defaults to now()
    reason: str = ""
    status: Literal[
        "scheduled", "in_progress", "paused", "completed", "cancelled"
    ] = "completed"


class EncounterTransition(_Strict):
    """Body for a lifecycle verb. All fields optional — a bare ``{}`` works."""

    reason: str | None = None
    #: End the visit even though a dictation session on it is still live.
    #: The clinician has been told what they are abandoning; the override is
    #: recorded in the audit payload.
    force: bool = False


class EncounterOut(_Strict):
    id: UUID
    patient_id: UUID
    kind: str
    reason: str
    occurred_at: datetime
    status: str
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    updated_at: datetime | None = None


class PatientBrief(_Strict):
    """Just enough patient to render a worklist row."""

    id: UUID
    name: dict[str, str]
    mrn: str = ""
    dob: date | None = None
    sex: str = "U"


class QueueEncounterOut(EncounterOut):
    """Encounter as it appears in a worklist (schedule / open visits).

    The queue surfaces carry the patient inline. Without it the SPA rendered
    nameless rows — and a nameless row is not a worklist a clinician can act
    on. Joined server-side rather than fetched per row.
    """

    patient: PatientBrief | None = None


def _to_out(row: asyncpg.Record) -> EncounterOut:
    return EncounterOut(
        id=row["id"],
        patient_id=row["patient_id"],
        kind=row["kind"],
        reason=row["reason"],
        occurred_at=row["occurred_at"],
        status=row["status"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        updated_at=row["updated_at"],
    )


def _to_queue_out(row: asyncpg.Record) -> QueueEncounterOut:
    base = _to_out(row)
    return QueueEncounterOut(
        **base.model_dump(),
        patient=PatientBrief(
            id=row["patient_id"],
            # Same {uk, en} shape the roster serves, so the SPA's existing
            # name resolver works unchanged.
            name={"uk": row["patient_name_uk"], "en": row["patient_name_en"]},
            mrn=row["patient_mrn"],
            dob=row["patient_dob"],
            sex=row["patient_sex"],
        ),
    )


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid datetime {value!r}",
        ) from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@router.get(
    "/patients/{patient_id}/encounters",
    response_model=list[EncounterOut],
    summary="Encounter history for a patient.",
)
async def list_encounters(
    patient_id: UUID,
    # One patient's visit history is record content (S15 gate); the
    # cross-tenant workflow lists (`/encounters/open`, `/schedule`) stay
    # on the roster permission — they are the admin's operational view.
    access: Annotated[PatientAccess, Depends(patient_record_access)],
) -> list[EncounterOut]:
    claims = access.claims
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await encounters_repository.list_for_patient(conn, patient_id=patient_id)
    return [_to_out(r) for r in rows]


@router.post(
    "/patients/{patient_id}/encounters",
    response_model=EncounterOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record an encounter; bumps the patient's last-visit.",
)
async def create_encounter(
    patient_id: UUID,
    body: EncounterCreate,
    claims: Annotated[Claims, Depends(requires("patient.write", "patient"))],
) -> EncounterOut:
    occurred_at = _parse_dt(body.datetime)
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        if await patients_repository.get_patient(conn, patient_id=patient_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        row = await encounters_repository.create_encounter(
            conn,
            tenant_id=claims.tid,
            patient_id=patient_id,
            created_by=claims.sub,
            kind=body.kind,
            reason=body.reason.strip(),
            occurred_at=occurred_at,
            status=body.status,
        )
        # Only a retro-logged, already-finished visit bumps last-visit here.
        # An open visit gets its bump when it is completed (0058) — before
        # the lifecycle existed, create-time was the only chance we had.
        if body.status == encounter_state.COMPLETED:
            await patients_repository.bump_last_visit(
                conn, patient_id=patient_id, when=occurred_at
            )
    await audit_helper.emit(
        state,
        claims,
        audit_kinds.ENCOUNTER_CREATED,
        target_kind="patient",
        target_id=patient_id,
        payload={"encounter_id": str(row["id"]), "kind": body.kind},
    )
    return _to_out(row)


@router.get(
    "/encounters/open",
    response_model=list[QueueEncounterOut],
    summary="Visits still open (in_progress | paused) — the clinician's pipeline.",
)
async def list_open_encounters(
    claims: Annotated[Claims, Depends(requires("patient.read", "patient"))],
    mine: Annotated[bool, Query()] = True,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[QueueEncounterOut]:
    """Declared before ``/encounters/{encounter_id}``: FastAPI matches routes
    in declaration order, and the parametrised one would swallow ``open``.

    ``mine=false`` widens to every open visit in the tenant — the view a
    tenant_admin needs to find visits colleagues left hanging.
    """
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await encounters_repository.list_open(
            conn, created_by=claims.sub if mine else None, limit=limit
        )
    return [_to_queue_out(r) for r in rows]


@router.get(
    "/encounters/{encounter_id}",
    response_model=EncounterOut,
    summary="Fetch one encounter.",
)
async def get_encounter(
    encounter_id: UUID,
    claims: Annotated[Claims, Depends(requires("patient.read", "patient"))],
) -> EncounterOut:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await encounters_repository.get_encounter(conn, encounter_id=encounter_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _to_out(row)


async def _apply_transition(
    *,
    encounter_id: UUID,
    action: EncounterAction,
    body: EncounterTransition,
    claims: Claims,
) -> EncounterOut:
    """Shared body for the five lifecycle verbs.

    Order matters: validate the transition against the row we read, refuse
    if a live recording would be orphaned, then CAS. The CAS re-checks the
    status we validated, so a concurrent transition loses rather than
    silently overwriting.
    """
    target = encounter_state.ACTION_TARGET[action]
    now = datetime.now(UTC)
    state = get_state()

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await encounters_repository.get_encounter(conn, encounter_id=encounter_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        current = row["status"]
        problem = encounter_state.transition_error(current, action)
        if problem is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=problem)

        live_sessions = 0
        if target in encounter_state.TERMINAL_STATUSES:
            live_sessions = await encounters_repository.count_live_sessions(
                conn,
                encounter_id=encounter_id,
                stale_after_seconds=settings.encounter_live_session_window_seconds,
            )
            if live_sessions and not body.force:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"{live_sessions} dictation session(s) on this encounter are "
                        "still live — stop the recording first, or retry with "
                        "force=true to end the visit anyway"
                    ),
                )

        updated = await encounters_repository.update_lifecycle(
            conn,
            encounter_id=encounter_id,
            expected_status=current,
            new_status=target,
            now=now,
        )
        if updated is None:
            # Lost the CAS — somebody else moved the row between our read
            # and our write.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="encounter changed concurrently; re-read and retry",
            )

        # The visit is over: this, not the moment it was created, is when the
        # patient was actually seen.
        if target == encounter_state.COMPLETED:
            await patients_repository.bump_last_visit(
                conn, patient_id=updated["patient_id"], when=now
            )

    payload: dict[str, object] = {
        "encounter_id": str(encounter_id),
        "from": current,
        "to": target,
    }
    if body.reason:
        payload["reason"] = body.reason
    if live_sessions and body.force:
        payload["forced_over_live_sessions"] = live_sessions
    await audit_helper.emit(
        state,
        claims,
        _ACTION_AUDIT_KIND[action],
        target_kind="patient",
        target_id=updated["patient_id"],
        payload=payload,
    )
    return _to_out(updated)


def _lifecycle_route(action: EncounterAction, summary: str) -> None:
    """Register ``POST /encounters/{id}/<action>``.

    Five near-identical handlers would be five places to forget a guard, so
    they are generated from one body. Explicit verbs (rather than a generic
    PATCH) keep the audit kind and the permission check tied to the clinical
    action being taken.
    """

    @router.post(
        f"/encounters/{{encounter_id}}/{action}",
        response_model=EncounterOut,
        summary=summary,
        name=f"{action}_encounter",
        operation_id=f"{action}_encounter",
    )
    async def _handler(  # noqa: D401 — body documented on _apply_transition
        encounter_id: UUID,
        claims: Annotated[Claims, Depends(requires("patient.write", "patient"))],
        body: EncounterTransition | None = None,
    ) -> EncounterOut:
        return await _apply_transition(
            encounter_id=encounter_id,
            action=action,
            body=body or EncounterTransition(),
            claims=claims,
        )


_lifecycle_route("start", "Begin a scheduled visit (→ in_progress).")
_lifecycle_route("pause", "Step out of an in-progress visit (→ paused).")
_lifecycle_route("resume", "Return to a paused visit (→ in_progress).")
_lifecycle_route("complete", "End the visit (→ completed); stamps the patient's last visit.")
_lifecycle_route("cancel", "Abandon the visit (→ cancelled).")


@router.get(
    "/schedule",
    response_model=list[QueueEncounterOut],
    summary="Scheduled visits for a day (defaults to today, UTC).",
)
async def list_schedule(
    claims: Annotated[Claims, Depends(requires("patient.read", "patient"))],
    day_iso: Annotated[str | None, Query(alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
) -> list[QueueEncounterOut]:
    day = datetime.fromisoformat(day_iso).date() if day_iso else datetime.now(UTC).date()
    day_start = datetime.combine(day, time.min, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await encounters_repository.list_schedule(
            conn, day_start=day_start, day_end=day_end
        )
    return [_to_queue_out(r) for r in rows]
