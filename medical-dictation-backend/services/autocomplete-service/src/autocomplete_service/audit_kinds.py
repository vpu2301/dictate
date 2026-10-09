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

# WER eval recorder persistence (migration 0089): a scripted take was stored /
# replaced, removed, or the tenant's takes were exported as one archive.
# Payloads carry script ids, conditions and byte counts — never audio.
CORPUS_EVAL_TAKE_SAVED: Final = "corpus.eval_take_saved"
CORPUS_EVAL_TAKE_DELETED: Final = "corpus.eval_take_deleted"
CORPUS_EVAL_EXPORTED: Final = "corpus.eval_exported"

# The eval pipeline (migration 0091): the corpus can now be authored,
# published and scored from the console, and each of those is a decision
# somebody made. Payloads carry script ids, categories, digests and scores —
# never audio, and never the text of a PII finding.
CORPUS_EVAL_LINE_ADDED: Final = "corpus.eval_line_added"
CORPUS_EVAL_LINE_UPDATED: Final = "corpus.eval_line_updated"
CORPUS_EVAL_LINE_DELETED: Final = "corpus.eval_line_deleted"
# Ad-hoc capture is its own kind, not a line-added with a flag: it is the
# only path where audio was recorded before its text existed, and its
# payload carries the no-patient-data attestation that allowed it.
CORPUS_EVAL_ADHOC_CAPTURED: Final = "corpus.eval_adhoc_captured"
CORPUS_EVAL_PUBLISHED: Final = "corpus.eval_published"
CORPUS_EVAL_RUN_STARTED: Final = "corpus.eval_run_started"
CORPUS_EVAL_RUN_COMPLETED: Final = "corpus.eval_run_completed"
# Bulk authoring from a CSV (0092, corpus-v2 §6). Dry runs are audited too:
# "we previewed importing 86 lines into the holdout and did not" is exactly
# the event worth having when the test set later looks larger than it should.
CORPUS_EVAL_IMPORTED: Final = "corpus.eval_imported"

# Corpus-v3 Epic B (migration 0093): a gold transcript was rewritten into the
# spoken form the style guide requires. Emitted only when a revision actually
# landed; the payload carries ids and counts — never the corpus text, which
# lives in corpus_eval_gold_revisions with both sides of the change.
CORPUS_EVAL_GOLD_REVISED: Final = "corpus.eval_gold_revised"

# Corpus-v3 Epic E (migration 0096): a stored take was marked unusable (or
# the mark was lifted). The only retake signal that is a human judgement
# rather than a derived observation, which is why it needs a named actor.
CORPUS_EVAL_TAKE_FLAGGED: Final = "corpus.eval_take_flagged"

# Corpus-v3 Epic F (migration 0097): speaker consent for the measurement
# corpus was granted or withdrawn, and the data register was exported for an
# auditor. Revocation is `warn`: it changes which takes may enter future
# snapshots, and somebody should be able to find it without knowing to look.
CORPUS_SPEAKER_CONSENT_GRANTED: Final = "corpus.speaker_consent_granted"
CORPUS_SPEAKER_CONSENT_REVOKED: Final = "corpus.speaker_consent_revoked"
CORPUS_DATA_REGISTER_EXPORTED: Final = "corpus.data_register_exported"
