"""Erasers — the destruction side of the fan-out map (executed by the
step-07 engine under the ``mdx_erasure`` role).

Every eraser consumes its Artifact's ``ids_sql`` (the map's join is THE
only patient-enumeration SQL) and returns honest accounting:
``(destroyed, retained)`` item lists that feed
``patient_privacy_requests.report_of_execution``.

Ordering contract (the engine runs ``erase_patient`` which enforces it):

1. blob-backed artifacts first (object deleted BEFORE its row — a crash
   between leaves a row pointing at nothing, the safe direction; both
   deletions tolerate absence so re-runs are idempotent);
2. leaves before parents (transcription_jobs/dictation_sessions before
   audio_files; synthesis jobs + versions before reports — the
   reports_current_version_fk circularity is handled by deferring it);
3. the patient identity OVERWRITE last, after everything destroyable is
   gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

import asyncpg

from .fanout import (
    BASIS_CLINICAL_RECORD_SIGNED,
    BASIS_CONSENT_RECORD,
    BASIS_ERASURE_PAPER_TRAIL,
    BASIS_QUALIFIED_SIGNATURE,
    FANOUT,
    Artifact,
)


class ObjectStore(Protocol):
    """The slice of storage.EncryptedObjectStore erasers need."""

    async def delete(self, *, key: str) -> None: ...


@dataclass
class EraserContext:
    """Wiring the step-07 engine provides."""

    audio_store: ObjectStore | None = None
    transcript_store: ObjectStore | None = None
    # 0065 — patient record attachments (mdx-patient-docs).
    document_store: ObjectStore | None = None
    report_retention_years: int = 25
    # Deletes a signed-PDF object by its full storage URI (bucket varies) —
    # used only for out-of-retention-window envelopes. None in tests that
    # don't exercise that path.
    pdf_object_deleter: Any | None = None


@dataclass(frozen=True)
class DestroyedItem:
    kind: str
    id: UUID
    detail: str


@dataclass(frozen=True)
class RetainedItem:
    kind: str
    id: UUID
    legal_basis: str


Outcome = tuple[list[DestroyedItem], list[RetainedItem]]

_BY_KIND: dict[str, Artifact] = {a.kind: a for a in FANOUT}

# Reports (and their versions) inside the retention window whose lifecycle
# reached a qualified signature are retained. One predicate, used by the
# whole report family.
_RETAINED_REPORTS_SQL = """
    SELECT r.id FROM reports r
    WHERE r.patient_id = $1
      AND r.status IN ('signed', 'amended')
      AND r.signed_at IS NOT NULL
      AND r.signed_at > $2
