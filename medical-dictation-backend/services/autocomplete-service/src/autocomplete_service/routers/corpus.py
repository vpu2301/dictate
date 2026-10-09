"""/corpus — the sprint-21 review surface (ADR-0043/0044).

Serves the admin-console review queue, decision recording (including
spot-audit of jury-accepted rows), dashboard stats, and the release
register — all behind `corpus.review` — plus the post-S21 ingest slice:
candidate submission from the console fill worksheet (typed or dictated;
`corpus.contribute`) and promotion of accepted candidates into the serving
corpus (`corpus.promote`). Both writes go through SECURITY DEFINER
functions (migration 0088) because the pipeline operates on global rows
app_role deliberately cannot touch directly.

Privacy note: candidate phrases are PHI-derived until reviewed — responses
carry them (that's the review), but nothing here logs phrase text, and
audit payloads carry ids and counts only.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from auth import Claims
from corpus_risk import default_flagger, route_tier
from db import tenant_connection

from .. import audit_kinds
from .. import corpus_repository as corpus_repo
from ..deps import get_state, requires
from .phrases import _check_rate_limit, _reject_pii

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/corpus", tags=["corpus"])


class CandidateDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    phrase: str
    language: str
    specialty: str | None
    section_hint: str | None
    source_kind: str
    tier: int | None
    risk_flags: list[str]
    review_state: str
    review_engine: str | None  # 'human' | 'jury:<model>:<prompt_ver>' | None
    submitted_by: UUID | None  # console submitter (authored rows); else None
    jury_votes: list[dict[str, Any]]
    validator_report: dict[str, Any]
    created_at: datetime


class CandidateListDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[CandidateDTO]
    total: int


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["accept", "reject", "edit"]
    edited_text: str | None = Field(default=None, min_length=1, max_length=80)
    latency_ms: int | None = Field(default=None, ge=0)
    mode: Literal["review", "audit"] = "review"

    @model_validator(mode="after")
    def _edit_needs_text(self) -> ReviewRequest:
        if self.decision == "edit" and not self.edited_text:
            raise ValueError("edit decision requires edited_text")
        if self.mode == "audit" and self.decision == "edit":
            raise ValueError("audit mode records accept/reject only")
        return self


class ReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    review_state: str  # accepted | rejected | audited


class StatsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by_state: dict[str, int]
    by_tier: dict[str, int]
    reviews_total: int
    reviews_last_24h: int
    median_latency_ms: float | None
    audit_rejections: int
    by_quota_cell: list[dict[str, Any]]


class ReleaseDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    created_at: datetime
    phrase_count: int
    manifest_sha256: str
    notes: str


class ReleaseListDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ReleaseDTO]


class GapDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prefix: str  # PII-scrubbed before it was ever stored (sprint 10)
    impressions: int
    accepts: int  # always 0 by construction — kept explicit, not implied
    all_timeouts: bool


class GapListDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[GapDTO]


class SubmitCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phrase: str = Field(min_length=1, max_length=80)
    language: Literal["uk", "en"]
    specialty: str | None = None
    section_hint: str | None = None
    capture: Literal["typed", "dictated"] = "typed"


class SubmitCandidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    review_state: Literal["candidate"]
    # Where the router actually put it (migration 0098). The author asked
    # "where did my phrase go?" and the answer used to be unanswerable from
    # this response: tier 3 means a human reads it next, tier 2 means the jury
    # does. Returned so the worksheet can say which, with the flags that
    # decided it.
    tier: int
    risk_flags: list[str]


class PromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # None = promote every accepted global candidate; ids = promote just these.
    candidate_ids: list[UUID] | None = Field(default=None, max_length=500)


class PromoteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    promoted: int  # candidates flipped accepted → promoted
    inserted: int  # phrase rows actually inserted (< promoted on replays)


@router.get("/candidates", response_model=CandidateListDTO)
async def list_candidates(
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
    review_state: Annotated[
        Literal["candidate", "accepted", "rejected", "promoted"], Query()
    ] = "candidate",
    tier: Annotated[int | None, Query(ge=1, le=3)] = None,
    language: Annotated[Literal["uk", "en"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    sample: Annotated[Literal["oldest", "random"], Query()] = "oldest",
) -> CandidateListDTO:
    """The review queue, oldest first. tier=3 is the human-mandatory queue.
    sample=random serves the spot-audit sampler: an unbiased draw over the
    whole matching population (pagination is meaningless there — pass
    offset=0 and treat each call as a fresh sample)."""
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows, total = await corpus_repo.list_candidates(
            conn,
            review_state=review_state,
            tier=tier,
            language=language,
            limit=limit,
            offset=offset,
            sample=sample,
        )
    return CandidateListDTO(
        items=[
            CandidateDTO(
                id=r["id"],
                phrase=r["phrase"],
                language=r["language"],
                specialty=r["specialty"],
                section_hint=r["section_hint"],
                source_kind=r["source_kind"],
                tier=r["tier"],
                risk_flags=list(r["risk_flags"] or ()),
                review_state=r["review_state"],
                review_engine=r["review_engine"],
                submitted_by=r["submitted_by"],
                jury_votes=json.loads(r["jury_votes"]) if isinstance(r["jury_votes"], str) else list(r["jury_votes"] or ()),
                validator_report=(
                    json.loads(r["validator_report"])
                    if isinstance(r["validator_report"], str)
                    else dict(r["validator_report"] or {})
                ),
                created_at=r["created_at"],
            )
            for r in rows
        ],
        total=total,
    )


@router.post(
    "/candidates",
    response_model=SubmitCandidateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def submit_candidate(
    body: SubmitCandidateRequest,
    claims: Annotated[Claims, Depends(requires("corpus.contribute", "phrase"))],
) -> SubmitCandidateResponse:
    """Authored ingest: a phrase typed or dictated in the console fill
    worksheet becomes a GLOBAL candidate awaiting review — never anything
    pre-accepted. PII is rejected here (422) before any row exists; the
    phrase-write rate limit is shared with the library POST.

    TIER ROUTING (migration 0098). The phrase is risk-flagged and routed with
    corpus_risk — the module corpus-forge runs on mined, generated and
    imported candidates — so a dose, a drug, a laterality, a negation, an ICD
    code or an unknown abbreviation lands in tier 3 and reaches the human
    review queue. Until 0098 this route wrote tier 2 with no flags for
    everything it was given, which meant nothing submitted from the console
    could appear in that queue at all, and a phrase carrying a dose was one
    jury majority from a clinician's cursor unread."""
    state = get_state()
    await _check_rate_limit(state, claims)
    await _reject_pii(
        state, claims, field="phrase", text=body.phrase,
        target_kind="corpus_candidates",
    )

    risk_flags = default_flagger().flags(body.phrase)
    # validators_passed is True because the schema's own constraints (length,
    # language, the PII gate above) are the validators this path has; it only
    # ever moves tier 1 vs 2 for MINED candidates, and nothing here is mined.
    tier = route_tier(
        source_kind="authored", risk_flags=risk_flags, validators_passed=True
    )

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        try:
            candidate_id, submit_status = await corpus_repo.submit_candidate(
                conn,
                phrase=body.phrase,
                language=body.language,
                specialty=body.specialty,
                section_hint=body.section_hint,
                capture=body.capture,
                submitted_by=claims.sub,
                tenant_id=claims.tid,
                tier=tier,
                risk_flags=risk_flags,
            )
        except asyncpg.UniqueViolationError:
            # Lost a same-identity race after the in-fn duplicate check.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"error": "candidate_already_exists"},
            ) from None
        except asyncpg.CheckViolationError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "validation"},
            ) from None

    if submit_status != "created":
        error = (
            "phrase_already_in_corpus"
            if submit_status == "already_in_corpus"
            else "candidate_already_exists"
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"error": error, "id": str(candidate_id)},
        )

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.CORPUS_CANDIDATE_SUBMITTED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="phrase",
        target_id=candidate_id,
        payload={
            "language": body.language,
            "capture": body.capture,
            "text_length": len(body.phrase),
            # Which queue this phrase entered, and why. Flag NAMES are not
            # phrase text — they are the routing decision, and an audit that
            # cannot show why a dose phrase skipped human review is not an
            # audit of this pipeline.
            "tier": tier,
            "risk_flags": risk_flags,
        },
    )
    return SubmitCandidateResponse(
        id=candidate_id,
        review_state="candidate",
        tier=tier,
        risk_flags=risk_flags,
    )


