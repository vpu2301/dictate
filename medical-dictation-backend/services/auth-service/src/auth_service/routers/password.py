"""Password recovery: forgot, reset, change, and the "that wasn't me" door.

Four public-facing acts, three of which run with no session at all.

    SPA ──{email}──▶ /auth/password/forgot ──▶ 202 (always)
                              └─ mint token, enqueue mail

    inbox ──{token}──▶ /auth/password/reset ──▶ set password in Keycloak,
                              kill sessions, enqueue security notification

    SPA ──{current, new}──▶ /auth/password/change  (authenticated)
                              └─ same tail: kill other sessions, notify

    inbox ──{token}──▶ /auth/security/lockdown ──▶ kill everything,
                              hand back a fresh reset token

── The three rules that shape every handler here ─────────────────────

**1. Never confirm whether an account exists.** ``/forgot`` returns the
same 202 and the same body for a real address, an unknown one, a
deactivated one, and one that tripped the rate limiter. Anything else
turns the endpoint into a membership oracle for a clinical system,
where "is this doctor a Klarnote user" is itself worth knowing. The
uniform response costs nothing; the timing difference between a real
and unknown address is not eliminated, and is noted as accepted
residual risk (evening it out would mean an artificial delay on every
request, which is a self-inflicted DoS lever).

**2. A password change ends every other session.** Changing a password
because you fear it was stolen, and leaving the thief's session live, is
the failure mode users least expect. Both reset and change push the
account onto the sprint-16 revocation denylist and call Keycloak's
logout — belt and braces, because the denylist is fail-open by design
and a Redis outage must not silently downgrade this.

**3. The token is never stored in plaintext.** Only ``sha256`` reaches
the tokens table. The mailed URL exists in ``auth_mail_outbox.secret_fields``
for the seconds between enqueue and send, and migration 0076's CHECK
constraint makes clearing it a database invariant rather than a habit.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from audit import Severity
from auth import Claims
from db import tenant_connection

from .. import audit_kinds
from ..config import settings
from ..deps import current_user, get_state
from ..domain import compose, password_policy
from ..domain import copy as copy_mod
from ..domain import repository as repo
from ..keycloak_client import KeycloakError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["password"])

_meter = metrics.get_meter("mdx.auth")
_password_counter = _meter.create_counter(
    "mdx_auth_password_total",
    description="Password-recovery operations by act and outcome",
    unit="1",
)

PURPOSE_RESET = "password_reset"
PURPOSE_LOCKDOWN = "account_lockdown"

# Tokens are 32 bytes of CSPRNG output, URL-safe. 256 bits is far past
# what a 30-minute single-use credential needs, and the cost of the
# extra characters in a mailed URL is nil.
_TOKEN_BYTES = 32


# ── Wire models ──────────────────────────────────────────────────────


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ForgotRequest(_Strict):
    email: EmailStr
    # Preferred language for the mail. The SPA knows what the user is
    # reading the interface in; there is no session to infer it from.
    lang: Literal["en", "de", "uk"] | None = None


class ForgotResponse(_Strict):
    # Intentionally content-free. See rule 1.
    status: Literal["accepted"] = "accepted"


class ResetRequest(_Strict):
    token: str = Field(min_length=16, max_length=256)
    new_password: str = Field(min_length=1, max_length=512)


class ChangeRequest(_Strict):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=512)


class LockdownRequest(_Strict):
    token: str = Field(min_length=16, max_length=256)


class LockdownResponse(_Strict):
    """What the lockdown page needs to finish the job.

    A fresh reset token comes back in the body rather than by email: the
    user has already proved possession of the mailbox by following the
    link, and sending a second mail would make them wait for a relay
    while an attacker is in their account.
    """

    reset_token: str
    expires_in: int
    sessions_revoked: bool


class PolicyResponse(_Strict):
    min_length: int
    max_length: int


class PasswordEvent(_Strict):
    kind: str
    via: str
    client_label: str
    created_at: datetime


class SessionSummary(_Strict):
    id: str
    ip_address: str
    started_at: datetime | None
    last_access_at: datetime | None
    current: bool


# ── Helpers ──────────────────────────────────────────────────────────


def _hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _client_ip(request: Request) -> str:
    """Best-effort client address.

    Trusts ``X-Forwarded-For``'s first hop because the deployment always
    sits behind our own edge proxy. That is only sound as long as the
    edge overwrites the header rather than appending to a
    client-supplied one — if this ever runs without that proxy, the
    per-IP rate limit becomes trivially evadable by spoofing the header.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _enabled_or_404() -> None:
    """A disabled feature must not be discoverable.

    404, not 403: a deployment that has not configured mail should look
    like one that has no such endpoint, so a prober learns nothing about
    what is switched off.
    """
    if not settings.password_reset_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Not Found"
        )


