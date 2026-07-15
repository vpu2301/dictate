"""``/patients`` — roster CRUD, search, and the unified clinical timeline."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from auth import Claims
from crypto.ipn import (
    InvalidIpnError,
    normalize_ipn,
    pack_ipn_envelope,
)
from crypto.ipn import (
    ipn_hmac as compute_ipn_hmac,
)
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
    # Raw ІПН (РНОКПП); accepted with spaces/dashes, validated by checksum.
    # Stored as HMAC (+ optional envelope ciphertext) — never echoed back.
    ipn: str | None = None


class PatientUpdate(_Strict):
    name: NameI18n | None = None
    dob: str | None = None
    sex: Literal["M", "F", "U"] | None = None
    mrn: str | None = None
    summary: NameI18n | None = None
    tags: list[str] | None = None
    # "erased" is accepted by the schema so the guard can answer with the
    # contract error code — the handler always rejects it (erasure engine only).
    status: Literal["active", "inactive", "deceased", "erased"] | None = None
    # None = unchanged; "" = clear the stored ІПН; digits = set/replace.
    ipn: str | None = None


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
    # Presence flag only — the hmac and the raw ІПН never leave the service.
    has_ipn: bool = False


class PatientList(_Strict):
    items: list[PatientOut]
    next_cursor: str | None = None


class TimelineItem(_Strict):
    id: UUID
    kind: str  # dictate | recording
    title: str
    date: datetime
    status: str | None = None
    by: str | None = None
    # kind == "recording" only (S11 step 02): metadata, never a media URL.
    encounter_id: UUID | None = None
    duration_s: float | None = None


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
        has_ipn=bool(row.get("has_ipn", False)),
    )


# ── ІПН handling ────────────────────────────────────────────────────


def _http_error(status_code: int, detail: str, **extras: object) -> HTTPException:
    """HTTPException with RFC 9457 extension members (``code`` et al.) —
    the machine-readable contract the SPA branches on (see
    observability.problem_details)."""
    exc = HTTPException(status_code=status_code, detail=detail)
    exc.problem_extras = extras  # type: ignore[attr-defined]
    return exc


async def _ipn_columns(
    raw: str, *, tenant_id: UUID, patient_id: UUID
) -> dict[str, bytes | None]:
    """Resolve a raw ІПН into the three storage columns.

    Normalizes + checksum-validates (422 ``ipn_invalid`` on failure), computes
    the lookup hmac, and — only when the DPO-gated raw-retention flag is on —
    envelope-encrypts the raw value with AAD bound to ``tenant_id ‖ patient_id``.
    The raw ІПН is never logged, echoed, or put in an error message.
    """
    try:
        ipn = normalize_ipn(raw)
    except InvalidIpnError as exc:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "ІПН must be exactly 10 digits with a valid РНОКПП checksum",
            code="ipn_invalid",
        ) from exc

    cols: dict[str, bytes | None] = {
        "ipn_hmac": compute_ipn_hmac(ipn, settings.patient_ipn_hmac_key),
        "ipn_encrypted": None,
        "ipn_dek": None,
    }
    if settings.patient_ipn_raw_enabled:
        envelope = get_state().envelope
        if envelope is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="raw-ІПН retention is enabled but envelope crypto is not wired",
            )
        blob = await envelope.encrypt(
            ipn.encode("utf-8"), tenant_id=tenant_id, aad=patient_id.bytes
        )
        cols["ipn_encrypted"], cols["ipn_dek"] = pack_ipn_envelope(blob)
    return cols


def _search_ipn_token(query: str) -> bytes | None:
    """If the roster search string is a valid ІПН, return its lookup hmac."""
    try:
        return compute_ipn_hmac(normalize_ipn(query), settings.patient_ipn_hmac_key)
    except InvalidIpnError:
        return None


async def _ipn_conflict(claims: Claims, ipn_hmac: bytes | None) -> HTTPException:
    """Build the duplicate-ІПН 409, carrying the existing patient's id."""
    existing: UUID | None = None
    if ipn_hmac is not None:
        state = get_state()
        async with tenant_connection(state.app_pool, claims.tid) as conn:
            existing = await patients_repository.find_patient_id_by_ipn_hmac(
                conn, ipn_hmac=ipn_hmac
            )
    return _http_error(
        status.HTTP_409_CONFLICT,
        "a patient with this ІПН already exists in this tenant",
        code="patient_ipn_exists",
        existing_patient_id=str(existing) if existing else None,
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

    # Generated here (not by the DB default) so the raw-ІПН envelope AAD can
    # bind to the row id before the INSERT.
    patient_id = uuid4()
    ipn_cols: dict[str, bytes | None] = {
        "ipn_hmac": None,
        "ipn_encrypted": None,
        "ipn_dek": None,
    }
    if body.ipn and body.ipn.strip():
        ipn_cols = await _ipn_columns(
            body.ipn, tenant_id=claims.tid, patient_id=patient_id
        )

    state = get_state()
    try:
        async with tenant_connection(state.app_pool, claims.tid) as conn:
            row = await patients_repository.create_patient(
                conn,
                patient_id=patient_id,
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
                ipn_hmac=ipn_cols["ipn_hmac"],
                ipn_encrypted=ipn_cols["ipn_encrypted"],
                ipn_dek=ipn_cols["ipn_dek"],
            )
    except asyncpg.UniqueViolationError as exc:
        if exc.constraint_name == "uq_patients_tenant_ipn":
            raise await _ipn_conflict(claims, ipn_cols["ipn_hmac"]) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a patient with MRN {body.mrn!r} already exists in this tenant",
        ) from exc

    await _audit(
        claims,
        audit_kinds.PATIENT_CREATED,
        row["id"],
        {
            "has_mrn": bool(body.mrn.strip()),
            "has_ipn": ipn_cols["ipn_hmac"] is not None,
        },
    )
    return _to_out(row)


