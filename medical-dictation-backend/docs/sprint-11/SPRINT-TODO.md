# Sprint 11 — Patients, Encounters, Consent & Right to Erasure (as-built, checked)

> Branch `S11`; migrations **0042–0044** (renumbered from the sprint
> doc's 0036–0038 — dev was at 0041). Base: `dev` == `main` @ 51fae04.

- [x] **01 — Patient identity (ІПН + erased status)** — 0042: `ipn_hmac`
      lookup (shared РНОКПП helper in `libs/crypto/ipn.py`, signing-service
      re-uses it), DPO-gated `ipn_encrypted`/`ipn_dek`, `erased` terminal
      status + partial-unique that frees the slot post-erasure; ADR-0027.
      Live E2E incl. envelope decrypt round-trip. (c090e31)
- [x] **02 — `audio_files.encounter_id` real FK + ingest validation** —
      0043 (first half): orphan-guarded FK ON DELETE RESTRICT; WS codes
      `encounter_invalid`/`encounter_closed` (closed = cancelled ONLY —
      encounters default 'completed'); ASR upload validates AND persists
      the id; timeline gains `recording` items. Fixed pre-existing
      S03 `SUM(...) FOR UPDATE` bug that 500'd every batch upload. (5a4132a)
- [x] **03 — Consent ↔ signing** — 0043 (second half):
      `signed_envelope_id` + `canonical_hash`; canonical consent doc v1
      binds the approved text file's sha256; signing-service's
      `mark_resource_signed` gains a STRICT consent branch (tamper ⇒ whole
      persist txn aborts); `resource_version_id == consent id`. Live E2E
      via dev_password incl. tamper-rollback proof. (d4aa70d)
- [x] **04 — Two-person erasure workflow + `mdx_erasure`** — 0044:
      per-kind state machines, `privacy_two_person` +
      `privacy_approved_has_review` CHECKs, LOGIN role (deviation from
      NOLOGIN+SET ROLE — membership from app_role would be an escalation
      path), grants + erasure RLS policies; `privacy.approve`
      (tenant_admin). Headline proof: app_role still cannot DELETE PHI;
      mdx_erasure can, tenant-scoped only. (a8d2b9f)
- [x] **05 — Fan-out map + CI gate** — `core_service/erasure/fanout.py`
      (14 artifact classes; `enumerate_patient` is the one inventory) +
      `check-erasure-fanout-coverage` in `ci-with-db` (FK closure +
      soft-linked `signed_envelopes`/`signing_sessions` asserted by name);
      non-PHI claims pinned with data. Findings: dictation transcripts
      live in `dictation_sessions.transcript_jsonb`; `signing_sessions`
      carry canonical PHI. (8987c71)
- [x] **06 — DSAR export engine** — assembly along the map, manifest w/
      per-file sha256 + named exclusions, envelope-encrypted ZIP,
      `patient.dsar` scope, authenticated decrypt-and-stream download
      (presigned = ciphertext by platform rule), on-request stale-takeover
      recovery (tenants RLS-invisible to app_role), 14-day package TTL
      cron. Full E2E on real MinIO+crypto. (02741cd)
- [x] **07 — Erasure engine** — advisory-locked phases; crypto-shred
      object-before-row; consents ALWAYS retained; retention boundary cuts
      envelope+PDF with the report; no failed state (last_error +
      idempotent re-run); scheduler cron + manual CLI. The (a)–(e) battery
      green with the wrapped-DEK-in-0-rows proof and the audit chain
      verifying end-to-end. (4b82863)
- [x] **08 — Close-out** — scrubber gap closed (rejection_reason →
      audit scrub-on-write; parity-pinned copy), kind-registration +
      label-discipline tests, Grafana dashboard + 4 promtool-checked
      alerts (fire-tested against a planted stuck row through the real
      OTLP→collector→Prometheus pipeline), runbook completed, records +
      carry-overs. (this commit)
- [x] **09 — Deployment (ADR-0028)** — `deploy/` privacy-ops overlay:
      erasure credential isolated to job containers (two-direction psql
      proof: app_role `permission denied`, mdx_erasure `DELETE 0`);
      first real backups (encrypted pg_dump → `mdx-backups`, 35-day ILM
      = the erasure completion horizon, `backups_purged_by` stamped into
      every report_of_execution); restore.sh with MANDATORY scripted
      post-restore erasure re-run (proven live: backup → erase →
      restore → re-erased, `operator=restore-rerun`); `mdx-dsar` 7-day
      ILM backstop + 15-minute HMAC download links (presigned-TTL
      equivalent under the ciphertext rule); alert data path fixed
      (job containers set OTLP endpoint), `DsarExportFailed` added on an
      unresolved-failures gauge, stuck-erasure alert fire-tested.
      Details + transcripts: `deploy/README.md`.
