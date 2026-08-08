"""``/patients`` — roster CRUD, search, and the unified clinical timeline."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from audit import Severity
from auth import Claims, can_claims
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
from ._phi_access_guard import PatientAccess, patient_record_access

router = APIRouter(prefix="/patients", tags=["patients"])


# ── Wire models ─────────────────────────────────────────────────────


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NameI18n(_Strict):
    uk: str = ""
    en: str = ""


# Contact fields carry a length ceiling plus a shape check on the two that
# are machine-dialled/machine-sent, so a typo is caught at registration
# rather than at the first call or the first send. The address components
# stay free-form: street naming, house numbering and postal-code formats
# differ per country, and a clinic must be able to record what the patient
# actually gave.
_PHONE_MAX = 32
_EMAIL_MAX = 254  # RFC 5321 path limit
_STREET_MAX = 200
_HOUSE_MAX = 32
_ZIP_MAX = 20
_CITY_MAX = 120
_COUNTRY_MAX = 120


class Address(_Strict):
    """Postal address, split at capture (0060).

    Every component is optional — a record with only a city is a valid
    partial address, and "not captured" is the empty string throughout.
    """

    street: str = Field(default="", max_length=_STREET_MAX)
    # Building + apartment as written locally: "12", "12/3, кв. 7", "5B".
    house: str = Field(default="", max_length=_HOUSE_MAX)
    zip: str = Field(default="", max_length=_ZIP_MAX)
    city: str = Field(default="", max_length=_CITY_MAX)
    country: str = Field(default="", max_length=_COUNTRY_MAX)


class PatientCreate(_Strict):
    name: NameI18n
    dob: str | None = None
    sex: Literal["M", "F", "U"] = "U"
    mrn: str = ""
    phone: str = Field(default="", max_length=_PHONE_MAX)
    email: str = Field(default="", max_length=_EMAIL_MAX)
    address: Address = Field(default_factory=Address)
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
    # None = unchanged; "" clears the field.
    phone: str | None = Field(default=None, max_length=_PHONE_MAX)
    email: str | None = Field(default=None, max_length=_EMAIL_MAX)
    # None = unchanged. An object REPLACES the whole address — every
    # component present in the model is written, so a blank one clears that
    # component. There is no per-component patch: the form always holds the
    # full address, and a partial merge would make "clear the house number"
    # unexpressible.
    address: Address | None = None
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
    phone: str = ""
    email: str = ""
    address: Address = Field(default_factory=Address)
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
    kind: str  # dictate | recording | scribe
    title: str
    date: datetime
    status: str | None = None
    by: str | None = None
    # kind == "recording" | "scribe" (S11 step 02 / S14): metadata only,
    # never a media URL and never transcript text.
    encounter_id: UUID | None = None
    duration_s: float | None = None
    # kind == "scribe" only: how many transcript segments the session
    # holds, so the card can distinguish a real consultation from one
    # that recorded nothing. The text itself stays on dictation-service.
    segments: int | None = None


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
        phone=row["phone"],
        email=row["email"],
        address=Address(
            street=row["address_street"],
            house=row["address_house"],
            zip=row["address_zip"],
            city=row["address_city"],
            country=row["address_country"],
        ),
        summary=NameI18n(uk=row["summary_uk"], en=row["summary_en"]),
        tags=list(row["tags"]),
        status=row["status"],
        last_visit=row["last_visit_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        has_ipn=bool(row.get("has_ipn", False)),
    )


def _to_out_redacted(row: asyncpg.Record) -> PatientOut:
    """The roster row an admin sees (S15): name + id, nothing clinical.

    Enough to find the record to break glass on — dob, sex, MRN, contact
    details, summary, tags and visit recency all stay behind the grant.
    ``status`` survives so the erased-tombstone listing keeps working.
    """
    return PatientOut(
        id=row["id"],
        name=NameI18n(uk=row["name_uk"], en=row["name_en"]),
        dob=None,
        sex="U",
        mrn="",
        summary=NameI18n(),
        tags=[],
        status=row["status"],
        last_visit=None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        has_ipn=False,
    )


# ── ІПН handling ────────────────────────────────────────────────────


def _http_error(status_code: int, detail: str, **extras: object) -> HTTPException:
    """HTTPException with RFC 9457 extension members (``code`` et al.) —
    the machine-readable contract the SPA branches on (see
    observability.problem_details)."""
    exc = HTTPException(status_code=status_code, detail=detail)
    exc.problem_extras = extras  # type: ignore[attr-defined]
    return exc


def _clean_email(raw: str) -> str:
    """Trim + lowercase an e-mail, rejecting an obviously malformed one.

    Deliberately a shape check, not RFC 5322: the goal is to catch the
    missing-@ / trailing-comma typo at registration. Anything stricter
    rejects addresses that deliver fine, and the field is optional — an
    empty string means "not captured".
    """
    email = raw.strip().lower()
    if not email:
        return ""
    local, _, domain = email.partition("@")
    if not local or not domain or "." not in domain or any(c.isspace() for c in email):
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "email must look like name@example.com",
            code="email_invalid",
        )
    return email


# Everything a human writes between the digits of a phone number. Stripped
# before validation and before storage, so "+380 (67) 123-45-67" and
# "+380671234567" are one value — a `tel:` link, a dedupe, or an SMS gateway
# can then use the column as-is.
_PHONE_SEPARATORS = re.compile(r"[\s()\-–—./]+")
# ASCII digits only — str.isdigit() would accept Arabic-Indic and superscript
# digits, which no dialler understands. E.164 caps the number at 15.
_PHONE_DIGITS = re.compile(r"^\d{7,15}$")


def _clean_phone(raw: str) -> str:
    """Normalize a telephone number, rejecting one that cannot be dialled.

    Accepts an optional leading ``+`` followed by 7–15 digits written with
    any human separators, and stores the compact form (``+380671234567``).
    A name, a stray letter, or half a number is rejected 422
    ``phone_invalid``. An empty string means "not captured".

    E.164-shaped rather than country-specific on purpose: a clinic near the
    border records Polish and Moldovan numbers too, and a per-country
    pattern would reject numbers that dial fine.
    """
    phone = raw.strip()
    if not phone:
        return ""
    plus = phone.startswith("+")
    digits = _PHONE_SEPARATORS.sub("", phone[1:] if plus else phone)
    if not _PHONE_DIGITS.match(digits):
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "phone must be 7–15 digits, optionally prefixed with +",
            code="phone_invalid",
        )
    return f"+{digits}" if plus else digits


def _address_columns(address: Address) -> dict[str, str]:
    """Address model → the five 0060 columns, each trimmed."""
    return {
        "address_street": address.street.strip(),
        "address_house": address.house.strip(),
        "address_zip": address.zip.strip(),
        "address_city": address.city.strip(),
        "address_country": address.country.strip(),
    }


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
    phone = _clean_phone(body.phone)
    email = _clean_email(body.email)
    address = _address_columns(body.address)

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
                phone=phone,
                email=email,
                **address,
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
        # Presence flags only — contact details are PII and never enter an
        # audit payload (the ids-only convention; test_non_phi_assertions).
        {
            "has_mrn": bool(body.mrn.strip()),
            "has_ipn": ipn_cols["ipn_hmac"] is not None,
            "has_phone": bool(phone),
            "has_email": bool(email),
            "has_address": any(address.values()),
        },
    )
    return _to_out(row)


# ── Bulk import ─────────────────────────────────────────────────────
#
# A clinic arriving from another system has its roster in a spreadsheet, and
# typing it into the single-create form is not a migration plan. This endpoint
# takes the parsed rows and answers per row, because a batch of demographics
# is never uniformly clean: one line has a malformed phone, another repeats a
# patient the clinic already registered last week.
#
# Three properties the design turns on:
#
# * **Partial success is the normal outcome.** Each row runs inside its own
#   SAVEPOINT, so a duplicate MRN on line 40 rolls back line 40 and nothing
#   else. An all-or-nothing import of 400 rows would be rejected by one typo
#   and re-uploaded until the file is perfect — which is not how clinic data
#   arrives.
# * **Duplicates are a decision, not an error.** Re-uploading last month's
#   file must not create twins, so an MRN or ІПН that already exists is
#   reported (skipped, or failed if the caller wants a hard stop) and never
#   silently merged. Nothing here overwrites an existing record: an import
#   cannot become a mass-edit of demographics.
# * **`dry_run` is the same code path.** Validation, duplicate lookups and
#   row statuses are computed identically; only the INSERT is skipped. A
#   preview that ran different logic would be a preview of nothing.


class PatientImportItem(PatientCreate):
    """One row of an import — identical to a single-create body.

    Subclassed rather than aliased so the wire contract stays visibly the
    same one: whatever the create form can express, a file can express too,
    and `extra="forbid"` is inherited.
    """


class PatientImportRequest(_Strict):
    items: list[PatientImportItem] = Field(min_length=1)
    # Validate + detect duplicates and write nothing. What the SPA shows in
    # its preview step before the clinician commits the file.
    dry_run: bool = False
    # What to do with a row whose MRN or ІПН is already on the roster:
    # leave the existing record alone and move on ("skip"), or mark the row
    # failed so a caller who expects a clean file sees a non-zero count.
    # Neither one modifies the existing patient.
    on_duplicate: Literal["skip", "fail"] = "skip"


class PatientImportRow(_Strict):
    """The verdict for one input row, positionally matched to `items`.

    `index` is the caller's row number, so a spreadsheet UI can point at the
    line that failed. `code` is the machine-readable reason, reusing the same
    vocabulary the single-create errors already use (`name_required`,
    `phone_invalid`, `email_invalid`, `ipn_invalid`) plus the duplicate codes
    (`mrn_exists`, `ipn_exists`, `duplicate_in_batch`).
    """

    index: int
    status: Literal["created", "valid", "skipped", "failed"]
    patient_id: UUID | None = None
    code: str | None = None
    message: str | None = None
    # Set on a duplicate so the UI can link to the record already on file.
    existing_patient_id: UUID | None = None


class PatientImportResult(_Strict):
    dry_run: bool
    total: int
    created: int
    skipped: int
    failed: int
    rows: list[PatientImportRow]


class _PreparedRow(BaseModel):
    """A row that passed validation, resolved to storage columns."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    patient_id: UUID
    name_uk: str
    name_en: str
    dob: date | None
    sex: str
    mrn: str
    phone: str
    email: str
    address: dict[str, str]
    summary_uk: str
    summary_en: str
    tags: list[str]
    ipn_hmac: bytes | None
    ipn_encrypted: bytes | None
    ipn_dek: bytes | None


