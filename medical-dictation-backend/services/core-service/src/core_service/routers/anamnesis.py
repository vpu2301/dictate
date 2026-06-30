"""Per-patient anamnesis (structured history) — single JSONB record, upserted."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from auth import Claims
from db import tenant_connection

from .. import audit_helper, audit_kinds
from ..deps import get_state, requires
from ..domain import anamnesis_repository, patients_repository

router = APIRouter(tags=["anamnesis"])


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnamnesisBody(_Strict):
    record: dict[str, Any] = Field(default_factory=dict)


class AnamnesisOut(_Strict):
    patient_id: UUID
    record: dict[str, Any]
    updated_at: datetime | None = None


def _to_out(row: asyncpg.Record) -> AnamnesisOut:
    record = row["record"]
    if isinstance(record, str):
        import json

        record = json.loads(record)
    return AnamnesisOut(
        patient_id=row["patient_id"], record=record, updated_at=row["updated_at"]
    )


@router.get(
    "/patients/{patient_id}/anamnesis",
    response_model=AnamnesisOut,
    summary="Fetch the patient's anamnesis (empty record if none yet).",
)
async def get_anamnesis(
    patient_id: UUID,
    claims: Annotated[Claims, Depends(requires("patient.read", "patient"))],
) -> AnamnesisOut:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        if await patients_repository.get_patient(conn, patient_id=patient_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        row = await anamnesis_repository.get_anamnesis(conn, patient_id=patient_id)
    if row is None:
        return AnamnesisOut(patient_id=patient_id, record={})
    return _to_out(row)


@router.put(
    "/patients/{patient_id}/anamnesis",
    response_model=AnamnesisOut,
    summary="Create or replace the patient's anamnesis.",
)
async def put_anamnesis(
    patient_id: UUID,
    body: AnamnesisBody,
    claims: Annotated[Claims, Depends(requires("patient.write", "patient"))],
) -> AnamnesisOut:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        if await patients_repository.get_patient(conn, patient_id=patient_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        row = await anamnesis_repository.upsert_anamnesis(
            conn,
            tenant_id=claims.tid,
            patient_id=patient_id,
            updated_by=claims.sub,
            record=body.record,
        )
    await audit_helper.emit(
        state,
        claims,
        audit_kinds.ANAMNESIS_UPDATED,
        target_kind="patient",
        target_id=patient_id,
    )
    return _to_out(row)
