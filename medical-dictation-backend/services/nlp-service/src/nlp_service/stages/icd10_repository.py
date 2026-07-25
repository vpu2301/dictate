"""ICD-10 lookup for the sprint-13 extractor (step 05 consumes this).

The extractor must NOT call report-service over HTTP mid-pipeline
(determinism + latency); nlp-service has its own pool, and
``icd10_codes`` is a global reference table with no tenant dimension
(no RLS), so a plain pool connection is correct.

The ranking SQL is imported verbatim from ``db.ICD10_SEARCH_SQL`` — the
same constant the report-service picker endpoint uses, so a proposal
the extractor surfaces and the code the clinician sees in the picker
can never be ranked differently.

Determinism: reads are deterministic for a frozen table. Reloading the
ICD-10 table is a pipeline-affecting event of the same class as a stage
change (documented in docs/runbooks/icd10.md); replay fixtures pin the
committed fixture table rather than whatever is loaded in prod.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Final

import asyncpg

from db import ICD10_SEARCH_SQL

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Icd10Hit:
    code: str
    display_uk: str
    display_en: str
    is_leaf: bool


DEFAULT_TIMEOUT_SECONDS: Final = 0.05


async def search_icd10(
    pool: asyncpg.Pool,
    *,
    query: str,
    limit: int = 5,
    leaves_only: bool = True,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[Icd10Hit]:
    """Ranked ICD-10 matches for ``query``.

    ``leaves_only`` (the extractor default) drops headings: a heading is
    not a codeable diagnosis, so proposing one would be a clinical and
    billing error. Returns an empty list — never raises — if the table
    is absent (dev hosts without migrations); the extractor then simply
    proposes nothing, which is the correct fail-safe.
    """
    query = query.strip()
    if not query:
        return []
    try:
        async with asyncio.timeout(timeout_seconds), pool.acquire() as conn:
            rows = await conn.fetch(ICD10_SEARCH_SQL, query, limit)
    except asyncpg.UndefinedTableError:
        logger.warning("icd10.table_missing")
        return []
    except TimeoutError:
        # Fail-EMPTY: the extractor proposes nothing and the pipeline
        # completes. Step 03 measured p95 ≈ 1.7 ms at full-table scale,
        # so a timeout here means the DB is unwell, not that the budget
        # is tight.
        logger.warning("icd10.lookup_timeout", extra={"timeout_s": timeout_seconds})
        return []

    hits = [
        Icd10Hit(
            code=row["code"],
            display_uk=row["display_uk"],
            display_en=row["display_en"],
            is_leaf=row["is_leaf"],
        )
        for row in rows
    ]
    return [h for h in hits if h.is_leaf] if leaves_only else hits
