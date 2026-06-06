# Sprint-08 Load Test Plan

Sprint-08 day-9 baseline. Captures the actual performance characteristics
of the reports surface at 100k records across 10 tenants and documents
how to reproduce.

## Setup

```bash
# 1. Apply migrations.
uv run alembic upgrade head            # or your migration runner

# 2. Seed 100k reports.
uv run python scripts/loadtest/sprint-08-seed.py \
  --dsn "$DB_APP_ROLE_DSN" --tenants 10 --reports-per-tenant 10000

# 3. Export a list of report IDs for k6 to autosave/diff against.
psql "$DB_APP_ROLE_DSN" -At -c \
  "SELECT id FROM reports WHERE status='draft' ORDER BY random() LIMIT 500" \
  | jq -R . | jq -s . > report-ids.json

# 4. Run k6.
k6 run --vus 200 --duration 5m \
  -e BASE_URL=https://api.dev.example -e BEARER="$JWT" \
  -e REPORT_IDS_JSON=report-ids.json \
  scripts/loadtest/sprint-08-k6.js
```

## Thresholds (spec §6)

| metric                                    | target           | source              |
| ----------------------------------------- | ---------------- | ------------------- |
| search p95 (with `q`)                     | ≤ 250 ms         | k6                  |
| search p95 (filter-only)                  | ≤ 100 ms         | k6                  |
| diff p95 (cold)                           | ≤ 150 ms         | k6 + Prom           |
| diff p95 (cached)                         | ≤ 5 ms           | service log         |
| autosave PUT p95                          | ≤ 200 ms         | k6                  |
| autosave success rate (200/total)         | ≥ 95%            | k6                  |
| burst-finalize 409 rate on same draft     | exactly 49/50    | manual test         |
| 100-parallel POST /v1/reports             | 0 code dupes     | manual test         |
| chain reconciler runtime on 100k          | ≤ 5 min          | logs                |
| RLS subquery cost (EXPLAIN ANALYZE)       | no N+1, GIN hit  | `psql EXPLAIN`      |

## EXPLAIN ANALYZE — search query

Expected plan shape for the FTS query at 100k:

```
Limit  (cost=...) (actual rows=25)
  ->  Nested Loop  (cost=...) (actual rows=25)
        ->  Bitmap Heap Scan on report_versions v
              Recheck Cond: (search_vector @@ plainto_tsquery('simple', $1))
              ->  Bitmap Index Scan on report_versions_search_vector_idx
        ->  Index Scan using reports_pkey on reports r
              Index Cond: (r.id = v.report_id)
              Filter: r.tenant_id = current_setting(...)::uuid
```

If a sequential scan appears on either table the GIN index is missing or
stale — `REINDEX INDEX CONCURRENTLY report_versions_search_vector_idx`.

## Known soft spots

1. **`patient_name_redacted`** is empty in seeded data (resolved by sprint-11);
   the load test exercises empty string FTS — fine, but real workload will
   include patient names in the snippet path.
2. **Burst finalize on same draft**: tested manually with 50 concurrent
   curl commands. The state machine's row-versioned UPDATE returns exactly
   one success.
3. **`total=exact`** is intentionally slow on 100k. Rate-limit at the
   gateway layer (5 RPS per tenant) ensures the slow path can't drag the
   primary down.

## Partition plan (deferred)

If any tenant exceeds 1M reports OR p95 search > 500 ms persistently:

- Partition `reports` and `report_versions` by `(tenant_id, year)`.
- Migration plan documented but unimplemented. Triggered by SRE
  observation, not preemptively.

## Day-9 ship criteria

Sprint-08 ships when:
- All targets above are met in the dev environment.
- EXPLAIN ANALYZE shows GIN hit, no N+1.
- A 24-hour soak at 50 RPS sustains all thresholds.
- The chain reconciler runs in < 5 min on the seeded 100k.
