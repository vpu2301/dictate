"""Privacy requests — DSAR and the two-person erasure workflow.

S11 step 04 upgrades the S11-M2 request log into the real state machine:

    dsar:    requested → executing → completed | failed
    erasure: requested → review → approved → executing → completed
             requested | review | approved → rejected

Two-person rule: the admin who approves or rejects an erasure can never
be the person who requested it — enforced here (403 ``two_person_rule``)
and by the DB CHECK ``privacy_two_person``. Approval sets the
grace-period target (``scheduled_for = now() + ERASURE_GRACE_DAYS``);
the erasure engine (step 07) refuses to execute before it, and an
approved request can still be rejected (cancelled) during grace.

``executing``/``completed``/``failed`` belong to the engines (steps
06/07) via repository functions — deliberately not exposed over HTTP.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict

from audit import Severity
from auth import Claims
from db import tenant_connection

from .. import audit_helper, audit_kinds
from ..config import settings
from ..deps import get_state, requires
from ..domain import patients_repository, privacy_repository

router = APIRouter(tags=["privacy"])


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrivacyRequestBody(_Strict):
    reason: str = ""


class RejectBody(_Strict):
    # The patient may escalate; the clinic needs its paper trail.
    rejection_reason: str


class PrivacyRequestOut(_Strict):
    id: UUID
    patient_id: UUID
    kind: str
    reason: str
    status: str
    requested_by: UUID
    requested_at: datetime
    scheduled_for: datetime | None
    reviewed_by: UUID | None = None
    reviewed_at: datetime | None = None
    rejection_reason: str | None = None
    completed_at: datetime | None = None


def _to_out(row: asyncpg.Record) -> PrivacyRequestOut:
    return PrivacyRequestOut(
        id=row["id"],
        patient_id=row["patient_id"],
        kind=row["kind"],
        reason=row["reason"],
        status=row["status"],
        requested_by=row["requested_by"],
        requested_at=row["requested_at"],
        scheduled_for=row["scheduled_for"],
        reviewed_by=row.get("reviewed_by"),
        reviewed_at=row.get("reviewed_at"),
        rejection_reason=row.get("rejection_reason"),
        completed_at=row.get("completed_at"),
    )


def _http_error(status_code: int, detail: str, **extras: object) -> HTTPException:
    exc = HTTPException(status_code=status_code, detail=detail)
    exc.problem_extras = extras  # type: ignore[attr-defined]
    return exc


def _invalid_transition(current: str) -> HTTPException:
    return _http_error(
        status.HTTP_409_CONFLICT,
        f"transition not allowed from status {current!r}",
        code="invalid_transition",
        current_status=current,
    )


def _two_person() -> HTTPException:
    return _http_error(
        status.HTTP_403_FORBIDDEN,
        "the person who requested an erasure can never approve or reject it "
        "— a second administrator must decide (two-person rule)",
        code="two_person_rule",
    )


# ── Request creation ─────────────────────────────────────────────────


async def _create(
    claims: Claims,
    patient_id: UUID,
    *,
    kind: str,
    reason: str,
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
            status="requested",
            scheduled_for=None,
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
        audit_kind=audit_kinds.PRIVACY_DSAR_REQUESTED,
    )


@router.post(
    "/patients/{patient_id}/erasure",
    response_model=PrivacyRequestOut,
    status_code=status.HTTP_201_CREATED,
    summary="Request erasure of a patient (two-person approval + grace apply).",
)
async def request_erasure(
    patient_id: UUID,
    body: PrivacyRequestBody,
    claims: Annotated[Claims, Depends(requires("patient.write", "patient"))],
) -> PrivacyRequestOut:
    return await _create(
        claims,
        patient_id,
        kind="erasure",
        reason=body.reason,
        audit_kind=audit_kinds.PRIVACY_ERASURE_REQUESTED,
    )


# ── Workflow transitions ─────────────────────────────────────────────


async def _load_request(conn: asyncpg.Connection, request_id: UUID) -> asyncpg.Record:
    row = await privacy_repository.get_request(conn, request_id=request_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return row


@router.post(
    "/privacy-requests/{request_id}/review",
    response_model=PrivacyRequestOut,
    summary="Mark an erasure request as under review.",
)
async def review_request(
    request_id: UUID,
    claims: Annotated[Claims, Depends(requires("patient.write", "patient"))],
) -> PrivacyRequestOut:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        current = await _load_request(conn, request_id)
        row = await privacy_repository.mark_review(conn, request_id=request_id)
        if row is None:
            raise _invalid_transition(current["status"])
    await audit_helper.emit(
        state,
        claims,
        audit_kinds.PRIVACY_ERASURE_REVIEWED,
        target_kind="patient",
        target_id=row["patient_id"],
        payload={"request_id": str(request_id)},
    )
    return _to_out(row)


@router.post(
    "/privacy-requests/{request_id}/approve",
    response_model=PrivacyRequestOut,
    summary="Approve an erasure request (second person; starts the grace period).",
)
async def approve_request(
    request_id: UUID,
    claims: Annotated[Claims, Depends(requires("privacy.approve", "patient"))],
) -> PrivacyRequestOut:
    state = get_state()
    scheduled_for = datetime.now(UTC) + timedelta(days=settings.erasure_grace_days)
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        current = await _load_request(conn, request_id)
        if current["requested_by"] == claims.sub:
            raise _two_person()
        row = await privacy_repository.approve(
            conn, request_id=request_id, reviewer=claims.sub, scheduled_for=scheduled_for
        )
        if row is None:
            raise _invalid_transition(current["status"])
    await audit_helper.emit(
        state,
        claims,
        audit_kinds.PRIVACY_ERASURE_APPROVED,
        target_kind="patient",
        target_id=row["patient_id"],
        payload={
            "request_id": str(request_id),
            "scheduled_for": scheduled_for.isoformat(),
            "grace_days": settings.erasure_grace_days,
        },
        severity=Severity.SEC,
    )
    return _to_out(row)


@router.post(
    "/privacy-requests/{request_id}/reject",
    response_model=PrivacyRequestOut,
    summary="Reject (or cancel during grace) an erasure request, with a reason.",
)
async def reject_request(
    request_id: UUID,
    body: RejectBody,
    claims: Annotated[Claims, Depends(requires("privacy.approve", "patient"))],
) -> PrivacyRequestOut:
    if not body.rejection_reason.strip():
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "a written rejection reason is required",
            code="rejection_reason_required",
        )
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        current = await _load_request(conn, request_id)
        if current["requested_by"] == claims.sub:
            raise _two_person()
        row = await privacy_repository.reject(
            conn,
            request_id=request_id,
            reviewer=claims.sub,
            rejection_reason=body.rejection_reason.strip(),
        )
        if row is None:
            raise _invalid_transition(current["status"])
    await audit_helper.emit(
        state,
        claims,
        audit_kinds.PRIVACY_ERASURE_REJECTED,
        target_kind="patient",
        target_id=row["patient_id"],
        payload={
            "request_id": str(request_id),
            "rejection_reason": body.rejection_reason.strip()[:200],
        },
        severity=Severity.SEC,
    )
    return _to_out(row)


@router.get(
    "/privacy-requests",
    response_model=list[PrivacyRequestOut],
    summary="Admin queue: privacy requests across the tenant.",
)
async def list_privacy_requests(
    claims: Annotated[Claims, Depends(requires("patient.read", "patient"))],
    status_filter: Annotated[
        Literal[
            "requested", "review", "approved", "executing",
            "completed", "rejected", "failed",
        ]
        | None,
        Query(alias="status"),
    ] = None,
    kind: Annotated[Literal["dsar", "erasure"] | None, Query()] = None,
) -> list[PrivacyRequestOut]:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await privacy_repository.list_requests(
            conn, status=status_filter, kind=kind
        )
    return [_to_out(r) for r in rows]
