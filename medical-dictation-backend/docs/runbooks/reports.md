# Runbook — Reports

Sprint-08 ships the central clinical artifact. This runbook lists the
operational fault-modes and their playbooks.

## Health checks

- `GET /healthz` on report-service returns 200 + JSON with db pool
  status.
- Grafana: `sprint-08-reports` dashboard.
- Daily reconciler: cron 04:30 UTC; logs to `report-service/chain-reconciler`.

## Incident playbooks

### High autosave conflict rate

Alert: `ReportAutosaveConflictRateHigh` (> 5% of PUTs returning 409
for 10 minutes).

Likely causes (ordered):
1. **FE protocol drift** — a FE update changed the autosave cadence
   or stopped sending `expected_version` correctly. Check the FE
   release log; coordinate with frontend lead.
2. **Clock skew** — autosaves arriving out-of-order due to retry
   logic interpreting timestamps incorrectly. Check `mdx_reports_autosave_latency_ms`
   for tail spikes.
3. **Two clinicians editing same draft** — sprint-08 doesn't
   support multi-author concurrent edit; conflict is the correct
   surface to the FE.

Mitigation:
- If protocol drift: roll back the FE.
- If genuine concurrent edit: educate; defer to sprint-future
  collaborative-editing work.
- If clock skew: investigate FE caching layer.

### Search performance issue

Alert: `ReportSearchLatencyHigh` (p95 > 500ms for 5 min).

1. SSH into a replica, `EXPLAIN ANALYZE` the slow query (use
   `pg_stat_statements` for the actual SQL).
2. If sequential scan appears on `report_versions.search_vector`:
   `REINDEX INDEX CONCURRENTLY report_versions_search_vector_idx;`
3. If GIN hit but still slow: check tenant has hit > 1M reports.
   See ADR-0021 for the partition trigger.
4. If RLS subquery showing N+1: the `EXISTS` predicate should push
   into the join. Investigate any recent migration that re-wrote the
   policy.

### Version chain break

Alert: `ReportChainIntegrityFailure` (critical; pages security lead).

**DO NOT auto-repair.** This is potentially a forensic event.

1. Pull the row from `audit.report_chain_failures` keyed by the alert
   payload's `report_id`.
2. Run `scripts/admin/report_chain_repair.py --report-id <uuid>` to
   dump the chain + history (read-only).
3. Open the security incident in the tracker.
4. Convene tech lead + DBA + security lead before any DB-level edit.
5. Manual repair: a single UPDATE with full notes in the incident
   record + manual hash-chained audit append.

### Code generation race

Symptom: two reports with identical `code`.

The advisory lock should make this impossible. If observed:
1. Check `pg_locks` for `pg_advisory_xact_lock` acquisition.
2. Confirm `report_code_counters` uniqueness constraint blocked the
   duplicate INSERT — only one of the two `RETURNING id` would have
   succeeded.
3. If somehow both succeeded, escalate to DB integrity incident.

### Stuck draft (> 30 days)

Idle-draft cleanup auto-archives at 30 days (`MDX_IDLE_DRAFT_DAYS`).
Since sprint 16 it runs in-process when `MDX_BACKGROUND_JOBS=true`
(interval `MDX_BACKGROUND_JOBS_INTERVAL_S`, default daily; ADR-0041),
or on demand:
`uv run --project services/report-service python -m report_service.jobs.idle_draft_cleanup`.
Each run audits `scheduler.job.completed` (global tenant) and
`report.cancelled` per archived draft.
For an urgent manual archive:

```sql
UPDATE reports
SET status='cancelled', cancelled_at=now(),
    cancelled_reason='manual_archive: <ticket>'
WHERE id=$1 AND status='draft';
```

Re-open within 90 days: the version chain is intact; INSERT a new
draft version and UPDATE `status='draft', cancelled_at=NULL`. Audit
this as `report.draft.updated` with payload `{manual_reopen: true}`.

## Operational tunables

| envvar / setting                              | default | purpose                                      |
| --------------------------------------------- | ------- | -------------------------------------------- |
| `MDX_AUTOSAVE_RATE_LIMIT_SECONDS`             | 5       | per-draft autosave minimum interval         |
| `MDX_REPORT_DIFF_CACHE_MAX_ENTRIES`           | 1024    | in-process LRU                              |
| `MDX_REPORT_CODE_NAMESPACE`                   | "REPO"  | advisory-lock namespace; never change       |
| `MDX_CHAIN_RECONCILER_BATCH_SIZE`             | 1000    | reports per batch in daily cron             |

## Secrets

None new in sprint-08. Sprint-09 will introduce signing-key material.

## Sprint-08 wrap

This runbook is the operational contract for the reports surface. If
a playbook step turns out wrong in practice, update this file in the
same PR as the fix.

## audio-clip-failures

`AudioClipFailuresHigh` (sprint 15, ADR-0037): the decrypt→slice→encode
pipeline on `POST /v1/audio-clips` is erroring (`outcome="pipeline_error"`,
502s to callers). 410s are NOT failures — they are the honest retention
answers (`no_audio_source` / `audio_not_retained` / `audio_erased` /
`audio_partially_retained`).

1. Is ffmpeg present in the report-service image? (`MDX_FFMPEG_PATH`,
   Dockerfile installs it since S15.) A missing binary fails EVERY clip.
2. `mdx_audio_clip_pipeline_latency_ms` p95 climbing toward the ffmpeg
   timeout → the source objects are huge (long sessions) or the host is
   CPU-starved; the whole-object GCM decrypt (~2 MB/min of audio) is
   expected cost, not a leak.
3. Corrupt source WAV (`unexpected WAV layout` in logs): the session was
   written by a pre-S04 build or the object was truncated — check
   `audio_files.sha256` against the object.
4. MinIO lifecycle: clips live 5 min (Redis registry) with a 1-day
   bucket ILM backstop on `mdx-audio-clips`; a full bucket is never the
   explanation — check the ILM rule survived a `minio-init` re-run.
