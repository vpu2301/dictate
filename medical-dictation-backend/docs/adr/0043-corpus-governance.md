# ADR-0043: Clinical corpus governance — provenance, tiers, releases

Date: 2026-08-12
Status: Accepted
Sprint: 21

## Context

Sprint 10 shipped the autocomplete serving stack with a 30-phrase seed
corpus and a README IOU ("~10k UK / ~3k EN is a clinical-content
deliverable"). Sprint 21 builds the corpus itself, from four source
families with very different risk profiles: n-grams mined from finalized
report versions (PHI-derived), telemetry gap prefixes (PHI-derived),
public terminology (ДРЛЗ, formulary, НК 025), and LLM generation. A wrong
completion in a clinician's cursor is a patient-safety event; a mined
phrase that identifies its author or patient is a privacy event. Both
failure modes are invisible without provenance.

Explore findings that shaped this decision (`docs/sprint-21/EXPLORE.md`):
dev telemetry holds 8 events, so the corpus target (~3k accepted phrases,
not 10k) comes from the human-review budget (~8 clinician-hours ≈ 1,900
decisions) and a quota matrix, not from telemetry; and report text is
plaintext under RLS, so mining is SQL against `report_versions` with no
decryption step.

## Decision

**Every phrase carries its provenance, every acceptance carries its
reviewer, and serving is gated on acceptance.**

1. **Provenance columns** on `autocomplete_phrases` (migration 0081,
   additive, existing rows backfilled `source_kind='seed'`,
   `review_state='accepted'`): `source_kind`
   (`mined | telemetry_gap | terminology | generated | authored | seed`),
   `source_ref` (dataset id + version + SHA), `tier` (1–3),
   `review_state` (`candidate | accepted | rejected | retired`),
   `reviewed_by`, `reviewed_at`, `review_engine`
   (`human` or `jury:<model>:<prompt_version>`), `corpus_release`,
   `risk_flags text[]`.
2. **One serving predicate, no new path.** The trie builder's only feed,
   `fetch_corpus()`, gains `AND review_state = 'accepted'`. Rows written
   through the existing phrases API default to
   `source_kind='authored', review_state='accepted'` — a clinician's own
   phrase is their own responsibility, unchanged from sprint 10.
3. **Staging table `corpus_candidates`** (migration 0082, RLS + FORCE):
   nothing enters `autocomplete_phrases` except via the promote job.
   Candidates carry `dedupe_key` (normalised phrase),
   `generation_batch_id`, `jury_votes jsonb`, `validator_report jsonb`.
4. **Append-only `corpus_reviews`** (migration 0083), hash-chained like
   the audit tables with a DB immutability trigger. It is both the DPO's
   audit trail and the instrument that measures real seconds-per-decision
   (`latency_ms`) — if review costs 90 s instead of 15 s we learn it on
   day 7, not at sign-off.
5. **Immutable releases** in `corpus_releases` (migration 0084): a release
   is a version string + manifest SHA-256 over the exact accepted row set
   (the `eval/corpus/v1/manifest.json` pattern). Coverage and WER
   regressions bisect to a release, not a commit. Accepted rows are
   stamped with `corpus_release`; a release artifact contains phrases
   only, never raw source datasets.
6. **Tiers** encode required scrutiny, not quality: tier 1 (mined, fully
   validated, no risk flags) may be machine-accepted after calibration;
   tier 2 (generated / terminology-derived) needs jury majority; tier 3
   (any risk flag: dose/digits, drug, laterality, negation, ICD,
   unfamiliar abbreviation) is **human-mandatory, no exceptions**. Risk
   flagging is biased to over-flag. A row with non-empty `risk_flags`
   and `tier < 3` fails CI.
7. **Mining gates live in SQL**, not Python: n-gram length 3–12 tokens,
   ≤80 chars, and **k-anonymity — ≥5 distinct authors across ≥2 tenants**
   (re-checked in Python as defense in depth). PII-matching n-grams are
   dropped, not redacted; tokens appearing in the tenant's `patients`
   name columns are rejected. The DPO signs the mining query text before
   first execution (`docs/signoffs/sprint-21-dpo.md`).

## Consequences

- Coverage math changes from "rows in table" to "rows in release";
  the coverage/usefulness harness (`scripts/eval/corpus_coverage.py`)
  runs per release and the ≥80%-useful / **0-harmful** gate blocks
  publication.
- The unique index on `(tenant, owner, phrase, language)` still holds;
  promote is idempotent by construction (`ON CONFLICT DO NOTHING` +
  candidate state transition).
- `list_phrases` (admin UI) intentionally still shows non-accepted rows —
  admins see the whole lifecycle; only the trie feed is gated.
- Rejected candidates stay in `corpus_candidates` forever: they are the
  negative training/calibration set and the dedupe backstop.
- Relaxing any gate here (k-anonymity threshold, tier-3 mandatory human,
  0-harmful) is an ADR amendment, not a config change.

## Alternatives rejected

- **Separate "reviewed corpus" table + new serving path** — rejected;
  sprint 10's serving stack is proven, and a second path doubles cache
  invalidation and RLS surface for zero user value.
- **Corpus target of 10k phrases** — rejected as budget-dishonest; 3k is
  what one part-time clinician can certify at 0-harmful (EXPLORE §5).
- **Redacting PII in mined phrases instead of dropping** — a redacted
  completion is useless and the redaction marker itself leaks that PII
  was present.
