# ADR-0025 — Trie + Redis caching for autocomplete latency floor

- Status: accepted
- Date: 2026-06-10
- Sprint: 10
- Deciders: tech lead, NLP engineer, SRE/DevOps, clinical content lead

## Context

The autocomplete `POST /autocomplete/suggest` endpoint is the highest-
frequency clinical touchpoint in the product (multiple calls per
second per active clinician). The latency floor is p95 ≤ 80 ms
end-to-end — below human perception. A regression here is observable
in every keystroke session.

The corpus is per-tenant: ~10k system phrases + tenant + user phrases.
Cardinality per tenant rarely exceeds 15k phrases; prefix-match
queries dominate (rare exact match).

Three candidates were considered:

1. **Postgres-only**: GIN-trigram + `LIKE 'prefix%'` query each request.
   Measured p95 = 25–40 ms cold, dominated by RTT + planner overhead.

   *Verified 2026-07-08 (S10 verification pass)* on the dev stack with a
   synthetic 10k-row uk corpus (rolled-back txn), 200-iteration loop of
   `SELECT … WHERE language=$1 AND lower(phrase) LIKE $2 || '%' LIMIT 20`
   from the host over TCP: first-run 8.0 ms; warm p50 = 7.4 ms,
   p95 = 7.7 ms (server-side execution 0.06 ms — the cost IS round-trip
   + driver). The original 25–40 ms band is the conservative
   cross-network figure; either way the warmed in-process trie lookup
   (sub-millisecond, no DB hop) is what makes the ≤ 80 ms end-to-end
   p95 comfortable, and the measured DB path bounds the degraded mode.
2. **Elasticsearch sidecar**: powerful but adds an operational
   dependency and a network hop for every keystroke.
3. **In-process trie + Redis cache** (this ADR): build per-tenant
   prefix→top-K map in memory, serialise + cache in Redis.

## Decision

Adopt **trie + Redis cache**. Implementation in `services/
autocomplete-service/src/autocomplete_service/trie/`:

- `builder.py` — builds the per-(tenant, language, user) trie from
  the corpus pull. The trie maps 1–6 char lowercased prefixes to the
  top-20 candidate ids (coarse-ranked).
- `serializer.py` — versioned binary format (`MDXT` magic + version
  byte). Version mismatch → cache miss → rebuild.
- `cache.py` — Redis-backed; per-key `SET NX EX` lock to prevent
  thundering-herd on cold cache; lazy version-tag invalidation
  (no DEL → no stampede).
- Suggest path: walk trie → coarse candidates → full ranker
  (Bayesian + recency + length + diversity) → top N.

Cold-start storm mitigation:

- Per-tenant rebuild lock with 200 ms wait + degraded fallback (direct
  DB query without cache populate).
- Roll-up bumps `version_tag`; trie cache rebuilds lazily on the next
  request.

## Consequences

Positive:
- Sub-80 ms p95 on cache hit is realistically achievable.
- No new infra beyond the existing Redis cluster.
- Same Redis serves rate limit, signing rate limit, autocomplete
  cache — operational story is consistent.

Negative / accepted:
- The cache is unique per `(tenant, language, user)`. With many
  cross-product combinations the memory bill grows. Sprint-10
  monitors `mdx_autocomplete_trie_size_bytes_histogram` and the
  3600 s TTL caps the bill.
- The trie itself is a Python dict; for tenants > 50k phrases the
  build cost begins to dominate. Sprint-future swap to
  `marisa-trie.RecordTrie` if needed (interface is bounded by
  `TenantTrie.candidates_for`).

## Trigger conditions for upgrade

| signal                                         | next step                              |
| ---------------------------------------------- | -------------------------------------- |
| Any tenant exceeds 50k user+tenant phrases      | swap inner trie to marisa-trie         |
| Cache hit ratio < 80% steady-state              | investigate eviction + TTL             |
| Suggest p95 > 150 ms                            | day-7 alert fires; investigate         |
| Clinical lead documents > 10 examples of poor quality | consider embedding-similarity sprint |

## Recorded side-decisions (sprint-10 close, step 08)

- **GUC plumbing (step 01):** the write/suggest paths need
  `app.user_id` + `app.user_role` GUCs beyond `db.tenant_connection`'s
  `app.tenant_id`. Decision: **inline `set_config` at the call sites**
  (3 sites; the spec's threshold for extracting an
  `authed_tenant_connection` helper into `libs/db` is the 4th site).
  Transaction-locality is test-proven. Caution (verification pass):
  after any transaction-local `set_config`, the session RESET value of
  the GUC on that pooled connection is the EMPTY STRING, not NULL —
  never query an RLS table on a bare pooled connection (see the
  runbook and the roll-up's nil-tenant corpus count).
- **Telemetry no-RLS exception:** `autocomplete_telemetry` carries no
  RLS (allowlisted in `scripts/ci/check-rls-policies.py` with a pointer
  here). Rationale: append-only, service-written under claims-derived
  tenant/user ids, never user-queried; reads happen only in the
  roll-up under explicit tenant grouping. RLS on a high-volume
  partitioned insert path would buy nothing and cost planner time.
- **Pilot-tunable ranking constants** (do NOT tune before telemetry
  exists): diversity-guard Levenshtein threshold **3** — may collapse
  legitimately distinct short suffixes; Bayesian prior **Beta(1,9)**
  (zero-history phrase scores exactly 0.1). Both are named constants
  (`ranking.py`, `suggest.py`).
- **Metric-name freeze:** dashboard/alert/k6 read
  `mdx_autocomplete_suggest_latency_ms_histogram*` (path label),
  `…_cache_lookups_total{hit}`, `…_degraded_total{reason}`,
  `…_trie_build_seconds*`, `…_trie_size_bytes*`,
  `…_rollup_last_run_unix_ts`, `…_corpus_size{source}` — renames must
  touch dashboard + alerts + k6 + tests together. The OTel collector's
  prometheus exporter must stay **namespace-free** (the sprint-10
  verification found a `medical_dictation` namespace had silently
  disconnected every dashboard/alert in the repo).

## Links

- `services/autocomplete-service/src/autocomplete_service/trie/`.
- `services/autocomplete-service/src/autocomplete_service/ranking.py`.
- Sprint-10 spec §2.4, §2.5.
