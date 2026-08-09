# Runbook — Autocomplete

## Health checks

- `GET /healthz`.
- Grafana `sprint-10-autocomplete` dashboard.
- Roll-up nightly at 03:30 UTC; success recorded in
  `autocomplete_rollup_progress` + `autocomplete.rollup.completed`
  audit event.

## Incident playbooks

### Suggest p95 latency spike

Alert: `AutocompleteSuggestLatencyHigh` (p95 > 150 ms for 5 min).

1. Check cache hit ratio panel — if dropped, see "Cache hit ratio drop".
2. Check `mdx_autocomplete_trie_build_ms_histogram` — if elevated, the
   corpus may have grown beyond 15k for a heavy tenant. Investigate.
3. Check Postgres for slow queries on `autocomplete_phrases`.

### Cache hit ratio drop

Alert: `AutocompleteCacheHitRatioLow` (< 80% for 10 min).

1. Verify Redis memory not at the eviction watermark.
2. Check for an unexpected `version_tag` flood (mass-write event).
3. Check TTL config (default 3600 s); a shorter TTL → lower hit ratio.

### Manual trie rebuild

```bash
redis-cli DEL "autocomplete:trie:<tenant_id>:*"
redis-cli INCR "autocomplete:tenant_phrase_version:<tenant_id>"
```

Next request rebuilds.

### Roll-up failure

1. Check the job logs (`autocomplete-service/rollup`).
2. Verify yesterday's partition has rows
   (`SELECT COUNT(*) FROM autocomplete_telemetry WHERE ...`).
3. Re-run by setting `day` arg to yesterday's ISO date and invoking
   `rollup.rollup_all(day=...)`.
4. The `autocomplete_rollup_progress` row makes the re-run idempotent.

### PII scrubber spike

Alert: `AutocompleteScrubberRedactionSpike` (> 1/s for 15 min).

- Investigate whether a particular UX flow is leaking PII into
  prefixes (e.g. FE field that auto-fills with patient identifiers).
- Clinical content lead reviews tenants with elevated rates.

### Phrase-write PII rejection spike

Alert: `AutocompletePhraseWritePiiRejectionSpike` (> 20 / hour).

- Possible misuse pattern. Pull `autocomplete.phrase.write_rejected_pii`
  audit rows; investigate per-user.
- If legitimate confusion (clinician didn't realise the field is
  shared), surface a UX improvement to the FE team.

### Telemetry partition missing / full

Symptom: `telemetry.batch_insert_failed: no partition of relation
"autocomplete_telemetry" found for row` warnings; telemetry rows
silently dropped (the endpoint still returns 204).

- The in-process maintenance loop (`MDX_BACKGROUND_JOBS`, on by
  default) ensures the **current and next** month partitions at
  startup and daily thereafter. A service restart therefore self-heals
  this condition.
- To create the partitions without restarting:
  ```bash
  uv run --project services/autocomplete-service python - <<'PY'
  import asyncio
  from db import create_pool
  from autocomplete_service.config import settings
  from autocomplete_service.jobs.partition_rotation import ensure_partitions

  async def main() -> None:
      pool = await create_pool(
          settings.db_app_role_dsn,
          application_name="manual-partition-fix",
          min_size=1, max_size=1,
      )
      try:
          print(await ensure_partitions(pool))
      finally:
          await pool.close()

  asyncio.run(main())
  PY
  ```
- Or run the SQL directly from
  `autocomplete_service.repository.create_next_telemetry_partition`.
- Note: rows that failed while the partition was missing are **lost**
  (fire-and-forget path) — expect a gap in the roll-up for that window.

### "Why didn't it learn immediately?"

Intra-day ranking is **static by design**: accepts recorded today reach
the counters only at the nightly roll-up (in-process job; first
iteration also runs at service startup), which then bumps the tenant
version_tag so the next request rebuilds the trie. A phrase accepted
today ranks noticeably higher **tomorrow** — tell the clinician this is
expected, not a bug.

### Manual roll-up re-runs — the progress-table guard

Counters are **monotonic increments**, not recomputations. The
`autocomplete_rollup_progress` row per (tenant, day) is the only thing
preventing double-counting — NEVER bypass it. Re-run a day
(`python -m autocomplete_service.jobs.rollup --day YYYY-MM-DD`) only
after deleting the progress row, and ONLY if the original run is known
to have failed **before** updating counters. Since 0040,
`last_accepted_at` uses GREATEST, so old-day re-runs cannot move it
backwards.

### Telemetry partition retention (90 days)

Rotation (in-process, daily) ensures current+2 months of partitions and
DETACH+DROPs partitions whose range ended > 90 days ago (via the
`autocomplete_drop_telemetry_partition` SECURITY DEFINER fn, 0040).
Dropped partitions are logged by name
(`partition_rotation.partition_dropped`). **Cold-storage archival
before drop shipped in sprint 16: set
`MDX_TELEMETRY_COLD_ARCHIVE_ENABLED=true` and rotation writes the
partition as gzip JSONL through `EncryptedObjectStore` to
`S3_TELEMETRY_ARCHIVE_BUCKET` (default `mdx-telemetry-archive`, global
tenant envelope) BEFORE dropping; an archive failure blocks the drop
until the next run. With the flag off (dev default) the drop is still
destructive.**

