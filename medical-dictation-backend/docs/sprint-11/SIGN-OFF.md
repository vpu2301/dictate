# Sprint 11 — Sign-Off

**Sprint dates:** 2026-07-15 (steps 01–08, branch `S11`)
**Scope:** lawful patient identity (ІПН), consent↔КЕП, DSAR export,
right to erasure with two-person control and crypto-shredding.

## Definition of Done — verification results

| DoD item | result |
|---|---|
| ІПН capture + HMAC lookup, no raw-ІПН queries; every prior socket a real FK | ✅ repo-grep test + live E2E (step 01); `audio_files.encounter_id` FK + RESTRICT proven (step 02) |
| Consent capturable, withdrawable, signable via S09 | ✅ live dev_password E2E; withdraw keeps envelope; tamper ⇒ rollback (step 03) |
| DSAR assembles the complete package from the fan-out map | ✅ manifest kinds == exportable fan-out kinds; per-file sha256 verified; audit slice filtered (step 06) |
| Erasure provably destroys / honestly retains; two-person enforced | ✅ the (a)–(e) battery: objects 404, wrapped-DEK material in 0 rows, retained[] with bases, chain verifies end-to-end; two-person at service AND DB layer (steps 04/07) |
| Fan-out map CI-guarded against drift | ✅ `check-erasure-fanout-coverage` in `ci-with-db`; live self-test (scratch FK → build fails naming it) (step 05) |
| All VERIFY green with real output | ✅ pasted per step; final gates below |

## Final gates

- `make ci-with-db` — exit 0 (includes the fan-out coverage gate).
- Unit suites: core-service **93**, signing-service 45, crypto 53
  (incl. 25 ІПН), auth 54 (perm parity), dictation 58, asr 25 — green.
- Integration (`RUN_DB_INTEGRATION=1`): core-service **17** + asr FK 3 +
  signing (unit-harness) — green (identity, FK, erasure role/CHECKs,
  fan-out, non-PHI, DSAR E2E, the erasure (a)–(e) battery).
- promtool: `SUCCESS: 4 rules found`; alert inputs fire-tested live
  (stuck-executing age 7264 s > 3600 via the real metrics pipeline).

## Named carry-overs (tracked in `todo.md`)

| item | owner |
|---|---|
| Raw-ІПН retention flag (`PATIENT_IPN_RAW_ENABLED`) | **DPO** |
| ІПН-hmac-at-erasure branch confirmation (NULLed today, ADR-0027) | **DPO** |
| DSAR audit-kind allowlist (`DSAR_AUDIT_KINDS`) | **DPO** |
| Raw audio in DSAR packages (`DSAR_INCLUDE_RAW_AUDIO`) | **DPO** |
| Clinical-record retention period (`REPORT_RETENTION_YEARS`, default 25) | **legal counsel** |
| Consent text approval (`infra/seeds/consents/`) | **legal counsel + clinical lead** |
| Runbook basis→uk patient-explanation wording review | **clinical lead** |
| Runbook second-engineer walkthrough (manual erasure on a scratch stack) | **engineering (non-author)** |

## Recorded deviations (all with rationale in code/docs)

1. `mdx_erasure` is a LOGIN role, not NOLOGIN+SET ROLE (escalation-path
   avoidance; crypto_writer precedent).
2. DSAR download is an authenticated decrypt-and-stream endpoint, not a
   presigned URL (platform rule: presigned serves ciphertext).
3. DSAR recovery is on-request stale-takeover, not a startup scan
   (tenants are RLS-invisible to app_role by design).
4. Per-artifact erasure audit events are emitted after each delete
   transaction commits (only audit_writer may INSERT audit rows);
   `erasure.executed` is exactly-once.
5. Consents are ALWAYS retained (step-07 mandate supersedes step-05's
   signed-only rule).
