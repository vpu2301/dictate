# ADR-0020 — Append-only versioning for clinical reports

- Status: accepted
- Date: 2026-05-13
- Sprint: 08
- Deciders: tech lead, security lead, clinical content lead, DPO

## Context

Sprint-08 stands up the central clinical artifact of the product —
the report. Every clinical decision pivots on whether the report
the clinician saw at a given moment can be reconstructed. The model
choices that anchor this are:

1. The **status lifecycle** (draft → finalized → signed → amended /
   cancelled) is irreversible in the forward direction.
2. Every content change creates a **new immutable row**, never an
   in-place UPDATE.
3. Sprint-09's KEP signing attaches to a specific version row; the
   exact bytes that were signed must be retrievable forever.

The shape we picked — `reports` (one head per logical report) +
`report_versions` (append-only history, with `current_version_id`
on the head) — leans on three load-bearing decisions made on day-1:

- Versions are JSONB blobs with strict Pydantic validation
  (`report_models.ReportContent`); the canonical bytes for signing
  are deterministic.
- `reports.current_version_id` is a deferrable FK so the
  two-step `INSERT reports` → `INSERT report_versions` → `UPDATE
  reports.current_version_id` pattern can run inside one
  serializable transaction (chicken-egg dependency resolved at
  COMMIT).
- `reports` denormalises a few columns (title, icd10_codes,
  encounter_date) that the search path filters/sorts on, so the
  primary index can be on `reports` alone — avoiding a costly join
  for the hot search path.

## Decision

Adopt append-only versioning with a head-row pointing at the current
version. Detailed rules:

- `report_versions` is INSERT-only for sprint-08 code; the row may
  later receive sprint-09 signing fields (`signed_at`, `signed_by`,
  …) via UPDATE — that's the *only* sanctioned mutation.
- DELETE on either table is forbidden at the RLS-policy level; the
  soft-delete is `cancel`.
- `parent_version_id` carries the chain link. For non-amendment
  versions parent = previous version (linear); amendments also use
  the immediately previous version as parent (linear) — see the
  day-4 property test and the chain-reconciler verifier.
- `version_number` is 1..N contiguous per report. Gaps are an
  integrity bug (caught by the daily reconciler + the CI property
  test that re-runs the same verifier).
- Idempotency for autosave retries: the most-recent version row's
  `metadata.body_hash` is checked against the incoming body hash.
  Match + same `expected_version` → return the prior version, no
  new row. Different body, same `expected_version` → 409 (FE must
  reload).

## Consequences

Positive:
- Storage cost is linear in writes; not a concern at expected scale.
- Retrievability is absolute: any historical state can be reconstructed
  by walking the version chain.
- The amendment chain integrity is a property the verifier (one
  module, two callers — CI + cron) actively maintains.

Negative / accepted:
- Reads that need "the report as it looked at time T" require a JOIN
  to `report_versions`. Mitigated by `reports.current_version_id` for
  the hot read path.
- The two-step insert is a sharp edge; documented in
  ``services/report-service/src/report_service/domain/reports_repository.py``
  and gated behind the `create_report_with_v1` helper. Bypassing the
  helper is a CI lint that blocks PRs (sprint-08 day-9 ships a
  ``check-report-create-helper-only`` gate; pending wire-up).

## Alternatives considered

- **Mutable rows + audit_log of diffs.** Rejected — audit_log carrying
  the canonical signed bytes turns the audit log into a signing
  oracle; we want signing attached to a *specific row* in the data
  schema, not a side log.
- **OCC via row version (`xmin`).** Works for autosave but doesn't
  give us the chain semantics we need for amendments.
- **Event-sourced model.** Strictly powerful but operationally heavy;
  picking it now would have cost a second sprint of plumbing. May be
  revisited in sprint-17 if FHIR export needs proper temporal
  semantics across patients.

## Links

- Sprint-08 spec §3.1, §3.2.
- ADR-0021 (Postgres `simple` FTS) — search atop this model.
- Sprint-09 KEP will reference `report_versions.signed_data`.
- `services/report-service/src/report_service/domain/chain_integrity.py`.
- `services/report-service/tests/property/test_amendment_chain.py`.
