# deploy/ — S11 privacy-ops deployment (ADR-0028)

Deployment artifacts for the sprint-11 privacy machinery: erasure-role
credential isolation, backups-vs-erasure mechanics, DSAR export storage
lifecycle, and the alert data path. Everything here is opt-in and joins
the running full stack (`docker compose up -d` in the repo root).

```
deploy/
  compose/privacy-ops.yml      erasure-scheduler + dsar-package-cleanup job
                               containers (core-service image, no ports)
  compose/erasure.env.example  template for secrets/erasure.env
  scripts/backup.sh            encrypted pg_dump → mdx-backups (35-day ILM)
  scripts/restore.sh           verify + decrypt + pg_restore + MANDATORY
                               post-restore erasure re-run
  scripts/rerun_erasures.py    the re-run engine driver (runs in the job
                               container via restore.sh)
  secrets/                     gitignored: erasure.env, backup.passphrase
  var/                         gitignored: ledgers/, scratch backup files
```

## Quick start

```bash
mkdir -p deploy/secrets
cp deploy/compose/erasure.env.example deploy/secrets/erasure.env
docker compose -f deploy/compose/privacy-ops.yml up -d   # jobs
deploy/scripts/backup.sh                                  # first backup
```

## Credential posture (proven 2026-07-16)

`DB_ERASURE_DSN` (role `mdx_erasure` — the ONLY identity that can
DELETE PHI rows, migration 0044) lives exclusively in
`deploy/secrets/erasure.env`, injected only into the privacy-ops job
containers. It is deliberately absent from every request-serving
service.

```
=== core-service env ===            DB_ERASURE_DSN: absent ✓
=== erasure-scheduler env ===       DB_ERASURE_DSN=postgresql://mdx_erasure:…@postgres:5432/medical_dictation

-- as app_role:
SET app.tenant_id = '…'; DELETE FROM audio_files;
ERROR:  permission denied for table audio_files

-- as mdx_erasure (tenant-scoped):
DELETE FROM audio_files WHERE id IS NULL;
DELETE 0        -- privilege exercised; no rows matched by design
```

## Backups vs erasure (policy: docs/runbooks/erasure.md)

- `backup.sh`: `pg_dump -Fc` → AES-256-CBC/PBKDF2 inside the postgres
  container → `mdx-backups` bucket. Passphrase auto-generated at
  `deploy/secrets/backup.passphrase` — **escrow it like the master key**.
- `mdx-backups` carries a **35-day ILM expiry** (minio-init) — the
  maximum retention window. The engine stamps
  `report_of_execution.backups_purged_by = executed_at + 35d`.
- **Backup scope is the Postgres dump only** — no bucket is mirrored
  into backups. `mdx-audio-clips` (S15 audio-replay derivatives,
  ADR-0037) is excluded **by policy**: clips are regenerable,
  envelope-encrypted ephemera (5-min Redis registry lifetime, 1-day
  bucket ILM backstop) and must never survive in a backup after their
  source audio is erased.
- `restore.sh` refuses to be a resurrection tool: it captures the
  erasure ledger pre-restore and re-runs every erasure completed after
  the backup through the idempotent engine (`operator=restore-rerun`).

Verified live (2026-07-16, full cycle):

```
patient 63337ea7 + clinical note created
backup mdx-medical_dictation-20260716T195109Z uploaded (sha256=87603d…)
erasure request f7452c89: request(clinician) → review → approve(admin) → sweep
  executed: destroyed=2 retained=1 → patient status=erased, notes=0
  report_of_execution: backups_purged_by=2026-08-20  (= executed_at + 35d)
restore.sh --latest --yes:
  ledger: 4 completed erasure(s); 1 completed after backup → re-run
  re-erased: request=f7452c89 destroyed=2 retained=1   ← data WAS back, destroyed again
final state: patient status=erased / name_uk=ERASED / notes=0
  request: completed / operator=restore-rerun / backups_purged_by=2026-08-20
audit: erasure.executing → 2× erasure.artifact_destroyed → erasure.executed
```

(The 5 ignored `pg_restore --clean` errors are inherited-PK drop noise on
`autocomplete_telemetry_*` partitions; the parent-table drop cascades.)

## DSAR export storage

- `mdx-dsar` bucket: **7-day ILM expiry rule** (object-storage backstop
  under the app-side TTL cron; compose sets `DSAR_PACKAGE_TTL_DAYS=7`).
- Download links: **15-minute HMAC tokens** minted by the status
  endpoint (`DSAR_DOWNLOAD_TOKEN_TTL_SECONDS=900`), verified by the
  authenticated decrypt-and-stream endpoint. Raw S3 presigned URLs
  would serve envelope ciphertext (platform rule 3) — useless to the
  data subject — so the short-TTL property lives on the link.

Verified live: ILM rules listed Enabled (7d / 35d); fresh link →
`HTTP 200 application/zip`; missing token, forged-expiry token, and the
same real link after its 15-minute window → `403 download_link_expired`.

## Alerts (infra/prometheus/rules/sprint-11-privacy.yml)

The privacy-ops job containers set
`OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317`, closing the
gap where the ops-host cron pushed gauges to localhost and the alerts
stayed dataless. `DsarExportFailed` (added this sprint) fires on
**unresolved** failures only (`mdx_privacy_dsar_failed_unresolved_count`)
— `failed` rows are permanent history and a recovered patient gets a
new completed request.

Verified live: all 5 rules load; a fixture erasure forced to
`executing` 2 h ago pushed gauge `7202` and `ErasureRequestStuckExecuting`
went `firing`; after recovery re-triggers, `DsarExportFailed` cleared
(unresolved gauge `0`) while lookback-window alerts decayed as designed.

## Production notes

- Dev credentials/passwords in these files mirror `init.sql`; production
  injects real ones through the same env-file seams and replaces the
  `DATABASE_URL` superuser scan DSN with a dedicated ops role.
- MinIO ILM granularity is whole days; the deletion sweep runs on the
  MinIO scanner's schedule, not at the exact hour boundary.
- Alertmanager is still not deployed (alerts render in the Prometheus
  UI); routing/paging is unchanged scope — see sprint-16 backlog.
