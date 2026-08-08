"""Answer persistence: envelope object, rows, provenance, traces, audit.

Ordering is deliberate and is the sprint's "audit is not best-effort" rule
(§11) made concrete:

    envelope object → answers row → segments → provenance → traces → audit

The row-writing steps share one RLS-scoped transaction, so a failure anywhere
in them leaves no half-answer. The audit write is last and, if it fails, the
transaction has already committed — so the answer is *deleted* rather than
left un-audited. An answer nobody can account for is worse than no answer.
"""

from __future__ import annotations

import logging
from uuid import UUID

import asyncpg
from audit import AuditWriter, Severity
from storage import EncryptedObjectStore

from evidence_answer import audit_kinds
from evidence_answer.adapters import pg
from evidence_models import (
    AnswerEnvelope,
    AnswerProvenance,
    AnswerStatus,
    StageTrace,
)

logger = logging.getLogger(__name__)


def envelope_key(tenant_id: UUID, answer_id: UUID) -> str:
    return f"answers/{tenant_id}/{answer_id}.json"


class AnswerPersister:
    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        store: EncryptedObjectStore,
        audit_writer: AuditWriter,
    ) -> None:
        self._pool = pool
        self._store = store
        self._audit = audit_writer

    async def load_envelope(self, *, tenant_id: UUID, key: str) -> AnswerEnvelope:
        raw = await self._store.get(key=key, tenant_id=tenant_id)
        return AnswerEnvelope.model_validate_json(raw)

    async def persist(
        self,
        *,
        conn_factory: object,
        tenant_id: UUID,
        user_sub: UUID,
        actor_role: str | None,
        question_id: UUID,
        envelope: AnswerEnvelope,
        provenance: AnswerProvenance,
        traces: list[StageTrace],
        connectors: list[str],
    ) -> str:
        """Persist everything. Returns the envelope object key.

        `conn_factory` is an async context manager factory producing a
        connection scoped to BOTH `app.tenant_id` and `app.user_id` — the
        dual-key RLS on `questions`/`answers` rejects the insert otherwise.
        """
        key = envelope_key(tenant_id, envelope.answer_id)
        # The envelope may echo the question, which may carry incidental PHI
        # (spec §7) — so it is an encrypted object, not a JSONB column.
        await self._store.put(
            key=key,
            plaintext=envelope.model_dump_json().encode("utf-8"),
            tenant_id=tenant_id,
        )

        async with conn_factory() as conn:  # type: ignore[operator]
            await pg.insert_answer(
                conn,
                answer_id=envelope.answer_id,
                tenant_id=tenant_id,
                user_sub=user_sub,
                question_id=question_id,
                status=envelope.status.value,
                mode="quick_search",
                envelope_ref=key,
            )
            await pg.insert_segments(
                conn,
                tenant_id=tenant_id,
                answer_id=envelope.answer_id,
                summary=envelope.summary_segments,
                detail=envelope.detail_segments,
            )
            await pg.insert_provenance(conn, tenant_id=tenant_id, provenance=provenance)
            await pg.insert_traces(
                conn, tenant_id=tenant_id, answer_id=envelope.answer_id, traces=traces
            )

        try:
            await self._audit.write_event(
                tenant_id=tenant_id,
                kind=audit_kinds.ANSWER_GENERATED,
                actor_sub=user_sub,
                actor_role=actor_role,
                target_kind="evidence",
                target_id=str(envelope.answer_id),
                payload={
                    "answer_id": str(envelope.answer_id),
                    "mode": "quick_search",
                    "status": envelope.status.value,
                    "connectors": ",".join(connectors),
                    "verified": "false",
                },
                severity=Severity.INFO,
            )
        except Exception:
            # Audit is not best-effort: an answer that could not be recorded
            # must not exist. The envelope object is left behind deliberately
            # (an orphan blob is inert; the row is what makes it reachable).
            logger.exception("persist.audit_failed_rolling_back_answer")
            async with conn_factory() as conn:  # type: ignore[operator]
                await conn.execute(
                    "DELETE FROM answers WHERE tenant_id = $1 AND id = $2",
                    tenant_id,
                    envelope.answer_id,
                )
            raise
        return key

    async def audit_deflection(
        self,
        *,
        tenant_id: UUID,
        user_sub: UUID,
        actor_role: str | None,
        question_id: UUID,
        reason_code: str,
        matched_rule: str | None,
        classifier_used: bool,
    ) -> None:
        await self._audit.write_event(
            tenant_id=tenant_id,
            kind=audit_kinds.QUESTION_DEFLECTED,
            actor_sub=user_sub,
            actor_role=actor_role,
            target_kind="evidence",
            target_id=str(question_id),
            payload={
                # The question text itself is NOT in the payload: audit
                # payloads are widely readable and a deflected question is
                # the most likely one to carry something sensitive.
                "reason_code": reason_code,
                "matched_rule": matched_rule or "",
                "classifier_used": str(classifier_used).lower(),
            },
            severity=Severity.INFO,
        )


def status_for(summary_count: int, detail_count: int) -> AnswerStatus:
    return AnswerStatus.ok if (summary_count + detail_count) else AnswerStatus.insufficient_basis