def _reject_weak(
    password: str, *, email: str = "", display_name: str = ""
) -> None:
    result = password_policy.check_password(
        password,
        min_length=settings.password_min_length,
        email=email,
        display_name=display_name,
    )
    if result.ok:
        return
    exc = HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="the chosen password does not meet the policy",
    )
    # Machine-readable so the SPA can render each reason in the user's
    # language and point at the field, instead of echoing English prose.
    exc.problem_extras = {  # type: ignore[attr-defined]
        "code": "weak_password",
        "reasons": list(result.reasons),
        "min_length": max(settings.password_min_length, password_policy.MIN_LENGTH_FLOOR),
    }
    raise exc


async def _audit(
    state: Any,
    *,
    tenant_id: UUID,
    sub: UUID,
    kind: str,
    payload: dict[str, Any],
    severity: Severity = Severity.SEC,
) -> None:
    """Best-effort audit — house rule on security paths.

    A password reset must not fail because the hash chain is
    unavailable, but the warning is the operator's signal that a
    security-relevant event went unrecorded.
    """
    try:
        await state.audit_writer.write_event(
            tenant_id=tenant_id,
            kind=kind,
            actor_sub=sub,
            target_kind="user",
            target_id=sub,
            payload=payload,
            severity=severity,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "auth.password.audit_write_failed",
            extra={"kind": kind, "error": str(exc)},
        )


async def _revoke_all_sessions(state: Any, sub: UUID) -> bool:
    """End every live session for a user. True if fully successful.

    Two mechanisms because they fail differently. Keycloak's logout
    invalidates the refresh tokens, which stops new access tokens being
    minted; the denylist stops the access tokens already issued, which
    Keycloak cannot reach. Neither alone closes the window.
    """
    ok = True
    try:
        await state.keycloak.logout_user(sub)
    except KeycloakError as exc:
        ok = False
        logger.error(
            "auth.password.keycloak_logout_failed",
            extra={"sub": str(sub), "kc_status": exc.status},
        )
    if state.denylist is not None:
        try:
            await state.denylist.revoke_sub(
                str(sub), ttl_seconds=settings.revoked_sub_ttl_seconds
            )
        except Exception as exc:  # noqa: BLE001
            ok = False
            logger.error(
                "auth.password.denylist_push_failed",
                extra={"sub": str(sub), "error": str(exc)},
            )
    return ok


async def _issue_token(
    conn: Any,
    *,
    tenant_id: UUID,
    sub: UUID,
    purpose: str,
    ttl_seconds: int,
    ip_hash: str,
) -> str:
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    await repo.insert_token(
        conn,
        tenant_id=tenant_id,
        subject_sub=sub,
        token_hash=_hash_token(token),
        purpose=purpose,
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        requested_ip_hash=ip_hash,
    )
    return token


