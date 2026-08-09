# ADR-0041: Scheduled jobs — one shared in-process runner per service, CLI twin for cron

Date: 2026-08-08
Status: Accepted
Sprint: 16

## Context

Three sprints left standing IOUs for a scheduler: idle-draft cleanup
(sprint 08, report-service), telemetry cold-archive before partition
drop (sprint 10, autocomplete-service), erasure backup-horizon notices
(sprint 11, core-service). The spec sketch said "a single lightweight
scheduler runner … hosting" all three — but one scheduler *process*
would have to import three services' job code, which the import-linter
contracts forbid (libs never import services; services never import
each other). The repo already holds two working precedents:

- in-process `asyncio` loops behind `MDX_BACKGROUND_JOBS`
  (autocomplete, sprint 10 verification);
- standalone CLI entrypoints for external cron
  (`scripts/jobs/*`, report-service chain reconciler — sprints 08/11).

## Decision

"One pattern" = **one shared runner, hosted per service**:

- `observability.run_periodic(job_name, interval_seconds, fn)` owns the
  loop mechanics and the metric contract:
  `mdx_scheduler_job_runs_total{job, outcome}`,
  `mdx_scheduler_job_duration_seconds{job}`. (It lives in the leaf
  observability lib, so it cannot write audit rows itself.)
- Each job writes its own per-run audit row
  (`scheduler.job.completed/failed`) under the **reserved global tenant**
  (nil UUID, migration 0068) — fleet-level runs belong to no clinical
  tenant. Domain events (report.cancelled,
  erasure.backup_horizon_reached) stay under the owning tenant.
- Hosting: service lifespan task behind `MDX_BACKGROUND_JOBS`
  (+ `MDX_BACKGROUND_JOBS_INTERVAL_S`), **off by default in dev**
  except autocomplete, which has run its rotation in-process since
  sprint 10 (existing behaviour preserved). Every job is also a
  `python -m` CLI so ops can run it from external cron instead.
- Every job is **idempotent** (first iteration fires at startup;
  re-runs converge): archive keys overwrite; `status='draft'` and the
  `backups_purged_confirmed_at` presence check are the guards.
- Cross-tenant enumeration uses the SECURITY DEFINER
  `active_tenant_ids()` (migration 0071) — the 0027/0036/0037
  precedent — because `tenants` is RLS-FORCEd to self-select.
- **Cold-archive fail-safe**: with the archive flag on, a failed
  archive write BLOCKS the partition drop (retried next run). With it
  off, the pre-sprint-16 destructive drop stands (dev default).

## Consequences

- Multi-replica deployments of a service run the loop in each replica;
  idempotence makes that safe, merely redundant. If replica counts grow,
  move the CLI twins onto a cron container and switch the flag off —
  zero code change.
- The sprint-08 chain reconciler stays a CLI (unchanged scope); its
  latent "tenants read under RLS sees nothing" warning now has a fix
  available (`active_tenant_ids()`), noted for its next touch.
