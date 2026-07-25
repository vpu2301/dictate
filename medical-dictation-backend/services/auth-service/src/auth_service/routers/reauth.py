"""POST /auth/reauth — prove the person at the keyboard is still the
account holder, and mint a single-use ticket that says so.

A live access token proves a session was started at some point. It does
not prove who is holding the laptop right now. Break-glass access to a
patient's report (S14) is exactly the class of act where that difference
matters, so the flow is:

    SPA  ──{password}──▶  auth-service  ──password_grant──▶  Keycloak
                                │
                                ├─ store sha256(ticket)
                                └─ 200 {reauth_ticket, expires_in}

    SPA  ──{reauth_ticket, reason}──▶  report-service
                                            └─ consume ticket (atomic,
                                               single-use), then grant

Why a ticket table rather than "just send the fresh access token":

  * The consumer would have to trust `iat` as a proxy for "typed their
    password", but an ordinary login produces an identical token — the
    step-up would be satisfied by having logged in five minutes ago.
  * A ticket is single-use by construction (`consumed_at`), so a replay
    of the same request body cannot mint a second grant.
  * `purpose` binds the ticket to the act it was minted for, so a step-up
    collected for one high-risk action can never be redeemed against a
    different one added later.

The password itself never leaves this module: it goes straight to
Keycloak and is not logged, audited, or stored in any form.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict, Field

from audit import Severity
from auth import Claims
from db import tenant_connection

from .. import audit_kinds
from ..config import settings
from ..deps import current_user, get_state
from ..keycloak_client import KeycloakError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

_meter = metrics.get_meter("mdx.auth")
_reauth_counter = _meter.create_counter(
    "mdx_auth_reauth_total",
    description="Step-up re-authentication attempts by outcome",
    unit="1",
)

# The vocabulary of acts a step-up may be minted for. Mirrors the CHECK
# on `auth_reauth_tickets.purpose` (migration 0056) — a member here
# without a matching CHECK value is an INSERT that fails at runtime.
ReauthPurpose = Literal["phi_access_request"]


class ReauthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=512)
    purpose: ReauthPurpose = "phi_access_request"


class ReauthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reauth_ticket: str
    expires_in: int
    purpose: str


def _hash_ticket(ticket: str) -> bytes:
    return hashlib.sha256(ticket.encode("utf-8")).digest()


async def _resolve_username(state: object, claims: Claims) -> str | None:
    """The identifier Keycloak's password grant expects.

    `preferred_username` is present on every token the realm issues, but
    the DB email is a deliberate fallback: a realm reconfigured to omit
    the profile scope would otherwise make step-up impossible for
    everyone at once, with no way to tell from the 401.
    """
    if claims.preferred_username:
        return claims.preferred_username
    async with tenant_connection(state.app_pool, claims.tid) as conn:  # type: ignore[attr-defined]
        email = await conn.fetchval("SELECT email FROM users WHERE sub = $1", claims.sub)
    return str(email) if email else None


@router.post(
    "/reauth",
    response_model=ReauthResponse,
    status_code=status.HTTP_200_OK,
    summary="Re-enter your password to mint a single-use step-up ticket",
)
async def reauth(
    body: ReauthRequest,
    claims: Annotated[Claims, Depends(current_user)],
) -> ReauthResponse:
    state = get_state()

    username = await _resolve_username(state, claims)
    if username is None:
        # Not the caller's fault and not a credential problem — say so
        # rather than returning a 401 they will retype their password at.
        logger.error("auth.reauth.no_username", extra={"sub": str(claims.sub)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="cannot resolve the account identifier for re-authentication",
        )

    try:
        await state.keycloak.password_grant(username=username, password=body.password)
    except KeycloakError as exc:
        _reauth_counter.add(1, {"result": "invalid_password", "purpose": body.purpose})
        await _audit(
            state,
            claims,
            kind=audit_kinds.AUTH_REAUTH_FAILED,
            payload={"purpose": body.purpose, "kc_status": exc.status},
        )
        # Deliberately indistinguishable from any other credential
        # failure. Brute-force protection is Keycloak's realm-level
        # detector, which counts these grants like any other — a
        # repeated wrong password here locks the account there.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="password does not match",
            headers={"WWW-Authenticate": 'Bearer realm="medical-dictation"'},
        ) from exc

    ticket = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(seconds=settings.reauth_ticket_ttl_seconds)
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        await conn.execute(
            """
            INSERT INTO auth_reauth_tickets
                (tenant_id, subject_sub, ticket_hash, purpose, expires_at)
            VALUES ($1, $2, $3, $4, $5)
            """,
            claims.tid,
            claims.sub,
            _hash_ticket(ticket),
            body.purpose,
            expires_at,
        )
        # Opportunistic sweep of this tenant's dead tickets. They carry no
        # PHI and no authority, so there is nothing to retain, and folding
        # it in here means the table needs no cron of its own.
        await conn.execute(
            """
            DELETE FROM auth_reauth_tickets
            WHERE expires_at < now() - INTERVAL '1 day'
               OR (consumed_at IS NOT NULL
                   AND consumed_at < now() - INTERVAL '1 day')
            """
        )

    _reauth_counter.add(1, {"result": "success", "purpose": body.purpose})
    await _audit(
        state,
        claims,
        kind=audit_kinds.AUTH_REAUTH_SUCCEEDED,
        payload={"purpose": body.purpose},
    )
    return ReauthResponse(
        reauth_ticket=ticket,
        expires_in=settings.reauth_ticket_ttl_seconds,
        purpose=body.purpose,
    )


async def _audit(
    state: object,
    claims: Claims,
    *,
    kind: str,
    payload: dict[str, object],
) -> None:
    """Best-effort audit. A step-up must not fail because the chain is
    unavailable — but the warning below is the operator's signal that a
    security-relevant event went unrecorded."""
    try:
        await state.audit_writer.write_event(  # type: ignore[attr-defined]
            tenant_id=claims.tid,
            kind=kind,
            actor_sub=claims.sub,
            actor_role=(claims.roles[0] if claims.roles else None),
            target_kind="user",
            target_id=claims.sub,
            payload=payload,
            severity=Severity.SEC,
        )
    except Exception as exc:
        logger.warning("auth.reauth.audit_write_failed", extra={"error": str(exc)})