async def _enqueue_security_notification(
    conn: Any,
    *,
    tenant_id: UUID,
    sub: UUID,
    email: str,
    display_name: str,
    lang: str,
    user_agent: str,
    ip_hash: str,
) -> None:
    """Mint a lockdown token and queue the "your password changed" mail.

    Runs inside the caller's transaction, so the token and the mail that
    carries it are committed together — a token with no mail is a dead
    row, a mail with no token is a broken button.
    """
    lockdown_token = await _issue_token(
        conn,
        tenant_id=tenant_id,
        sub=sub,
        purpose=PURPOSE_LOCKDOWN,
        ttl_seconds=settings.lockdown_token_ttl_seconds,
        ip_hash=ip_hash,
    )
    await repo.enqueue_mail(
        conn,
        tenant_id=tenant_id,
        subject_sub=sub,
        kind=copy_mod.KIND_PASSWORD_CHANGED,
        lang=lang,
        to_address=email,
        render_fields=compose.password_changed_fields(
            lang=lang,
            email=email,
            display_name=display_name,
            user_agent=user_agent,
            support_url=settings.support_url,
            app_base_url=settings.app_base_url,
            changed_at=datetime.now(UTC),
            lockdown_ttl_seconds=settings.lockdown_token_ttl_seconds,
        ),
        secret_fields=compose.lockdown_secret_fields(
            app_base_url=settings.app_base_url, token=lockdown_token
        ),
    )


# ── Endpoints ────────────────────────────────────────────────────────


@router.get(
    "/password/policy",
    response_model=PolicyResponse,
    summary="The password rules this deployment enforces",
)
async def password_policy_info() -> PolicyResponse:
    """So the SPA's strength meter agrees with the server.

    Unauthenticated: the reset page needs it before anyone has a
    session, and the numbers are not a secret — an attacker learns the
    minimum length by trying a short password once.
    """
    _enabled_or_404()
    return PolicyResponse(
        min_length=max(
            settings.password_min_length, password_policy.MIN_LENGTH_FLOOR
        ),
        max_length=password_policy.MAX_LENGTH,
    )


@router.post(
    "/password/forgot",
    response_model=ForgotResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request a password-reset link",
)
async def forgot_password(body: ForgotRequest, request: Request) -> ForgotResponse:
    _enabled_or_404()
    state = get_state()

    email = str(body.email).strip()
    ip = _client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    ip_hash = compose.hash_ip(ip, salt=settings.password_reset_ip_hash_salt.value())

    # Rate limit BEFORE the account lookup, so a refused request costs no
    # database work and reveals nothing through timing.
    if state.password_rate_limiter is not None:
        allowed = await state.password_rate_limiter.check(ip=ip, email=email)
        if not allowed:
            _password_counter.add(1, {"act": "forgot", "result": "rate_limited"})
            logger.warning(
                "auth.password.reset_rate_limited", extra={"ip_hash": ip_hash}
            )
            # Same 202 as every other outcome. A 429 here would confirm
            # nothing about the account, but it WOULD tell an attacker
            # their sweep is being counted, which is free intelligence.
            return ForgotResponse()

    async with state.app_pool.acquire() as conn:
        account = await repo.resolve_account_by_email(conn, email=email)

    if account is None or str(account["status"]) != "active":
        # Unknown or deactivated. No token, no mail, no audit row — see
        # the note in audit_kinds about enumeration.
        _password_counter.add(1, {"act": "forgot", "result": "no_account"})
        logger.info(
            "auth.password.reset_requested_unknown", extra={"ip_hash": ip_hash}
        )
        return ForgotResponse()

    tenant_id = UUID(str(account["tenant_id"]))
    sub = UUID(str(account["subject_sub"]))
    lang = copy_mod.normalise_lang(body.lang)
    display_name = str(account["display_name"] or "")
    account_email = str(account["email"])

    async with tenant_connection(state.app_pool, tenant_id) as conn:
        token = await _issue_token(
            conn,
            tenant_id=tenant_id,
            sub=sub,
            purpose=PURPOSE_RESET,
            ttl_seconds=settings.password_reset_ttl_seconds,
            ip_hash=ip_hash,
        )
        await repo.enqueue_mail(
            conn,
            tenant_id=tenant_id,
            subject_sub=sub,
            kind=copy_mod.KIND_PASSWORD_RESET,
            lang=lang,
            to_address=account_email,
            render_fields=compose.password_reset_fields(
                lang=lang,
                email=account_email,
                display_name=display_name,
                user_agent=user_agent,
                support_url=settings.support_url,
                app_base_url=settings.app_base_url,
                requested_at=datetime.now(UTC),
                ttl_seconds=settings.password_reset_ttl_seconds,
            ),
            secret_fields=compose.reset_secret_fields(
                app_base_url=settings.app_base_url, token=token
            ),
        )
        await repo.record_password_event(
            conn,
            tenant_id=tenant_id,
            subject_sub=sub,
            kind="reset_requested",
            via="reset_link",
            ip_hash=ip_hash,
            client_label=compose.client_label(user_agent, lang),
        )
        await repo.sweep_dead_tokens(conn)

    _password_counter.add(1, {"act": "forgot", "result": "queued"})
    await _audit(
        state,
        tenant_id=tenant_id,
        sub=sub,
        kind=audit_kinds.AUTH_PASSWORD_RESET_REQUESTED,
        payload={"ip_hash": ip_hash},
    )
    return ForgotResponse()


