"""TOTP enrolment + admin reset (sprint 16 — the sprint-02 MFA IOU).

Flow (ADR-0039):

1. ``POST /auth/mfa/enrol``  — any authenticated user. Generates a fresh
   TOTP secret, stores it envelope-encrypted in the user's Keycloak
   attributes under ``totp_secret_enc_pending``, returns the
   ``otpauth://`` provisioning URI (the SPA renders the QR) plus the
   base32 secret for manual entry. Enrolment is *pending* until verified
   — a half-finished enrolment never locks anyone out.
2. ``POST /auth/mfa/verify`` — completes enrolment: a valid code
   promotes the pending secret to ``totp_secret_enc``, stamps
   ``mfa_enrolled=true`` (the protocol mapper turns that into the
   ``mfa``/``mfa_enrolled`` claims), and audits ``auth.mfa.enrolled``.
   From the next login on, auth-service refuses to release a token
   without a valid TOTP code.
3. ``DELETE /auth/mfa/{sub}`` — admin-assisted reset (lost phone).
   ``user.reset_mfa`` permission, audited ``sec`` as ``user.reset_mfa``,
   revokes the user's live Keycloak sessions.

The endpoints exist regardless of ``MDX_REQUIRE_MFA`` so a tenant can
stage enrolment before enforcement; the surface itself is switched by
``MDX_MFA_ENROLMENT_ENABLED`` (off in dev by default).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict, Field

from audit import Severity
from auth import Claims
from crypto import CryptoError, MasterKeyError
from db import tenant_connection

from .. import audit_kinds, totp
from ..config import settings
from ..deps import current_user, get_state, requires, requires_mfa
from ..keycloak_client import KeycloakError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/mfa", tags=["mfa"])

_meter = metrics.get_meter("mdx.auth.mfa")
_mfa_counter = _meter.create_counter(
    "mdx_auth_mfa_events_total",
    description="MFA lifecycle events by kind/outcome",
    unit="1",
)

ATTR_SECRET = "totp_secret_enc"
ATTR_SECRET_PENDING = "totp_secret_enc_pending"
ATTR_ENROLLED = "mfa_enrolled"
ATTR_ENROLLED_AT = "mfa_enrolled_at"


class EnrolResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provisioning_uri: str
    secret: str
    issuer: str
    account: str


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=6, max_length=8)


class VerifyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enrolled: bool
    enrolled_at: str


def _enrolment_switched_off() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="MFA enrolment is not enabled on this deployment "
        "(MDX_MFA_ENROLMENT_ENABLED)",
    )


def _attr_first(rep: dict[str, object], key: str) -> str | None:
    attrs = rep.get("attributes")
    if isinstance(attrs, dict):
        val = attrs.get(key)
        if isinstance(val, list) and val and isinstance(val[0], str) and val[0]:
            return val[0]
    return None


async def _envelope_or_503() -> object:
    state = get_state()
    try:
        return await state.get_envelope()
    except MasterKeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"MFA secret store unavailable: {exc}",
        ) from exc


@router.post(
    "/enrol",
    response_model=EnrolResponse,
    summary="Begin TOTP enrolment — returns the provisioning URI / QR payload",
)
async def enrol(claims: Annotated[Claims, Depends(current_user)]) -> EnrolResponse:
    if not settings.mfa_enrolment_enabled:
        raise _enrolment_switched_off()
    state = get_state()
    envelope = await _envelope_or_503()

    try:
        rep = await state.keycloak.get_user(claims.sub)
    except KeycloakError as exc:
        raise HTTPException(status_code=503, detail="identity provider error") from exc
    if _attr_first(rep, ATTR_SECRET):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already enrolled; ask an administrator to reset MFA first",
        )

    secret = totp.generate_secret()
    packed = await totp.encrypt_secret(
        envelope,  # type: ignore[arg-type]
        secret=secret,
        tenant_id=claims.tid,
        sub=claims.sub,
    )
    account = claims.preferred_username or claims.email or str(claims.sub)
    try:
        await state.keycloak.set_user_attributes(
            claims.sub, {ATTR_SECRET_PENDING: [packed]}
        )
    except KeycloakError as exc:
        raise HTTPException(status_code=503, detail="identity provider error") from exc

    _mfa_counter.add(1, {"kind": "enrol_started", "outcome": "ok"})
    return EnrolResponse(
        provisioning_uri=totp.provisioning_uri(
            secret, account=account, issuer=settings.mfa_totp_issuer
        ),
        secret=secret,
        issuer=settings.mfa_totp_issuer,
        account=account,
    )


@router.post(
    "/verify",
    response_model=VerifyResponse,
    summary="Complete TOTP enrolment with a first valid code",
)
async def verify(
    body: VerifyRequest, claims: Annotated[Claims, Depends(current_user)]
) -> VerifyResponse:
    if not settings.mfa_enrolment_enabled:
        raise _enrolment_switched_off()
    state = get_state()
    envelope = await _envelope_or_503()

    try:
        rep = await state.keycloak.get_user(claims.sub)
    except KeycloakError as exc:
        raise HTTPException(status_code=503, detail="identity provider error") from exc
    packed = _attr_first(rep, ATTR_SECRET_PENDING)
    if packed is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no pending enrolment; call POST /auth/mfa/enrol first",
        )
    try:
        secret = await totp.decrypt_secret(
            envelope,  # type: ignore[arg-type]
            packed=packed,
            tenant_id=claims.tid,
            sub=claims.sub,
        )
    except CryptoError as exc:
        logger.error("mfa.pending_secret_undecryptable", extra={"error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="pending enrolment is unreadable; restart enrolment",
        ) from exc

    if not totp.verify_code(secret, body.code):
        _mfa_counter.add(1, {"kind": "enrol_verify", "outcome": "invalid_code"})
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid TOTP code"
        )

    enrolled_at = datetime.now(UTC).isoformat()
    try:
        await state.keycloak.set_user_attributes(
            claims.sub,
            {
                ATTR_SECRET: [packed],
                ATTR_SECRET_PENDING: [],
                ATTR_ENROLLED: ["true"],
                ATTR_ENROLLED_AT: [enrolled_at],
            },
        )
    except KeycloakError as exc:
        raise HTTPException(status_code=503, detail="identity provider error") from exc

    # Keep the roster's mfa_enrolled_at column in step (admin listing), and
    # close any standing access-review reminder in the SAME transaction.
    #
    # This is the only way an `mfa_reminders` row is ever closed — there is
    # no dismiss button anywhere in the product. Enrolling is what makes the
    # banner go away, which is the entire design of the reminder.
    try:
        async with (
            tenant_connection(state.tenant_writer_pool, claims.tid) as conn,
            conn.transaction(),
        ):
            await conn.execute(
                "UPDATE users SET mfa_enrolled_at = now(), updated_at = now() "
                "WHERE sub = $1",
                claims.sub,
            )
            await conn.execute(
                "UPDATE mfa_reminders SET resolved_at = now() "
                "WHERE subject_sub = $1 AND resolved_at IS NULL",
                claims.sub,
            )
    except Exception as db_exc:  # noqa: BLE001 — KC is the source of truth here
        logger.warning("mfa.users_row_update_failed", extra={"error": str(db_exc)})

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.AUTH_MFA_ENROLLED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="user",
        target_id=str(claims.sub),
        payload={},
        severity=Severity.INFO,
    )
    _mfa_counter.add(1, {"kind": "enrol_verify", "outcome": "ok"})
    return VerifyResponse(enrolled=True, enrolled_at=enrolled_at)


@router.delete(
    "/{sub}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Admin-assisted MFA reset (lost device)",
    dependencies=[Depends(requires_mfa())],
)
async def reset(
    sub: UUID,
    claims: Annotated[Claims, Depends(requires("user.reset_mfa", "user"))],
) -> None:
    state = get_state()

    # Tenant scoping: the target must be a user of the caller's tenant —
    # the RLS-scoped read is the check (same pattern as reactivate).
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await conn.fetchrow("SELECT sub FROM users WHERE sub = $1", sub)
    if row is None:
        raise HTTPException(status_code=404, detail="user not found in this tenant")

    try:
        await state.keycloak.set_user_attributes(
            sub,
            {
                ATTR_SECRET: [],
                ATTR_SECRET_PENDING: [],
                ATTR_ENROLLED: [],
                ATTR_ENROLLED_AT: [],
            },
        )
        # A reset without session revocation would leave a live mfa=true
        # session usable by whoever holds it.
        await state.keycloak.logout_user(sub)
    except KeycloakError as exc:
        raise HTTPException(status_code=503, detail="identity provider error") from exc

    # A resolved `mfa_reminders` row is deliberately left resolved here. A
    # reset is the lost-phone path — the user is mid-recovery and the grace
    # flow already walks them into enrolment — so silently reopening an old
    # finding would put a scolding banner in front of someone who is doing
    # the right thing. If they still have not enrolled by the next review, a
    # reviewer raises it again and the upsert reopens the same row.
    try:
        async with tenant_connection(state.tenant_writer_pool, claims.tid) as conn:
            await conn.execute(
                "UPDATE users SET mfa_enrolled_at = NULL, updated_at = now() "
                "WHERE sub = $1",
                sub,
            )
    except Exception as db_exc:  # noqa: BLE001
        logger.warning("mfa.users_row_reset_failed", extra={"error": str(db_exc)})

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.USER_RESET_MFA,
        actor_sub=claims.sub,
        actor_role="tenant_admin",
        target_kind="user",
        target_id=str(sub),
        payload={"sessions_revoked": True},
        severity=Severity.SEC,
    )
    _mfa_counter.add(1, {"kind": "reset", "outcome": "ok"})
