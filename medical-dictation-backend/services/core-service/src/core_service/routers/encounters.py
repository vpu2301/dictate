"""Encounters — per-patient visit history, single-encounter read, and the
day's scheduled-visit queue."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict

from auth import Claims
from db import tenant_connection

from .. import audit_helper, audit_kinds
from ..deps import get_state, requires
from ..domain import encounters_repository, patients_repository

router = APIRouter(tags=["encounters"])

EncounterKind = Literal["visit", "phone", "video", "scribe", "followup", "other"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EncounterCreate(_Strict):
    kind: EncounterKind = "visit"
    datetime: str | None = None  # ISO 8601; defaults to now()
    reason: str = ""
    status: Literal["scheduled", "in_progress", "completed", "cancelled"] = "completed"


class EncounterOut(_Strict):
    id: UUID
    patient_id: UUID
    kind: str
    reason: str
    occurred_at: datetime
    status: str
    created_at: datetime


def _to_out(row: asyncpg.Record) -> EncounterOut:
    return EncounterOut(
        id=row["id"],
        patient_id=row["patient_id"],
        kind=row["kind"],
        reason=row["reason"],
        occurred_at=row["occurred_at"],
        status=row["status"],
        created_at=row["created_at"],
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
    claims: Annotated[Claims, Depends(requires("patient.read", "patient"))],
) -> list[EncounterOut]:
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
        if body.status != "scheduled":
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


@router.get(
    "/schedule",
    response_model=list[EncounterOut],
    summary="Scheduled visits for a day (defaults to today, UTC).",
)
async def list_schedule(
    claims: Annotated[Claims, Depends(requires("patient.read", "patient"))],
    date: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
) -> list[EncounterOut]:
    day = datetime.fromisoformat(date).date() if date else datetime.now(UTC).date()
    day_start = datetime.combine(day, time.min, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await encounters_repository.list_schedule(
            conn, day_start=day_start, day_end=day_end
        )
    return [_to_out(r) for r in rows]