@router.post("/promote", response_model=PromoteResponse)
async def promote_candidates(
    body: PromoteRequest,
    claims: Annotated[Claims, Depends(requires("corpus.promote", "phrase"))],
) -> PromoteResponse:
    """Accepted global candidates → serving corpus (system-scope phrases,
    live immediately). Idempotent; never un-retires. Release manifests stay
    a corpus-forge operator action."""
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        promoted, inserted = await corpus_repo.promote_accepted(
            conn, candidate_ids=body.candidate_ids
        )

    if promoted:
        await state.audit_writer.write_event(
            tenant_id=claims.tid,
            kind=audit_kinds.CORPUS_CANDIDATES_PROMOTED,
            actor_sub=claims.sub,
            actor_role=(claims.roles[0] if claims.roles else None),
            target_kind="phrase",
            target_id=None,
            payload={
                "promoted": promoted,
                "inserted": inserted,
                "requested_ids": (
                    len(body.candidate_ids) if body.candidate_ids is not None else None
                ),
            },
        )
        # New system phrases serve all tenants; other tenants' cached tries
        # converge via the cache TTL (same as a CLI release --apply).
        await state.trie_cache.bump_version_tag(tenant_id=claims.tid)
    return PromoteResponse(promoted=promoted, inserted=inserted)