"""


def _retention_cutoff(ctx: EraserContext) -> datetime:
    return datetime.now(UTC) - timedelta(days=365 * ctx.report_retention_years)


def _object_key(storage_uri: str) -> str:
    """`minio://bucket/tenant/id.enc` → `tenant/id.enc`."""
    without_scheme = storage_uri.split("://", 1)[1]
    return without_scheme.split("/", 1)[1]


async def _delete_rows(
    conn: asyncpg.Connection, kind: str, sql: str, *args: Any
) -> list[DestroyedItem]:
    rows = await conn.fetch(sql, *args)
    return [DestroyedItem(kind=kind, id=r["id"], detail="row deleted") for r in rows]


# ── Blob-backed (CRYPTO_SHRED) ──────────────────────────────────────


async def _shred_blob_artifact(
    conn: asyncpg.Connection,
    artifact: Artifact,
    patient_id: UUID,
    store: ObjectStore | None,
) -> Outcome:
    rows = await conn.fetch(
        f"SELECT id, {artifact.object_uri_column} AS uri FROM {artifact.table} "
        f"WHERE id IN ({artifact.ids_sql})",
        patient_id,
    )
    destroyed: list[DestroyedItem] = []
    for row in rows:
        detail = "row deleted"
        if row["uri"]:
            if store is None:
                raise RuntimeError(
                    f"{artifact.kind} has object {row['uri']} but no object "
                    "store is wired — refusing a partial crypto-shred"
                )
            # Object first: its per-object DEK (header + row metadata copy)
            # dies with it. delete() tolerates absent keys → idempotent.
            await store.delete(key=_object_key(row["uri"]))
            detail = "object crypto-shredded + row deleted"
        await conn.execute(
            f"DELETE FROM {artifact.table} WHERE id = $1", row["id"]
        )
        destroyed.append(DestroyedItem(kind=artifact.kind, id=row["id"], detail=detail))
    return destroyed, []


async def erase_recordings(
    conn: asyncpg.Connection, patient_id: UUID, ctx: EraserContext
) -> Outcome:
    return await _shred_blob_artifact(
        conn, _BY_KIND["recording"], patient_id, ctx.audio_store
    )


async def erase_transcription_jobs(
    conn: asyncpg.Connection, patient_id: UUID, ctx: EraserContext
) -> Outcome:
    return await _shred_blob_artifact(
        conn, _BY_KIND["transcription_job"], patient_id, ctx.transcript_store
    )


async def erase_patient_documents(
    conn: asyncpg.Connection, patient_id: UUID, ctx: EraserContext
) -> Outcome:
    """Attachments carry no independent retention basis: a referral letter
    is not a signed clinical record, so Art. 17 takes it whole."""
    return await _shred_blob_artifact(
        conn, _BY_KIND["patient_document"], patient_id, ctx.document_store
    )


# ── Plain HARD_DELETE ───────────────────────────────────────────────


async def erase_dictation_sessions(
    conn: asyncpg.Connection, patient_id: UUID, ctx: EraserContext
) -> Outcome:
    a = _BY_KIND["dictation_session"]
    return await _delete_rows(
        conn,
        a.kind,
        f"DELETE FROM {a.table} WHERE id IN ({a.ids_sql}) RETURNING id",
        patient_id,
    ), []


async def erase_signing_sessions(
    conn: asyncpg.Connection, patient_id: UUID, ctx: EraserContext
) -> Outcome:
    a = _BY_KIND["signing_session"]
    return await _delete_rows(
        conn,
        a.kind,
        f"DELETE FROM {a.table} WHERE id IN ({a.ids_sql}) RETURNING id",
        patient_id,
    ), []


async def erase_clinical_notes(
    conn: asyncpg.Connection, patient_id: UUID, ctx: EraserContext
) -> Outcome:
    return await _delete_rows(
        conn,
        "clinical_note",
        "DELETE FROM clinical_notes WHERE patient_id = $1 RETURNING id",
        patient_id,
    ), []


async def erase_anamnesis(
    conn: asyncpg.Connection, patient_id: UUID, ctx: EraserContext
) -> Outcome:
    return await _delete_rows(
        conn,
        "anamnesis",
        "DELETE FROM patient_anamnesis WHERE patient_id = $1 RETURNING patient_id AS id",
        patient_id,
    ), []


async def erase_encounters(
    conn: asyncpg.Connection, patient_id: UUID, ctx: EraserContext
) -> Outcome:
    # Runs after recordings/notes/consents are gone — the audio_files FK
    # (ON DELETE RESTRICT) guarantees the order was respected.
    return await _delete_rows(
        conn,
        "encounter",
        "DELETE FROM encounters WHERE patient_id = $1 RETURNING id",
        patient_id,
    ), []


# ── RETAIN_IF_SIGNED ────────────────────────────────────────────────


async def retain_consents(
    conn: asyncpg.Connection, patient_id: UUID, ctx: EraserContext
) -> Outcome:
    """Consents are ALWAYS retained (S11 step 07): the lawful-basis proof
    must survive its subject's erasure — surfaced, never hidden."""
    rows = await conn.fetch(
        "SELECT id FROM patient_consents WHERE patient_id = $1", patient_id
    )
    return [], [
        RetainedItem(kind="consent", id=r["id"], legal_basis=BASIS_CONSENT_RECORD)
        for r in rows
    ]


