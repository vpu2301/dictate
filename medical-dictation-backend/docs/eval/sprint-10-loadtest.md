# Sprint-10 Autocomplete Load Test

## Setup

```bash
# 1. Apply migrations through 0026 (system corpus seed).
# 2. Optionally add per-tenant phrases to exercise the user-scope cache.
psql "$DB_APP_ROLE_DSN" -At -c \
  "INSERT INTO autocomplete_phrases (tenant_id, owner_user_id, phrase, language, source) \
   SELECT t.id, NULL, p, 'uk', 'tenant' FROM tenants t \
   CROSS JOIN (VALUES ('задишка варіант 1'),('задишка варіант 2')) AS x(p);"

# 3. Run k6.
k6 run --vus 50 --duration 10m \
  -e BASE_URL=https://api.dev.example -e BEARER="$JWT" \
  scripts/loadtest/sprint-10-k6.js
```

## Thresholds (spec §10)

| metric                             | target       | source            |
| ---------------------------------- | ------------ | ----------------- |
| suggest p95 (cache hit)            | ≤ 80 ms      | k6                |
| suggest p95 (cache miss)           | ≤ 250 ms     | k6 + Prom         |
| cache hit ratio (steady state)     | ≥ 95%        | Prom              |
| trie build p95                     | ≤ 100 ms     | Prom              |
| 500 RPS burst                      | 0× 5xx       | k6                |
| cold-start storm (1000 RPS)        | recovery <30 s | k6              |

## Ranking quality eyeball

Take 50 anonymised real prefixes from the telemetry table; for each:

1. Issue `POST /autocomplete/suggest`.
2. Clinical content lead rates the top-3 as
   useful / neutral / harmful.
3. Target: ≥ 80% useful in top-3; 0 harmful.

Eyeball results recorded at
`docs/eval/sprint-10-ranking-eyeball.md` (created after pilot data
collection).

## Cold-start storm methodology

```bash
redis-cli -n 0 KEYS 'autocomplete:trie:*' | xargs redis-cli -n 0 DEL
k6 run --vus 200 --duration 30s \
  -e BASE_URL=... -e BEARER=... -e RATE=1000 \
  scripts/loadtest/sprint-10-k6.js
```

Expected behaviour:
- Per-tenant lock (`autocomplete:trie:...:lock`) is held by exactly
  one builder.
- Other VUs wait up to 200 ms then read the populated cache.
- p95 spikes briefly during the rebuild; recovers within ~30 s.

## Sprint-10 ship criteria

- All thresholds above met in dev.
- Eyeball test ≥ 80% useful, 0 harmful (clinical content lead).
- Scrubber 100% redacts the day-6 PII corpus.
- Roll-up + cache invalidation flow exercised end-to-end on a synthetic
  1k-event day.