@router.post("/candidates/{candidate_id}/review", response_model=ReviewResponse)
async def review_candidate(
    candidate_id: UUID,
    body: ReviewRequest,
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
) -> ReviewResponse:
    """Record one decision. mode='review' moves candidate→accepted/rejected
    (409 once decided). mode='audit' spot-checks a jury-accepted row without
    changing its state — an audit rejection is the jury-quality alert signal
    (`audit_rejections` in /corpus/stats)."""
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        new_state = await corpus_repo.record_review(
            conn,
            candidate_id=candidate_id,
            reviewer_id=claims.sub,
            decision=body.decision,
            edited_text=body.edited_text,
            latency_ms=body.latency_ms,
            mode=body.mode,
        )
        if new_state is None:
            if not await corpus_repo.candidate_exists(conn, candidate_id):
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown candidate")
            detail = (
                "not auditable: spot-audit applies to jury-accepted rows only"
                if body.mode == "audit"
                else "already decided"
            )
            raise HTTPException(status.HTTP_409_CONFLICT, detail=detail)

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.CORPUS_CANDIDATE_REVIEWED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="phrase",
        target_id=candidate_id,
        payload={
            "decision": body.decision,
            "mode": body.mode,
            "latency_ms": body.latency_ms,
            "new_state": new_state,
        },
    )
    return ReviewResponse(id=candidate_id, review_state=new_state)


@router.get("/gaps", response_model=GapListDTO)
async def corpus_gaps(
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> GapListDTO:
    """The demand half of "what should be authored": prefixes users typed for
    which autocomplete produced zero accepted suggestions, ranked by volume —
    the same rows `corpus-forge gaps` prints (migration 0090 pins the shared
    query). Platform-wide by design; prefixes were PII-scrubbed at ingest and
    redaction-marked rows are excluded."""
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await corpus_repo.telemetry_gaps(conn, limit=limit)
    return GapListDTO(
        items=[
            GapDTO(
                prefix=r["prefix"],
                impressions=int(r["impressions"]),
                accepts=int(r["accepts"]),
                all_timeouts=bool(r["all_timeouts"]),
            )
            for r in rows
        ]
    )


@router.get("/stats", response_model=StatsDTO)
async def corpus_stats(
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
) -> StatsDTO:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        data = await corpus_repo.stats(conn)
    return StatsDTO(**data)


@router.get("/releases", response_model=ReleaseListDTO)
async def corpus_releases(
    claims: Annotated[Claims, Depends(requires("corpus.review", "phrase"))],
) -> ReleaseListDTO:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await corpus_repo.list_releases(conn)
    return ReleaseListDTO(
        items=[
            ReleaseDTO(
                version=r["version"],
                created_at=r["created_at"],
                phrase_count=int(r["phrase_count"]),
                manifest_sha256=r["manifest_sha256"],
                notes=r["notes"],
            )
            for r in rows
        ]
    )
