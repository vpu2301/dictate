# EVA-S03 — Backend — Retrieval, Ranking & Connector Registry

**Objective:** `evidence-retrieval` (:8011): hybrid dense+lexical retrieval
with RRF fusion and a self-hosted medical reranker, filterable on every RR2
field, deterministic and snapshot-pinned; the frozen `EvidenceSource`
connector interface; retrieval quality baselined in a gated eval harness.

*(Framework planning file: `~/Desktop/evidence-framework/docs/sprints/sprint-03-retrieval-and-ranking.md`.)*

---

## As-built (2026-08-05)

### What shipped, where

| Deliverable | Location | Proof |
|---|---|---|
| Contracts v1.0 additions (additive) | `evidence_models.connector`: PassageScores, extended EvidencePassage, ConnectorMeta, ConnectorStatus, RetrieveResponse, ConnectorDescriptor registry fields | contracts-check + lint_additive green; dictat `evidence.d.ts` regen (154 types), strict tsc OK |
| Gateway `rerank` role | bge-reranker-v2-m3 CrossEncoder, `/v1/rerank`, client `rerank()`; PINS row active | live: both roles ready; 11 gateway tests |
| `evidence-retrieval` service | routers/{retrieve,health}, domain/{plan, preprocess (lexicon v1 uk/en ~40 entries), fusion (RRF k=60), rerank (budgeted degrade), scoring (authority+recency, BOOST_WEIGHT), registry (frozen protocol + isolation), connectors/{corpus, stubs}, cache (Redis 120 s, degraded-never-cached)}, adapters/{pg, opensearch} | 45 unit + 7 live tests; live uk query E2E with per-stage scores |
| Migration `0069_evidence_eval` | `evidence_eval` schema: questions/judgments/runs/baseline — documented non-RLS exception (own schema, outside the check-rls scan, app_role-only grants) | up→down→up ×2; check-rls unaffected (51) |
| OpenSearch RR2 parity | mapping + indexer + pipeline now carry jurisdiction/specialty/license_class/char offsets; `scripts/dev/reindex_lexical.py` = reconciliation tool | reindex run; filter-equivalence live test |
| Eval harness | `eval/harness/run.py` (recall@20, nDCG@10, MRR@10; modes incl. `rerank_pure` diagnostic), protected `eval/gates.yaml`, seed 10 questions / 22 judgments (locator-based, re-ingest-proof), persisted to `evidence_eval.*` | baseline adopted; gate green on rerun; negative: lexical-only run breaches both gates |
| OpenAPI snapshots (E11) | `scripts/dev/dump_openapi.py` + `make openapi-dump/openapi-check` (in `ci`) for all three services | ci green |
| Live verification suite | `tests/integration/test_retrieval_live.py` (RUN_RETRIEVAL_LIVE=1) | 7/7: determinism ×100, filter matrix both engines, snapshot pinning, k-cap, stub honesty |

### Ablation (fixture corpus, CPU, judge=dev-agent-v1 rubric 1.0)

| mode | recall@20 | nDCG@10 | MRR@10 |
|---|---|---|---|
| dense | 0.8167 | 0.7450 | 0.8333 |
| lexical | 0.6167 | 0.6016 | 0.6333 |
| hybrid (RRF) | 0.8167 | 0.7957 | 0.8333 |
| **hybrid+rerank (baseline)** | **0.8167** | **0.7634** | **0.9000** |

AC-S03-B-3 (hybrid+rerank ≥ each single method) holds on all three metrics.

Per-language split (`--by-lang`): Ukrainian lexical is the weak leg
(recall .43 vs .80 en — no uk stemming in `medical_icu`, ICU folding only);
dense carries uk and hybrid recovers fully (uk MRR 1.0 in both hybrid modes).
Lexicon-effect measurement (`--lexicon-effect`, spec §9.7 informational):
zero delta on this seed set — expansions fire (unit-tested) but the 27-chunk
corpus already matches on surrounding tokens; re-measure after the bulk load.

### Live findings (fixed during verification — the point of the protocol)

1. **Boost inversion**: full-strength authority/recency boosts flipped clear
   relevance ordering (nDCG .53); diagnosed with a `rerank_pure` mode (.79)
   → `BOOST_WEIGHT = 0.3` exponent (ADR-0004 §4). 
2. **UNDATED_MULT 0.85 → 0.60**: an undated abstract outranked a dated 2023
   protocol.
3. **Tokenizer range bug**: `[a-za-я...]` spanned Latin→Cyrillic codepoints
   (matched Greek etc.) — found by the unit-test subagent, fixed to explicit
   ranges.
4. **600 ms local connector budget can't hold a CPU query-embed** — timeout
   now config-driven; the query vector is embedded once per request (memo),
   not once per connector.
5. **Unavailable requested connector now implies `degraded:true`** (a
   zero-result response that silently hid a dead connector was misleading).

### As-built deltas

1. **Auth**: `/retrieve` is service-token only (`X-Service-Token`,
   dev-open with startup WARNING) with `tenant_id` in the body — identity
   attaches in evidence-answer (S04) per spec §7; no per-user audit here
   (§8 decision recorded in the platform catalogue note).
2. **Rerank budget 400 ms is a rig target** — dev runs override via env
   (CPU rerank of 22 passages ≈ 4.5 s); the degrade path covers production
   incidents AND dev defaults.
3. **Eval numbers are plumbing-grade**: 27-chunk fixture corpus, judgments
   authored by the dev agent (judge `dev-agent-v1`), clinical countersign
   pending (sign-off row) — the mechanism is real, the numbers rebaseline
   after the bulk load (same posture as the platform's WER plumbing gate).
4. **Nightly scheduling** of `make eval-retrieval` needs a CI system this
   workspace doesn't have yet — target exists, wiring lands with the CI
   pipeline (SPRINT-TODO).
5. **Lexicon licensing**: v1 is freshly curated (~40 uk/en abbreviations),
   NOT copied from the dictate NLP tables (license review of those pending).
6. **`search_many`** ships as the protocol's default sequential impl (S09
   batching hook) — no native batch connector yet.
7. Harness B-1 rides with the deferred batch harness (S00 debt).
8. Snapshot-pinning test premise corrected mid-verification: fixtures-v1
   was frozen after v2 existed, so the proof uses a v3 marker ingested after
   fixtures-v2 froze.
9. OpenSearch field boosts are `section_path^2 > text` — the projection has
   no separate title field (title lives in the section trail); revisit if a
   title field is added to the mapping.
10. §8 no-audit decision recorded in the platform event-kinds catalogue.

### Acceptance criteria

- AC-S03-B-1 ✅ fused+reranked passages with per-stage scores + connector
  meta (live E2E + 7 integration tests).
- AC-S03-B-2 ✅ baseline recorded + adopted (`evidence_eval.baseline`);
  gates committed in protected `eval/gates.yaml`.
- AC-S03-B-3 ✅ ablation table above.
- AC-S03-B-4 ✅ protocol frozen (freeze suite: method surface + descriptor
  fields + recorded round-trip fixture); stubs honest.
- AC-S03-B-5 ✅ degradation + isolation proven (unit chaos test ≤1 s wall;
  live budget-1ms instance served fused order degraded).
- AC-S03-B-6 ✅ determinism ×100 + snapshot pinning live.
- AC-S03-B-7 ✅/⏳ gate runnable + proven both directions; nightly wiring
  pending CI (delta 4).
