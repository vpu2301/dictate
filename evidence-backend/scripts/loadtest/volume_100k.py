#!/usr/bin/env python3
"""AC-S02-B-1/B-7 index-path capacity exercise.

Measures, at 100k-chunk scale, the two write paths the real bulk load will
hit — pgvector HNSW inserts and OpenSearch bulk indexing — using synthetic
chunk rows under a clearly-marked throwaway document in the global tenant.
Synthetic data NEVER enters a snapshot (the document is license_class
'restricted') and the script deletes everything it created at the end
unless --keep is passed.

Also measures real BGE-M3 embed throughput on a small sample to project the
embedding backlog drain time (the 6h NFR is a GPU-rig target; the Mac number
is recorded for scale reference).

Usage:
  uv run --project services/evidence-ingest python scripts/loadtest/volume_100k.py \
      [--chunks 100000] [--keep]
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import time
import uuid

from db import create_pool, tenant_connection
from evidence_ingest.adapters.search import LexicalIndex
from evidence_ingest.config import settings
from evidence_ingest.constants import GLOBAL_TENANT
from opensearchpy import helpers

from models import ModelGatewayClient

WORDS = [
    "пацієнт",
    "терапія",
    "дозування",
    "креатинін",
    "фібриляція",
    "антикоагулянт",
    "нирки",
    "серце",
    "guideline",
    "dose",
    "renal",
    "atrial",
    "anticoagulant",
    "therapy",
    "creatinine",
    "cardiac",
    "monitoring",
    "adverse",
    "interaction",
    "contraindication",
    "recommendation",
    "evidence",
]


def _text(rng: random.Random) -> str:
    return " ".join(rng.choices(WORDS, k=80))


def _vector(rng: random.Random) -> str:
    raw = [rng.gauss(0, 1) for _ in range(1024)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return "[" + ",".join(f"{x / norm:.6f}" for x in raw) + "]"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks", type=int, default=100_000)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    rng = random.Random(42)

    # HNSW maintenance makes batch inserts slow as the graph grows — a 30s
    # command timeout (the pool default) trips around ~25k rows.
    pool = await create_pool(
        settings.db_app_role_dsn,
        application_name="evidence-loadtest",
        max_size=4,
        command_timeout=600.0,
    )
    lexical = LexicalIndex(url=settings.opensearch_url, index="evidence-chunks-loadtest")
    gateway = ModelGatewayClient(settings.gateway_base_url)

    # 1. Real embed throughput sample (3 batches of 64).
    sample = [_text(rng) for _ in range(64)]
    t0 = time.monotonic()
    for _ in range(3):
        await gateway.embed("embed.dense", sample)
    embed_seconds = time.monotonic() - t0
    chunks_per_s = 192 / embed_seconds
    print(
        f"embed.dense (CPU): {chunks_per_s:.1f} chunks/s "
        f"→ 100k backlog ≈ {100_000 / chunks_per_s / 3600:.1f} h on this host"
    )

    # 2. Throwaway document + version.
    async with tenant_connection(pool, GLOBAL_TENANT) as conn:
        doc_id = await conn.fetchval(
            """
            INSERT INTO documents (tenant_id, canonical_id, title, source_authority,
                                   evidence_tier, license_class)
            VALUES ($1, 'loadtest:volume-100k', 'LOADTEST volume document',
                    'other', 'other', 'restricted')
            ON CONFLICT (tenant_id, canonical_id) DO UPDATE SET title = EXCLUDED.title
            RETURNING id
            """,
            GLOBAL_TENANT,
        )
        version_id = await conn.fetchval(
            """
            INSERT INTO document_versions (tenant_id, document_id, version, content_ref, checksum)
            VALUES ($1, $2, (SELECT coalesce(max(version),0)+1 FROM document_versions
                             WHERE document_id = $2), 'loadtest://none', 'loadtest')
            RETURNING id
            """,
            GLOBAL_TENANT,
            doc_id,
        )

    # 3. pgvector insert at scale.
    batch_size = 1_000
    t0 = time.monotonic()
    inserted = 0
    while inserted < args.chunks:
        n = min(batch_size, args.chunks - inserted)
        rows = [
            (
                GLOBAL_TENANT,
                version_id,
                f"loadtest/{inserted + i}",
                (inserted + i) * 100,
                (inserted + i) * 100 + 90,
                _text(rng),
                _vector(rng),
            )
            for i in range(n)
        ]
        async with tenant_connection(pool, GLOBAL_TENANT) as conn:
            await conn.executemany(
                """
                INSERT INTO chunks (tenant_id, document_version_id, section_path,
                                    char_start, char_end, text, embedding)
                VALUES ($1, $2, $3, $4, $5, $6, $7::vector)
                """,
                rows,
            )
        inserted += n
        if inserted % 20_000 == 0:
            rate = inserted / (time.monotonic() - t0)
            print(f"  pgvector: {inserted}/{args.chunks} ({rate:.0f} rows/s)")
    pg_seconds = time.monotonic() - t0
    print(
        f"pgvector+HNSW insert: {args.chunks} rows in {pg_seconds:.0f}s "
        f"({args.chunks / pg_seconds:.0f} rows/s)"
    )

    # 4. OpenSearch bulk at scale.
    await lexical.ensure_index()
    t0 = time.monotonic()
    for start in range(0, args.chunks, 5_000):
        actions = [
            {
                "_op_type": "index",
                "_index": "evidence-chunks-loadtest",
                "_id": f"lt-{i}",
                "chunk_id": f"lt-{i}",
                "tenant_id": str(GLOBAL_TENANT),
                "document_id": str(doc_id),
                "document_version_id": str(version_id),
                "section_path": f"loadtest/{i}",
                "text": _text(rng),
                "language": "uk" if i % 2 else "en",
                "source_authority": "other",
                "evidence_tier": "other",
                "published_at": None,
                "retracted": False,
            }
            for i in range(start, min(start + 5_000, args.chunks))
        ]
        await helpers.async_bulk(lexical._client, actions)  # noqa: SLF001 — loadtest
    os_seconds = time.monotonic() - t0
    count = await lexical._client.count(index="evidence-chunks-loadtest")  # noqa: SLF001
    print(
        f"opensearch bulk: {count['count']} docs in {os_seconds:.0f}s "
        f"({args.chunks / os_seconds:.0f} docs/s)"
    )

    # 5. Query sanity at scale.
    t0 = time.monotonic()
    async with tenant_connection(pool, GLOBAL_TENANT) as conn:
        probe = _vector(rng)
        rows = await conn.fetch(
            "SELECT id FROM chunks WHERE document_version_id = $1"
            " ORDER BY embedding <=> $2::vector LIMIT 10",
            version_id,
            probe,
        )
    print(
        f"pgvector ANN top-10 over {args.chunks}: {(time.monotonic() - t0) * 1000:.0f} ms, "
        f"{len(rows)} rows"
    )
    t0 = time.monotonic()
    result = await lexical._client.search(  # noqa: SLF001
        index="evidence-chunks-loadtest",
        body={"query": {"match": {"text": "дозування антикоагулянт"}}, "size": 10},
    )
    print(
        f"opensearch BM25 top-10: {(time.monotonic() - t0) * 1000:.0f} ms, "
        f"{result['hits']['total']['value']} total hits"
    )

    if not args.keep:
        async with tenant_connection(pool, GLOBAL_TENANT) as conn:
            await conn.execute("DELETE FROM chunks WHERE document_version_id = $1", version_id)
            await conn.execute("DELETE FROM document_versions WHERE id = $1", version_id)
            await conn.execute("DELETE FROM documents WHERE id = $1", doc_id)
        await lexical._client.indices.delete(index="evidence-chunks-loadtest")  # noqa: SLF001
        print("cleanup: loadtest rows + index removed")

    await gateway.aclose()
    await lexical.aclose()
    await pool.close()
    print(f"note: synthetic run id doc={doc_id} (marker uuid {uuid.uuid4().hex[:8]})")


if __name__ == "__main__":
    asyncio.run(main())
