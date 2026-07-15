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
