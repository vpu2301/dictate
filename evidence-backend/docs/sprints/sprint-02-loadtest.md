# EVA-S02 — 100k-chunk index-path capacity exercise (AC-S02-B-1 / B-7)

Run: 2026-08-05/06, Mac dev host (Apple Silicon, CPU-only), platform stack +
evidence infra up. Script: `scripts/loadtest/volume_100k.py` (synthetic
chunk rows + normalized random vectors under a throwaway `restricted`
document in the global tenant; self-cleaning — post-run residue verified 0).

## Measured numbers

| Path | Result | Notes |
|---|---|---|
| `embed.dense` real throughput (BGE-M3, CPU) | **5.7 chunks/s** → 100k backlog ≈ **4.9 h** | real model, 64-text batches ×3; inside the 6 h NFR even on this host; the rig target is far lower |
| pgvector + HNSW insert | **100,000 rows in 1557 s** (94→64 rows/s as the graph grows) | 1000-row batches; graph-build cost dominates and decays roughly linearly; `command_timeout` must be raised for bulk inserts (pool default 30 s trips ~25k) |
| OpenSearch bulk index | **100,000 docs in 11 s** (~9,200 docs/s) | never the bottleneck (count read 98k pre-refresh; index verified complete before deletion) |
| pgvector ANN top-10 @ 100k | **21 ms** | HNSW cosine, single query, RLS-scoped connection |
| OpenSearch BM25 top-10 @ 100k | **39 ms** | `medical_icu` analyzer, uk query |

## What this proves / doesn't

- **Proves (AC-S02-B-7 dev-host half):** both index write paths and both
  query paths hold at the 100k-chunk target scale; the parse→chunk→enrich
  stages measured separately at 0.03–0.5 s/doc (≫ the 50 docs/min NFR); the
  embedding backlog drains inside the NFR window even on CPU.
- **Does not prove:** real-corpus retrieval quality at scale (needs the
  operator bulk load + rebaseline, SPRINT-TODO) and rig-profile latencies
  (400 ms rerank / 600 ms connector budgets).

## Operational lessons (folded into code/runbooks)

1. Bulk pgvector inserts need `command_timeout≈600 s` and ≤1000-row batches
   (script updated; relevant for the real bulk load too).
2. HNSW insert throughput decays with graph size — for the real ≥100k load,
   budget ~30 min of index build per 100k chunks on CPU-class hardware, or
   build the index AFTER bulk-inserting embeddings on the rig.
3. OpenSearch `_count` immediately after `async_bulk` can read pre-refresh
   values — refresh before asserting counts.
