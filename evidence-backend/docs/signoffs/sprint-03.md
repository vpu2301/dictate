# EVA-S03 — Sign-off register

## Tech lead

| Artifact | Verification | Status |
|---|---|---|
| evidence-retrieval service (hybrid+RRF+rerank, deterministic) | 45 unit + 7 live tests; determinism ×100; `make ci` green incl. openapi-check | ⏳ |
| Frozen EvidenceSource protocol | freeze suite (method surface, descriptor fields, recorded round-trip) | ⏳ |
| Gateway rerank role + client | 11 gateway tests; live both-roles readyz | ⏳ |
| Migration 0069 (evidence_eval schema) | cycle ×2; check-rls unaffected | ⏳ |

## Security

| Artifact | Verification | Status |
|---|---|---|
| No patient data in this service (QS1 pattern start) | service imports no snapshot models; identity never attaches; service-token auth | ⏳ |
| Tenant scoping on both engines | SQL via tenant_connection; OpenSearch term filter tenant∈{caller,nil} (ADR-0003 invariant, live filter tests) | ⏳ |
| Connector isolation | chaos test (5 s hang → 1 s response, unavailable) + live stubs honesty | ⏳ |

## Clinical advisor

| Artifact | Verification | Status |
|---|---|---|
| Scoring constants (authority mults, half-lives, UNDATED, BOOST_WEIGHT) | ADR-0004 §4 + live-tuning evidence | ⏳ **required** |
| Lexicon v1 (~40 uk/en abbreviations) | preprocess.py table + measured expansion effect | ⏳ **required** |
| Eval rubric 1.0 + seed judgments (judge dev-agent-v1) | eval/seed/*.jsonl + evidence_eval rows | ⏳ **required before real-corpus rebaseline** |

## SRE

| Artifact | Verification | Status |
|---|---|---|
| Degrade paths (lexical down, rerank over budget, connector dead) | live budget-1ms proof + unit suite | ⏳ |
| Standing gate mechanics | adopt → green rerun → lexical-run breach (both directions) | ⏳ |
