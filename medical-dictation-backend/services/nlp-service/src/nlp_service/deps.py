"""FastAPI dependency wiring + per-tenant/per-IP rate limit."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, status
from opentelemetry import metrics

from audit import Severity
from auth import Action, AuthzDeniedError, Claims, TargetKind, check

from .config import settings
from .main_deps import ServiceState

logger = logging.getLogger(__name__)

_meter = metrics.get_meter("mdx.nlp.authz")
_authz_denied = _meter.create_counter(
    "mdx_authz_denied_total",
    description="nlp-service requires() rejections",
    unit="1",
)
_rate_limit_hits = _meter.create_counter(
    "mdx_nlp_rate_limit_total",
    description="429s emitted by the per-tenant / per-IP limiter",
    unit="1",
)
_oversized_input = _meter.create_counter(
    "mdx_nlp_oversized_input_total",
    description="413s emitted by the request-size validator",
    unit="1",
)

_state: ServiceState | None = None


def install_state(state: ServiceState) -> None:
    global _state
    _state = state


def get_state() -> ServiceState:
    if _state is None:
        raise RuntimeError(
            "ServiceState not installed; this code must run after lifespan startup"
        )
    return _state


async def current_user(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> Claims:
    state = get_state()
    if not hasattr(state, "_current_user_dep"):
        from auth import build_current_user

        state._current_user_dep = build_current_user(  # type: ignore[attr-defined]
            jwks_cache=state.jwks_cache,
            expected_audience=settings.auth_audience,
            expected_issuer=settings.auth_issuer,
            clock_skew_seconds=settings.auth_clock_skew_seconds,
        )
    dep = state._current_user_dep  # type: ignore[attr-defined]
    result: Claims = await dep(request, authorization)
    return result


def requires(
    action: Action, target_kind: TargetKind, *, scope: str | None = None
) -> Callable[..., Awaitable[Claims]]:
    async def dep(claims: Annotated[Claims, Depends(current_user)]) -> Claims:
        try:
            check(claims, action=action, target_kind=target_kind, scope=scope)
        except AuthzDeniedError as exc:
            _authz_denied.add(
                1,
                {"action": exc.action, "target_kind": exc.target_kind, "reason": exc.reason},
            )
            state = _state
            if state is not None:
                try:
                    await state.audit_writer.write_event(
                        tenant_id=exc.claims.tid,
                        kind="authz.denied",
                        actor_sub=exc.claims.sub,
                        actor_role=(exc.claims.roles[0] if exc.claims.roles else None),
                        target_kind=exc.target_kind,
                        target_id=None,
                        payload={"action": exc.action, "reason": exc.reason},
                        severity=Severity.SEC,
                    )
                except Exception as audit_exc:
                    logger.warning(
                        "authz_denied.audit_write_failed",
                        extra={"error": str(audit_exc)},
                    )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"deny: roles={list(claims.roles)} cannot {action!r} on {target_kind!r}"
                ),
            ) from exc
        return claims

    return dep


# ── Rate limiter (per-tenant + per-IP) ──────────────────────────────


async def rate_limited(
    request: Request,
    claims: Annotated[Claims, Depends(current_user)],
) -> Claims:
    state = get_state()
    second_bucket = int(time.time())
    ip = (request.client.host if request.client else "unknown")
    pipe = state.redis.pipeline()
    pipe.incr(f"mdx:nlp:rl:t:{claims.tid}:{second_bucket}")
    pipe.expire(f"mdx:nlp:rl:t:{claims.tid}:{second_bucket}", 2)
    pipe.incr(f"mdx:nlp:rl:ip:{ip}:{second_bucket}")
    pipe.expire(f"mdx:nlp:rl:ip:{ip}:{second_bucket}", 2)
    results: list[Any] = await pipe.execute()
    tenant_count = int(results[0])
    ip_count = int(results[2])
    if tenant_count > settings.rate_limit_per_tenant_rps:
        _rate_limit_hits.add(1, {"scope": "tenant"})
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "type": "urn:mdx:nlp:rate_limit:per_tenant",
                "code": "rate_limited",
                "scope": "tenant",
                "limit": settings.rate_limit_per_tenant_rps,
            },
        )
    if ip_count > settings.rate_limit_per_ip_rps:
        _rate_limit_hits.add(1, {"scope": "ip"})
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "type": "urn:mdx:nlp:rate_limit:per_ip",
                "code": "rate_limited",
                "scope": "ip",
                "limit": settings.rate_limit_per_ip_rps,
            },
        )
    return claims


def assert_size(text: str, n_words: int) -> None:
    """Enforce input caps. Caller invokes before constructing context."""
    if len(text.encode("utf-8")) > settings.max_input_bytes:
        _oversized_input.add(1, {"reason": "bytes"})
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "type": "urn:mdx:nlp:input_too_large",
                "code": "input_too_large",
                "limit_bytes": settings.max_input_bytes,
            },
        )
    if n_words > settings.max_input_words:
        _oversized_input.add(1, {"reason": "words"})
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "type": "urn:mdx:nlp:input_too_large",
                "code": "input_too_large",
                "limit_words": settings.max_input_words,
            },
        )