async def _prepare_import_row(item: PatientImportItem, *, tenant_id: UUID) -> _PreparedRow:
    """Validate one row and resolve it to columns, or raise the same
    HTTPException the single-create path would have raised."""
    name_uk = item.name.uk.strip() or item.name.en.strip()
    name_en = item.name.en.strip() or item.name.uk.strip()
    if not name_uk:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "patient name is required (uk or en)",
            code="name_required",
        )
    summary = item.summary or NameI18n()
    patient_id = uuid4()
    ipn_cols: dict[str, bytes | None] = {
        "ipn_hmac": None,
        "ipn_encrypted": None,
        "ipn_dek": None,
    }
    if item.ipn and item.ipn.strip():
        ipn_cols = await _ipn_columns(
            item.ipn, tenant_id=tenant_id, patient_id=patient_id
        )
    return _PreparedRow(
        patient_id=patient_id,
        name_uk=name_uk,
        name_en=name_en,
        dob=parse_dob(item.dob),
        sex=item.sex,
        mrn=item.mrn.strip(),
        # These two raise on a malformed value — caught per row by the caller.
        phone=_clean_phone(item.phone),
        email=_clean_email(item.email),
        address=_address_columns(item.address),
        summary_uk=summary.uk.strip(),
        summary_en=summary.en.strip(),
        tags=[t.strip() for t in item.tags if t.strip()],
        ipn_hmac=ipn_cols["ipn_hmac"],
        ipn_encrypted=ipn_cols["ipn_encrypted"],
        ipn_dek=ipn_cols["ipn_dek"],
    )


