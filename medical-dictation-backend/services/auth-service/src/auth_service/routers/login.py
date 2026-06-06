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
from pydantic import BaseModel, Field

from opentelemetry import metrics

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


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class LoginResponse(BaseModel):
    access_token: str
    expires_in: int
    token_type: str = "Bearer"


def _set_refresh_cookie(response: Response, refresh_token: str, max_age: int) -> None:
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=refresh_token,
        max_age=max_age,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="strict",
        path=settings.auth_cookie_path,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.auth_cookie_name,
        path=settings.auth_cookie_path,
    )


async def _audit_login(
    state: Any, *, access_token: str, kind: str, severity: Severity
) -> None:
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
async def login(body: LoginRequest, response: Response, request: Request) -> LoginResponse:
    state = get_state()
    try:
        tok = await state.keycloak.password_grant(
            username=body.username, password=body.password
        )
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
                "username_hash": _hash_for_log(body.username),
                "kc_error": kc_error,
                "kc_status": exc.status,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": 'Bearer realm="medical-dictation"'},
        ) from exc

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
            _refresh_replay_counter.add(
                1, {"tenant_id": str(tid) if tid else "unknown"}
            )
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
            _clear_refresh_cookie(response)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="refresh token is no longer valid",
            ) from exc
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
    raw_auth = request.headers.get("Authorization", "") or (authorization or "")
    if raw_auth.startswith("Bearer "):
        try:
            from auth import verify_token

            claims = await verify_token(
                raw_auth[len("Bearer ") :],
                expected_audience=settings.auth_audience,
                expected_issuer=settings.auth_issuer,
                jwks_cache=state.jwks_cache,
            )
            tid_for_audit = claims.tid
            sub_for_audit = claims.sub
        except Exception as exc:
            logger.info("auth.logout.bearer_invalid", extra={"error": str(exc)})

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
            logger.warning(
                "audit.logout.write_failed", extra={"error": str(audit_exc)}
            )

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
