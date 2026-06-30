"""Privacy requests — DSAR (data subject access) and scheduled erasure.

This logs the request and (for erasure) sets a grace-period target. The
actual export / purge job is out of scope here; the audit trail + the
``patient_privacy_requests`` row are the durable record a compliance
workflow acts on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from audit import Severity
from auth import Claims
from db import tenant_connection

from .. import audit_helper, audit_kinds
from ..deps import get_state, requires
from ..domain import patients_repository, privacy_repository

router = APIRouter(tags=["privacy"])

# Erasure grace period before the purge job is eligible to run.
_ERASURE_GRACE_DAYS = 30


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrivacyRequestBody(_Strict):
    reason: str = ""


class PrivacyRequestOut(_Strict):
    id: UUID
    patient_id: UUID
    kind: str
    reason: str
    status: str
    requested_at: datetime
    scheduled_for: datetime | None


def _to_out(row: asyncpg.Record) -> PrivacyRequestOut:
    return PrivacyRequestOut(
        id=row["id"],
        patient_id=row["patient_id"],
        kind=row["kind"],
        reason=row["reason"],
        status=row["status"],
        requested_at=row["requested_at"],
        scheduled_for=row["scheduled_for"],
    )


async def _create(
    claims: Claims,
    patient_id: UUID,
    *,
    kind: str,
    reason: str,
    status_: str,
    scheduled_for: datetime | None,
    audit_kind: str,
) -> PrivacyRequestOut:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        if await patients_repository.get_patient(conn, patient_id=patient_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        row = await privacy_repository.create_request(
            conn,
            tenant_id=claims.tid,
            patient_id=patient_id,
            requested_by=claims.sub,
            kind=kind,
            reason=reason.strip(),
            status=status_,
            scheduled_for=scheduled_for,
        )
    await audit_helper.emit(
        state,
        claims,
        audit_kind,
        target_kind="patient",
        target_id=patient_id,
        payload={"request_id": str(row["id"]), "kind": kind},
        severity=Severity.SEC,
    )
    return _to_out(row)


@router.post(
    "/patients/{patient_id}/dsar",
    response_model=PrivacyRequestOut,
    status_code=status.HTTP_201_CREATED,
    summary="Log a data-subject access request.",
)
async def request_dsar(
    patient_id: UUID,
    body: PrivacyRequestBody,
    claims: Annotated[Claims, Depends(requires("patient.write", "patient"))],
) -> PrivacyRequestOut:
    return await _create(
        claims,
        patient_id,
        kind="dsar",
        reason=body.reason,
        status_="pending",
        scheduled_for=None,
        audit_kind=audit_kinds.PRIVACY_DSAR_REQUESTED,
    )


@router.post(
    "/patients/{patient_id}/erasure",
    response_model=PrivacyRequestOut,
    status_code=status.HTTP_201_CREATED,
    summary="Schedule erasure of a patient (grace period applies).",
)
async def schedule_erasure(
    patient_id: UUID,
    body: PrivacyRequestBody,
    claims: Annotated[Claims, Depends(requires("patient.write", "patient"))],
) -> PrivacyRequestOut:
    scheduled_for = datetime.now(UTC) + timedelta(days=_ERASURE_GRACE_DAYS)
    return await _create(
        claims,
        patient_id,
        kind="erasure",
        reason=body.reason,
        status_="scheduled",
        scheduled_for=scheduled_for,
        audit_kind=audit_kinds.PRIVACY_ERASURE_SCHEDULED,
    )
