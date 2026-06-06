"""HTTP companion endpoints for the WS surface.

The frontend uses these to:
- List sessions running on other devices ("you are dictating on another tab").
- Fetch a finalized session's transcript JSONB.
- Force-finalize a stuck `reconnecting` session (sprint 04 spec §6).

All endpoints are RLS-scoped via :func:`db.tenant_connection`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from audit import Severity
from auth import Claims
from db import tenant_connection

from .. import audit_kinds
from ..deps import get_state, requires
from ..domain import repository
from ..session.state import SessionState

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dictate", tags=["dictate"])


class SessionSummary(BaseModel):
    id: UUID
    status: str
    language: str
    target_kind: str
    started_at: datetime | None
    last_active_at: datetime
    finalized_at: datetime | None = None
    total_audio_ms: int = 0
    network_drop_count: int = 0


class SessionDetail(BaseModel):
    id: UUID
    tenant_id: UUID
    user_id: UUID
    status: str
    language: str
    target_kind: str
    prompt_id: UUID
    transcript: list[dict[str, Any]]
    total_audio_ms: int
    avg_partial_latency_ms: int | None
    avg_final_latency_ms: int | None
    network_drop_count: int
    started_at: datetime | None
    last_active_at: datetime
    finalized_at: datetime | None


@router.get(
    "/sessions/{session_id}",
    response_model=SessionDetail,
    summary="Fetch a session detail + transcript",
)
async def get_session(
    session_id: UUID,
    claims: Annotated[Claims, Depends(requires("dictation.read", "dictation_session"))] = ...,  # type: ignore[assignment]
) -> SessionDetail:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await repository.get_session(conn, session_id=session_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return SessionDetail(
        id=row["id"],
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        status=row["status"],
        language=row["language"],
        target_kind=row["target_kind"],
        prompt_id=row["prompt_id"],
        transcript=row["transcript_jsonb"] or [],
        total_audio_ms=int(row["total_audio_ms"]),
        avg_partial_latency_ms=row["avg_partial_latency_ms"],
        avg_final_latency_ms=row["avg_final_latency_ms"],
        network_drop_count=int(row["network_drop_count"]),
        started_at=row["started_at"],
        last_active_at=row["last_active_at"],
        finalized_at=row["finalized_at"],
    )


@router.get(
    "/sessions",
    response_model=list[SessionSummary],
    summary="List recent sessions for the caller (or filtered by status).",
)
async def list_sessions(
    claims: Annotated[Claims, Depends(requires("dictation.read", "dictation_session"))],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    status_filter: Annotated[SessionState | None, Query(alias="status")] = None,
) -> list[SessionSummary]:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        if status_filter is None:
            rows = await repository.list_active_sessions_for_user(
                conn, user_id=claims.sub, limit=limit
            )
        else:
            rows = await repository.list_sessions(
                conn, status=status_filter, limit=limit
            )
    return [
        SessionSummary(
            id=r["id"],
            status=r["status"],
            language=r["language"],
            target_kind=r["target_kind"],
            started_at=r.get("started_at"),
            last_active_at=r["last_active_at"],
            finalized_at=r.get("finalized_at"),
            total_audio_ms=int(r.get("total_audio_ms", 0)),
            network_drop_count=int(r.get("network_drop_count", 0)),
        )
        for r in rows
    ]


@router.post(
    "/sessions/{session_id}/finalize",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Force-finalize a stuck reconnecting session.",
)
async def finalize_session_endpoint(
    session_id: UUID,
    claims: Annotated[Claims, Depends(requires("dictation.finalize", "dictation_session"))] = ...,  # type: ignore[assignment]
) -> dict[str, str]:
    state = get_state()
    # Two cases: the session is still in-process (live ctx exists) or
    # it's "abandoned"-shaped but the user wants to commit whatever's
    # left. We honour both.
    ctx = state.session_manager.get(session_id)
    if ctx is not None and ctx.user_id == claims.sub:
        from ..ws.handler import _finalize_normal  # local import — avoid cycle

        await _finalize_normal(ctx, state, reason="force_finalize")
        return {"status": "finalized"}

    # No in-process ctx — just verify ownership and mark the DB row.
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await repository.get_session(conn, session_id=session_id)
        if row is None or row["user_id"] != claims.sub:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if row["status"] in {"finalized", "abandoned", "failed"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"session is already terminal: {row['status']}",
            )
        await repository.update_status(
            conn, session_id=session_id, new_status=SessionState.FINALIZED
        )
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.SESSION_FINALIZED,
        actor_sub=claims.sub,
        target_kind="dictation_session",
        target_id=str(session_id),
        payload={"reason": "force_finalize_no_live_ctx"},
        severity=Severity.WARN,
    )
    return {"status": "finalized"}
