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

### Telemetry partition full

- Monthly cron creates next partition. If it didn't run, manually
  invoke:
  ```bash
  uv run python -c \
    "import asyncio; from autocomplete_service.jobs.partition_rotation import \
    run_forever; asyncio.run(run_forever(interval_seconds=0))"
  ```
- Or run the SQL directly from
  `autocomplete_service.repository.create_next_telemetry_partition`.

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
