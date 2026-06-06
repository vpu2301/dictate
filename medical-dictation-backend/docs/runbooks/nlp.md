# Runbook — nlp-service

Operational guide for the sprint-05 NLP post-processing pipeline.

## Key paths

| Concern               | Path / command                                              |
| --------------------- | ----------------------------------------------------------- |
| Service code          | `services/nlp-service/`                                     |
| Pipeline version      | constant `PIPELINE_VERSION` in `nlp_service.__init__`       |
| Voice command seed    | `infra/postgres/seed/voice_commands_*.json`                 |
| Abbreviation seed     | `infra/postgres/seed/abbreviations_global.sql`              |
| Cache prefix          | Redis: `mdx:nlp:cache:*`                                    |
| Dashboard             | Grafana → "Sprint 05 — NLP Health"                          |
| Alerts                | `infra/prometheus/rules/sprint-05-nlp.yml`                  |

## Failure modes

### § punctuation-model-unavailable

Symptom: `/readyz` returns 503; `mdx_nlp_punctuation_fallback_total`
incrementing on every call.

1. Check container disk: `kubectl exec -- ls -lh /tmp/hf-cache`.
   If empty or partial, the model download failed.
2. Confirm the bundled model in the image:
   `kubectl exec -- python -c "from transformers import AutoModel; AutoModel.from_pretrained('oliverguhr/fullstop-punctuation-multilang-large')"`.
3. If model is corrupt: rebuild + redeploy the image.
4. Until restored, the fallback path (rule-based) is active —
   F1 drops but the service stays up.

### § high-punctuation-fallback-rate

Symptom: `mdx_nlp_punctuation_fallback_total / total_requests > 10%` for 5 m.

Likely: model timed out on long segments (token budget exhausted by
chunked inference). Investigate `mdx_nlp_request_duration_ms{stage="punctuation"}`.

Mitigation: tune `MDX_NLP_PUNCTUATION_TOKEN_BUDGET` smaller or
`MDX_NLP_PUNCTUATION_TIMEOUT_MS` larger; rebalance.

### § voice-command-false-positive-triage

Symptom: `mdx_nlp_voice_command_undo_rate > 5%` (alert).

1. Pull last 100 `voice_command.executed` audit rows:
   ```sql
   SELECT payload->>'intent', COUNT(*)
   FROM audit.events
   WHERE kind='voice_command.executed'
     AND created_at > now() - interval '1 hour'
   GROUP BY 1 ORDER BY 2 DESC;
   ```
2. For the noisiest intent, sample 10 sessions: replay their audio,
   review with the clinical content lead.
3. Tune options:
   - Bump `requires_pause_before_ms` for the command (DB seed update).
   - Bump `min_avg_probability` for the command.
   - Remove the command from the catalogue if false-positive economics
     are unfavourable.
4. Update DB seed + re-run `scripts/seed/seed_voice_commands.py`.

### § abbreviation-dictionary-empty

Symptom: every NLP request's `abbreviation.snapshot_fingerprint`
matches the empty-table fingerprint; users notice abbreviations not
applied.

1. Check the table:
   `psql -c "SELECT COUNT(*) FROM abbreviation_dictionary"`.
2. If empty, re-run the global seed:
   `psql -f infra/postgres/seed/abbreviations_global.sql`.
3. If non-empty but RLS is blocking, check
   `current_setting('app.tenant_id')` is set on the client connection.

### § idempotence-violation

Symptom: `mdx_nlp_idempotence_violations_total > 0` (pages immediately).

This is a correctness bug — same input + same context produced
different output across a cache hit vs fresh run.

1. Capture the inputs from the structured log entry
   (`nlp.stage_failed` or `nlp.cache_mismatch`).
2. Reproduce: same body, same `Idempotence-Key` header.
3. Bisect by stage — disable stages one at a time via env (e.g.,
   `MDX_NLP_PUNCTUATION_DISABLED=true`); the first stage that, when
   disabled, makes the bug disappear is the culprit.
4. Most likely sources: dict iteration order (only on Python < 3.7,
   we're on 3.12 so this is moot), float ops, time-based defaults.

### § cache-hit-ratio-dropped

Symptom: `mdx_nlp_cache_hit_ratio` ~0 sustained.

Either the `PIPELINE_VERSION` was bumped (expected — cache invalidated
on purpose) or Redis is unhealthy. Check Redis health first.

### § per-tenant-rate-limit-hits

Symptom: `mdx_nlp_rate_limit_total{scope="tenant"}` non-zero.

A tenant is exceeding 1000 rps. Either:
- Legitimate spike: raise the limit per env via
  `MDX_NLP_RATE_LIMIT_PER_TENANT_RPS`.
- Abusive client: investigate; the dictation/asr-service call
  patterns can't realistically exceed this limit, so it's almost
  always misconfigured client retries.
