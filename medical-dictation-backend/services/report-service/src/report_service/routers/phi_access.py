"""``/v1/phi-access-requests`` — break-glass access to a single report
or a single patient record.

An administrator holds no standing clinical read (S14) and no standing
full patient read (S15). When one genuinely needs a specific report or
patient record — a complaint to answer, a subpoena, a billing dispute —
this is the door, and it is deliberately a door and not a queue:

    POST /auth/reauth              → reauth_ticket   (auth-service)
    POST /v1/phi-access-requests   → grant, valid for a bounded window
    GET  /v1/reports/{id}          → now succeeds, and is counted
    (or GET /patients/{id} — patient-kind grants are enforced by
    core-service's twin guard)

There is no approval step. An admin facing a legal deadline at 2am has
nobody to approve it, and a control that cannot be used in the situation
it was built for gets routed around. The control is therefore AFTER the
fact, and it is threefold:

  * a reason from a closed vocabulary, so "why" is reviewable in aggregate
  * a password re-entry, so a walk-up on an unlocked laptop is not enough
  * `sec`-severity audit + an immediate notification to the report's
    authors, so the clinician whose record it is finds out

Grants are per-report, time-boxed, counted on each use, and revocable.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict, Field

from audit import Severity
from auth import Claims
from db import tenant_connection
from notification_events import Category

from .. import audit_kinds
from ..config import settings
from ..deps import get_state, requires
from ..domain import phi_access_repository as grants
from ..domain import reports_repository as repo
from ..notifications import emit_report_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/phi-access-requests", tags=["phi-access"])

_meter = metrics.get_meter("mdx.report.phi_access")
_granted_counter = _meter.create_counter(
    "mdx_phi_access_granted_total",
    description="Break-glass grants minted, by reason code",
    unit="1",
)
_rejected_counter = _meter.create_counter(
    "mdx_phi_access_rejected_total",
    description="Break-glass requests refused, by cause",
    unit="1",
)
# The anomaly signal (hotfix). Distinct from the S14 counter above, which
# stays for the existing dashboards: this one carries the labels the
# snooping alert needs — one principal opening many charts in a short
# window is the signature, and a metric without a principal label cannot
# express it.
#
# On cardinality: break-glass is rare by construction, and a series is
# only created the first time a given principal actually breaks glass. A
# clinic where three of fifty staff ever use the door produces three
# series. If that stops being true, the door is being used as a routine
# read and the cardinality is the least of the problems.
_break_glass_counter = _meter.create_counter(
    "mdx_break_glass_total",
    description="Break-glass grants minted, by reason, resource kind, role and principal",
    unit="1",
)

# Mirrors the CHECK on `phi_access_requests.reason_code` (migrations 0056
# + 0074). A member here without a matching CHECK value is an INSERT that
# fails at runtime, so the two move together.
ReasonCode = Literal[
    # Administrative cases (0056) — the admin-only era.
    "patient_complaint",
    "legal_request",
    "billing_dispute",
    "quality_review",
    "care_continuity",
    "data_correction",
    "other",
    # Clinical cases (0074) — a clinician with no treatment relationship
    # to this patient. Distinct codes rather than folding them into
    # `other`, because "an unrelated clinician opened this chart" and
    # "the reason was an emergency" are separate facts and a review needs
    # to filter on the second.
    "emergency_care",
    "care_coordination",
    "patient_request",
    "technical_support",
]

# `other` is the escape hatch and must not become the way to skip the
# justification. The DB CHECK enforces the same floor; this is the copy
# that produces a 422 the user can act on instead of a 500.
_OTHER_MIN_NOTE_CHARS = 10


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PhiAccessCreate(_Strict):
    # 'report' since S14; 'patient' since S15 — one patient's
    # demographics/timeline in core-service go through the same door.
    resource_kind: Literal["report", "patient"] = "report"
    resource_id: UUID = Field(description="The report or patient to open.")
    reason_code: ReasonCode
    reason_note: str = Field(default="", max_length=2000)
    reauth_ticket: str = Field(
        min_length=1,
        max_length=512,
        description="Single-use ticket from POST /auth/reauth (auth-service).",
    )


class PhiAccessOut(_Strict):
    id: UUID
    requested_by: UUID
    resource_kind: str
    resource_id: UUID
    patient_id: UUID | None
    reason_code: str
    reason_note: str
    status: str
    granted_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    revoked_by: UUID | None
    use_count: int
    last_used_at: datetime | None


class PhiAccessList(_Strict):
    items: list[PhiAccessOut]


class ReasonOption(_Strict):
    code: str
    label_uk: str
    label_en: str
    requires_note: bool


class ReasonCatalog(_Strict):
    reasons: list[ReasonOption]
    grant_ttl_minutes: int
    note_min_chars: int


# The dropdown, served rather than hard-coded in the SPA: the vocabulary
# is pinned by a DB CHECK, and a client that invents an eighth option
# would only discover it at INSERT time.
_REASONS: tuple[ReasonOption, ...] = (
    ReasonOption(
        code="patient_complaint",
        label_uk="Скарга пацієнта",
        label_en="Patient complaint",
        requires_note=False,
    ),
    ReasonOption(
        code="legal_request",
        label_uk="Юридичний запит",
        label_en="Legal or regulatory request",
        requires_note=False,
    ),
    ReasonOption(
        code="billing_dispute",
        label_uk="Спір щодо оплати",
        label_en="Billing dispute",
        requires_note=False,
    ),
    ReasonOption(
        code="quality_review",
        label_uk="Перевірка якості",
        label_en="Quality review",
        requires_note=False,
    ),
    ReasonOption(
        code="care_continuity",
        label_uk="Безперервність надання допомоги",
        label_en="Continuity of care",
        requires_note=False,
    ),
    ReasonOption(
        code="data_correction",
        label_uk="Виправлення даних",
        label_en="Data correction",
        requires_note=False,
    ),
    # ── Clinical cases (0074) ────────────────────────────────────────
    # A clinician who is not on this patient's care. Ordered before
    # `other` so the dropdown reads administrative → clinical → escape
    # hatch. `emergency_care` is deliberately note-free: demanding prose
    # from someone mid-resuscitation is how a control gets routed around.
    ReasonOption(
        code="emergency_care",
        label_uk="Невідкладна допомога",
        label_en="Emergency care",
        requires_note=False,
    ),
    ReasonOption(
        code="care_coordination",
        label_uk="Координація допомоги",
        label_en="Care coordination",
        requires_note=False,
    ),
    ReasonOption(
        code="patient_request",
        label_uk="Запит пацієнта",
        label_en="Patient request",
        requires_note=False,
    ),
    ReasonOption(
        code="technical_support",
        label_uk="Технічна підтримка",
        label_en="Technical support",
        # The one clinical-era code that demands prose: "support" covers
        # everything from a rendering bug to reading a whole chart, and
        # the difference has to be written down.
        requires_note=True,
    ),
    ReasonOption(
        code="other",
        label_uk="Інше (вкажіть причину)",
        label_en="Other (state the reason)",
        requires_note=True,
    ),
)

# Codes that demand a written justification, derived from the catalogue
# above so the two can never disagree. `other` was the only such code
# before the hotfix and the check was written against it by name.
_NOTE_REQUIRED_CODES: frozenset[str] = frozenset(
    opt.code for opt in _REASONS if opt.requires_note
)


def _to_out(row: asyncpg.Record) -> PhiAccessOut:
    return PhiAccessOut(
        id=row["id"],
        requested_by=row["requested_by"],
        resource_kind=row["resource_kind"],
        resource_id=row["resource_id"],
        patient_id=row["patient_id"],
        reason_code=row["reason_code"],
        reason_note=row["reason_note"],
        status=row["status"],
        granted_at=row["granted_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        revoked_by=row["revoked_by"],
        use_count=row["use_count"],
        last_used_at=row["last_used_at"],
    )


def _http_error(status_code: int, detail: str, **extras: object) -> HTTPException:
    exc = HTTPException(status_code=status_code, detail=detail)
    exc.problem_extras = extras  # type: ignore[attr-defined]
    return exc


@router.get(
    "/reasons",
    response_model=ReasonCatalog,
    summary="The break-glass reason vocabulary and grant window.",
)
async def list_reasons(
    claims: Annotated[
        Claims, Depends(requires("phi_access.request", "phi_access_request"))
    ],
) -> ReasonCatalog:
    return ReasonCatalog(
        reasons=list(_REASONS),
        grant_ttl_minutes=settings.phi_access_grant_ttl_minutes,
        note_min_chars=_OTHER_MIN_NOTE_CHARS,
    )


@router.post(
    "",
    response_model=PhiAccessOut,
    status_code=status.HTTP_201_CREATED,
    summary="Break glass on one report or patient: reason + password re-entry → a time-limited grant.",
)
async def create_request(
    body: PhiAccessCreate,
    claims: Annotated[
        Claims, Depends(requires("phi_access.request", "phi_access_request"))
    ],
) -> PhiAccessOut:
    note = body.reason_note.strip()
    if body.reason_code in _NOTE_REQUIRED_CODES and len(note) < _OTHER_MIN_NOTE_CHARS:
        _rejected_counter.add(1, {"cause": "note_too_short"})
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"reason_code {body.reason_code!r} requires a note of at least "
            f"{_OTHER_MIN_NOTE_CHARS} characters explaining the access",
            code="reason_note_required",
            min_chars=_OTHER_MIN_NOTE_CHARS,
        )

    state = get_state()
    ticket_hash = hashlib.sha256(body.reauth_ticket.encode("utf-8")).digest()
    expires_at = datetime.now(UTC) + timedelta(
        minutes=settings.phi_access_grant_ttl_minutes
    )

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        # The resource must exist BEFORE the ticket is spent — otherwise a
        # typo in resource_id burns the step-up and the user has to
        # retype their password to correct it. (RLS scopes both lookups,
        # so cross-tenant ids read as "not found".)
        report = None
        if body.resource_kind == "report":
            report = await repo.fetch_report(conn, report_id=body.resource_id)
            if report is None:
                _rejected_counter.add(1, {"cause": "report_not_found"})
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND, detail="report not found"
                )
            patient_id = report.patient_id
        else:
            patient = await repo.fetch_patient_label(
                conn, patient_id=body.resource_id
            )
            if patient is None:
                _rejected_counter.add(1, {"cause": "patient_not_found"})
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND, detail="patient not found"
                )
            # For patient-kind grants the denormalised patient_id IS the
            # resource — kept redundantly so the oversight view's
            # group-by-patient works identically across both kinds.
            patient_id = body.resource_id

        consumed = await grants.consume_reauth_ticket(
            conn,
            subject_sub=claims.sub,
            ticket_hash=ticket_hash,
            purpose="phi_access_request",
        )
        if not consumed:
            _rejected_counter.add(1, {"cause": "bad_ticket"})
            await _audit(
                state,
                claims,
                kind=audit_kinds.PHI_ACCESS_DENIED,
                target_kind=body.resource_kind,
                target_id=body.resource_id,
                payload={
                    "reason_code": body.reason_code,
                    "cause": "reauth_ticket_invalid",
                },
            )
            # One code for expired, already-spent, wrong-user and
            # never-existed alike: the client's remedy is identical (send
            # the user back through the password step), and distinguishing
            # them would confirm ticket values to a prober.
            raise _http_error(
                status.HTTP_401_UNAUTHORIZED,
                "the re-authentication ticket is invalid, expired or already used",
                code="reauth_required",
            )

        row = await grants.create_grant(
            conn,
            tenant_id=claims.tid,
            requested_by=claims.sub,
            resource_kind=body.resource_kind,
            resource_id=body.resource_id,
            patient_id=patient_id,
            reason_code=body.reason_code,
            reason_note=note,
            expires_at=expires_at,
        )

    _granted_counter.add(1, {"reason_code": body.reason_code})
    _break_glass_counter.add(
        1,
        {
            "tenant_id": str(claims.tid),
            "reason": body.reason_code,
            "resource_kind": body.resource_kind,
            # The role they were acting under. Since the hotfix this is no
            # longer always `tenant_admin` — an unrelated clinician breaks
            # glass too, and "which population is using this door" is the
            # first thing a review wants to split on.
            "role": (claims.roles[0] if claims.roles else "unknown"),
            "principal": str(claims.sub),
        },
    )

    # The audit row carries the reason NOTE — it is the justification, it
    # is written by staff about staff conduct, and the chain is where a
    # compliance review looks for it. (Notifications get only the code;
    # see the render allow-list.)
    await _audit(
        state,
        claims,
        kind=audit_kinds.PHI_ACCESS_GRANTED,
        target_kind=body.resource_kind,
        target_id=body.resource_id,
        payload={
            "grant_id": str(row["id"]),
            "reason_code": body.reason_code,
            "reason_note": note,
            "expires_at": expires_at.isoformat(),
            "patient_id": str(patient_id) if patient_id else None,
        },
    )

    # Tell the clinicians whose report it is. Fire-and-forget by design
    # (ADR-0029) — but note the asymmetry with the audit write above: the
    # grant is already committed, so a bus outage costs a notification,
    # never the record that this happened. A patient record has no author
    # to tell — for patient-kind grants the after-the-fact control is the
    # `sec` audit trail plus the oversight list only.
    if report is not None:
        await emit_report_event(
            state.redis,
            category=Category.PHI_ACCESS_GRANTED,
            tenant_id=claims.tid,
            report_id=body.resource_id,
            report_code=report.code,
            actor_user_id=claims.sub,
            primary_author_id=report.primary_author_id,
            co_author_ids=tuple(report.co_author_ids),
            extra_payload={
                "reason_code": body.reason_code,
                "requested_by_display": _actor_display(claims),
                "expires_at": expires_at.isoformat(timespec="minutes"),
            },
        )

    return _to_out(row)


@router.get(
    "",
    response_model=PhiAccessList,
    summary="Oversight: who broke glass, on what, why, and how often they used it.",
)
async def list_requests(
    claims: Annotated[Claims, Depends(requires("phi_access.read", "phi_access_request"))],
    resource_id: Annotated[UUID | None, Query()] = None,
    requested_by: Annotated[UUID | None, Query()] = None,
    active_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PhiAccessList:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await grants.list_grants(
            conn,
            requested_by=requested_by,
            resource_id=resource_id,
            active_only=active_only,
            limit=limit,
        )
    return PhiAccessList(items=[_to_out(r) for r in rows])


@router.post(
    "/{grant_id}/revoke",
    response_model=PhiAccessOut,
    summary="Close an open grant early.",
)
async def revoke_request(
    grant_id: UUID,
    claims: Annotated[Claims, Depends(requires("phi_access.read", "phi_access_request"))],
) -> PhiAccessOut:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await grants.revoke_grant(
            conn, grant_id=grant_id, revoked_by=claims.sub
        )
        if row is None:
            existing = await grants.get_grant(conn, grant_id=grant_id)
            if existing is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="grant not found")
            raise _http_error(
                status.HTTP_409_CONFLICT,
                "grant is already revoked",
                code="already_revoked",
            )
    await _audit(
        state,
        claims,
        kind=audit_kinds.PHI_ACCESS_REVOKED,
        target_kind=row["resource_kind"],
        target_id=row["resource_id"],
        payload={"grant_id": str(grant_id), "reason_code": row["reason_code"]},
    )
    return _to_out(row)


# ── helpers ──────────────────────────────────────────────────────────


def _actor_display(claims: Claims) -> str:
    """A human label for the admin, for the clinician's notification.

    Staff identity, never a patient's — and clamped, because it lands in
    a payload with a length budget (libs/notification_events).
    """
    label = claims.name or claims.preferred_username or "Адміністратор"
    return label[:120]


async def _audit(
    state: object,
    claims: Claims,
    *,
    kind: str,
    target_kind: str,
    target_id: UUID,
    payload: dict[str, object],
) -> None:
    try:
        await state.audit_writer.write_event(  # type: ignore[attr-defined]
            tenant_id=claims.tid,
            kind=kind,
            actor_sub=claims.sub,
            actor_role=(claims.roles[0] if claims.roles else None),
            target_kind=target_kind,
            target_id=target_id,
            payload=payload,
            severity=Severity.SEC,
        )
    except Exception as exc:
        logger.warning("phi_access.audit_write_failed", extra={"error": str(exc)})