### Trie memory / marisa-trie upgrade trigger (ADR-0025)

The in-memory trie is a plain Python dict. **Any tenant crossing 50k
phrases triggers the swap to `marisa_trie.RecordTrie`** behind the same
`TenantTrie.candidates_for()` seam — the dependency already ships, the
swap is mechanical. Watch `mdx_autocomplete_trie_size_bytes` (histogram,
observed at serialize time); sustained growth there is the early signal.

### Redis down / degraded path

Suggest keeps answering with Redis unavailable: every Redis error routes
to the degraded direct-DB build (no caching), logged throttled (30 s) as
`trie_cache.redis_unavailable_degraded` and counted in
`mdx_autocomplete_degraded_total{reason}`. Expect p95 to rise to the DB
range (~25–40 ms + overhead) — that is degraded-by-design, not an outage
of the feature.

### Cache / Redis operations

- **Safe trie flush** (forces rebuilds; NEVER FLUSHALL — the Redis is
  shared with auth/signing rate-limit keys):
  ```bash
  docker exec medical-dictation-redis-1 sh -c \
    "redis-cli --scan --pattern 'autocomplete:trie:*' | xargs -r redis-cli del"
  ```
- **vtag inspection** (per-tenant version counter the roll-up bumps):
  ```bash
  docker exec medical-dictation-redis-1 redis-cli \
    get "autocomplete:tenant_phrase_version:<tenant_uuid>"
  ```
- TTLs: trie blobs + tags 3600 s (`MDX_TRIE_CACHE_TTL`); build lock
  10 s; lock-lost poll 200 ms. All in `trie/cache.py`.

### Corpus drops (the ~10k authoring pipeline)

Clinical lead authors CSV/JSON → `scripts/validate-autocomplete-corpus.py`
(PII + shape gate) → engineering `--emit-sql` → migration PR → clinical
sign-off. Full contract: `infra/seeds/autocomplete/README.md`. The
`make check-autocomplete-corpus` CI gate re-validates committed files.

## Alerts

Rules: `infra/prometheus/rules/sprint-10-alerts.yml` (the ONLY loaded
location — `/etc/prometheus/rules/*.yml`). **No Alertmanager routing
exists yet**: alerts appear in the Prometheus UI only; paging is a
deployment-sprint concern — do not assume anyone is paged.

**Accepted substitution** (vs the sprint doc's alert list): the
"trie size" and "telemetry buffer overflow" alerts are dashboard
capacity-watch panels instead; their alert slots went to the two
PII-control spike alerts — capacity is a trend, PII spikes are
security signals.

### alert-suggest-latency

p95 (cache-hit path) > 150 ms for 10 m. First commands: check the
cache hit-ratio gauge → `docker logs medical-dictation-redis-1` /
`redis-cli ping` → degraded counter by reason
(`mdx_autocomplete_degraded_total`). Degraded-dominant → Redis;
hit-dominant latency → check DB and trie build duration.

### alert-cache-hit-ratio

Hit ratio < 80% for 15 m. Causes: Redis restart/eviction (cold keys),
version_tag churn (phrase-write storms bump per write), lock storms
(many distinct (tenant,lang,user) tuples cold at once). Check
`trie_cache.*` warnings in service logs; ratio recovers within one
TTL (3600 s) after the cause clears.

### alert-scrubber-spike

Redaction rate 4× day-over-day AND > 0.05/s: the SPA is very likely
sending field contents (not the typed token) in telemetry prefixes.
Check the FE prefix extraction (src/autocomplete/prefix.js) and recent
SPA deploys. DPO owns the pattern set — see
docs/security/autocomplete-pii-scrubber.md.

### alert-pii-rejections

Phrase-write rejections > 0.02/s for 15 m: users pasting patient data
into saved phrases. Pull `autocomplete.phrase.write_rejected_pii`
audit rows (payload has pattern classes + field, never text);
investigate per-user; likely a UX/training signal.

### alert-rollup-missed

Roll-up age > 26 h (pages). The job runs in-process
(MDX_BACKGROUND_JOBS) daily + at startup. First commands: check
service logs for `rollup.iteration_failed`; run manually
`uv run --project services/autocomplete-service python -m
autocomplete_service.jobs.rollup` (yesterday) — the progress table
makes re-runs safe; verify the gauge recovered:
`time() - max(mdx_autocomplete_rollup_last_run_unix_ts)`.

## Operational tunables

| envvar / setting                          | default | purpose                                  |
| ----------------------------------------- | ------- | ---------------------------------------- |
| `MDX_TRIE_CACHE_TTL`                      | 3600    | Per-key TTL                              |
| `MDX_SUGGEST_DEFAULT_LIMIT`               | 3       | Default top-N                            |
| `MDX_SUGGEST_MAX_LIMIT`                   | 10      | Hard cap                                 |
| `MDX_TELEMETRY_FLUSH_S`                   | 5.0     | Buffer flush interval                    |
| `MDX_TELEMETRY_FLUSH_BATCH`               | 100     | Buffer flush size                        |
| `MDX_PHRASE_MAX_PER_HOUR`                 | 100     | Per-user phrase write rate limit         |

## Sprint-10 closure

Update this runbook in the same PR as any operational fix.
