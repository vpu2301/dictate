"""The fan-out map — every place patient data lives, as data not code.

One `Artifact` per artifact class. Each entry's ``ids_sql`` is THE join
from ``patients.id`` to that artifact's rows — the DSAR exporter SELECTs
along it, the erasure engine DELETEs along it, and no patient-enumeration
SQL exists anywhere else (grep-checked in the step-05 PR).

The map is CI-guarded: ``scripts/ci/check_erasure_fanout_coverage.py``
computes the FK closure from ``patients`` on the live schema and fails
the build when a table in the closure is neither registered here nor in
``KNOWN_NON_PHI``. Two PHI carriers are linked only softly (by
``resource_id``, no FK) and are asserted by name in the gate —
``SOFT_LINKED_PHI`` — because that is exactly the class of edge an FK
scan misses.

Inventory notes (verified against the live dev schema, 2026-07-15):

- Dictation transcripts persist in ``dictation_sessions.transcript_jsonb``
  (S04 finalize); batch ASR transcripts are MinIO objects referenced by
  ``transcription_jobs.result_storage_uri``. Both are registered.
- ``audio_files`` rows reach a patient only via ``encounter_id`` (the
  step-02 FK). Ad-hoc recordings with a NULL encounter are not
  patient-linked *by construction* — the report-side patient requirement
  (0033) is the gate that ties their output to a person at finalize.
- ``signed_envelopes`` are RETAINED (qualified-signature evidence, Law
  2155-VIII); ``signing_sessions`` are transient operational rows whose
  ``canonical_json`` carries patient names → hard-deleted.
- ``patient_privacy_requests`` is the erasure's own paper trail → NEVER
  erased; it survives because the patient row itself survives as an
  ``erased`` tombstone (identity overwritten per ADR-0027).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import asyncpg


class Erasability(StrEnum):
    HARD_DELETE = "hard_delete"          # DELETE the rows
    CRYPTO_SHRED = "crypto_shred"        # delete MinIO object (its DEK dies) + rows
    OVERWRITE = "overwrite"              # identity overwrite; row survives as tombstone
    RETAIN_IF_SIGNED = "retain_if_signed"  # signed → retained w/ basis; else hard-delete
    NEVER = "never"                      # legal record; always retained w/ basis


# Machine-stable legal-basis identifiers (human text in
# docs/architecture/erasure.md).
BASIS_CLINICAL_RECORD_SIGNED = "retention:clinical_record_signed"
BASIS_CONSENT_RECORD = "retention:consent_record"
BASIS_QUALIFIED_SIGNATURE = "retention:qualified_signature"
BASIS_ERASURE_PAPER_TRAIL = "retention:erasure_paper_trail"


@dataclass(frozen=True)
class ExportItem:
    kind: str
    id: UUID
    created_at: datetime | None
    payload: dict[str, Any] | None = None   # row content (sanitized)
    object_ref: str | None = None           # ciphertext object URI, if blob-backed


@dataclass(frozen=True)
class Artifact:
    kind: str
    table: str
    # THE join from patients.id ($1) to this artifact's ids. Used verbatim
    # by exporters (step 06) and erasers (step 07).
    ids_sql: str
    # Full export query: ($1 = patient_id) → id, created_at, payload jsonb,
    # object_ref. NULL payload/object_ref where not applicable.
    export_sql: str
    exportable: bool
    erasability: Erasability
    retention_basis: str | None = None
    notes: str = ""
    # Blob-backed artifacts: which object store the eraser needs.
    object_store: str | None = None  # "audio" | "transcripts" | None
    object_uri_column: str | None = None


def _sanitize(*cols: str) -> str:
    """to_jsonb(x) minus internal/PII-token columns."""
    strip = ("tenant_id", "search_vector", *cols)
    return "to_jsonb(x)" + "".join(f" - '{c}'" for c in strip)


FANOUT: tuple[Artifact, ...] = (
    Artifact(
        kind="patient",
        table="patients",
        ids_sql="SELECT id FROM patients WHERE id = $1",
        export_sql=f"""
            SELECT id, created_at,
                   ({_sanitize("ipn_hmac", "ipn_encrypted", "ipn_dek")})
                       || jsonb_build_object('has_ipn', x.ipn_hmac IS NOT NULL)
                       AS payload,
                   NULL::text AS object_ref
            FROM patients x WHERE id = $1
        """,
        exportable=True,
        erasability=Erasability.OVERWRITE,
        notes="Identity overwrite (names→'ERASED', ІПН columns NULLed, "
              "status='erased' + erased_at); the tombstone row survives so "
              "the paper trail keeps a referent. ADR-0027.",
    ),
    Artifact(
        kind="encounter",
        table="encounters",
        ids_sql="SELECT id FROM encounters WHERE patient_id = $1",
        export_sql=f"""
            SELECT id, created_at, {_sanitize()} AS payload, NULL::text AS object_ref
            FROM encounters x WHERE patient_id = $1
        """,
        exportable=True,
        erasability=Erasability.HARD_DELETE,
    ),
    Artifact(
        kind="clinical_note",
        table="clinical_notes",
        ids_sql="SELECT id FROM clinical_notes WHERE patient_id = $1",
        export_sql=f"""
            SELECT id, created_at, {_sanitize()} AS payload, NULL::text AS object_ref
            FROM clinical_notes x WHERE patient_id = $1
        """,
        exportable=True,
        erasability=Erasability.HARD_DELETE,
    ),
    Artifact(
        kind="anamnesis",
        table="patient_anamnesis",
        ids_sql="SELECT patient_id AS id FROM patient_anamnesis WHERE patient_id = $1",
        export_sql=f"""
            SELECT patient_id AS id, updated_at AS created_at,
                   {_sanitize()} AS payload, NULL::text AS object_ref
            FROM patient_anamnesis x WHERE patient_id = $1
        """,
        exportable=True,
        erasability=Erasability.HARD_DELETE,
    ),
    Artifact(
        kind="consent",
        table="patient_consents",
        ids_sql="SELECT id FROM patient_consents WHERE patient_id = $1",
        export_sql=f"""
            SELECT id, granted_at AS created_at,
                   {_sanitize("canonical_hash")} AS payload, NULL::text AS object_ref
            FROM patient_consents x WHERE patient_id = $1
        """,
        exportable=True,
        erasability=Erasability.NEVER,
        retention_basis=BASIS_CONSENT_RECORD,
        notes="Consents are ALWAYS retained (S11 step 07): the lawful-basis "
              "proof must survive its subject's erasure — surfaced in "
              "retained[], never hidden. They reference only the tombstone "
              "patient row.",
    ),
    Artifact(
        kind="privacy_request",
        table="patient_privacy_requests",
        ids_sql="SELECT id FROM patient_privacy_requests WHERE patient_id = $1",
        export_sql=f"""
            SELECT id, requested_at AS created_at,
                   {_sanitize()} AS payload, NULL::text AS object_ref
            FROM patient_privacy_requests x WHERE patient_id = $1
        """,
        exportable=True,
        erasability=Erasability.NEVER,
        retention_basis=BASIS_ERASURE_PAPER_TRAIL,
        notes="The record that erasure was requested/approved/executed — "
              "destroying it would destroy the proof of compliance.",
    ),
    Artifact(
        kind="report",
        table="reports",
        ids_sql="SELECT id FROM reports WHERE patient_id = $1",
        export_sql=f"""
            SELECT id, created_at, {_sanitize()} AS payload, NULL::text AS object_ref
            FROM reports x WHERE patient_id = $1
        """,
        exportable=True,
        erasability=Erasability.RETAIN_IF_SIGNED,
        retention_basis=BASIS_CLINICAL_RECORD_SIGNED,
        notes="Signed/amended reports inside REPORT_RETENTION_YEARS are "
              "retained (Ukrainian clinical-record rules — todo.md legal "
              "confirmation); drafts/finalized-unsigned/cancelled are "
              "hard-deleted with their versions.",
    ),
    Artifact(
        kind="report_version",
        table="report_versions",
        ids_sql="""
            SELECT v.id FROM report_versions v
            JOIN reports r ON r.id = v.report_id WHERE r.patient_id = $1
        """,
        export_sql=f"""
            SELECT x.id, x.created_at, {_sanitize("signed_data")} AS payload,
                   NULL::text AS object_ref
            FROM report_versions x
            JOIN reports r ON r.id = x.report_id WHERE r.patient_id = $1
        """,
        exportable=True,
        erasability=Erasability.RETAIN_IF_SIGNED,
        retention_basis=BASIS_CLINICAL_RECORD_SIGNED,
        notes="Follows its report's verdict.",
    ),
    Artifact(
        kind="synthesis_job",
        table="report_synthesis_jobs",
        ids_sql="""
            SELECT j.id FROM report_synthesis_jobs j
            JOIN reports r ON r.id = j.report_id WHERE r.patient_id = $1
        """,
        export_sql="""
            SELECT x.id, x.created_at, NULL::jsonb AS payload,
                   NULL::text AS object_ref
            FROM report_synthesis_jobs x
            JOIN reports r ON r.id = x.report_id WHERE r.patient_id = $1
        """,
        exportable=False,
        erasability=Erasability.HARD_DELETE,
        notes="Operational job rows; may embed transcript text in inputs — "
              "destroyed, never exported.",
    ),
    Artifact(
        kind="recording",
        table="audio_files",
        ids_sql="""
            SELECT a.id FROM audio_files a
            JOIN encounters e ON e.id = a.encounter_id WHERE e.patient_id = $1
        """,
        export_sql=f"""
            SELECT x.id, x.created_at,
                   {_sanitize("sha256", "envelope_metadata")} AS payload,
                   x.storage_uri AS object_ref
            FROM audio_files x
            JOIN encounters e ON e.id = x.encounter_id WHERE e.patient_id = $1
        """,
        exportable=True,
        erasability=Erasability.CRYPTO_SHRED,
        object_store="audio",
        object_uri_column="storage_uri",
        notes="MinIO object deleted FIRST (its per-object DEK dies with the "
              "header + the envelope_metadata row copy), then the row. A "
              "crash between leaves a row pointing at nothing — the safe "
              "direction; re-runs tolerate absence.",
    ),
    Artifact(
        kind="patient_document",
        table="patient_documents",
        ids_sql="SELECT id FROM patient_documents WHERE patient_id = $1",
        export_sql=f"""
            SELECT x.id, x.created_at,
                   {_sanitize("sha256", "envelope_metadata")} AS payload,
                   x.storage_uri AS object_ref
            FROM patient_documents x WHERE patient_id = $1
        """,
        exportable=True,
        erasability=Erasability.CRYPTO_SHRED,
        object_store="patient_docs",
        object_uri_column="storage_uri",
        notes="Migration 0065 — record attachments (referrals, lab PDFs, "
              "scans). MinIO object deleted FIRST (its per-object DEK dies "
              "with the header), then the row; re-runs tolerate absence.",
    ),
    Artifact(
        kind="transcription_job",
        table="transcription_jobs",
        ids_sql="""
            SELECT t.id FROM transcription_jobs t
            JOIN audio_files a ON a.id = t.audio_id
            JOIN encounters e ON e.id = a.encounter_id WHERE e.patient_id = $1
        """,
        export_sql=f"""
            SELECT x.id, x.queued_at AS created_at,
                   {_sanitize("metadata")} AS payload,
                   x.result_storage_uri AS object_ref
            FROM transcription_jobs x
            JOIN audio_files a ON a.id = x.audio_id
            JOIN encounters e ON e.id = a.encounter_id WHERE e.patient_id = $1
        """,
        exportable=True,
        erasability=Erasability.CRYPTO_SHRED,
        object_store="transcripts",
        object_uri_column="result_storage_uri",
        notes="Batch transcript object (mdx-transcripts) + job row.",
    ),
    Artifact(
        kind="dictation_session",
        table="dictation_sessions",
        ids_sql="""
            SELECT d.id FROM dictation_sessions d
            WHERE d.encounter_id IN (SELECT id FROM encounters WHERE patient_id = $1)
               OR d.audio_file_id IN (
                    SELECT a.id FROM audio_files a
                    JOIN encounters e ON e.id = a.encounter_id
                    WHERE e.patient_id = $1)
        """,
        export_sql=f"""
            SELECT x.id, x.created_at,
                   {_sanitize()} AS payload, NULL::text AS object_ref
            FROM dictation_sessions x
            WHERE x.encounter_id IN (SELECT id FROM encounters WHERE patient_id = $1)
               OR x.audio_file_id IN (
                    SELECT a.id FROM audio_files a
                    JOIN encounters e ON e.id = a.encounter_id
                    WHERE e.patient_id = $1)
        """,
        exportable=True,
        erasability=Erasability.HARD_DELETE,
        notes="transcript_jsonb IS the streaming transcript (S04 finalize) — "
              "PHI at rest in the row itself. Reached via encounter_id (bare "
              "UUID, no FK) OR audio_file_id (FK) — both joins used.",
    ),
    Artifact(
        kind="signed_envelope",
        table="signed_envelopes",
        ids_sql="""
            SELECT s.id FROM signed_envelopes s
            WHERE (s.resource_type IN ('report', 'amendment')
                   AND s.resource_id IN (SELECT id FROM reports WHERE patient_id = $1))
               OR (s.resource_type = 'consent'
                   AND s.resource_id IN (
                        SELECT id FROM patient_consents WHERE patient_id = $1))
               OR (s.resource_type = 'note'
                   AND s.resource_id IN (
                        SELECT id FROM clinical_notes WHERE patient_id = $1))
               OR (s.resource_type = 'anamnesis' AND s.resource_id = $1)
        """,
        export_sql="""
            SELECT x.id, x.created_at,
                   jsonb_build_object(
                       'resource_type', x.resource_type,
                       'resource_id', x.resource_id,
                       'signed_at', x.signed_at,
                       'signer_full_name', x.signer_full_name,
                       'signature_level', x.signature_level,
                       'is_qualified', x.is_qualified,
                       'verification_token', x.verification_token
                   ) AS payload,
                   NULL::text AS object_ref
            FROM signed_envelopes x
            WHERE (x.resource_type IN ('report', 'amendment')
                   AND x.resource_id IN (SELECT id FROM reports WHERE patient_id = $1))
               OR (x.resource_type = 'consent'
                   AND x.resource_id IN (
                        SELECT id FROM patient_consents WHERE patient_id = $1))
               OR (x.resource_type = 'note'
                   AND x.resource_id IN (
                        SELECT id FROM clinical_notes WHERE patient_id = $1))
               OR (x.resource_type = 'anamnesis' AND x.resource_id = $1)
        """,
        exportable=True,
        erasability=Erasability.NEVER,
        retention_basis=BASIS_QUALIFIED_SIGNATURE,
        notes="Qualified-signature evidence (Law 2155-VIII). Soft resource_id "
              "link — asserted by name in the CI gate, no FK to scan.",
    ),
    Artifact(
        kind="signing_session",
        table="signing_sessions",
        ids_sql="""
            SELECT s.id FROM signing_sessions s
            WHERE (s.resource_type IN ('report', 'amendment')
                   AND s.resource_id IN (SELECT id FROM reports WHERE patient_id = $1))
               OR (s.resource_type = 'consent'
                   AND s.resource_id IN (
                        SELECT id FROM patient_consents WHERE patient_id = $1))
        """,
        export_sql="""
            SELECT x.id, x.created_at, NULL::jsonb AS payload,
                   NULL::text AS object_ref
            FROM signing_sessions x
            WHERE (x.resource_type IN ('report', 'amendment')
                   AND x.resource_id IN (SELECT id FROM reports WHERE patient_id = $1))
               OR (x.resource_type = 'consent'
                   AND x.resource_id IN (
                        SELECT id FROM patient_consents WHERE patient_id = $1))
        """,
        exportable=False,
        erasability=Erasability.HARD_DELETE,
        notes="Transient operational rows whose canonical_json carries "
              "patient names — destroyed, never exported. Soft link like "
              "signed_envelopes.",
    ),
)


# Tables reachable in the FK closure (or holding patient-adjacent data)
# that are deliberately NOT patient artifacts. Every entry needs a
# justification — the CI gate prints it when explaining coverage.
KNOWN_NON_PHI: dict[str, str] = {
    # Closure sweeps via structural FKs that carry no patient data:
    "tenants": "structural parent — tenant metadata, no patient linkage",
    "users": "staff identity, not patient data (FK targets like created_by)",
    "medical_prompts": "system ASR prompt texts, patient-free",
    "report_templates": "system/tenant template definitions, patient-free",
    # Not in the closure but named here to pin the convention with tests
    # (test_non_phi_assertions.py):
    "autocomplete_telemetry": "scrubbed prefixes + ids only (S10 scrubber; "
                              "DPO sign-off docs/security/autocomplete-pii-scrubber.md)",
    "audit.events": "append-only audit chain; payload convention: ids only, "
                    "never identity strings (pinned by test)",
}

# PHI carriers linked only by resource_id (no FK) — the CI gate asserts
# these names are registered in FANOUT, because FK-scanning cannot see
# them. Removing an entry here or from FANOUT fails the gate.
SOFT_LINKED_PHI: frozenset[str] = frozenset({"signed_envelopes", "signing_sessions"})


# ── Shared enumeration entry point ──────────────────────────────────


Exporter = Callable[[asyncpg.Connection, UUID], Awaitable[list[ExportItem]]]


@dataclass
class FanoutInventory:
    """Per-artifact item lists — DSAR renders it, erasure destroys along
    it, tests fixture against it."""

    patient_id: UUID
    items: dict[str, list[ExportItem]] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        return {kind: len(v) for kind, v in self.items.items()}

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.items.values())


async def enumerate_patient(
    conn: asyncpg.Connection, patient_id: UUID
) -> FanoutInventory:
    """Run every artifact's export query and return the full inventory.

    The connection must be tenant-scoped (``db.tenant_connection``) —
    RLS bounds every query to the caller's tenant.
    """
    inventory = FanoutInventory(patient_id=patient_id)
    for artifact in FANOUT:
        rows = await conn.fetch(artifact.export_sql, patient_id)
        inventory.items[artifact.kind] = [
            ExportItem(
                kind=artifact.kind,
                id=row["id"],
                created_at=row["created_at"],
                payload=_payload_dict(row["payload"]),
                object_ref=row["object_ref"],
            )
            for row in rows
        ]
    return inventory


def _payload_dict(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    import json

    return json.loads(raw)
