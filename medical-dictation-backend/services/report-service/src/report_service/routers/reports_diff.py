"""GET /reports/{id}/diff — day-5."""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from auth import Action, Claims, TargetKind
from db import tenant_connection
from report_models import DiffResponse

from ..deps import get_state, requires
from ..domain import reports_repository as repo
from ..domain.diff_engine import compute_diff

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/reports", tags=["reports"])


@router.get("/{report_id}/diff", response_model=DiffResponse)
async def get_diff(
    report_id: UUID,
    claims: Annotated[Claims, Depends(requires(Action.READ, TargetKind.REPORT))],
    from_: Annotated[str, Query(alias="from", description="version_id or version_number")],
    to: Annotated[str, Query(description="version_id or version_number")],
) -> DiffResponse:
    state = get_state()

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        report = await repo.fetch_report(conn, report_id=report_id)
        if report is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="report not found")

        from_version = await _resolve(conn, report_id=report_id, ref=from_)
        to_version = await _resolve(conn, report_id=report_id, ref=to)
        if from_version is None or to_version is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail="version not found in this report"
            )

        cache_hit = state.diff_cache.get(
            report_id=report_id, from_id=from_version.id, to_id=to_version.id
        )
        if cache_hit is not None:
            state.diff_cache_hit_metric.add(1, {"hit": "true"})
            return cache_hit
        state.diff_cache_hit_metric.add(1, {"hit": "false"})

        diff = compute_diff(
            report_id=str(report_id),
            from_version_id=str(from_version.id),
            from_version_number=from_version.version_number,
            from_content=from_version.content,
            to_version_id=str(to_version.id),
            to_version_number=to_version.version_number,
            to_content=to_version.content,
        )

    state.diff_cache.put(
        report_id=report_id,
        from_id=from_version.id,
        to_id=to_version.id,
        value=diff,
    )
    return diff


async def _resolve(conn, *, report_id: UUID, ref: str):
    if ref.isdigit():
        return await repo.fetch_version_by_number(
            conn, report_id=report_id, version_number=int(ref)
        )
    try:
        version_id = UUID(ref)
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"version reference {ref!r} is neither a version_number nor a UUID",
        ) from None
    v = await repo.fetch_version(conn, version_id=version_id)
    if v is None or v.report_id != report_id:
        return None
    return v