def _row_error(exc: HTTPException, index: int) -> PatientImportRow:
    """Turn a per-row validation failure into its result row, keeping the
    machine-readable `code` the single-create path would have returned."""
    extras = getattr(exc, "problem_extras", {}) or {}
    code = extras.get("code")
    return PatientImportRow(
        index=index,
        status="failed",
        code=str(code) if code else "invalid",
        message=str(exc.detail),
    )


@router.post(
    "/import",
    response_model=PatientImportResult,
    summary="Import a batch of patients onto the roster.",
)
async def import_patients(
    body: PatientImportRequest,
    claims: Annotated[Claims, Depends(requires("patient.write", "patient"))],
) -> PatientImportResult:
    max_rows = settings.patient_import_max_rows
    if len(body.items) > max_rows:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"an import carries at most {max_rows} rows per request",
            code="import_too_large",
            max_rows=max_rows,
        )

    rows: list[PatientImportRow] = []
    created = skipped = failed = 0
    # Duplicates *within the file* are caught here rather than left to the
    # unique index: on a dry run nothing is written, so the second copy of a
    # row would otherwise be reported as importable and then fail for real.
    seen_mrn: dict[str, int] = {}
    seen_ipn: dict[bytes, int] = {}

    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        for index, item in enumerate(body.items):
            try:
                prepared = await _prepare_import_row(item, tenant_id=claims.tid)
            except HTTPException as exc:
                rows.append(_row_error(exc, index))
                failed += 1
                continue

            # ── duplicate detection ──────────────────────────────
            dup_code: str | None = None
            dup_existing: UUID | None = None
            dup_message = ""
            if prepared.mrn and prepared.mrn in seen_mrn:
                dup_code = "duplicate_in_batch"
                dup_message = (
                    f"MRN {prepared.mrn!r} repeats row {seen_mrn[prepared.mrn]}"
                )
            elif prepared.ipn_hmac is not None and prepared.ipn_hmac in seen_ipn:
                dup_code = "duplicate_in_batch"
                dup_message = f"ІПН repeats row {seen_ipn[prepared.ipn_hmac]}"
            else:
                if prepared.mrn:
                    dup_existing = await patients_repository.find_patient_id_by_mrn(
                        conn, mrn=prepared.mrn
                    )
                    if dup_existing is not None:
                        dup_code = "mrn_exists"
                        dup_message = f"MRN {prepared.mrn!r} is already on the roster"
                if dup_code is None and prepared.ipn_hmac is not None:
                    dup_existing = (
                        await patients_repository.find_patient_id_by_ipn_hmac(
                            conn, ipn_hmac=prepared.ipn_hmac
                        )
                    )
                    if dup_existing is not None:
                        dup_code = "ipn_exists"
                        dup_message = "this ІПН is already on the roster"

            if prepared.mrn:
                seen_mrn.setdefault(prepared.mrn, index)
            if prepared.ipn_hmac is not None:
                seen_ipn.setdefault(prepared.ipn_hmac, index)

            if dup_code is not None:
                is_skip = body.on_duplicate == "skip"
                rows.append(
                    PatientImportRow(
                        index=index,
                        status="skipped" if is_skip else "failed",
                        code=dup_code,
                        message=dup_message,
                        existing_patient_id=dup_existing,
                    )
                )
                if is_skip:
                    skipped += 1
                else:
                    failed += 1
                continue

            if body.dry_run:
                rows.append(PatientImportRow(index=index, status="valid"))
                continue

            # ── write ────────────────────────────────────────────
            # Own SAVEPOINT: a unique violation this loop did not predict
            # (a concurrent import, a race with the create form) rolls back
            # this row only, leaving the outer transaction usable.
            try:
                async with conn.transaction():
                    record = await patients_repository.create_patient(
                        conn,
                        patient_id=prepared.patient_id,
                        tenant_id=claims.tid,
                        created_by=claims.sub,
                        name_uk=prepared.name_uk,
                        name_en=prepared.name_en,
                        dob=prepared.dob,
                        sex=prepared.sex,
                        mrn=prepared.mrn,
                        phone=prepared.phone,
                        email=prepared.email,
                        **prepared.address,
                        summary_uk=prepared.summary_uk,
                        summary_en=prepared.summary_en,
                        tags=prepared.tags,
                        ipn_hmac=prepared.ipn_hmac,
                        ipn_encrypted=prepared.ipn_encrypted,
                        ipn_dek=prepared.ipn_dek,
                    )
            except asyncpg.UniqueViolationError as exc:
                is_ipn = exc.constraint_name == "uq_patients_tenant_ipn"
                is_skip = body.on_duplicate == "skip"
                rows.append(
                    PatientImportRow(
                        index=index,
                        status="skipped" if is_skip else "failed",
                        code="ipn_exists" if is_ipn else "mrn_exists",
                        message="already on the roster",
                    )
                )
                if is_skip:
                    skipped += 1
                else:
                    failed += 1
                continue

            created += 1
            rows.append(
                PatientImportRow(
                    index=index, status="created", patient_id=record["id"]
                )
            )
            # Per-record trail, same payload shape as the single create: an
            # imported patient must be as auditable as a typed one.
            await _audit(
                claims,
                audit_kinds.PATIENT_CREATED,
                record["id"],
                {
                    "has_mrn": bool(prepared.mrn),
                    "has_ipn": prepared.ipn_hmac is not None,
                    "has_phone": bool(prepared.phone),
                    "has_email": bool(prepared.email),
                    "has_address": any(prepared.address.values()),
                    "source": "import",
                },
            )

    # One event for the run itself — counts only, no names, no MRNs.
    await audit_helper.emit(
        state,
        claims,
        audit_kinds.PATIENT_IMPORTED,
        target_kind="tenant",
        target_id=claims.tid,
        payload={
            "total": len(body.items),
            "created": created,
            "skipped": skipped,
            "failed": failed,
            "dry_run": body.dry_run,
        },
    )

    return PatientImportResult(
        dry_run=body.dry_run,
        total=len(body.items),
        created=created,
        skipped=skipped,
        failed=failed,
        rows=rows,
    )


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
    # S15 admin ⟂ PHI: without the full-read permission the roster comes
    # back redacted — name + id is what an admin needs to FIND a record;
    # everything else waits behind a per-patient break-glass grant.
    serialize = (
        _to_out
        if can_claims(claims, "patient.read_full", "patient")
        else _to_out_redacted
    )
    return PatientList(items=[serialize(r) for r in rows], next_cursor=next_cursor)


