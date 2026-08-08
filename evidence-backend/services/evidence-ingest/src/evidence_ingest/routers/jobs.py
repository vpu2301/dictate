"""Internal ops API: jobs, quarantine review (spec §3, D5)."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated
from uuid import UUID

from audit import Severity
from auth import Claims
from db import tenant_connection
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from evidence_ingest import audit_kinds
from evidence_ingest.deps import requires
from evidence_ingest.domain import repository
from evidence_ingest.main_deps import get_state

router = APIRouter(tags=["ingest"])

_MANAGE = Depends(requires("evidence.corpus.manage", "evidence_corpus"))


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_uri: str
    kind: str
    metadata_overrides: dict[str, str] = {}


class CreateJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    deduplicated: bool = False


@router.post("/ingest/jobs", status_code=202)
async def create_job(
    body: CreateJobRequest, claims: Annotated[Claims, _MANAGE]
) -> CreateJobResponse:
    state = get_state()
    if body.kind not in ("pdf", "pmc_xml", "html_guideline", "markdown", "docx"):
        raise HTTPException(status_code=422, detail=f"unsupported_format: {body.kind}")
    idempotence_key = hashlib.sha256(
        (body.source_uri + json.dumps(body.metadata_overrides, sort_keys=True)).encode()
    ).hexdigest()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        job_id = await repository.create_job(
            conn,
            tenant_id=claims.tid,
            source_uri=body.source_uri,
            kind=body.kind,
            idempotence_key=idempotence_key,
            metadata_overrides=body.metadata_overrides,
        )
        if job_id is None:
            existing = await conn.fetchval(
                "SELECT id FROM ingest_jobs WHERE tenant_id = $1 AND idempotence_key = $2",
                claims.tid,
                idempotence_key,
            )
            return CreateJobResponse(job_id=existing, deduplicated=True)
    await state.producer.send(
        value=json.dumps({"job_id": str(job_id)}).encode(),
        key=str(job_id).encode(),
        headers={"tenant_id": str(claims.tid), "schema_version": "1"},
    )
    return CreateJobResponse(job_id=job_id)


@router.get("/ingest/jobs/{job_id}")
async def get_job(job_id: UUID, claims: Annotated[Claims, _MANAGE]) -> dict[str, object]:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        job = await repository.get_job(conn, job_id=job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        errors = await conn.fetch(
            "SELECT stage, error, created_at FROM ingest_errors WHERE job_id = $1", job_id
        )
    return {
        "job_id": str(job["id"]),
        "state": job["state"],
        "kind": job["kind"],
        "source_uri": job["source_uri"],
        "attempts": job["attempts"],
        "last_error": job["last_error"],
        "timings": json.loads(job["timings"])
        if isinstance(job["timings"], str)
        else job["timings"],
        "document_id": str(job["document_id"]) if job["document_id"] else None,
        "errors": [dict(e) for e in errors],
    }


class QuarantineDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str  # approved | rejected


@router.post("/quarantine/{quarantine_id}/decision")
async def decide(
    quarantine_id: UUID,
    body: QuarantineDecisionRequest,
    claims: Annotated[Claims, _MANAGE],
) -> dict[str, str]:
    if body.decision not in ("approved", "rejected"):
        raise HTTPException(status_code=422, detail="decision must be approved|rejected")
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await repository.decide_quarantine(
            conn, quarantine_id=quarantine_id, reviewed_by=claims.sub, decision=body.decision
        )
        if row is None:
            raise HTTPException(status_code=404, detail="quarantine entry not found or decided")
        job_id: UUID = row["job_id"]
        next_state = "chunking" if body.decision == "approved" else "dead"
        await repository.set_job_state(conn, job_id=job_id, state=next_state)
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.QUARANTINE_DECIDED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="quarantine",
        target_id=quarantine_id,
        payload={"decision": body.decision, "job_id": str(job_id)},
        severity=Severity.SEC,
    )
    if body.decision == "approved":
        await state.producer.send(
            value=json.dumps({"job_id": str(job_id)}).encode(),
            key=str(job_id).encode(),
            headers={"tenant_id": str(claims.tid), "schema_version": "1"},
        )
    return {"status": body.decision}