# ── List / search ───────────────────────────────────────────────────


@router.get("", response_model=PatientList, summary="List / search the roster.")
async def list_patients(
    claims: Annotated[Claims, Depends(requires("patient.read", "patient"))],
    query: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: Annotated[str | None, Query()] = None,
    include_erased: Annotated[bool, Query()] = False,
) -> PatientList:
    if include_erased and "tenant_admin" not in claims.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="include_erased requires the tenant_admin role",
        )
    limit = min(limit, settings.patient_list_max_limit)
    decoded = decode_cursor(cursor) if cursor else None
    # A search string that IS a valid ІПН dispatches to the exact hmac
    # lookup; anything else takes the text path. Never both.
    ipn_token = _search_ipn_token(query) if query else None
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await patients_repository.list_patients(
            conn,
            query=query,
            limit=limit,
            cursor=decoded,
            ipn_hmac=ipn_token,
            include_erased=include_erased,
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
    # `erased` is terminal and owned by the erasure engine (S11 step 07):
    # the public surface can neither set it nor modify an erased patient.
    if body.status == "erased":
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "the erased status is set only by the erasure engine",
            code="status_immutable_erased",
        )

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
    if body.ipn is not None:
        if body.ipn.strip():
            fields.update(
                await _ipn_columns(body.ipn, tenant_id=claims.tid, patient_id=patient_id)
            )
        else:
            # Explicit empty string clears the stored ІПН (all three columns).
            fields.update({"ipn_hmac": None, "ipn_encrypted": None, "ipn_dek": None})

    state = get_state()
    try:
        async with tenant_connection(state.app_pool, claims.tid) as conn:
            current = await patients_repository.get_patient(conn, patient_id=patient_id)
            if current is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
            if current["status"] == "erased":
                raise _http_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "an erased patient record cannot be modified",
                    code="status_immutable_erased",
                )
            row = await patients_repository.update_patient(
                conn, patient_id=patient_id, fields=fields
            )
    except asyncpg.UniqueViolationError as exc:
        if exc.constraint_name == "uq_patients_tenant_ipn":
            ipn_token = fields.get("ipn_hmac")
            raise await _ipn_conflict(
                claims, ipn_token if isinstance(ipn_token, bytes) else None
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MRN already in use in this tenant",
        ) from exc
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    # Internal ІПН column names collapse to one "ipn" marker in the audit
    # payload — presence only, never material.
    audit_fields = sorted({"ipn" if f.startswith("ipn_") else f for f in fields})
    await _audit(
        claims, audit_kinds.PATIENT_UPDATED, patient_id, {"fields": audit_fields}
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
    """Reports and encounter-linked recordings for this patient, newest first.

    The SPA merges this with encounters / notes / consents (each fetched from
    its own endpoint) to build the on-screen feed, and reads ``kind='dictate'``
    rows here to populate the Reports tab — so this endpoint deliberately
    skips the core-owned records to avoid double-counting. ``kind='recording'``
    rows (S11 step 02) carry metadata only — never a media URL; audio access
    stays on the ASR surface with its own authz + audit.
    """
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        if await patients_repository.get_patient(conn, patient_id=patient_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        reports = await timeline_repository.list_patient_reports(
            conn, patient_id=patient_id
        )
        recordings = await timeline_repository.list_patient_recordings(
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
    ] + [
        TimelineItem(
            id=a["id"],
            kind="recording",
            title="Recording",
            date=a["created_at"],
            status=a["status"],
            encounter_id=a["encounter_id"],
            duration_s=(a["duration_ms"] / 1000.0) if a["duration_ms"] is not None else None,
        )
        for a in recordings
    ]
    items.sort(key=lambda i: i.date, reverse=True)
    return Timeline(items=items)


# ── helpers ─────────────────────────────────────────────────────────


async def _audit(
    claims: Claims, kind: str, target_id: UUID, payload: dict[str, object]
) -> None:
    await audit_helper.emit(
        get_state(), claims, kind, target_kind="patient", target_id=target_id, payload=payload
    )
