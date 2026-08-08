"""Retraction feed processing (spec D8, rules RR5/ET3).

Feed format: CSV with a header containing at least one of `doi`, `pmid`
(Retraction Watch export class). Matching documents get `retracted = true`
(kept for provenance; excluded from the NEXT snapshot; already-frozen
snapshots keep their member list — immutability). The run is atomic: one
transaction, nothing half-flagged."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from uuid import UUID

import asyncpg
from audit import AuditWriter, Severity
from db import tenant_connection

from evidence_ingest import audit_kinds
from evidence_ingest.adapters.search import LexicalIndex
from evidence_ingest.domain import repository


class MalformedFeedError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RetractionResult:
    flagged: int
    canonical_ids: list[str]


def parse_feed(raw: bytes) -> list[str]:
    """Extract canonical ids (doi:/pmid:) from a retraction CSV."""
    try:
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            raise MalformedFeedError("feed has no header row")
        fields = {f.strip().lower(): f for f in reader.fieldnames}
        doi_col = next((fields[k] for k in fields if "doi" in k), None)
        pmid_col = next((fields[k] for k in fields if "pmid" in k or "pubmedid" in k), None)
        if doi_col is None and pmid_col is None:
            raise MalformedFeedError("feed has neither a DOI nor a PMID column")
        ids: list[str] = []
        for row in reader:
            if doi_col and (row.get(doi_col) or "").strip():
                ids.append("doi:" + row[doi_col].strip().lower())
            elif pmid_col and (row.get(pmid_col) or "").strip():
                ids.append("pmid:" + row[pmid_col].strip())
        return ids
    except csv.Error as exc:
        raise MalformedFeedError(f"CSV parse error: {exc}") from exc


async def process_feed(
    *,
    pool: asyncpg.Pool,
    audit_writer: AuditWriter,
    lexical: LexicalIndex,
    tenant_id: UUID,
    feed_raw: bytes,
) -> RetractionResult:
    canonical_ids = parse_feed(feed_raw)
    if not canonical_ids:
        return RetractionResult(flagged=0, canonical_ids=[])
    async with tenant_connection(pool, tenant_id) as conn:
        flagged = await repository.mark_retracted(
            conn, tenant_id=tenant_id, canonical_ids=canonical_ids
        )
    for row in flagged:
        await lexical.mark_retracted(document_id=row["id"])
        await audit_writer.write_event(
            tenant_id=tenant_id,
            kind=audit_kinds.DOCUMENT_RETRACTED,
            target_kind="documents",
            target_id=row["id"],
            payload={"canonical_id": row["canonical_id"]},
            severity=Severity.SEC,
        )
    return RetractionResult(
        flagged=len(flagged), canonical_ids=[r["canonical_id"] for r in flagged]
    )
