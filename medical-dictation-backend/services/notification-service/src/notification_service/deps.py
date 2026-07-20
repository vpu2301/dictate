"""FastAPI dependencies."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from auth import Action, AuthzDeniedError, Claims, TargetKind, check

from .main_deps import ServiceState

logger = logging.getLogger(__name__)

_state: ServiceState | None = None


def install_state(state: ServiceState) -> None:
    global _state
    _state = state


def get_state() -> ServiceState:
    if _state is None:
        raise RuntimeError("ServiceState not installed")
    return _state


async def current_user(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> Claims:
    return await get_state().current_user_dep(request, authorization)


def requires(
    action: Action, target_kind: TargetKind, *, scope: str | None = None
) -> Callable[..., Awaitable[Claims]]:
    """Enforce the perms matrix for one (action, target_kind)."""

    async def dep(claims: Annotated[Claims, Depends(current_user)]) -> Claims:
        try:
            check(claims, action=action, target_kind=target_kind, scope=scope)
        except AuthzDeniedError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"deny: cannot {action!r} {target_kind!r}",
            ) from exc
        return claims

    return dep
