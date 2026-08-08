# ADR-0004 — Hybrid retrieval: pgvector+OpenSearch, RRF fusion, cross-encoder rerank (spec "ADR-000E")

- **Date:** 2026-08-05
- **Status:** Accepted
- **Deciders:** development agent; scoring constants pending clinical countersign

## Context

S03 needs one retrieval discipline all connectors feed into (rule RR1) with
authority precedence (RR3) and recency decay (RR4) that reorder but never
delete, deterministic output, and a degrade story for every optional stage.

## Decision

1. **Engine pair per ADR-0003**: dense = pgvector HNSW (mandatory; down ⇒
   503), lexical = OpenSearch `medical_icu` (degradable; down ⇒ dense-only +
   `degraded`).
2. **Fusion = reciprocal-rank fusion, k=60**, per-stage score retention
   (`PassageScores{dense,lexical,fused,rerank,final}`) — RRF is scale-free
   (no cross-engine score calibration) and deterministic with a chunk-id
   tie-break.
3. **Rerank = BAAI/bge-reranker-v2-m3** behind the gateway `rerank` role,
   batches of 32, budgeted (`400 ms` rig target; dev overrides via env);
   over-budget/down ⇒ fused order + `degraded:true`.
4. **Final score** = `sigmoid(rerank | fused-scaled) × boost^0.3` where boost
   = authority_mult × recency_decay. The 0.3 exponent is a LIVE S03 finding:
   at full strength the boosts inverted clear relevance (ablation nDCG .53
   vs .79 pure-rerank); at 0.3 hybrid+rerank ≥ every single method and has
   the best MRR (.90). Constants (authority 1.30/1.20/1.10/1.00/0.95;
   half-lives guideline 3y / sysrev 5y / RCT+observational 8y; undated 0.60;
   floor 0.30) live in `scoring.py` and are pipeline-version events.
5. **Frozen `EvidenceSource` protocol** with per-connector timeout isolation;
   stubs are honest `unavailable` (P4).

## Consequences

Deterministic, snapshot-pinnable retrieval with visible per-stage provenance;
one more model resident in the gateway (CPU dev: ~4.5 s per 22-passage rerank
— the 400 ms budget is only honest on the rig).

## Alternatives considered

- **Single-engine** (either only): loses either uk lexical precision or
  semantic recall — ablation shows hybrid > each (nDCG .796 vs .745/.602).
- **ColBERT-style late interaction**: better quality ceiling, but a heavier
  serving+index footprint and no OpenSearch reuse; revisit at scale.
- **Score-calibrated linear fusion**: needs per-corpus calibration; RRF wins
  on robustness.

## Trigger conditions for revisiting

- Real-corpus baseline (post bulk load) shows rerank or boost regressions.
- p95 budget unreachable on the rig with batches of 32.
- ColBERT/SPLADE serving cost drops below the two-engine ops cost.
