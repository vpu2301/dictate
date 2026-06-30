"""Tiny wrapper so every router emits audit events the same way and an audit
failure never blocks the user's write."""

from __future__ import annotations

import logging
from uuid import UUID

from audit import Severity
from auth import Claims

from .main_deps import ServiceState

logger = logging.getLogger(__name__)


async def emit(
    state: ServiceState,
    claims: Claims,
    kind: str,
    *,
    target_kind: str,
    target_id: UUID | str | None,
    payload: dict[str, object] | None = None,
    severity: Severity = Severity.INFO,
) -> None:
    try:
        await state.audit_writer.write_event(
            tenant_id=claims.tid,
            kind=kind,
            actor_sub=claims.sub,
            actor_role=(claims.roles[0] if claims.roles else None),
            target_kind=target_kind,
            target_id=str(target_id) if target_id is not None else None,
            payload=payload or {},
            severity=severity,
        )
    except Exception as exc:  # pragma: no cover - audit must not block writes
        logger.warning(
            "core.audit_write_failed", extra={"kind": kind, "error": str(exc)}
        )