async def erase_report_family(
    conn: asyncpg.Connection, patient_id: UUID, ctx: EraserContext
) -> Outcome:
    """Synthesis jobs, versions, reports — one eraser because the family
    shares the retention verdict and the current_version_id circularity
    (deferrable FK; versions delete first, reports second, the deferred
    check passes at commit)."""
    cutoff = _retention_cutoff(ctx)
    destroyed: list[DestroyedItem] = []
    retained: list[RetainedItem] = []

    # Synthesis jobs are operational — destroyed for ALL of the patient's
    # reports, retained or not (they may embed transcript inputs).
    destroyed += await _delete_rows(
        conn,
        "synthesis_job",
        "DELETE FROM report_synthesis_jobs WHERE report_id IN "
        "(SELECT id FROM reports WHERE patient_id = $1) RETURNING id",
        patient_id,
    )

    # An OUT-of-retention-window signed report is destroyed together with
    # its envelope (row + stored PDF object) — the retention boundary cuts
    # the whole record, not just the content (S11 step 07 §8).
    doomed_envelopes = await conn.fetch(
        f"""
        SELECT id, pdf_storage_uri FROM signed_envelopes
        WHERE resource_type IN ('report', 'amendment')
          AND resource_id IN (SELECT id FROM reports WHERE patient_id = $1)
          AND resource_id NOT IN ({_RETAINED_REPORTS_SQL})
        """,
        patient_id,
        cutoff,
    )
    for env in doomed_envelopes:
        if env["pdf_storage_uri"] and ctx.pdf_object_deleter is not None:
            await ctx.pdf_object_deleter(env["pdf_storage_uri"])
        await conn.execute("DELETE FROM signed_envelopes WHERE id = $1", env["id"])
        destroyed.append(
            DestroyedItem(
                kind="signed_envelope", id=env["id"],
                detail="out-of-retention-window envelope destroyed with its report",
            )
        )

    await conn.execute("SET CONSTRAINTS reports_current_version_fk DEFERRED")
    destroyed += await _delete_rows(
        conn,
        "report_version",
        f"DELETE FROM report_versions WHERE report_id IN "
        f"(SELECT id FROM reports WHERE patient_id = $1) "
        f"AND report_id NOT IN ({_RETAINED_REPORTS_SQL}) RETURNING id",
        patient_id,
        cutoff,
    )
    destroyed += await _delete_rows(
        conn,
        "report",
        f"DELETE FROM reports WHERE patient_id = $1 "
        f"AND id NOT IN ({_RETAINED_REPORTS_SQL}) RETURNING id",
        patient_id,
        cutoff,
    )

    for r in await conn.fetch(_RETAINED_REPORTS_SQL, patient_id, cutoff):
        retained.append(
            RetainedItem(kind="report", id=r["id"], legal_basis=BASIS_CLINICAL_RECORD_SIGNED)
        )
    return destroyed, retained


# ── NEVER / OVERWRITE ───────────────────────────────────────────────


async def retain_signed_envelopes(
    conn: asyncpg.Connection, patient_id: UUID, ctx: EraserContext
) -> Outcome:
    a = _BY_KIND["signed_envelope"]
    rows = await conn.fetch(a.ids_sql, patient_id)
    return [], [
        RetainedItem(kind=a.kind, id=r["id"], legal_basis=BASIS_QUALIFIED_SIGNATURE)
        for r in rows
    ]


async def retain_privacy_requests(
    conn: asyncpg.Connection, patient_id: UUID, ctx: EraserContext
) -> Outcome:
    a = _BY_KIND["privacy_request"]
    rows = await conn.fetch(a.ids_sql, patient_id)
    return [], [
        RetainedItem(kind=a.kind, id=r["id"], legal_basis=BASIS_ERASURE_PAPER_TRAIL)
        for r in rows
    ]


async def overwrite_patient_identity(
    conn: asyncpg.Connection, patient_id: UUID, ctx: EraserContext
) -> Outcome:
    """The tombstone write (ADR-0027): identity destroyed in place —
    names, DOB, MRN, contact details (phone/e-mail/address, 0060), tags,
    summaries, and ALL ІПН columns (hmac included: total identity
    destruction, DPO branch recorded in the ADR)."""
    result = await conn.execute(
        """
        UPDATE patients
        SET name_uk = 'ERASED', name_en = 'ERASED', dob = NULL, sex = 'U',
            mrn = '', summary_uk = '', summary_en = '', tags = '{}',
            phone = '', email = '',
            address_street = '', address_house = '', address_zip = '',
            address_city = '', address_country = '',
            ipn_hmac = NULL, ipn_encrypted = NULL, ipn_dek = NULL,
            status = 'erased', erased_at = now(), updated_at = now()
        WHERE id = $1 AND status <> 'erased'
        """,
        patient_id,
    )
    if result == "UPDATE 0":
        return [], []  # already a tombstone — idempotent re-run
    return [
        DestroyedItem(kind="patient", id=patient_id, detail="identity overwritten (tombstone)")
    ], []


# The engine's execution order (leaves → parents → identity last).
ERASERS_IN_ORDER: tuple = (
    erase_patient_documents,
    erase_transcription_jobs,
    erase_dictation_sessions,
    erase_recordings,
    erase_signing_sessions,
    erase_report_family,
    erase_clinical_notes,
    retain_consents,
    erase_anamnesis,
    retain_signed_envelopes,
    retain_privacy_requests,
    erase_encounters,
    overwrite_patient_identity,
)
