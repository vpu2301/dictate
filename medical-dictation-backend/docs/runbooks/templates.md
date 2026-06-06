# Runbook — Templates (report-service sprint-06 slice)

Operational guide for the templates surface.

## Key paths

| Concern | Path / command |
| --- | --- |
| Service code | `services/report-service/` |
| Schema models | `libs/template_models/` |
| Seed files | `infra/seeds/templates/*.json` |
| Seed runner | `python scripts/seed/seed_templates.py` |
| Validator | `python scripts/validate-templates.py` |
| Migration | `0013_create_templates.sql`, `0014_seed_system_templates.sql` |
| Cache | in-process TTLCache, key=(tenant_id, template_id), TTL 60s |
| Dashboard | Grafana → "Sprint 06 — Templates" |
| Alerts | `infra/prometheus/rules/sprint-06-templates.yml` |

## Failure modes

### § cache-miss-storm

Symptom: `mdx_template_cache_hits_total / total < 80%` for 10 min.

Either eviction at the maxsize boundary, or massive cross-tenant fan-in.

1. Check `MDX_TEMPLATE_CACHE_MAXSIZE` (default 5000).
2. If a single tenant has > 1000 templates, raise the per-process cap
   or shard report-service.
3. If cache hit ratio drops after a deploy, suspect a regression in
   the cache key (must include `tenant_id`).

### § system-template-urgent-edit

A system template ships with a clinical bug (typo in ASR prompt, wrong
billing code, etc.). Tenant clones may already exist — those don't
auto-update.

1. Edit `infra/seeds/templates/<code>.json`.
2. Run `python scripts/validate-templates.py` — must pass.
3. PR review by clinical content lead.
4. Deploy + `make seed-templates`.
5. The `upsert_system_template` function detects cosmetic vs
   structural; if structural, a NEW system row is created (clones
   don't migrate; documented contract per E3 in spec).

### § tenant-deprecation-blocked-by-fk

`DELETE /templates/{id}` returns 409 with `detail: "templates referenced by reports cannot be deprecated; ..."`.

Tenant has reports that reference this template. Until sprint-17
admin UI ships a re-bind flow, the workaround is: tenant cannot
deprecate this template. They can still:
- Create a new template (cosmetic edit creates a new row; clinicians
  pick the new one on next session).
- Mark the old row's `status='draft'` to hide from listings (note:
  RLS allows UPDATE; status check is application-level).

### § wer-regression-on-a-template

Nightly WER eval shows a specific section's WER regressed > 2 pp.

1. Identify the section: `mdx_asr_wer_with_section_prompt{section}` regressed.
2. Pull the current ASR prompt from `infra/seeds/templates/<code>.json`.
3. Re-author with linguist + clinical content lead.
4. Redeploy seed; re-run eval.

### § section-switch-latency-spike

`mdx_dictation_section_switch_latency_ms p95 > 200 ms` for 5 min.

The dictation hot path looks up the section prompt in-process from
`SessionContext.template_doc` — no HTTP / no DB. Latency spike here
means the windower's prompt-swap is slow OR the entire window-tick
loop is slow.

1. Check `mdx_asr_inference_seconds` (sprint-03 metric) — if Whisper
   inference is slow, the swap appears slow as a downstream effect.
2. Check GPU saturation.
3. Verify the swap is in fact in-process (no HTTP call on the hot
   path).

### § template-integrity-drift

Weekly cron `template_integrity_check` flags rows whose
`schema_jsonb` no longer validates against `TemplateDefinition`.

Cause: a Pydantic model change without a backfill. Action:
1. List the failing rows: `SELECT id, code, schema_version FROM templates WHERE id IN (...);`.
2. Either backfill (preferred) or roll back the model change.
