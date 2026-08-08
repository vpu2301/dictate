# Runbook — evidence-retrieval

## Bring-up (dev, Mac)

```bash
make evidence-up            # opensearch (+icu), clamav, bucket
make run-model-gateway      # :8015 — loads BGE-M3 + bge-reranker (two ~2.3 GB downloads on first start)
EVA_RETRIEVAL_LOCAL_TIMEOUT_MS=8000 EVA_RETRIEVAL_RERANK_BUDGET_MS=15000 \
  uv run --project services/evidence-retrieval uvicorn evidence_retrieval.main:app --port 8011
```

The two env overrides exist because CPU inference cannot hold the rig
budgets (600 ms connector / 400 ms rerank); on the rig run defaults.

## Smoke

```bash
curl -s -X POST :8011/retrieve -H 'Content-Type: application/json' -d \
  '{"query":"лікування АГ","k":5,"tenant_id":"<tenant-uuid>"}' | jq '.passages[].scores'
```

## Standing gate

```bash
make eval-retrieval           # vs adopted baseline; exit 1 on breach
make eval-retrieval-ablation  # dense/lexical/hybrid/+rerank table
# re-baseline (after corpus/model/prompt changes, with sign-off):
uv run --project services/evidence-retrieval python eval/harness/run.py --adopt
```

## Failure triage

| Symptom | Meaning | Fix |
|---|---|---|
| 503 `retrieval_unavailable` | Postgres/pgvector down — dense is mandatory | restore pg |
| `degraded:true`, meta all ok | rerank over budget or gateway down | check :8015/readyz; fused order still correct |
| meta `unavailable` for corpus | connector timeout (budget too low for host) or pg error inside connector | raise EVA_RETRIEVAL_LOCAL_TIMEOUT_MS on CPU hosts; check logs |
| lexical results missing, `degraded:true` | OpenSearch down | restart container; dense-only continues |
| stale/odd results | Redis response cache (TTL 120 s) | `redis-cli --scan --pattern 'eva:retrieve:*' | xargs redis-cli del` |
| lexical missing recent docs | projection drift | `uv run --project services/evidence-ingest python scripts/dev/reindex_lexical.py` |

## Invariants

- OpenSearch has NO RLS: every query path in this service filters
  `tenant_id ∈ {caller, nil-uuid}` — never widen (ADR-0003).
- Determinism contract covers (query, snapshot_id, sources, filters, k,
  lexicon_version, model pins) — bump `lexicon_version` when editing the
  abbreviation table, or cached/deterministic results lie.
- `eval/gates.yaml` is protected: threshold changes need an ADR + clinical
  sign-off (rule T4).