@router.post(
    "/password/reset",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set a new password using a mailed token",
)
async def reset_password(body: ResetRequest, request: Request) -> Response:
    _enabled_or_404()
    state = get_state()
    user_agent = request.headers.get("user-agent", "")
    ip_hash = compose.hash_ip(
        _client_ip(request), salt=settings.password_reset_ip_hash_salt.value()
    )

    token_hash = _hash_token(body.token)

    def _invalid() -> HTTPException:
        _password_counter.add(1, {"act": "reset", "result": "invalid_token"})
        exc = HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="this reset link is no longer valid",
        )
        exc.problem_extras = {"code": "invalid_reset_token"}  # type: ignore[attr-defined]
        return exc

    # PEEK, judge, and only then consume.
    #
    # The obvious order — consume first, validate second — means a user
    # who fat-fingers a weak password has burned their single-use link
    # and has to go back to their inbox for another. That is a real cost
    # to a legitimate, locked-out person and it buys nothing: an
    # attacker holding a stolen link does not fail the strength check,
    # they submit a strong password and win on the first attempt.
    #
    # (Found by running the flow rather than by a test: the 422 came
    # back, and the very next request with a good password was refused.)
    async with state.app_pool.acquire() as conn:
        peeked = await repo.peek_token(
            conn, token_hash=token_hash, purpose=PURPOSE_RESET
        )
    if peeked is None:
        raise _invalid()

    tenant_id = UUID(str(peeked["tenant_id"]))
    sub = UUID(str(peeked["subject_sub"]))

    async with tenant_connection(state.app_pool, tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT email, COALESCE(display_name, '') AS display_name "
            "FROM users WHERE sub = $1",
            sub,
        )
    email = str(row["email"]) if row else ""
    display_name = str(row["display_name"]) if row else ""

    # Raises 422 without having spent anything.
    _reject_weak(body.new_password, email=email, display_name=display_name)

    # Now commit the single use. Still an atomic compare-and-swap, so
    # two concurrent redemptions of one link produce exactly one winner
    # and the loser gets the same 400 as an expired link.
    async with state.app_pool.acquire() as conn:
        claimed = await repo.consume_token(
            conn, token_hash=token_hash, purpose=PURPOSE_RESET
        )
    if claimed is None:
        raise _invalid()

    try:
        await state.keycloak.set_password(sub, new_password=body.new_password)
    except KeycloakError as exc:
        _password_counter.add(1, {"act": "reset", "result": "keycloak_error"})
        logger.error(
            "auth.password.set_failed",
            extra={"sub": str(sub), "kc_status": exc.status},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="the identity provider rejected the new password",
        ) from exc

    await _revoke_all_sessions(state, sub)

    lang = copy_mod.normalise_lang(request.headers.get("accept-language", ""))
    async with tenant_connection(state.app_pool, tenant_id) as conn:
        # Any other live reset link is now a spare key to an account the
        # user believes they have just secured.
        await repo.spend_all_tokens(conn, subject_sub=sub)
        await repo.record_password_event(
            conn,
            tenant_id=tenant_id,
            subject_sub=sub,
            kind="reset_completed",
            via="reset_link",
            ip_hash=ip_hash,
            client_label=compose.client_label(user_agent, lang),
        )
        if email:
            await _enqueue_security_notification(
                conn,
                tenant_id=tenant_id,
                sub=sub,
                email=email,
                display_name=display_name,
                lang=lang,
                user_agent=user_agent,
                ip_hash=ip_hash,
            )

    _password_counter.add(1, {"act": "reset", "result": "success"})
    await _audit(
        state,
        tenant_id=tenant_id,
        sub=sub,
        kind=audit_kinds.AUTH_PASSWORD_RESET_COMPLETED,
        payload={"ip_hash": ip_hash},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/password/change",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change your own password",
)
async def change_password(
    body: ChangeRequest,
    request: Request,
    claims: Annotated[Claims, Depends(current_user)],
) -> Response:
    _enabled_or_404()
    state = get_state()
    user_agent = request.headers.get("user-agent", "")
    ip_hash = compose.hash_ip(
        _client_ip(request), salt=settings.password_reset_ip_hash_salt.value()
    )

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await conn.fetchrow(
            "SELECT email, COALESCE(display_name, '') AS display_name "
            "FROM users WHERE sub = $1",
            claims.sub,
        )
    email = str(row["email"]) if row else ""
    display_name = str(row["display_name"]) if row else ""
    username = claims.preferred_username or email
    if not username:
        logger.error("auth.password.no_username", extra={"sub": str(claims.sub)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="cannot resolve the account identifier",
        )

    # Proof of presence. A live session is not proof that the account
    # holder is the one at the keyboard, and this is the one act that
    # locks everyone else out — including the real owner.
    try:
        await state.keycloak.password_grant(
            username=username, password=body.current_password
        )
    except KeycloakError as exc:
        _password_counter.add(1, {"act": "change", "result": "bad_current"})
        await _audit(
            state,
            tenant_id=claims.tid,
            sub=claims.sub,
            kind=audit_kinds.AUTH_REAUTH_FAILED,
            payload={"purpose": "password_change", "kc_status": exc.status},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="current password does not match",
            headers={"WWW-Authenticate": 'Bearer realm="medical-dictation"'},
        ) from exc

    _reject_weak(body.new_password, email=email, display_name=display_name)

    if body.new_password == body.current_password:
        exc_same = HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="the new password must differ from the current one",
        )
        exc_same.problem_extras = {"code": "password_unchanged"}  # type: ignore[attr-defined]
        raise exc_same

    try:
        await state.keycloak.set_password(claims.sub, new_password=body.new_password)
    except KeycloakError as exc:
        _password_counter.add(1, {"act": "change", "result": "keycloak_error"})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="the identity provider rejected the new password",
        ) from exc

    await _revoke_all_sessions(state, claims.sub)

    lang = copy_mod.normalise_lang(request.headers.get("accept-language", ""))
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        await repo.spend_all_tokens(conn, subject_sub=claims.sub)
        await repo.record_password_event(
            conn,
            tenant_id=claims.tid,
            subject_sub=claims.sub,
            kind="password_changed",
            via="self",
            ip_hash=ip_hash,
            client_label=compose.client_label(user_agent, lang),
        )
        if email:
            await _enqueue_security_notification(
                conn,
                tenant_id=claims.tid,
                sub=claims.sub,
                email=email,
                display_name=display_name,
                lang=lang,
                user_agent=user_agent,
                ip_hash=ip_hash,
            )

    _password_counter.add(1, {"act": "change", "result": "success"})
    await _audit(
        state,
        tenant_id=claims.tid,
        sub=claims.sub,
        kind=audit_kinds.AUTH_PASSWORD_CHANGED,
        payload={"ip_hash": ip_hash},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/security/lockdown",
    response_model=LockdownResponse,
    summary="'This wasn't me' — end every session and retake the account",
)
async def account_lockdown(
    body: LockdownRequest, request: Request
) -> LockdownResponse:
    """The button in the security-notification email.

    Ordered so the destructive half happens first: sessions die, then
    outstanding links die, and only then do we mint the replacement. If
    anything fails after the revocation, the attacker is already out and
    the user can fall back to the ordinary forgot-password flow. Minting
    first and revoking second would leave a window where both the
    attacker and a fresh token are live.
    """
    _enabled_or_404()
    state = get_state()
    user_agent = request.headers.get("user-agent", "")
    ip_hash = compose.hash_ip(
        _client_ip(request), salt=settings.password_reset_ip_hash_salt.value()
    )

    async with state.app_pool.acquire() as conn:
        claimed = await repo.consume_token(
            conn, token_hash=_hash_token(body.token), purpose=PURPOSE_LOCKDOWN
        )

    if claimed is None:
        _password_counter.add(1, {"act": "lockdown", "result": "invalid_token"})
        exc = HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="this security link is no longer valid",
        )
        exc.problem_extras = {"code": "invalid_lockdown_token"}  # type: ignore[attr-defined]
        raise exc

    tenant_id = UUID(str(claimed["tenant_id"]))
    sub = UUID(str(claimed["subject_sub"]))

    revoked = await _revoke_all_sessions(state, sub)

    lang = copy_mod.normalise_lang(request.headers.get("accept-language", ""))
    async with tenant_connection(state.app_pool, tenant_id) as conn:
        await repo.spend_all_tokens(conn, subject_sub=sub)
        reset_token = await _issue_token(
            conn,
            tenant_id=tenant_id,
            sub=sub,
            purpose=PURPOSE_RESET,
            ttl_seconds=settings.password_reset_ttl_seconds,
            ip_hash=ip_hash,
        )
        await repo.record_password_event(
            conn,
            tenant_id=tenant_id,
            subject_sub=sub,
            kind="lockdown_triggered",
            via="reset_link",
            ip_hash=ip_hash,
            client_label=compose.client_label(user_agent, lang),
        )

    _password_counter.add(1, {"act": "lockdown", "result": "success"})
    await _audit(
        state,
        tenant_id=tenant_id,
        sub=sub,
        kind=audit_kinds.AUTH_ACCOUNT_LOCKDOWN,
        payload={"ip_hash": ip_hash, "sessions_revoked": revoked},
    )
    logger.warning(
        "auth.account.lockdown_triggered",
        extra={"sub": str(sub), "tenant_id": str(tenant_id), "revoked": revoked},
    )
    return LockdownResponse(
        reset_token=reset_token,
        expires_in=settings.password_reset_ttl_seconds,
        sessions_revoked=revoked,
    )


