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

Idle-draft cleanup auto-archives at 30 days (sprint-16 scheduler).
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
