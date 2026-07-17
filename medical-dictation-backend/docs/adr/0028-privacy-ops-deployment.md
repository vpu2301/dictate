# ADR-0028 — Privacy-ops deployment: erasure job isolation, backups vs erasure, export lifecycle

Date: 2026-07-16 · Status: accepted · Sprint: 11 (deployment)

## Context

S11 introduced erasure and DSAR engines whose guarantees are partly
infrastructural: the `mdx_erasure` role's DELETE grants (migration 0044)
need a credential that request-serving services never hold; deleting
from the live DB does not delete from backups; and DSAR packages need
bounded lifetimes in object storage. The dev/pilot stack had none of
this: no backup mechanism existed at all, the erasure scheduler was an
ops-host cron whose OTLP gauges silently defaulted to `localhost`
(leaving the sprint-11 alerts dataless), and the only DSAR expiry was an
app-side cron. The cron files' "a job container would need an ADR
amendment" note is settled here.

## Decision

1. **A separate compose project, `deploy/compose/privacy-ops.yml`**,
   joins the running stack's network and is the ONLY runtime holding
   `DB_ERASURE_DSN` (from gitignored `deploy/secrets/erasure.env`).
   It runs the erasure scheduler (15-min loop) and the DSAR package
   cleanup (daily loop), reusing the built core-service image — same
   code, different credentials, no listening port. Request-serving
   services must never carry the erasure DSN; the base compose stays
   infra-only, the root override stays the app stack, privacy ops are
   an explicit third opt-in.
2. **Backups become real and erasure-aware**: `deploy/scripts/backup.sh`
   writes AES-256-encrypted `pg_dump` archives to a new `mdx-backups`
   bucket carrying a **35-day ILM expiry** — the maximum retention
   window. The engine stamps `backups_purged_by = executed_at + 35d`
   into every `report_of_execution`; `deploy/scripts/restore.sh`
   captures the erasure ledger before overwriting the DB and
   mandatorily re-runs post-backup erasures through the idempotent
   engine (`rerun_erasures.py`, operator `restore-rerun`).
3. **DSAR export lifetimes are layered**: 7-day bucket ILM expiry on
   `mdx-dsar` (backstop) under the app-side TTL cron
   (`DSAR_PACKAGE_TTL_DAYS=7` in compose), and download links become
   15-minute HMAC tokens (`DSAR_DOWNLOAD_TOKEN_TTL_SECONDS`) minted by
   the status endpoint — the honest equivalent of "presigned at 15
   minutes" given rule 3 (presigned URLs serve ciphertext), keeping the
   authenticated decrypt-and-stream path from step 06.

## Consequences

- Two-direction privilege proof becomes a deployment invariant:
  `app_role` cannot DELETE PHI rows anywhere; only the privacy-ops
  containers can, and only with the env file present.
- The 35-day window is a promise to data subjects — shrinking it is
  safe, growing it extends every already-reported completion horizon
  and needs DPO sign-off.
- MinIO ILM does the final deletion of both backups and expired DSAR
  ciphertext; the app-side cron remains the audited, primary path for
  DSAR (ILM deletions themselves are not in `audit.events`).
- The ops-host cron files under `infra/compose/cron/` remain valid for
  non-compose deployments; compose deployments should prefer the
  privacy-ops project so the OTLP gauge path and credentials are wired
  by construction.
