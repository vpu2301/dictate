# Runbook — DSAR export & right to erasure (S11)

> Step 06 ships the DSAR half; step 07 adds the erasure-execution half
> (this file is shared and extended there). Architecture:
> `docs/architecture/erasure.md`.

## DSAR export (GDPR Art. 15 / Law 2297-VI)

### Flow

1. Tenant admin (only role with `patient.dsar`) →
   `POST /patients/{id}/dsar` → `202`, engine runs as an in-service
   background task (states `requested → executing → completed | failed`).
2. Poll `GET /privacy-requests/{id}` — when `completed` it returns
   `download.url` (+ `expires_at`) and `manifest_summary`.
3. `GET /privacy-requests/{id}/download` streams the decrypted ZIP.
   **Authenticated, not presigned**: presigned URLs serve ciphertext
   (platform rule), so the package is decrypted through the envelope
   path under the same `patient.dsar` gate. Every mint AND every
   download is a sec-severity audit event.

### Operator knobs (env, core-service)

| setting | default | meaning |
|---|---|---|
| `DSAR_AUDIT_KINDS` | patient/consent/privacy lifecycle kinds | The subject-accessible audit slice — **the DPO's knob**. Operator/security internals stay excluded; widening it is a policy decision, not code. |
| `DSAR_INCLUDE_RAW_AUDIO` | `false` | Raw recordings in the package (streamed through envelope decrypt). Off = the manifest + README say "available on request" — never a silent gap. |
| `DSAR_INCLUDE_RAW_IPN` | `false` | Raw ІПН in `patient.json` — only possible when `PATIENT_IPN_RAW_ENABLED` captured a ciphertext. |
| `DSAR_STALE_MINUTES` | 30 | An `executing` export older than this is presumed dead; the next POST takes it over and re-runs (idempotent — the object is replaced). |
| `DSAR_PACKAGE_TTL_DAYS` | 14 | The cleanup cron (`scripts/jobs/dsar_package_cleanup.py`, `infra/compose/cron/dsar-package-cleanup.cron`) deletes the ZIP and stamps `package_deleted_at`; downloads then answer `410 package_expired`. |

### Answers for the front desk

- **"The patient was erased — can I export?"** No: `409 patient_erased`.
  A DSAR after erasure is definitionally empty; the erasure execution
  report (`report_of_execution` on the erasure request) is the document
  of record.
- **"Where's the audio?"** Excluded by default; the manifest's
  `excluded` section and the README say so explicitly. Flip
  `DSAR_INCLUDE_RAW_AUDIO` per DPO decision.
- **"The download says expired."** The package auto-deletes after
  14 days. Trigger a fresh export — it rebuilds from live data.
- **"Export stuck in executing?"** POST again after
  `DSAR_STALE_MINUTES`; the engine takes the stale request over.

### Package anatomy

`manifest.json` is the contract: `format_version`, per-file sha256,
`inventory_counts` from the fan-out map, and `excluded` naming every
omission with its reason. The README (uk + en) explains the structure
to the patient. Reports appear as pretty-printed JSON (current +
full amendment history) plus the signed PDF when one is stored;
consents carry their КЕП verification token and `/verify` path.

### Why no queue

DSAR volume is per-request-tiny; the engine is an in-service
`asyncio` task with DB-state recovery (stale-takeover). A Redis-stream
worker would add an infra dependency for no throughput need — revisit
only if exports start timing out under real load.

## Erasure execution (step 07)

### Flow (the clinic's seat)

request (`patient.write`) → **second-person** approval
(`privacy.approve`, never the requester) → grace period
(`ERASURE_GRACE_DAYS`, default 7 — rejectable/cancellable throughout) →
automatic execution (cron every 15 min) → a readable
`report_of_execution` on the request:
*"destroyed: recording, transcript, dictation session, draft report,
identity data; retained: 1 signed clinical report
(retention:clinical_record_signed), N consent records
(retention:consent_record), the privacy requests themselves
(retention:erasure_paper_trail)."*

### Operator procedures

- **Supervised manual run** (same advisory lock as cron — double-running
  is impossible):
  `uv run --project services/core-service python -m core_service.erasure.run --tenant <tid> --request <rid>`
- **A run failed?** The request stays `executing` with `last_error` on
  the row. The recovery requires no judgment: **run it again.** Every
  eraser tolerates already-gone; the re-run completes the inventory and
  `erasure.executed` fires exactly once, at completion.
- **Grace not elapsed** → refusal `grace_period_active` (cron skips it
  silently until due).
- **Patient already erased on a fresh request** → refusal; reject the
  request with a reason instead.

### Answers for the DPO

- **"Prove the audio is gone."** The battery's own proof: the MinIO
  object 404s, and the row that carried its wrapped DEK is deleted —
  crypto-shred per ADR-0027. `report_of_execution.destroyed[]` lists
  every artifact by id.
- **"Why does a signed report survive?"** Statutory retention
  (`REPORT_RETENTION_YEARS`): it is *reported* as retained with its
  legal basis — never silently skipped. Outside the window it is
  destroyed together with its envelope.
- **"Does the retained report still verify?"** Yes — `/verify/{token}`
  on a legally retained record is unaffected by its subject's erasure;
  the envelope binds the report content, not the roster row.
- **"Does erasure damage the audit log?"** Never. The chain is
  append-only, the engine writes THROUGH it (every destruction is an
  event), and the chain verifier passes end-to-end after every erasure
  — asserted in the step-07 battery.
