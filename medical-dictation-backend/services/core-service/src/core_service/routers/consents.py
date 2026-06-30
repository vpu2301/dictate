"""Per-patient consents (AI-scribe / data-processing) with withdrawal."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from auth import Claims
from db import tenant_connection

from .. import audit_helper, audit_kinds
from ..deps import get_state, requires
from ..domain import consents_repository, patients_repository

router = APIRouter(tags=["consents"])


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConsentCreate(_Strict):
    type: str = "ai_scribe"
    method: str = "verbal"
    version: str = ""
    encounter_id: UUID | None = None
    status: str = "granted"  # accepted for FE symmetry; new rows are 'granted'


class ConsentOut(_Strict):
    id: UUID
    patient_id: UUID
    encounter_id: UUID | None
    type: str
    method: str
    version: str
    status: str
    granted_at: datetime
    withdrawn_at: datetime | None


def _to_out(row: asyncpg.Record) -> ConsentOut:
    return ConsentOut(
        id=row["id"],
        patient_id=row["patient_id"],
        encounter_id=row["encounter_id"],
        type=row["type"],
        method=row["method"],
        version=row["version"],
        status=row["status"],
        granted_at=row["granted_at"],
        withdrawn_at=row["withdrawn_at"],
    )


@router.get(
    "/patients/{patient_id}/consents",
    response_model=list[ConsentOut],
    summary="Consent history for a patient.",
)
async def list_consents(
    patient_id: UUID,
    claims: Annotated[Claims, Depends(requires("patient.read", "patient"))],
) -> list[ConsentOut]:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await consents_repository.list_for_patient(conn, patient_id=patient_id)
    return [_to_out(r) for r in rows]


@router.post(
    "/patients/{patient_id}/consents",
    response_model=ConsentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record a consent.",
)
async def create_consent(
    patient_id: UUID,
    body: ConsentCreate,
    claims: Annotated[Claims, Depends(requires("patient.write", "patient"))],
) -> ConsentOut:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        if await patients_repository.get_patient(conn, patient_id=patient_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        row = await consents_repository.create_consent(
            conn,
            tenant_id=claims.tid,
            patient_id=patient_id,
            encounter_id=body.encounter_id,
            created_by=claims.sub,
            type_=body.type,
            method=body.method,
            version=body.version,
        )
    await audit_helper.emit(
        state,
        claims,
        audit_kinds.CONSENT_GRANTED,
        target_kind="patient",
        target_id=patient_id,
        payload={"consent_id": str(row["id"]), "type": body.type},
    )
    return _to_out(row)


@router.post(
    "/patients/{patient_id}/consents/{consent_id}/withdraw",
    response_model=ConsentOut,
    summary="Withdraw a previously-granted consent.",
)
async def withdraw_consent(
    patient_id: UUID,
    consent_id: UUID,
    claims: Annotated[Claims, Depends(requires("patient.write", "patient"))],
) -> ConsentOut:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await consents_repository.withdraw_consent(
            conn, consent_id=consent_id, patient_id=patient_id, when=datetime.now(UTC)
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="consent not found or already withdrawn",
        )
    await audit_helper.emit(
        state,
        claims,
        audit_kinds.CONSENT_WITHDRAWN,
        target_kind="patient",
        target_id=patient_id,
        payload={"consent_id": str(consent_id)},
    )
    return _to_out(row)
