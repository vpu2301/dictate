"""POST/GET /v1/reports/{id}/synthesize — report synthesis (spec item 1).

Synthesis rewrites the *raw dictation* of selected sections into clean,
presentation-ready prose via the configured :class:`Synthesizer` (a
deterministic offline mock by default). It is **read-only**: it persists a
job row and returns both the original dictation and the synthesised text
per section so the SPA can diff/revert. Applying the result is the caller's
job, done later through the existing draft PUT.
"""

from __future__ import annotations

import hashlib
import json
import logging
from functools import lru_cache
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from audit import Severity
from auth import Claims
from db import tenant_connection
from report_models import ReportContent

from .. import audit_kinds
from ..config import settings
from ..deps import get_state, requires
from ..domain import reports_repository as repo
from ..domain import synthesis_jobs
from ..domain.synthesis import Synthesizer, build_synthesizer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/reports", tags=["reports"])


# ── Request / response shapes ───────────────────────────────────────


class SynthesizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sections: list[str] | None = None  # default: all sections in the report
    language: Literal["uk", "en"]


class SynthesizedSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_key: str
    original: str
    text: str


class SynthesizeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    status: str
    sections: list[SynthesizedSection]


class SynthesisJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    sections: list[SynthesizedSection]


# ── Helpers ─────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def get_synthesizer() -> Synthesizer:
    """The configured synthesizer. Production swap = implement
    ``AnthropicSynthesizer`` + set ``MDX_SYNTHESIS_PROVIDER=anthropic``."""
    return build_synthesizer(settings.synthesis_provider, settings.synthesis_model)


def _request_hash(
    *, report_id: UUID, version_number: int, sections: list[str], language: str, body_hash: str
) -> str:
    """Idempotency key over the request's identity-bearing inputs."""
    key = {
        "report_id": str(report_id),
        "version_number": version_number,
        "sections": sorted(sections),
        "language": language,
        "body_hash": body_hash,
    }
    return hashlib.sha256(
        json.dumps(key, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


async def _resolve_section_prompts(conn: object, *, content: ReportContent) -> dict[str, tuple[str, str]]:
    """Map section_key → (synthesis_prompt, asr_prompt) from the template.

    Degrades to ``{}`` (never raises) so a missing/unparseable template
    still yields synthesis — the mock ignores the prompts, and the
    production path can fall back to defaults.
    """
    from template_models import TemplateDefinition

    from ..domain.repository import get_template

    try:
        tmpl_row = await get_template(conn, template_id=content.template_id)  # type: ignore[arg-type]
        if tmpl_row is None:
            return {}
        raw = tmpl_row["schema_jsonb"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        definition = TemplateDefinition.model_validate(raw)
    except Exception:
        logger.warning(
            "could not resolve template %s for synthesis prompts",
            content.template_id,
            exc_info=True,
        )
        return {}
    return {s.id: (s.synthesis_prompt, s.asr_prompt) for s in definition.sections}


# ── Routes ──────────────────────────────────────────────────────────


@router.post("/{report_id}/synthesize", response_model=SynthesizeResponse)
async def synthesize_report(
    report_id: UUID,
    body: SynthesizeRequest,
    claims: Annotated[Claims, Depends(requires("report.write", "report"))],
) -> SynthesizeResponse:
    state = get_state()
    synthesizer = get_synthesizer()

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await repo.fetch_report(conn, report_id=report_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="report not found")

        version = await repo.fetch_version(conn, version_id=row.current_version_id)
        if version is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="report has no content")
        content = version.content

        present = [s.section_key for s in content.sections]
        present_set = set(present)
        if body.sections is None:
            target = present
        else:
            unknown = [k for k in body.sections if k not in present_set]
            if unknown:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"title": "Unknown sections", "unknown": unknown},
                )
            # Preserve report order, restricted to the requested set.
            requested = set(body.sections)
            target = [k for k in present if k in requested]

        body_hash = repo.body_hash_for(content)
        request_hash = _request_hash(
            report_id=report_id,
            version_number=row.current_version_number,
            sections=target,
            language=body.language,
            body_hash=body_hash,
        )

        # Idempotency: identical completed job → return it, no recompute.
        existing = await synthesis_jobs.find_completed_job(
            conn, report_id=report_id, request_hash=request_hash
        )
        if existing is not None:
            return SynthesizeResponse(
                job_id=existing.id,
                status=existing.status,
                sections=[SynthesizedSection(**s) for s in existing.sections],
            )

        await state.audit_writer.write_event(
            tenant_id=claims.tid,
            kind=audit_kinds.REPORT_SYNTHESIS_STARTED,
            actor_sub=claims.sub,
            actor_role=(claims.roles[0] if claims.roles else None),
            target_kind="report",
            target_id=str(report_id),
            payload={
                "section_count": len(target),
                "language": body.language,
                "provider": settings.synthesis_provider,
            },
            severity=Severity.INFO,
        )

        prompts = await _resolve_section_prompts(conn, content=content)
        by_key = {s.section_key: s for s in content.sections}
        results: list[dict[str, str]] = []
        for key in target:
            raw_text = by_key[key].text
            synthesis_prompt, asr_prompt = prompts.get(key, ("", ""))
            text = synthesizer.synthesize_section(
                section_key=key,
                raw_text=raw_text,
                synthesis_prompt=synthesis_prompt,
                asr_prompt=asr_prompt,
                language=body.language,
            )
            results.append({"section_key": key, "original": raw_text, "text": text})

        job_id = await synthesis_jobs.insert_job(
            conn,
            tenant_id=claims.tid,
            report_id=report_id,
            version_number=row.current_version_number,
            language=body.language,
            sections=results,  # type: ignore[arg-type]
            request_hash=request_hash,
            status="completed",
        )

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.REPORT_SYNTHESIS_COMPLETED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="report",
        target_id=str(report_id),
        payload={
            "job_id": str(job_id),
            "section_count": len(target),
            "language": body.language,
            "provider": settings.synthesis_provider,
        },
        severity=Severity.INFO,
    )

    return SynthesizeResponse(
        job_id=job_id,
        status="completed",
        sections=[SynthesizedSection(**s) for s in results],
    )


@router.get("/{report_id}/synthesize/{job_id}", response_model=SynthesisJobResponse)
async def get_synthesis_job(
    report_id: UUID,
    job_id: UUID,
    claims: Annotated[Claims, Depends(requires("report.write", "report"))],
) -> SynthesisJobResponse:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        job = await synthesis_jobs.fetch_job(conn, job_id=job_id, report_id=report_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="synthesis job not found")
    return SynthesisJobResponse(
        status=job.status,
        sections=[SynthesizedSection(**s) for s in job.sections],
    )
