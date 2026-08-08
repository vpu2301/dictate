#!/usr/bin/env python3
"""Rebuild the OpenSearch projection from Postgres (the source of truth,
ADR-0003). Used after mapping changes and as the reconciliation tool.

  uv run --project services/evidence-ingest python scripts/dev/reindex_lexical.py
"""

from __future__ import annotations

import asyncio

from db import create_pool
from evidence_ingest.adapters.search import LexicalIndex
from evidence_ingest.config import settings


async def main() -> None:
    pool = await create_pool(
        settings.db_app_role_dsn, application_name="evidence-reindex", max_size=2
    )
    lexical = LexicalIndex(url=settings.opensearch_url, index=settings.opensearch_index)
    await lexical.ensure_index()

    # Superuser-free: iterate distinct tenants via scoped connections is not
    # possible generically, so reindex per known partition. Global first;
    # tenant corpora reindex when S12's portal lands (documented).
    from uuid import UUID

    from db import tenant_connection
    from evidence_ingest.constants import GLOBAL_TENANT

    tenants: list[UUID] = [GLOBAL_TENANT]
    total = 0
    for tenant in tenants:
        async with tenant_connection(pool, tenant) as conn:
            versions = await conn.fetch(
                """
                SELECT dv.id AS version_id, d.id AS document_id, d.source_authority,
                       d.evidence_tier, d.published_at, d.jurisdiction, d.specialty,
                       d.license_class, d.retracted
                FROM document_versions dv JOIN documents d ON d.id = dv.document_id
                WHERE dv.tenant_id = $1
                """,
                tenant,
            )
            for v in versions:
                chunks = await conn.fetch(
                    "SELECT id, section_path, char_start, char_end, text FROM chunks"
                    " WHERE document_version_id = $1 ORDER BY char_start",
                    v["version_id"],
                )
                if not chunks:
                    continue
                # language isn't persisted in pg (TODO: column on documents);
                # re-derive with the parser heuristic over the first chunks.
                sample = " ".join(c["text"] for c in chunks[:3])
                import re

                cyr = len(re.findall(r"[а-яіїєґ]", sample, re.IGNORECASE))
                lat = len(re.findall(r"[a-z]", sample, re.IGNORECASE))
                language = None if cyr + lat < 50 else ("uk" if cyr >= lat else "en")
                total += await lexical.index_chunks(
                    tenant_id=tenant,
                    document_id=v["document_id"],
                    document_version_id=v["version_id"],
                    language=language,
                    source_authority=v["source_authority"],
                    evidence_tier=v["evidence_tier"],
                    published_at=v["published_at"].isoformat() if v["published_at"] else None,
                    chunks=[dict(c) for c in chunks],
                    jurisdiction=v["jurisdiction"],
                    specialty=list(v["specialty"] or []),
                    license_class=v["license_class"],
                    retracted=v["retracted"],
                )
    print(f"reindexed {total} chunks")
    await lexical.aclose()
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
