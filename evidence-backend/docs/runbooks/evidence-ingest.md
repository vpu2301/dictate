# Runbook — evidence-ingest

## Bring-up (dev, Mac)

```bash
cd ../medical-dictation-backend && make dev-up && make migrate-up && make seed
cd ../evidence-backend
make evidence-up          # opensearch + clamav + eva-corpus bucket (joins the platform network)
make run-model-gateway    # :8015 — first start downloads BGE-M3 (~2.3 GB) into the HF cache
make run-ingest-api       # :8010 ops API (optional; the CLI works without it)
make run-ingest-worker    # queue worker (optional; the CLI runs the pipeline inline)
```

Dev shortcut: `EVA_VIRUS_SCAN_ENABLED` defaults to false locally (startup
WARNING). The compose entry enables it against the clamav container.

## Operator CLI

```bash
uv run --project services/evidence-ingest evidence-ingest \
    ingest dir /path/to/docs --set license_class=public_domain --set source_authority=national
uv run --project services/evidence-ingest evidence-ingest snapshot global-v1
uv run --project services/evidence-ingest evidence-ingest retractions feed.csv
uv run --project services/evidence-ingest evidence-ingest stats
```

Default tenant is `global` (the reserved nil uuid); pass `--tenant <uuid>`
for tenant corpora. Re-running an ingest is safe: idempotence keys make
unchanged files no-ops, changed files become new document versions.

## Failure triage

| Symptom | Where to look | Fix |
|---|---|---|
| job `dead` | `GET /ingest/jobs/{id}` → `errors[]`, or `ingest_errors` table | fix input; re-submit (new content hash ⇒ new job) |
| job stuck `embedding` | gateway `GET :8015/readyz` | start/restart the gateway; job resumes at the pending-chunk batch |
| `snapshot_dirty` 409 | `stats` → `jobs_by_state` | drain or dead-letter pending jobs, retry |
| quarantine grows | `quarantine` table, audit kind `evidence.document_quarantined` | knowledge_admin reviews via `POST /quarantine/{id}/decision` |
| OpenSearch red | `curl :9200/_cluster/health` | restart container; lexical writes retry on next job run; snapshot blocked meanwhile |
| clamd unreachable & scanning enabled | ingest fails closed at parsing | restore clamav or (dev only) disable the flag |

## Invariants worth remembering

- Unlicensed (`restricted`) documents index but never ship in a snapshot.
- `answer`-side tables are untouched by this service; corpus tables accept
  global rows only when the operator scopes to the nil uuid.
- OpenSearch is a projection; Postgres is the source of truth (ADR-0003).