@router.get(
    "/password/events",
    response_model=list[PasswordEvent],
    summary="Recent security activity on your account",
)
async def password_events(
    claims: Annotated[Claims, Depends(current_user)],
) -> list[PasswordEvent]:
    _enabled_or_404()
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await repo.recent_password_events(conn, subject_sub=claims.sub)
    return [
        PasswordEvent(
            kind=str(r["kind"]),
            via=str(r["via"]),
            client_label=str(r["client_label"]),
            created_at=r["created_at"],
        )
        for r in rows
    ]


@router.get(
    "/sessions",
    response_model=list[SessionSummary],
    summary="Devices currently signed in to your account",
)
async def list_sessions(
    claims: Annotated[Claims, Depends(current_user)],
) -> list[SessionSummary]:
    state = get_state()
    try:
        rows = await state.keycloak.list_sessions(claims.sub)
    except KeycloakError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="cannot list sessions right now",
        ) from exc

    def _ms(value: Any) -> datetime | None:
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
        except (TypeError, ValueError):
            return None

    return [
        SessionSummary(
            id=str(r.get("id", "")),
            ip_address=str(r.get("ipAddress", "")),
            started_at=_ms(r.get("start")),
            last_access_at=_ms(r.get("lastAccess")),
            # Lets the SPA label one row "this device" so a user does not
            # have to guess which session they are about to end.
            current=str(r.get("id", "")) == (claims.sid or ""),
        )
        for r in rows
    ]


@router.post(
    "/sessions/revoke-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out of every device, including this one",
)
async def revoke_all_sessions(
    request: Request,
    claims: Annotated[Claims, Depends(current_user)],
) -> Response:
    state = get_state()
    ip_hash = compose.hash_ip(
        _client_ip(request), salt=settings.password_reset_ip_hash_salt.value()
    )
    await _revoke_all_sessions(state, claims.sub)
    await _audit(
        state,
        tenant_id=claims.tid,
        sub=claims.sub,
        kind=audit_kinds.AUTH_SESSION_REVOKED,
        payload={"scope": "all", "ip_hash": ip_hash, "initiated_by": "self"},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
