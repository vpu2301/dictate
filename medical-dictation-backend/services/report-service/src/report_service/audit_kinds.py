"""Audit kinds emitted by report-service. See docs/audit/event-kinds.md."""

from __future__ import annotations

from typing import Final

TEMPLATE_CREATED: Final = "template.created"  # plain POST /templates (M1·A4)
TEMPLATE_CLONED: Final = "template.cloned"
TEMPLATE_UPDATED: Final = "template.updated"  # cosmetic edit
TEMPLATE_VERSIONED: Final = "template.versioned"  # structural edit → new row
TEMPLATE_DEPRECATED: Final = "template.deprecated"  # soft-delete
TEMPLATE_VIEWED_FULL: Final = "template.viewed_full"  # GET /templates/{id}
TEMPLATE_REBOUND: Final = "template.rebound"  # sprint-17: draft moved to successor

# Sprint-08: reports slice.
REPORT_CREATED: Final = "report.created"
REPORT_DRAFT_UPDATED: Final = "report.draft.updated"  # aggregated per session
REPORT_FINALIZED: Final = "report.finalized"
REPORT_SIGN_REQUESTED: Final = "report.sign_requested"  # S09-rev: sign surface invoked
REPORT_COMPLETED: Final = "report.completed"  # finalize completion summary (M1·A5)
REPORT_REVERTED: Final = "report.reverted"
REPORT_CANCELLED: Final = "report.cancelled"
REPORT_AMENDED: Final = "report.amended"  # post-sign (sprint-09)
REPORT_AMENDMENT_DRAFTED: Final = "report.amendment_drafted"  # pre-sign
REPORT_VIEWED_FULL: Final = "report.viewed_full"  # carries purpose
REPORT_SEARCHED: Final = "report.searched"
REPORT_CHAIN_INTEGRITY_FAILURE: Final = "report.chain_integrity_failure"
REPORT_PDF_RENDERED: Final = "report.pdf_rendered"  # GET /reports/{id}/pdf (M1·A3)

# Spec item 1: report synthesis (raw dictation → clean prose).
REPORT_SYNTHESIS_STARTED: Final = "report.synthesis_started"
REPORT_SYNTHESIS_COMPLETED: Final = "report.synthesis_completed"

# Sprint-13: typed anamnesis fields. Both are the extractor-quality
# feedback loop (step 08's override-rate dashboard).
#
# PHI RULE: payloads carry the section id, the field type and — only for
# closed vocabularies — the option slug or ICD-10 code. Free-text values
# are NEVER included; a "what did they change it to" payload over prose
# would put PHI in the audit chain. Test-enforced.
# Emitted ONCE per finalized report, summarising how many typed fields
# carried machine-extracted values at the moment of finalize. Aggregated
# on purpose: a row per utterance would be audit-chain pollution.
ANAMNESIS_FIELD_EXTRACTED: Final = "anamnesis.field.extracted"
ANAMNESIS_FIELD_CONFIRMED: Final = "anamnesis.field.confirmed"
ANAMNESIS_FIELD_OVERRIDDEN: Final = "anamnesis.field.overridden"

# ── S14: break-glass PHI access ──────────────────────────────────────
# All `sec` severity. `PHI_ACCESS_USED` is emitted per read under a
# grant, in ADDITION to REPORT_VIEWED_FULL — the ordinary view event
# alone would make an administrator's break-glass read indistinguishable
# from a clinician's routine one when the trail is read back.
PHI_ACCESS_GRANTED: Final = "phi_access.granted"
PHI_ACCESS_DENIED: Final = "phi_access.denied"
PHI_ACCESS_USED: Final = "phi_access.used"
PHI_ACCESS_REVOKED: Final = "phi_access.revoked"

# ── S15: audio replay (ADR-0037) ─────────────────────────────────────
# One event per clip created — replay is a per-decision review act, not
# a keystroke stream, so no aggregation. Payload: clip_id, source_kind,
# ms range, purpose, is_author, break_glass. Never transcript text.
REPORT_AUDIO_REPLAYED: Final = "report.audio_replayed"

# ── S15: query expansion (ADR-0038) ──────────────────────────────────
# search.expanded is AGGREGATED (one row per tenant per flush interval;
# payload: count, expanded_terms_total — never the query text). The
# synonym.group.* kinds fire per curation act; terms are dictionary
# entries, not prose.
SEARCH_EXPANDED: Final = "search.expanded"
SYNONYM_GROUP_CREATED: Final = "synonym.group.created"
SYNONYM_GROUP_UPDATED: Final = "synonym.group.updated"
SYNONYM_GROUP_DELETED: Final = "synonym.group.deleted"

# ── Sprint 16 — scheduler runs ──────────────────────────────────────────
SCHEDULER_JOB_COMPLETED: Final = "scheduler.job.completed"
SCHEDULER_JOB_FAILED: Final = "scheduler.job.failed"
