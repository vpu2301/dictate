"""``/patients`` — roster CRUD, search, and the unified clinical timeline."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from auth import Claims
from db import tenant_connection

from .. import audit_helper, audit_kinds
from ..config import settings
from ..deps import get_state, requires
from ..domain import patients_repository, timeline_repository
from ..domain.common import decode_cursor, encode_cursor, parse_dob

router = APIRouter(prefix="/patients", tags=["patients"])


# ── Wire models ─────────────────────────────────────────────────────


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NameI18n(_Strict):
    uk: str = ""
    en: str = ""


class PatientCreate(_Strict):
    name: NameI18n
    dob: str | None = None
    sex: Literal["M", "F", "U"] = "U"
    mrn: str = ""
    summary: NameI18n | None = None
    tags: list[str] = Field(default_factory=list)


class PatientUpdate(_Strict):
    name: NameI18n | None = None
    dob: str | None = None
    sex: Literal["M", "F", "U"] | None = None
    mrn: str | None = None
    summary: NameI18n | None = None
    tags: list[str] | None = None
    status: Literal["active", "inactive", "deceased"] | None = None


class PatientOut(_Strict):
    id: UUID
    name: NameI18n
    dob: date | None
    sex: str
    mrn: str
    summary: NameI18n
    tags: list[str]
    status: str
    last_visit: datetime | None
    created_at: datetime
    updated_at: datetime


class PatientList(_Strict):
    items: list[PatientOut]
    next_cursor: str | None = None


class TimelineItem(_Strict):
    id: UUID
    kind: str  # encounter | note | consent
    title: str
    date: datetime
    status: str | None = None
    by: str | None = None


class Timeline(_Strict):
    items: list[TimelineItem]


# ── Serialization ───────────────────────────────────────────────────


def _to_out(row: asyncpg.Record) -> PatientOut:
    return PatientOut(
        id=row["id"],
        name=NameI18n(uk=row["name_uk"], en=row["name_en"]),
        dob=row["dob"],
        sex=row["sex"],
        mrn=row["mrn"],
        summary=NameI18n(uk=row["summary_uk"], en=row["summary_en"]),
        tags=list(row["tags"]),
        status=row["status"],
        last_visit=row["last_visit_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Create ──────────────────────────────────────────────────────────


@router.post(
    "",
    response_model=PatientOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a patient to the tenant roster.",
)
async def create_patient(
    body: PatientCreate,
    claims: Annotated[Claims, Depends(requires("patient.write", "patient"))],
) -> PatientOut:
    name_uk = body.name.uk.strip() or body.name.en.strip()
    name_en = body.name.en.strip() or body.name.uk.strip()
    if not name_uk:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="patient name is required (uk or en)",
        )
    summary = body.summary or NameI18n()

    state = get_state()
    try:
        async with tenant_connection(state.app_pool, claims.tid) as conn:
            row = await patients_repository.create_patient(
                conn,
                tenant_id=claims.tid,
                created_by=claims.sub,
                name_uk=name_uk,
                name_en=name_en,
                dob=parse_dob(body.dob),
                sex=body.sex,
                mrn=body.mrn.strip(),
                summary_uk=summary.uk.strip(),
                summary_en=summary.en.strip(),
                tags=[t.strip() for t in body.tags if t.strip()],
            )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a patient with MRN {body.mrn!r} already exists in this tenant",
        ) from exc

    await _audit(claims, audit_kinds.PATIENT_CREATED, row["id"], {"has_mrn": bool(body.mrn.strip())})
    return _to_out(row)


# ── List / search ───────────────────────────────────────────────────


@router.get("", response_model=PatientList, summary="List / search the roster.")
async def list_patients(
    claims: Annotated[Claims, Depends(requires("patient.read", "patient"))],
    query: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> PatientList:
    limit = min(limit, settings.patient_list_max_limit)
    decoded = decode_cursor(cursor) if cursor else None
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await patients_repository.list_patients(
            conn, query=query, limit=limit, cursor=decoded
        )
    next_cursor: str | None = None
    if len(rows) > limit:
        last = rows[limit - 1]
        sort_key = last["last_visit_at"] or last["created_at"]
        next_cursor = encode_cursor(sort_key, last["id"])
        rows = rows[:limit]
    return PatientList(items=[_to_out(r) for r in rows], next_cursor=next_cursor)


# ── Read ────────────────────────────────────────────────────────────


@router.get("/{patient_id}", response_model=PatientOut, summary="Fetch one patient.")
async def get_patient(
    patient_id: UUID,
    claims: Annotated[Claims, Depends(requires("patient.read", "patient"))],
) -> PatientOut:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await patients_repository.get_patient(conn, patient_id=patient_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    await _audit(claims, audit_kinds.PATIENT_VIEWED, patient_id, {})
    return _to_out(row)


# ── Update ──────────────────────────────────────────────────────────


@router.put("/{patient_id}", response_model=PatientOut, summary="Update a patient.")
async def update_patient(
    patient_id: UUID,
    body: PatientUpdate,
    claims: Annotated[Claims, Depends(requires("patient.write", "patient"))],
) -> PatientOut:
    fields: dict[str, object] = {}
    if body.name is not None:
        name_uk = body.name.uk.strip() or body.name.en.strip()
        name_en = body.name.en.strip() or body.name.uk.strip()
        if name_uk:
            fields["name_uk"] = name_uk
            fields["name_en"] = name_en
    if body.dob is not None:
        fields["dob"] = parse_dob(body.dob)
    if body.sex is not None:
        fields["sex"] = body.sex
    if body.mrn is not None:
        fields["mrn"] = body.mrn.strip()
    if body.summary is not None:
        fields["summary_uk"] = body.summary.uk.strip()
        fields["summary_en"] = body.summary.en.strip()
    if body.tags is not None:
        fields["tags"] = [t.strip() for t in body.tags if t.strip()]
    if body.status is not None:
        fields["status"] = body.status

    state = get_state()
    try:
        async with tenant_connection(state.app_pool, claims.tid) as conn:
            row = await patients_repository.update_patient(
                conn, patient_id=patient_id, fields=fields
            )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MRN already in use in this tenant",
        ) from exc
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    await _audit(
        claims, audit_kinds.PATIENT_UPDATED, patient_id, {"fields": sorted(fields)}
    )
    return _to_out(row)


# ── Timeline ────────────────────────────────────────────────────────


@router.get(
    "/{patient_id}/timeline",
    response_model=Timeline,
    summary="Dictated reports (and, later, scribe sessions) for the patient.",
)
async def patient_timeline(
    patient_id: UUID,
    claims: Annotated[Claims, Depends(requires("patient.read", "patient"))],
) -> Timeline:
    """Reports filed against this patient, newest first.

    The SPA merges this with encounters / notes / consents (each fetched from
    its own endpoint) to build the on-screen feed, and reads ``kind='dictate'``
    rows here to populate the Reports tab — so this endpoint deliberately
    returns reports only, not the core-owned records, to avoid double-counting.
    """
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        if await patients_repository.get_patient(conn, patient_id=patient_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        reports = await timeline_repository.list_patient_reports(
            conn, patient_id=patient_id
        )

    items = [
        TimelineItem(
            id=r["id"],
            kind="dictate",
            title=r["title"] or r["code"],
            date=r["updated_at"] or r["created_at"],
            status=r["status"],
        )
        for r in reports
    ]
    return Timeline(items=items)


# ── helpers ─────────────────────────────────────────────────────────


async def _audit(
    claims: Claims, kind: str, target_id: UUID, payload: dict[str, object]
) -> None:
    await audit_helper.emit(
        get_state(), claims, kind, target_kind="patient", target_id=target_id, payload=payload
    )
