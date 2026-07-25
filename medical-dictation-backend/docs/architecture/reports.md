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
  hatch — realized in sprint 13; the normative key registry is below.
- **Sprint-14 (conversation)**: dictation sessions create drafts
  via existing POST `/v1/reports`.
- **Sprint-15 (note review)**: `transcript_segment_ids` already on
  every section.
- **Sprint-16**: idle-draft cleanup scheduler;
  partition trigger conditions documented in
  `docs/eval/sprint-08-loadtest.md`.
- **Sprint-17 (FHIR)**: `reports.icd10_codes`, `encounter_date`,
  `template.metadata.fhir_template` are the bridges.

## `field_specific_metadata` — the normative key registry (sprint 13)

The dict stays `dict[str, Any]` on `ReportSection` — sprint-09
signatures commit to canonical bytes, so the storage shape never
depends on the registry. Discipline is enforced at the **write path**:
report-service validates every non-empty dict against the section's
template `field_type` (`report_models.validate_field_metadata`), and
the nlp extractor constructs metadata via the typed models in
`report_models.field_metadata` — raw-dict assembly is forbidden.

| `field_type` | allowed keys |
| --- | --- |
| `choice` | `{selected: <option value>, confidence?: float 0..1, source: "extracted"\|"manual"}` |
| `multi_choice` | `{selected: [<option values>, ≥1 unique], confidence?, source}` |
| `structured_diagnosis` | `{proposals: [{code, display?, confidence}], confidence?, source}` |
| `numeric_with_unit` | `{value: number, unit: str, confidence?, source}` |
| `date` / `date_with_note` | `{date: "YYYY-MM-DD" (real calendar date), confidence?, source}` |
| `free_text` (and any other) | none — an empty dict only |

Common rules (enforced by the typed models):

- **An empty dict is always valid** — every pre-S13 report.
- **`source` is required** whenever any other key is present.
  `extracted` = a pipeline proposal (FE renders it as such);
  `manual` = clinician-confirmed. Nothing ever auto-promotes
  extracted → manual; promotion is exclusively an explicit user action
  arriving as a draft PUT.
- **`confidence` is required for `extracted`** (the extractor always
  knows it) **and must be omitted for `manual`** — a clinician
  confirmation is not a probability. Confirming a choice therefore
  writes `{selected, source: "manual"}`.
- **Unknown keys are unwritable**: report-service rejects them with
  `422 field_metadata_invalid` (section-addressed). A `selected` value
  that is not one of the section's template option `value`s is
  `422 choice_value_unknown`.
- Sprint-15 (note review) adds its keys by extending
  `META_MODEL_BY_FIELD_TYPE` in `report_models.field_metadata` — never
  by bypassing it.

### `proposals` vs `section.icd10` — single authority for codes

The sprint-13 sprint doc sketched diagnosis metadata as a *mirror* of
the section's ICD-10 list. Realized refinement: **`section.icd10` is
the single authority for confirmed codes**; the metadata carries
`proposals` — the extractor's staging area (code + display +
confidence, never auto-selected). Confirming a proposal moves its code
into `section.icd10` and may clear `proposals`; rejecting clears the
metadata leaving the dictated prose. Mirroring confirmed codes into
metadata would create a dual-write invariant that *will* drift — a
wrong auto-mirrored ICD-10 is a clinical and billing error, so the
design makes it impossible by construction.


## Typed finalize completeness (sprint 13)

`min_chars` measures prose, so it says nothing about a `choice`
section whose answer lives in `field_specific_metadata`. Each typed
field type gets its own "filled" rule. Free-text sections behave
exactly as they did in sprint 08 — zero change for existing templates.

| `field_type` | filled when | violation code |
| --- | --- | --- |
| `free_text` | `min_chars` (unchanged) | `missing_required_section` / `below_min_chars` |
| `choice` | metadata `selected` present (any `source`) | `choice_not_selected` |
| `multi_choice` | `selected` non-empty (any `source`) | `choice_not_selected` |
| `numeric_with_unit` | metadata `value` **and** `unit` present | `numeric_not_filled` |
| `date` / `date_with_note` | metadata `date` present; `date_with_note` also applies `min_chars` to the note | `date_not_filled` (+ `below_min_chars`) |
| `structured_diagnosis` | `section.icd10` non-empty — **confirmed codes only** | `missing_icd10` / `diagnosis_not_confirmed` |

All violations travel in the existing sprint-08 `FinalizeProblem`
shape at **422** (409 stays reserved for status/version conflicts);
the new codes are registered in `_REASON_BY_CODE`.

### Why `extracted` counts for most fields but never for diagnoses

An `extracted` choice satisfies "filled": the clinician saw the
proposal and chose to finalize, which is acceptance. A diagnosis is
different — it drives billing and downstream clinical decisions, and a
finalized report is signable. So **`section.icd10` is the only thing
that counts**; `field_specific_metadata.proposals` never satisfies a
diagnosis section, under any configuration.

### The `require_confirmed_diagnosis_on_finalize` flag

Default **true**. It governs **messaging, not authority**:

- **true** — proposals present, nothing confirmed ⇒
  `diagnosis_not_confirmed` ("підтвердіть запропонований діагноз"),
  and the FE's confirm affordance is one tap away.
- **false** — the same state reports `missing_icd10` ("вкажіть
  діагноз").

Turning it off does **not** auto-promote proposals. The sprint-13 doc
left room for that reading; it was rejected because it directly
contradicts the never-guess directive — auto-promotion would put a
machine-chosen ICD-10 into a signed clinical record. Whether a tenant
may ever opt into auto-promotion is a clinical-policy question
recorded in `todo.md`, not an engineering default.

Implementation note: the flag is currently **platform-wide service
config** (`MDX_REQUIRE_CONFIRMED_DIAGNOSIS_ON_FINALIZE`), because the
repo has no tenant-settings mechanism yet. `validate_finalize` already
takes it as an argument, so per-tenant resolution is a one-line change
once that mechanism exists.

### Provenance in signed content

A non-required typed section may carry `source: "extracted"` into a
finalized and signed report. That is deliberate and honest: the
signature covers the content including its provenance marker, so a
reader can always tell which values a machine proposed and the
clinician left standing, versus which they entered or confirmed
themselves.
