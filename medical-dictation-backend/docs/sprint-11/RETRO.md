# Sprint 11 — Retro

## What went well

- **The fan-out map paid off immediately**: DSAR and erasure consumed the
  same inventory with zero duplicated enumeration SQL, and the CI gate
  caught its own self-test scratch table on the first try. "The schema
  proves we enumerate everything" held.
- **Live E2E over unit-only paid for itself repeatedly**: it caught the
  consent `granted_at` DB-default skew (canonical recomputation could
  never match), the empty audit slice (audit_reader is tenant-GUC-scoped),
  the S03 `SUM(...) FOR UPDATE` bug that had silently broken every batch
  upload, and the master-key startup_self_check omission.
- **Role-split as last-line defense worked as designed** — including
  against our own test fixtures (the two-person CHECK rejected the
  fire-test plant until it carried a distinct reviewer).

## What was harder than expected

- **Stale sprint-doc assumptions**: migration numbers (0036 vs actual
  0042), `patient_privacy_requests` column names, the "open" encounter
  status that doesn't exist (default is `completed`). Ground-truthing
  against the live schema before writing SQL was the correct reflex every
  time.
- **Cross-tenant operational scans vs RLS**: three features (DSAR
  recovery, queue metrics, erasure scheduling) wanted a cross-tenant view
  that app_role rightly cannot have. The pattern that emerged — ops
  cron scripts under an operational DSN, in-service logic strictly
  tenant-scoped — should be written down as the standing answer.
- **Shared migration files across steps** (0043, 0044) forced repeated
  down/edit/up cycles against the checksum guard. Workable, but a
  one-migration-per-step layout would have been calmer.

## Actions

1. Document the "cross-tenant ops = cron script + ops DSN" pattern in
   the architecture docs (candidate for the sprint-12 paved road).
2. Consider promoting the pinned-copy scrubber into a leaf lib next time
   a THIRD copy is needed (two copies + parity test is the ceiling).
3. The fixture/cleanup helpers in core-service integration tests are now
   load-bearing across three modules — worth a conftest promotion.
