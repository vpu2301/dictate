"""DB access for the answer pipeline. Every function takes an RLS-scoped
connection (from `tenant_connection`) positionally; everything else
keyword-only (platform repository convention).

`questions` and `answers` are dual-key RLS (tenant AND owning user), so the
connection must also carry `app.user_id` — see `deps.user_scoped_connection`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import asyncpg

from evidence_models import AnswerProvenance, Segment, StageTrace


@dataclass(frozen=True, slots=True)
class QuestionRow:
    id: UUID
    text: str
    mode: str
    locale: str
    created_at: datetime
    answer_id: UUID | None
    answer_status: str | None


async def insert_question(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    user_sub: UUID,
    text: str,
    mode: str,
    locale: str,
) -> UUID:
    return await conn.fetchval(  # type: ignore[no-any-return]
        """
        INSERT INTO questions (tenant_id, user_sub, text, mode, locale)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        tenant_id,
        user_sub,
        text,
        mode,
        locale,
    )


async def insert_answer(
    conn: asyncpg.Connection,
    *,
    answer_id: UUID,
    tenant_id: UUID,
    user_sub: UUID,
    question_id: UUID,
    status: str,
    mode: str,
    envelope_ref: str,
) -> UUID:
    return await conn.fetchval(  # type: ignore[no-any-return]
        """
        INSERT INTO answers
            (id, tenant_id, user_sub, question_id, status, mode, verified, envelope_ref)
        VALUES ($1, $2, $3, $4, $5, $6, false, $7)
        RETURNING id
        """,
        answer_id,
        tenant_id,
        user_sub,
        question_id,
        status,
        mode,
        envelope_ref,
    )


async def insert_segments(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    answer_id: UUID,
    summary: list[Segment],
    detail: list[Segment],
) -> None:
    """Summary first, then detail — `ord` is the render order of the whole
    answer, which is how a reader reconstructs it without a second column."""
    rows = [
        (
            tenant_id,
            answer_id,
            ordinal,
            segment.kind.value,
            segment.text,
            segment.strength.value if segment.strength else None,
            json.dumps([c.model_dump(mode="json") for c in segment.citations]),
            json.dumps(segment.patient_fact_refs),
        )
        for ordinal, segment in enumerate([*summary, *detail])
    ]
    if not rows:
        return
    await conn.executemany(
        """
        INSERT INTO answer_segments
            (tenant_id, answer_id, ord, kind, text, strength, citations, patient_fact_refs)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb)
        """,
        rows,
    )


async def insert_provenance(
    conn: asyncpg.Connection, *, tenant_id: UUID, provenance: AnswerProvenance
) -> None:
    """Append-only (DB trigger enforces it). A failed write aborts the answer:
    audit and provenance are never best-effort (spec §11)."""
    await conn.execute(
        """
        INSERT INTO answer_provenance
            (tenant_id, answer_id, question_ref, snapshot_hash, consumed_fields,
             passage_ids, web_refs, connectors, corpus_snapshot_id, model_pins,
             prompt_versions, pipeline_version, build_version)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb, $9,
                $10::jsonb, $11::jsonb, $12, $13)
        """,
        tenant_id,
        provenance.answer_id,
        provenance.question_ref,
        provenance.snapshot_hash,
        json.dumps(provenance.consumed_fields),
        json.dumps(provenance.passage_ids),
        json.dumps([ref.model_dump(mode="json") for ref in provenance.web_refs]),
        json.dumps(provenance.connectors),
        provenance.corpus_snapshot_id,
        json.dumps(provenance.model_pins),
        json.dumps(provenance.prompt_versions),
        provenance.pipeline_version,
        provenance.build_version,
    )


async def insert_traces(
    conn: asyncpg.Connection, *, tenant_id: UUID, answer_id: UUID, traces: list[StageTrace]
) -> None:
    if not traces:
        return
    await conn.executemany(
        """
        INSERT INTO answer_traces
            (tenant_id, answer_id, stage, started_at, ended_at, outcome, meta)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
        """,
        [
            (
                tenant_id,
                answer_id,
                trace.stage,
                trace.started_at,
                trace.ended_at,
                trace.outcome.value,
                json.dumps(trace.meta),
            )
            for trace in traces
        ],
    )


async def get_answer_envelope_ref(
    conn: asyncpg.Connection, *, tenant_id: UUID, answer_id: UUID
) -> str | None:
    return await conn.fetchval(
        "SELECT envelope_ref FROM answers WHERE tenant_id = $1 AND id = $2",
        tenant_id,
        answer_id,
    )


async def list_questions(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    limit: int,
    before: datetime | None,
) -> list[QuestionRow]:
    """Cursor page, newest first. The cursor is the created_at of the last row
    seen — stable under concurrent inserts in a way OFFSET is not."""
    rows = await conn.fetch(
        """
        SELECT q.id, q.text, q.mode, q.locale, q.created_at,
               a.id AS answer_id, a.status AS answer_status
        FROM questions q
        LEFT JOIN LATERAL (
            SELECT id, status FROM answers
            WHERE answers.question_id = q.id
            ORDER BY created_at DESC
            LIMIT 1
        ) a ON true
        WHERE q.tenant_id = $1 AND ($2::timestamptz IS NULL OR q.created_at < $2)
        ORDER BY q.created_at DESC
        LIMIT $3
        """,
        tenant_id,
        before,
        limit,
    )
    return [
        QuestionRow(
            id=row["id"],
            text=row["text"],
            mode=row["mode"],
            locale=row["locale"],
            created_at=row["created_at"],
            answer_id=row["answer_id"],
            answer_status=row["answer_status"],
        )
        for row in rows
    ]


async def popular_questions(conn: asyncpg.Connection, *, tenant_id: UUID, limit: int) -> list[str]:
    """Most-asked questions in this tenant, for the suggestions surface.

    RLS keeps this to the caller's own history — `questions` is user-private,
    so "popular" here means "popular with you". A tenant-wide version would
    need a separate aggregate written by a job under a different role, which
    is S12 work, not a quiet policy relaxation.
    """
    rows = await conn.fetch(
        """
        SELECT text, count(*) AS uses
        FROM questions
        WHERE tenant_id = $1 AND created_at > now() - interval '90 days'
        GROUP BY text
        ORDER BY uses DESC, max(created_at) DESC
        LIMIT $2
        """,
        tenant_id,
        limit,
    )
    return [str(row["text"]) for row in rows]
