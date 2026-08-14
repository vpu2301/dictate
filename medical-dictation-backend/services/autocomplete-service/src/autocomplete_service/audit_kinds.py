"""Audit kinds emitted by autocomplete-service."""

from __future__ import annotations

from typing import Final

PHRASE_CREATED: Final = "autocomplete.phrase.created"
PHRASE_UPDATED: Final = "autocomplete.phrase.updated"
PHRASE_DELETED: Final = "autocomplete.phrase.deleted"
SNIPPET_CREATED: Final = "autocomplete.snippet.created"
SNIPPET_UPDATED: Final = "autocomplete.snippet.updated"
SNIPPET_DELETED: Final = "autocomplete.snippet.deleted"
PHRASE_WRITE_REJECTED_PII: Final = "autocomplete.phrase.write_rejected_pii"
ROLLUP_COMPLETED: Final = "autocomplete.rollup.completed"

# ── Sprint 16 — scheduler runs (telemetry cold-archive + rotation) ──────
SCHEDULER_JOB_COMPLETED: Final = "scheduler.job.completed"
SCHEDULER_JOB_FAILED: Final = "scheduler.job.failed"

# Sprint 21 — corpus review surface (ADR-0043/0044). Same kind string as
# corpus-forge's CLI review path; docs/audit/event-kinds.md lists both emitters.
CORPUS_CANDIDATE_REVIEWED: Final = "corpus.candidate_reviewed"

# Authored ingest + HTTP promotion (post-S21 gap-fill): a console-submitted
# (typed or dictated) candidate entered the review queue / accepted global
# candidates were published into the serving corpus.
CORPUS_CANDIDATE_SUBMITTED: Final = "corpus.candidate_submitted"
CORPUS_CANDIDATES_PROMOTED: Final = "corpus.candidates_promoted"
