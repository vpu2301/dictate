# ADR-0003 — Index technology: pgvector + OpenSearch (spec "ADR-000D")

- **Date:** 2026-08-05
- **Status:** Accepted
- **Deciders:** Volodymyr Pugachov (product), development agent

## Context

Hybrid retrieval (rule RR1) needs a dense index and a lexical index. The
dense side has an obvious home — the platform Postgres already runs
`pgvector/pgvector:pg16` with the `vector` extension, and chunks live in an
RLS-governed table there. The lexical side could be Postgres FTS (single
engine, as the platform's report search does) or a dedicated engine.

## Decision

- **Dense:** pgvector, HNSW index over `chunks.embedding vector(1024)`
  (cosine). One consistency and RLS domain with the row data.
- **Lexical:** **OpenSearch** (2.19, single node in dev via
  `infra/compose/evidence.yaml`), index `evidence-chunks`, custom analyzer
  `medical_icu` = `icu_tokenizer + icu_folding + lowercase` (the analysis-icu
  plugin ships in the distribution) covering uk/en in one field.
- The indexer treats OpenSearch as a **projection**: Postgres rows are the
  source of truth; the OpenSearch doc carries denormalized filter fields
  (tenant_id, authority, tier, dates, retracted). Retrieval-side tenancy MUST
  filter on `tenant_id ∈ {caller, GLOBAL}` — OpenSearch has no RLS (S03
  enforces this in `evidence-retrieval`; noted in the threat model).
- Snapshot builds require both indexes consistent (`snapshot_dirty` guard +
  spec §11 failure table).

## Consequences

One more stateful dev container (~1 GB heap); BM25 relevance out of the box;
uk/en analyzers without maintaining tsvector configs; a projection that can
drift and therefore needs the S03 reconciliation check.

## Alternatives considered

- **Single-engine Postgres (tsvector + pgvector):** fewest moving parts and
  the platform precedent, but `simple`-config Ukrainian stemming is poor,
  BM25 must be approximated with ts_rank, and the S03 reranking/RRF work
  would target a throwaway backend.
- **Single-engine OpenSearch (kNN + BM25):** one engine but moves vectors
  OUT of the RLS domain and doubles storage of the safety-critical data.

## Trigger conditions for revisiting

- Ops cost of OpenSearch in on-prem bundles (rule DP2) proves too high →
  fall back to single-engine Postgres with a Ukrainian dictionary config.
- Corpus grows past what one Postgres instance serves for ANN → dedicated
  vector engine review.