# ── Read ────────────────────────────────────────────────────────────


@router.get("/{patient_id}", response_model=PatientOut, summary="Fetch one patient.")
async def get_patient(
    patient_id: UUID,
    access: Annotated[PatientAccess, Depends(patient_record_access)],
) -> PatientOut:
    # Two standings reach this handler (S15): ordinary `patient.read_full`,
    # or a live break-glass grant on THIS patient. The guard has already
    # resolved which and counted the use; from here the only difference
    # is what the audit trail says.
    claims = access.claims
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await patients_repository.get_patient(conn, patient_id=patient_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    await _audit(
        claims,
        audit_kinds.PATIENT_VIEWED,
        patient_id,
        {"break_glass": access.is_break_glass},
        severity=Severity.SEC if access.is_break_glass else Severity.INFO,
    )
    if access.is_break_glass:
        await _audit_grant_use(access, patient_id, surface="patient_detail")
    return _to_out(row)


# ── Update ──────────────────────────────────────────────────────────


@router.put("/{patient_id}", response_model=PatientOut, summary="Update a patient.")
async def update_patient(
    patient_id: UUID,
    body: PatientUpdate,
    claims: Annotated[Claims, Depends(requires("patient.write", "patient"))],
    # Editing presumes reading (S15): an admin's edit needs a live grant
    # on this patient, a clinician's rides `patient.read_full`.
    access: Annotated[PatientAccess, Depends(patient_record_access)],
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
    if body.phone is not None:
        fields["phone"] = _clean_phone(body.phone)
    if body.email is not None:
        fields["email"] = _clean_email(body.email)
    if body.address is not None:
        fields.update(_address_columns(body.address))
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
        claims,
        audit_kinds.PATIENT_UPDATED,
        patient_id,
        {"fields": audit_fields, "break_glass": access.is_break_glass},
        severity=Severity.SEC if access.is_break_glass else Severity.INFO,
    )
    if access.is_break_glass:
        await _audit_grant_use(access, patient_id, surface="patient_update")
    return _to_out(row)


# ── Timeline ────────────────────────────────────────────────────────


@router.get(
    "/{patient_id}/timeline",
    response_model=Timeline,
    summary="Dictated reports, recordings and scribe sessions for the patient.",
)
async def patient_timeline(
    patient_id: UUID,
    access: Annotated[PatientAccess, Depends(patient_record_access)],
) -> Timeline:
    """Reports, encounter-linked recordings and conversations, newest first.

    The SPA merges this with encounters / notes / consents (each fetched from
    its own endpoint) to build the on-screen feed, and reads ``kind='dictate'``
    rows here to populate the Reports tab — so this endpoint deliberately
    skips the core-owned records to avoid double-counting. ``kind='recording'``
    rows (S11 step 02) carry metadata only — never a media URL; audio access
    stays on the ASR surface with its own authz + audit. ``kind='scribe'``
    rows (S14) are conversation-mode consultations, and carry a segment
    COUNT rather than transcript text for the same reason.
    """
    claims = access.claims
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
        conversations = await timeline_repository.list_patient_conversations(
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
    ] + [
        TimelineItem(
            id=c["id"],
            kind="scribe",
            title="Conversation",
            date=c["finalized_at"] or c["created_at"],
            status=c["status"],
            encounter_id=c["encounter_id"],
            duration_s=(c["total_audio_ms"] / 1000.0) if c["total_audio_ms"] else None,
            segments=c["segments"],
        )
        for c in conversations
    ]
    items.sort(key=lambda i: i.date, reverse=True)
    if access.is_break_glass:
        await _audit_grant_use(access, patient_id, surface="patient_timeline")
    return Timeline(items=items)


# ── helpers ─────────────────────────────────────────────────────────


async def _audit(
    claims: Claims,
    kind: str,
    target_id: UUID,
    payload: dict[str, object],
    *,
    severity: Severity = Severity.INFO,
) -> None:
    await audit_helper.emit(
        get_state(),
        claims,
        kind,
        target_kind="patient",
        target_id=target_id,
        payload=payload,
        severity=severity,
    )


async def _audit_grant_use(
    access: PatientAccess, patient_id: UUID, *, surface: str
) -> None:
    """A second, distinctly-kinded event per break-glass read, so "every
    break-glass access" is one query over the chain rather than a filter
    over every patient view ever recorded (mirrors report-service)."""
    await _audit(
        access.claims,
        audit_kinds.PHI_ACCESS_USED,
        patient_id,
        {
            "grant_id": str(access.grant_id),
            "reason_code": access.reason_code,
            "surface": surface,
        },
        severity=Severity.SEC,
    )
