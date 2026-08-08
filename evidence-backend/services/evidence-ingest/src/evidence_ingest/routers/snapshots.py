"""Snapshot + retraction + stats ops endpoints (spec §3)."""

from __future__ import annotations

from typing import Annotated

from auth import Claims
from db import tenant_connection
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from evidence_ingest.deps import requires
from evidence_ingest.domain import repository, retractions, snapshots
from evidence_ingest.main_deps import get_state

router = APIRouter(tags=["corpus"])

_MANAGE = Depends(requires("evidence.corpus.manage", "evidence_corpus"))
_OPS_READ = Depends(requires("evidence.ops.read", "evidence"))


class SnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str


@router.post("/corpus/snapshots", status_code=201)
async def create_snapshot(
    body: SnapshotRequest, claims: Annotated[Claims, _MANAGE]
) -> dict[str, object]:
    state = get_state()
    try:
        result = await snapshots.build_snapshot(
            pool=state.app_pool,
            audit_writer=state.audit_writer,
            tenant_id=claims.tid,
            label=body.label,
        )
    except snapshots.SnapshotDirtyError as exc:
        raise HTTPException(
            status_code=409, detail=f"snapshot_dirty: {exc.pending} pending job(s)"
        ) from exc
    return {
        "snapshot_id": str(result.snapshot_id),
        "member_count": result.member_count,
        "license_excluded": result.license_excluded,
    }


class RetractionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Object key (minio://) or inline CSV content; inline wins when both set.
    feed_ref: str | None = None
    feed_csv: str | None = None


@router.post("/retractions/run")
async def run_retractions(
    body: RetractionsRequest, claims: Annotated[Claims, _MANAGE]
) -> dict[str, object]:
    state = get_state()
    if body.feed_csv is not None:
        raw = body.feed_csv.encode("utf-8")
    elif body.feed_ref is not None and body.feed_ref.startswith("minio://"):
        key = body.feed_ref.split("/", 3)[3]
        raw = await state.corpus_store.get_raw(key=key, tenant_id=claims.tid)
    else:
        raise HTTPException(status_code=422, detail="feed_ref or feed_csv required")
    try:
        result = await retractions.process_feed(
            pool=state.app_pool,
            audit_writer=state.audit_writer,
            lexical=state.lexical,
            tenant_id=claims.tid,
            feed_raw=raw,
        )
    except retractions.MalformedFeedError as exc:
        raise HTTPException(status_code=422, detail=f"malformed_feed: {exc}") from exc
    return {"flagged": result.flagged, "canonical_ids": result.canonical_ids}


@router.get("/corpus/stats")
async def stats(claims: Annotated[Claims, _OPS_READ]) -> dict[str, object]:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        return await repository.corpus_stats(conn, tenant_id=claims.tid)
