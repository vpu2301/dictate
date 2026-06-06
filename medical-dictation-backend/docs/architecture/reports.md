# Reports architecture (sprint-08)

The central clinical artifact. Sprint-08 establishes the data model,
lifecycle, search, and diff surface; sprint-09 attaches KEP signing.

## Data model

```
reports ── 1:N ── report_versions
  (head)              (append-only)
  ├─ current_version_id ──────┐
  └─ status enum              │
                              │
   report_versions
     ├─ parent_version_id  ◄──┘  (chain link)
     ├─ version_number     (1..N contiguous)
     ├─ is_amendment       (true for amendments)
     ├─ content_jsonb      (Pydantic-validated)
     ├─ rendered_text      (FTS-indexed)
     ├─ diff_jsonb         (summary of changes vs parent)
     ├─ metadata           ({body_hash: ...} for idempotency)
     └─ signing fields     (filled by sprint-09)
```

The two-step insert (ADR-0020) creates `reports` with NULL
`current_version_id`, inserts v1 in `report_versions`, then UPDATEs
the head. The FK is `DEFERRABLE INITIALLY DEFERRED` so the
constraint check happens at COMMIT.

## Status lifecycle

Allowed transitions (`domain/report_lifecycle.py`):

```
   ┌──────────┐   finalize   ┌──────────────┐    sign    ┌─────────┐  amend   ┌─────────┐
   │  draft   │─────────────►│  finalized   │───────────►│ signed  │─────────►│ amended │
   └────┬─────┘ ◄────revert──└──────┬───────┘            └────┬────┘          └─────────┘
        │     (1h, author)          │                         │
        │ cancel                    │ cancel                  │ (amend chain
        ▼                           ▼                         │  re-enters by
   ┌──────────┐                ┌──────────┐                   │  going via sign
   │ cancelled│                │ cancelled│                   │  again in
   └──────────┘                └──────────┘                   │  sprint-09)
```

Transitions are single-statement UPDATEs with `WHERE status =
expected_from`; concurrent transitions are caught and 409'd. The
revert window is 1 hour and limited to the primary author.

## Optimistic locking

`PUT /v1/reports/{id}/draft` requires `expected_version`. The
service:
1. row-locks the `reports` row,
2. compares `expected_version` to `current_version.version_number`,
3. mismatch → 409 with hint payload,
4. match → append new version, update `current_version_id`.

Idempotent retries use `metadata.body_hash`: same body + same
`expected_version` returns the prior version with `idempotent_replay:
true`.

## Amendments

`POST /v1/reports/{id}/amend` is allowed only on signed reports. It
creates a new `report_versions` row with `is_amendment=true`. Status
stays `signed` until sprint-09 signs the amendment, at which point
the status transitions to `amended` (sprint-09 hook).

## Chain integrity

`domain/chain_integrity.py` is a pure-Python verifier; it's called
by:
- The CI property test (`tests/property/test_amendment_chain.py`),
- The daily reconciler (`jobs/chain_reconciler.py`, cron 04:30 UTC).

Anomalies recorded both in `audit.report_chain_failures` (for the
dashboard) AND in the hash-chained `audit.events` log.

## Search

`GET /v1/reports/search` — `simple` FTS config (ADR-0021), GIN
indexes on `reports.search_vector` (title/code) and
`report_versions.search_vector` (rendered_text). Filters compose
with AND. `simple`'s lack of stemming is documented in the user-facing
search tips screen (sprint-15).

Cursor pagination on `(encounter_date DESC NULLS LAST, id DESC)`. The
cursor is opaque base64 JSON.

## Read purpose

Non-author full-read of `GET /v1/reports/{id}` requires
`?purpose=<value>`. Allowed: `clinical_continuity`, `audit`, `legal`,
`qa_review`, `consultation`. Captured into the
`report.viewed_full` audit row.

## PII redaction in snippets

`domain/pii_redactor.py` runs on snippets returned to viewers who
are not on the treatment team (primary_author, co_author,
tenant_admin, dpo). Conservative regex sweep — second line of defence
behind the role check; clinical content lead reviews quality each
release.

## Diff endpoint

`GET /v1/reports/{id}/diff?from=<v>&to=<v>` — both arguments accept a
`version_number` or a UUID `version_id`. `difflib.SequenceMatcher`
char-level diff; metadata diff for title/icd10/encounter_date.

In-process LRU cache (`domain/diff_cache.py`) keyed by
`(report_id, from_id, to_id)`. Versions are immutable so cache hits
are always safe.

## Observability

- Metrics: `mdx_reports_created_total`, `mdx_reports_finalized_total`,
  `mdx_reports_amended_total{amendment_type}`,
  `mdx_reports_autosave_conflicts_total`,
  `mdx_reports_search_latency_ms_histogram{has_q}`,
  `mdx_reports_diff_cache_lookups_total{hit}`,
  `mdx_reports_chain_integrity_check_failures_total`.
- Dashboard: `sprint-08-reports.json`.
- Alerts: `sprint-08-alerts.yml`.

## Hand-offs

- **Sprint-09 (KEP signing)**: `signed_data`, `signed_data_hash`,
  `signing_record_id` already on `report_versions`. Canonical bytes:
  `report_models.canonical_content_bytes(ReportContent)`.
- **Sprint-11 (patients)**: `reports.patient_id` FK with ON DELETE
  RESTRICT. Patient soft-delete only.
- **Sprint-13 (anamnesis)**:
  `ReportSection.field_specific_metadata` is the typed-slot escape
  hatch.
- **Sprint-14 (conversation)**: dictation sessions create drafts
  via existing POST `/v1/reports`.
- **Sprint-15 (note review)**: `transcript_segment_ids` already on
  every section.
- **Sprint-16**: idle-draft cleanup scheduler;
  partition trigger conditions documented in
  `docs/eval/sprint-08-loadtest.md`.
- **Sprint-17 (FHIR)**: `reports.icd10_codes`, `encounter_date`,
  `template.metadata.fhir_template` are the bridges.
