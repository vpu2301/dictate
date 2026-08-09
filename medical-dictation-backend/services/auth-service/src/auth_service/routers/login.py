"""POST /auth/login, POST /auth/refresh, POST /auth/logout.

Login flow:
  1. Take {username, password} from JSON body.
  2. Proxy to Keycloak via the confidential mdx-backend client.
  3. On success: set the refresh token as a HttpOnly cookie (path=AUTH_COOKIE_PATH,
     SameSite=Strict, Secure in non-dev), return access_token + expires_in in JSON.
  4. Verify the access token to extract claims and emit an audit ``auth.login`` event.

Refresh flow:
  1. Read the refresh token from the cookie.
  2. Call Keycloak refresh; on success, rotate the cookie (new refresh, old invalidated).
  3. On "Token is not active" / 400 → audit ``auth.refresh_replay_detected`` (severity sec).
     The old refresh was consumed by an earlier call; this attempt is a replay.

Logout flow:
  1. Read cookie; call Keycloak logout to revoke; clear cookie; 204.

MFA is intentionally NOT enforced here — pilot deployment runs MFA-off per
the user's instruction. Re-enabling it later means flipping the
``requires_mfa`` dep on these routes and adding TOTP enrolment endpoints.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, HTTPException, Request, Response, status
from opentelemetry import metrics
from pydantic import BaseModel

from audit import Severity
from auth import verify_token

from .. import audit_kinds
from ..config import settings
from ..deps import get_state
from ..keycloak_client import KeycloakError

_meter = metrics.get_meter("mdx.auth")
_login_counter = _meter.create_counter(
    "mdx_auth_login_total",
    description="Login attempts by outcome",
    unit="1",
)
_refresh_replay_counter = _meter.create_counter(
    "mdx_auth_refresh_replay_total",
    description="Refresh-token replays detected (always anomalous)",
    unit="1",
)
_logout_counter = _meter.create_counter(
    "mdx_auth_logout_total",
    description="Logout calls",
    unit="1",
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


class LoginResponse(BaseModel):
    access_token: str
    expires_in: int
    token_type: str = "Bearer"


async def _extract_credentials(request: Request) -> tuple[str, str, str | None]:
    """Parse login credentials from either a JSON body or an
    application/x-www-form-urlencoded form (the SPA sends form). Accepts the
    identifier under `email` or `username`, plus an optional `otp` (TOTP
    code — sprint 16). Raises 422 on a missing required field."""
    content_type = request.headers.get("content-type", "")
    data: dict[str, Any]
    if "application/json" in content_type:
        try:
            data = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=422, detail="invalid JSON body") from exc
    else:
        # form-urlencoded (or multipart) — the SPA path.
        form = await request.form()
        data = {k: v for k, v in form.items() if isinstance(v, str)}

    identifier = (data.get("email") or data.get("username") or "").strip()
    password = data.get("password") or ""
    otp = (data.get("otp") or "").strip() or None
    if not identifier or not password:
        raise HTTPException(
            status_code=422,
            detail="login requires `password` and one of `email`/`username`",
        )
    return identifier, password, otp


def _set_refresh_cookie(response: Response, refresh_token: str, max_age: int) -> None:
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=refresh_token,
        max_age=max_age,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,  # type: ignore[arg-type]
        path=settings.auth_cookie_path,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.auth_cookie_name,
        path=settings.auth_cookie_path,
    )


async def _audit_login(state: Any, *, access_token: str, kind: str, severity: Severity) -> None:
    """Verify the access token to recover tid+sub, then emit an audit event."""
    try:
        claims = await verify_token(
            access_token,
            expected_audience=settings.auth_audience,
            expected_issuer=settings.auth_issuer,
            jwks_cache=state.jwks_cache,
        )
    except Exception as exc:
        # Pre-pilot we don't expect tokens we issued to fail verify; log
        # loudly and skip the audit (no usable tenant context).
        logger.warning(
            "audit_login.verify_failed",
            extra={"kind": kind, "error": str(exc), "error_class": type(exc).__name__},
        )
        return

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=kind,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        payload={"sid": claims.sid},
        severity=severity,
    )


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Exchange username + password for an access token",
)
async def login(request: Request, response: Response) -> LoginResponse:
    state = get_state()
    username, password, otp = await _extract_credentials(request)
    try:
        tok = await state.keycloak.password_grant(username=username, password=password)
    except KeycloakError as exc:
        # 401: invalid_grant (bad password / unknown user / disabled).
        # 400: malformed (shouldn't happen via this proxy).
        body_obj = exc.body if isinstance(exc.body, dict) else {}
        kc_error = body_obj.get("error", "")
        kc_desc = body_obj.get("error_description", "")
        if "Account is not fully set up" in kc_desc or "account is locked" in kc_desc.lower():
            _login_counter.add(1, {"result": "locked"})
            raise HTTPException(
                status_code=status.HTTP_423_LOCKED,
                detail=f"account locked: {kc_desc}",
            ) from exc
        _login_counter.add(1, {"result": "invalid_creds"})
        logger.info(
            "auth.login_failed",
            extra={
                "username_hash": _hash_for_log(username),
                "kc_error": kc_error,
                "kc_status": exc.status,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": 'Bearer realm="medical-dictation"'},
        ) from exc

    # ── Sprint 16: second factor for enrolled users ─────────────────────
    # The password grant succeeded, but the token is NOT released until an
    # enrolled user's TOTP code validates. Keycloak isn't publicly exposed
    # in the production topology, so this proxy check is the enforcement
    # point (ADR-0039). Enrolment status comes from the token's own
    # `mfa_enrolled` claim (attribute-mapped), so the check costs no extra
    # Keycloak round-trip for the unenrolled majority.
    await _enforce_totp_if_enrolled(state, access_token=tok.access_token, otp=otp)

    _set_refresh_cookie(response, tok.refresh_token, tok.refresh_expires_in)
    _login_counter.add(1, {"result": "success"})

    # Audit the success (out of band; failures are logged only because we
    # don't yet have reliable tenant resolution for unknown users).
    await _audit_login(
        state,
        access_token=tok.access_token,
        kind=audit_kinds.AUTH_LOGIN,
        severity=Severity.INFO,
    )

    return LoginResponse(access_token=tok.access_token, expires_in=tok.expires_in)


async def _enforce_totp_if_enrolled(state: Any, *, access_token: str, otp: str | None) -> None:
    """Reject the login (401) when the user is TOTP-enrolled and ``otp``
    is missing or wrong. No-op for unenrolled users.

    Machine codes for the SPA: ``otp_required`` (ask for the code) and
    ``otp_invalid`` (wrong code — retry).
    """
    try:
        claims = await verify_token(
            access_token,
            expected_audience=settings.auth_audience,
            expected_issuer=settings.auth_issuer,
            jwks_cache=state.jwks_cache,
        )
    except Exception as exc:
        # We just minted this token via Keycloak; if we cannot verify it,
        # the JWKS path is broken — log loudly, don't invent an MFA state.
        logger.warning(
            "auth.login.mfa_check_verify_failed",
            extra={"error": str(exc), "error_class": type(exc).__name__},
        )
        return
    if not claims.mfa_enrolled:
        return

    def _reject(code: str, detail: str) -> HTTPException:
        _login_counter.add(1, {"result": code})
        exc = HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": 'MFA realm="medical-dictation"'},
        )
        exc.problem_extras = {"code": code}  # type: ignore[attr-defined]
        return exc

    if not otp:
        raise _reject("otp_required", "TOTP code required for this account")

    from crypto import CryptoError, MasterKeyError

    from .. import totp as totp_mod
    from .mfa import ATTR_SECRET, _attr_first

    try:
        envelope = await state.get_envelope()
        rep = await state.keycloak.get_user(claims.sub)
        packed = _attr_first(rep, ATTR_SECRET)
        if packed is None:
            # Claim says enrolled but no stored secret — a half-reset.
            # Fail closed and point at the admin reset path.
            raise _reject(
                "otp_unavailable",
                "MFA state is inconsistent; ask an administrator to reset MFA",
            )
        secret = await totp_mod.decrypt_secret(
            envelope, packed=packed, tenant_id=claims.tid, sub=claims.sub
        )
    except HTTPException:
        raise
    except (MasterKeyError, CryptoError, KeycloakError) as exc:
        logger.error(
            "auth.login.mfa_secret_unavailable",
            extra={"error": str(exc), "error_class": type(exc).__name__},
        )
        # Fail closed: an enrolled account never logs in without the
        # second factor, even when the secret store is down.
        raise _reject("otp_unavailable", "MFA verification unavailable; try again") from exc

    if not totp_mod.verify_code(secret, otp):
        raise _reject("otp_invalid", "invalid TOTP code")


@router.post(
    "/refresh",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Rotate refresh cookie; return a new access token",
)
async def refresh(
    response: Response,
    request: Request,
    mdx_rt: Annotated[str | None, Cookie(alias=None)] = None,
) -> LoginResponse:
    state = get_state()
    # Pydantic Cookie() doesn't accept dynamic alias; resolve via raw cookies.
    refresh_token = request.cookies.get(settings.auth_cookie_name)
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="no refresh cookie",
        )

    try:
        tok = await state.keycloak.refresh(refresh_token=refresh_token)
    except KeycloakError as exc:
        body_obj = exc.body if isinstance(exc.body, dict) else {}
        kc_error = body_obj.get("error", "")
        kc_desc = body_obj.get("error_description", "")
        # Replay / invalid: the refresh has already been consumed (rotation
        # is on at the realm level, so a re-used refresh is a sec event).
        if kc_error == "invalid_grant":
            # Try to extract the user's sub from the unverified refresh
            # payload so we can audit + force-revoke their sessions.
            sub = _unverified_sub(refresh_token)
            tid = _unverified_tid(refresh_token)
            # Keycloak refresh tokens do not carry the custom `tid` claim, so
            # resolve the tenant from `sub` via the DB (SECURITY DEFINER
            # `tenant_of_sub`); otherwise the security audit event below would be
            # silently skipped (Sprint A1, DEF-A1-20).
            if tid is None and sub is not None:
                try:
                    tid = await state.app_pool.fetchval("SELECT public.tenant_of_sub($1)", sub)
                except Exception as resolve_exc:
                    logger.warning(
                        "auth.refresh_replay.tid_resolve_failed",
                        extra={"error": str(resolve_exc)},
                    )
            _refresh_replay_counter.add(1, {"tenant_id": str(tid) if tid else "unknown"})
            if tid is not None:
                try:
                    await state.audit_writer.write_event(
                        tenant_id=tid,
                        kind=audit_kinds.AUTH_REFRESH_REPLAY_DETECTED,
                        actor_sub=sub,
                        payload={"kc_error": kc_error, "kc_desc": kc_desc},
                        severity=Severity.SEC,
                    )
                except Exception as audit_exc:
                    logger.warning(
                        "audit.refresh_replay.write_failed",
                        extra={"error": str(audit_exc)},
                    )
            if sub is not None:
                try:
                    await state.keycloak.logout_user(sub)
                except Exception as logout_exc:
                    logger.warning(
                        "auth.refresh_replay.revoke_failed",
                        extra={"sub": str(sub), "error": str(logout_exc)},
                    )
                # Sprint 16: a replayed refresh means the token chain may be
                # in hostile hands — kill every outstanding ACCESS token of
                # the user too, not just the Keycloak sessions.
                if state.denylist is not None:
                    try:
                        await state.denylist.revoke_sub(
                            str(sub),
                            ttl_seconds=settings.revoked_sub_ttl_seconds,
                        )
                        if tid is not None:
                            await state.audit_writer.write_event(
                                tenant_id=tid,
                                kind=audit_kinds.AUTH_SESSION_REVOKED,
                                actor_sub=sub,
                                payload={"reason": "refresh_replay"},
                                severity=Severity.SEC,
                            )
                    except Exception as push_exc:  # noqa: BLE001
                        logger.warning(
                            "auth.refresh_replay.denylist_push_failed",
                            extra={"error": str(push_exc)},
                        )
            _clear_refresh_cookie(response)
            replay_exc = HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="refresh token is no longer valid",
            )
            # Machine-readable code so the SPA can distinguish a replay (force
            # clean re-login, do NOT retry) from an ordinary expired-token 401.
            replay_exc.problem_extras = {"code": "auth_refresh_replay"}  # type: ignore[attr-defined]
            raise replay_exc from exc
        # Other failure modes (5xx etc.) bubble as 503.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"identity provider error: {kc_desc or kc_error}",
        ) from exc

    _set_refresh_cookie(response, tok.refresh_token, tok.refresh_expires_in)
    _login_counter.add(1, {"result": "refresh"})
    await _audit_login(
        state,
        access_token=tok.access_token,
        kind=audit_kinds.AUTH_REFRESH,
        severity=Severity.INFO,
    )
    return LoginResponse(access_token=tok.access_token, expires_in=tok.expires_in)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke refresh token and clear cookie",
)
async def logout(
    request: Request,
    response: Response,
    authorization: Annotated[str | None, "Authorization"] = None,
) -> Response:
    """Revoke the refresh token, clear the cookie.

    If an ``Authorization: Bearer <access_token>`` header is also sent, the
    verified ``tid``/``sub`` are used to emit an ``auth.logout`` audit event.
    Refresh tokens deliberately don't carry the ``tid`` claim (Keycloak
    default), so without the access token we have no verified tenant
    context and skip the audit.
    """
    state = get_state()
    refresh_token = request.cookies.get(settings.auth_cookie_name)
    # Audit context — prefer the access token (verified); fall back to the
    # refresh token's sub (unverified) for log-line correlation only.
    tid_for_audit = None
    sub_for_audit = None
    bearer_claims = None
    raw_auth = request.headers.get("Authorization", "") or (authorization or "")
    if raw_auth.startswith("Bearer "):
        try:
            claims = await verify_token(
                raw_auth[len("Bearer ") :],
                expected_audience=settings.auth_audience,
                expected_issuer=settings.auth_issuer,
                jwks_cache=state.jwks_cache,
            )
            tid_for_audit = claims.tid
            sub_for_audit = claims.sub
            bearer_claims = claims
        except Exception as exc:
            logger.info("auth.logout.bearer_invalid", extra={"error": str(exc)})

    # ── Sprint 16: close the 15-minute window ───────────────────────────
    # The refresh token dies at Keycloak below, but the ACCESS token would
    # stay signature-valid until `exp`. Denylist its sid so the very next
    # request anywhere in the fleet is rejected. TTL = remaining lifetime.
    if bearer_claims is not None and state.denylist is not None:
        import time as _time

        ttl = max(int(bearer_claims.exp - _time.time()), 1)
        try:
            await state.denylist.revoke_sid(bearer_claims.sid, ttl_seconds=ttl)
            await state.audit_writer.write_event(
                tenant_id=bearer_claims.tid,
                kind=audit_kinds.AUTH_SESSION_REVOKED,
                actor_sub=bearer_claims.sub,
                payload={"reason": "logout", "sid": bearer_claims.sid},
                severity=Severity.INFO,
            )
        except Exception as push_exc:  # noqa: BLE001 — logout must still succeed
            logger.warning(
                "auth.logout.denylist_push_failed", extra={"error": str(push_exc)}
            )

    if refresh_token:
        try:
            await state.keycloak.logout(refresh_token=refresh_token)
        except KeycloakError as exc:
            # If the refresh was already expired/revoked, that's fine; log
            # but don't fail the logout (idempotent).
            logger.info(
                "auth.logout.kc_already_revoked",
                extra={"kc_status": exc.status},
            )

    if tid_for_audit is not None:
        try:
            await state.audit_writer.write_event(
                tenant_id=tid_for_audit,
                kind=audit_kinds.AUTH_LOGOUT,
                actor_sub=sub_for_audit,
                payload={},
                severity=Severity.INFO,
            )
        except Exception as audit_exc:
            logger.warning("audit.logout.write_failed", extra={"error": str(audit_exc)})

    _logout_counter.add(1)
    # Build a fresh response so the Set-Cookie header is the only one we
    # set; FastAPI would otherwise merge the injected `response` with our
    # returned one and we'd risk losing the cookie deletion.
    out = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_refresh_cookie(out)
    return out


# ── helpers ─────────────────────────────────────────────────────────────


def _hash_for_log(value: str) -> str:
    """Don't log raw usernames; emit a stable short hash so SOC can correlate."""
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _unverified_jwt_payload(token: str) -> dict[str, Any] | None:
    """Decode the *unverified* payload of a JWT — used ONLY to recover
    sub/tid for audit on a refresh that we already know is invalid.

    Never trust these claims for authorisation; the chain didn't verify.
    """
    import base64
    import json

    try:
        _, payload_b64, _ = token.split(".", 2)
    except ValueError:
        return None
    pad = "=" * (-len(payload_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64 + pad)
    except Exception:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _unverified_sub(token: str) -> Any:
    from uuid import UUID

    payload = _unverified_jwt_payload(token)
    if payload is None:
        return None
    sub_str = payload.get("sub")
    if not isinstance(sub_str, str):
        return None
    try:
        return UUID(sub_str)
    except ValueError:
        return None


def _unverified_tid(token: str) -> Any:
    from uuid import UUID

    payload = _unverified_jwt_payload(token)
    if payload is None:
        return None
    tid_str = payload.get("tid")
    if not isinstance(tid_str, str):
        return None
    try:
        return UUID(tid_str)
    except ValueError:
        return None
