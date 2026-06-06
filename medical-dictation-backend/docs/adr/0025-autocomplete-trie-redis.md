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

## Links

- `services/autocomplete-service/src/autocomplete_service/trie/`.
- `services/autocomplete-service/src/autocomplete_service/ranking.py`.
- Sprint-10 spec §2.4, §2.5.
