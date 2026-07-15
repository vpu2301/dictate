# Erasure & DSAR — the fan-out map (S11 step 05)

**A patient is a join point, not a copy point.** One typed registry —
`services/core-service/src/core_service/erasure/fanout.py::FANOUT` —
enumerates every place patient data lives. The DSAR export (step 06)
renders it, the erasure engine (step 07) destroys along it, and the CI
gate `check-erasure-fanout-coverage` (in `make ci-with-db`) fails the
build when a patient-linked table isn't registered. Each Artifact's
`ids_sql` is THE join from `patients.id` — no patient-enumeration SQL
exists outside the package.

## The artifact classes

| kind | table | erasability | exportable | notes |
|---|---|---|---|---|
| `patient` | patients | **OVERWRITE** | yes | Identity overwritten in place (names→`ERASED`, DOB/MRN/tags/summaries cleared, ALL ІПН columns NULLed — ADR-0027); the row survives as an `erased` tombstone so the paper trail keeps a referent. |
| `encounter` | encounters | HARD_DELETE | yes | Deleted after its dependents (audio FK is RESTRICT — ordering enforced by the DB). |
| `clinical_note` | clinical_notes | HARD_DELETE | yes | |
| `anamnesis` | patient_anamnesis | HARD_DELETE | yes | |
| `consent` | patient_consents | RETAIN_IF_SIGNED | yes | Signed consents (envelope linked) are the clinic's proof of lawful basis. |
| `privacy_request` | patient_privacy_requests | **NEVER** | yes | The erasure's own paper trail. |
| `report` | reports | RETAIN_IF_SIGNED | yes | See retention rule below. |
| `report_version` | report_versions | RETAIN_IF_SIGNED | yes | Follows its report; delete order handles the deferrable `current_version_id` circularity. |
| `synthesis_job` | report_synthesis_jobs | HARD_DELETE | no | Operational; may embed transcript inputs — destroyed, never exported. |
| `recording` | audio_files | **CRYPTO_SHRED** | yes | MinIO object deleted FIRST (its per-object DEK dies — header + `envelope_metadata` copy), then the row. Crash between = row pointing at nothing (safe direction); re-runs idempotent. |
| `transcription_job` | transcription_jobs | CRYPTO_SHRED | yes | Batch transcript object (`mdx-transcripts`) + row. |
| `dictation_session` | dictation_sessions | HARD_DELETE | yes | `transcript_jsonb` IS the streaming transcript (S04). Joined via `encounter_id` (bare UUID) OR `audio_file_id` (FK) — both paths. |
| `signed_envelope` | signed_envelopes | **NEVER** | yes | Qualified-signature evidence. **Soft `resource_id` link** — asserted by name in the gate. |
| `signing_session` | signing_sessions | HARD_DELETE | no | Transient rows whose `canonical_json` carries patient names. Soft link, gate-asserted. |

## Retention rule (reports)

Reports whose lifecycle reached a qualified signature
(`status IN ('signed','amended')`) and whose `signed_at` is inside
`REPORT_RETENTION_YEARS` (config, default **25** — Ukrainian clinical
record retention; legal confirmation tracked in `todo.md`) are
**retained** with basis `retention:clinical_record_signed`, together
with their versions. Drafts, unsigned-finalized, and cancelled reports
are hard-deleted.

## Legal basis strings (machine-stable; used in `report_of_execution`)

| basis | meaning |
|---|---|
| `retention:clinical_record_signed` | Signed clinical record inside the statutory retention window (МОЗ clinical-record rules; Law 2297-VI art. 6 §5 permitted processing). |
| `retention:signed_consent_evidence` | Signed consent kept as proof of lawful basis for past processing. |
| `retention:qualified_signature` | КЕП envelope — evidence under Law 2155-VIII. |
| `retention:erasure_paper_trail` | The privacy-request record itself: proof the erasure was requested, approved (two-person) and executed. |

## Confirmed non-PHI (pinned by tests, not comments)

- `autocomplete_telemetry` — scrubbed prefixes + ids only (S10 scrubber,
  DPO sign-off `docs/security/autocomplete-pii-scrubber.md`).
- `audit.events` — payload convention: ids only, never identity strings.

Both claims are asserted with data in
`tests/integration/test_non_phi_assertions.py`; a failure there is a
real leak, not a test to soften.

## The CI gate

`scripts/ci/check_erasure_fanout_coverage.py` (run as
`make check-erasure-fanout`, wired into `ci-with-db`):

1. computes the FK closure from `patients(id)` to depth 3 on the live
   schema;
2. fails on any closure table absent from `FANOUT ∪ KNOWN_NON_PHI`,
   naming the table and the two ways to fix it;
3. fails on dead map entries (registered table no longer exists);
4. asserts the soft-linked PHI tables (`signed_envelopes`,
   `signing_sessions`) by name — exactly the class of edge FK scanning
   misses.

The guarantee: future sprints **cannot forget**. Adding a
patient-linked table without deciding its DSAR/erasure fate breaks the
build with an instructive message.

## Grants lockstep

The `mdx_erasure` grant list (migration `0044`) must equal the set of
tables the erasers touch: every HARD_DELETE/CRYPTO_SHRED table gets
SELECT+DELETE; `patients` and `patient_privacy_requests` get
SELECT+UPDATE; NEVER-class tables get nothing beyond what reading
requires. The gate keeps the map honest; the step-04 integration tests
keep the grants honest.
