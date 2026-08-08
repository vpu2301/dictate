"""The ingest stage graph (spec §1): parse → chunk → enrich(dedup/version)
→ embed → index, resumable at stage granularity (embedding resumes at chunk
granularity). The job row's `state` names the NEXT stage to run; artifacts
between stages live in the encrypted corpus store under job-scoped keys, so
a worker restart resumes from the last completed stage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import asyncpg
from audit import AuditWriter, Severity
from db import tenant_connection
from storage import EncryptedObjectStore

from evidence_ingest import audit_kinds
from evidence_ingest.adapters.clamav import ClamAvScanner, VirusFoundError, VirusScanError
from evidence_ingest.adapters.search import LexicalIndex
from evidence_ingest.adapters.store import CorpusStore
from evidence_ingest.config import Settings
from evidence_ingest.domain import repository
from evidence_ingest.domain.chunking import chunk_document
from evidence_ingest.domain.enrich import content_checksum, enrich
from evidence_ingest.domain.parse_types import ParserError, full_text
from evidence_ingest.domain.parsers import docx, html_guideline, markdown, pdf, pmc_xml
from evidence_safety import screen_text
from models import ModelGatewayClient

logger = logging.getLogger(__name__)

_PARSERS = {
    "pdf": pdf.parse,
    "pmc_xml": pmc_xml.parse,
    "html_guideline": html_guideline.parse,
    "markdown": markdown.parse,
    "docx": docx.parse,
}


class PermanentJobError(Exception):
    """Not retryable: bad input, virus, quarantine rejection."""


@dataclass(slots=True)
class PipelineContext:
    settings: Settings
    app_pool: asyncpg.Pool
    corpus_store: CorpusStore
    object_store: EncryptedObjectStore
    lexical: LexicalIndex
    gateway: ModelGatewayClient
    audit_writer: AuditWriter
    scanner: ClamAvScanner | None


async def _load_source(ctx: PipelineContext, *, source_uri: str, tenant_id: UUID) -> bytes:
    if source_uri.startswith("file://"):
        path = Path(source_uri.removeprefix("file://"))
        data = await asyncio.to_thread(path.read_bytes)
    elif source_uri.startswith("minio://"):
        key = source_uri.split("/", 3)[3]
        data = await ctx.corpus_store.get_raw(key=key, tenant_id=tenant_id)
    else:
        raise PermanentJobError(f"unsupported source scheme: {source_uri}")
    if len(data) > ctx.settings.max_upload_bytes:
        raise PermanentJobError(
            f"source exceeds {ctx.settings.max_upload_bytes} bytes ({len(data)})"
        )
    if not data:
        raise PermanentJobError("source is empty")
    return data


async def run_job(ctx: PipelineContext, *, job_id: UUID, tenant_id: UUID) -> str:
    """Run the job from its current state to completion / quarantine / failure.

    Returns the final state. Raises PermanentJobError (do not retry) or the
    original exception (retryable) after recording the failure on the row.
    """
    async with tenant_connection(ctx.app_pool, tenant_id) as conn:
        job = await repository.get_job(conn, job_id=job_id)
    if job is None:
        raise PermanentJobError(f"job {job_id} not found in tenant {tenant_id}")
    state = job["state"]
    if state == "queued":
        state = "parsing"

    try:
        while state not in ("done", "dead", "quarantined"):
            started = time.monotonic()
            runner = {
                "parsing": _stage_parse,
                "chunking": _stage_chunk,
                "enriching": _stage_enrich,
                "embedding": _stage_embed,
                "indexing": _stage_index,
            }[state]
            next_state = await runner(ctx, job=job, tenant_id=tenant_id)
            async with tenant_connection(ctx.app_pool, tenant_id) as conn:
                await repository.set_job_state(
                    conn,
                    job_id=job_id,
                    state=next_state,
                    stage=state,
                    stage_seconds=time.monotonic() - started,
                )
                job = await repository.get_job(conn, job_id=job_id)
            assert job is not None
            state = next_state
        return state
    except (PermanentJobError, VirusFoundError) as exc:
        await _record_failure(
            ctx, job_id=job_id, tenant_id=tenant_id, stage=state, error=str(exc), dead=True
        )
        raise PermanentJobError(str(exc)) from exc
    except Exception as exc:
        attempts = int(job["attempts"]) + 1
        dead = attempts >= ctx.settings.ingest_max_retries
        await _record_failure(
            ctx, job_id=job_id, tenant_id=tenant_id, stage=state, error=str(exc), dead=dead
        )
        if dead:
            raise PermanentJobError(f"retries exhausted at {state}: {exc}") from exc
        raise


async def _record_failure(
    ctx: PipelineContext, *, job_id: UUID, tenant_id: UUID, stage: str, error: str, dead: bool
) -> None:
    async with tenant_connection(ctx.app_pool, tenant_id) as conn:
        await repository.record_failure(
            conn, job_id=job_id, tenant_id=tenant_id, stage=stage, error=error, dead=dead
        )


# ── stages ──────────────────────────────────────────────────────────────


async def _stage_parse(ctx: PipelineContext, *, job: asyncpg.Record, tenant_id: UUID) -> str:
    job_id: UUID = job["id"]
    content = await _load_source(ctx, source_uri=job["source_uri"], tenant_id=tenant_id)

    if ctx.settings.virus_scan_enabled:
        if ctx.scanner is None:
            raise VirusScanError("virus scan enabled but scanner not configured")
        await ctx.scanner.scan(content)  # VirusFoundError → permanent

    parser = _PARSERS.get(job["kind"])
    if parser is None:
        raise PermanentJobError(f"unsupported format: {job['kind']}")
    try:
        parsed = await asyncio.wait_for(
            asyncio.to_thread(parser, content),
            timeout=ctx.settings.parse_timeout_seconds,
        )
    except ParserError as exc:
        raise PermanentJobError(f"parse failed: {exc}") from exc
    except TimeoutError as exc:
        raise PermanentJobError("parse timed out") from exc

    await ctx.corpus_store.put_raw(
        key=CorpusStore.raw_key(str(job_id), 0), content=content, tenant_id=tenant_id
    )
    await ctx.corpus_store.put_parsed(
        key=CorpusStore.parsed_key(str(job_id), 0), doc=parsed, tenant_id=tenant_id
    )

    hits = screen_text(full_text(parsed))
    if hits:
        async with tenant_connection(ctx.app_pool, tenant_id) as conn:
            quarantine_id = await repository.create_quarantine(
                conn,
                tenant_id=tenant_id,
                job_id=job_id,
                document_ref=CorpusStore.parsed_key(str(job_id), 0),
                reason="instruction-pattern payload (LM4 ingest screen)",
                patterns=[{"pattern": h.pattern, "excerpt": h.excerpt} for h in hits],
            )
        await ctx.audit_writer.write_event(
            tenant_id=tenant_id,
            kind=audit_kinds.DOCUMENT_QUARANTINED,
            target_kind="quarantine",
            target_id=quarantine_id,
            payload={"job_id": str(job_id), "patterns": [h.pattern for h in hits]},
            severity=Severity.SEC,
        )
        return "quarantined"
    return "chunking"


async def _stage_chunk(ctx: PipelineContext, *, job: asyncpg.Record, tenant_id: UUID) -> str:
    job_id: UUID = job["id"]
    parsed = await ctx.corpus_store.get_parsed(
        key=CorpusStore.parsed_key(str(job_id), 0), tenant_id=tenant_id
    )
    spans = chunk_document(
        parsed,
        target_tokens=ctx.settings.chunk_target_tokens,
        max_tokens=ctx.settings.chunk_max_tokens,
        min_tokens=ctx.settings.chunk_min_tokens,
    )
    if not spans:
        raise PermanentJobError("document produced zero chunks")
    payload = json.dumps(
        [
            {
                "section_path": s.section_path,
                "char_start": s.char_start,
                "char_end": s.char_end,
                "text": s.text,
            }
            for s in spans
        ]
    ).encode("utf-8")
    await ctx.object_store.put(key=_chunks_key(job_id), plaintext=payload, tenant_id=tenant_id)
    return "enriching"


def _chunks_key(job_id: UUID) -> str:
    return f"corpus/parsed/{job_id}/v0.chunks.json"


async def _stage_enrich(ctx: PipelineContext, *, job: asyncpg.Record, tenant_id: UUID) -> str:
    job_id: UUID = job["id"]
    content = await ctx.corpus_store.get_raw(
        key=CorpusStore.raw_key(str(job_id), 0), tenant_id=tenant_id
    )
    parsed = await ctx.corpus_store.get_parsed(
        key=CorpusStore.parsed_key(str(job_id), 0), tenant_id=tenant_id
    )
    overrides = json.loads(job["metadata_overrides"] or "{}")
    metadata = enrich(parsed, content, overrides)
    checksum = content_checksum(content)

    chunk_blob = await ctx.object_store.get(key=_chunks_key(job_id), tenant_id=tenant_id)
    chunk_dicts = json.loads(chunk_blob)

    async with tenant_connection(ctx.app_pool, tenant_id) as conn:
        document_id = await repository.upsert_document(
            conn,
            tenant_id=tenant_id,
            canonical_id=metadata.canonical_id,
            title=metadata.title,
            source_authority=metadata.source_authority,
            evidence_tier=metadata.evidence_tier,
            jurisdiction=metadata.jurisdiction,
            specialty=metadata.specialty,
            published_at=metadata.published_at,
            license_class=metadata.license_class,
        )
        latest = await repository.latest_version(conn, document_id=document_id)
        if latest is not None and latest["checksum"] == checksum:
            # Unchanged content: idempotent no-op (spec D4) — UNLESS a prior
            # run died between enrich and embed, leaving the version with
            # unembedded chunks; then resume instead of declaring done.
            await repository.link_document(
                conn, job_id=job_id, document_id=document_id, document_version_id=latest["id"]
            )
            pending = await repository.pending_embedding_batch(
                conn, document_version_id=latest["id"], limit=1
            )
            if pending:
                logger.info("ingest.resume_unembedded", extra={"job_id": str(job_id)})
                return "embedding"
            await repository.set_job_state(conn, job_id=job_id, state="done")
            logger.info("ingest.noop_unchanged", extra={"job_id": str(job_id)})
            return "done"
        version = 1 if latest is None else int(latest["version"]) + 1
        version_id = await repository.insert_version(
            conn,
            tenant_id=tenant_id,
            document_id=document_id,
            version=version,
            content_ref=f"minio://{ctx.object_store.bucket}/{CorpusStore.raw_key(str(job_id), 0)}",
            parsed_ref=f"minio://{ctx.object_store.bucket}/{CorpusStore.parsed_key(str(job_id), 0)}",
            checksum=checksum,
        )
        await repository.insert_chunks(
            conn, tenant_id=tenant_id, document_version_id=version_id, chunks=chunk_dicts
        )
        await repository.link_document(
            conn, job_id=job_id, document_id=document_id, document_version_id=version_id
        )
    return "embedding"


async def _stage_embed(ctx: PipelineContext, *, job: asyncpg.Record, tenant_id: UUID) -> str:
    version_id: UUID | None = job["document_version_id"]
    if version_id is None:
        raise PermanentJobError("embedding stage reached without a document version")
    while True:
        async with tenant_connection(ctx.app_pool, tenant_id) as conn:
            batch = await repository.pending_embedding_batch(
                conn, document_version_id=version_id, limit=ctx.settings.embed_batch_size
            )
        if not batch:
            return "indexing"
        vectors = await ctx.gateway.embed("embed.dense", [r["text"] for r in batch])
        async with tenant_connection(ctx.app_pool, tenant_id) as conn:
            await repository.store_embeddings(conn, ids=[r["id"] for r in batch], vectors=vectors)


async def _stage_index(ctx: PipelineContext, *, job: asyncpg.Record, tenant_id: UUID) -> str:
    job_id: UUID = job["id"]
    version_id: UUID | None = job["document_version_id"]
    document_id: UUID | None = job["document_id"]
    if version_id is None or document_id is None:
        raise PermanentJobError("indexing stage reached without a document version")
    overrides = json.loads(job["metadata_overrides"] or "{}")
    parsed = await ctx.corpus_store.get_parsed(
        key=CorpusStore.parsed_key(str(job_id), 0), tenant_id=tenant_id
    )
    async with tenant_connection(ctx.app_pool, tenant_id) as conn:
        rows = await repository.chunks_for_version(conn, document_version_id=version_id)
        doc = await conn.fetchrow(
            "SELECT source_authority, evidence_tier, published_at, jurisdiction,"
            " specialty, license_class FROM documents WHERE id = $1",
            document_id,
        )
    assert doc is not None
    await ctx.lexical.ensure_index()
    await ctx.lexical.index_chunks(
        tenant_id=tenant_id,
        document_id=document_id,
        document_version_id=version_id,
        language=overrides.get("language") or parsed.language,
        source_authority=doc["source_authority"],
        evidence_tier=doc["evidence_tier"],
        published_at=doc["published_at"].isoformat() if doc["published_at"] else None,
        chunks=[dict(r) for r in rows],
        jurisdiction=doc["jurisdiction"],
        specialty=list(doc["specialty"] or []),
        license_class=doc["license_class"],
    )
    await ctx.audit_writer.write_event(
        tenant_id=tenant_id,
        kind=audit_kinds.DOCUMENT_INGESTED,
        target_kind="documents",
        target_id=document_id,
        payload={"job_id": str(job_id), "version_id": str(version_id), "chunks": len(rows)},
        severity=Severity.INFO,
    )
    return "done"
