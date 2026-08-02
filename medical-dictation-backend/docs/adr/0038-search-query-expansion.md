# ADR-0038: Search query expansion — synonym dictionary over `simple` FTS

Date: 2026-08-02
Status: Accepted
Sprint: 15 (delivers the ADR-0021 named follow-up)

## Context

ADR-0021 accepted `simple` FTS (no stemming, no synonyms) and named
sprint 15 for "a `query_expansion` layer" plus the search-tips content.
Clinicians write «ІМ» and search «інфаркт міокарда» — or the reverse —
and `simple` matches neither direction.

## Decision

- **`medical_synonyms` table** (migration 0063): `{group_id, term,
  lexemes text[], language uk|en, source system|tenant}`, RLS+FORCE with
  the autocomplete-phrases policy shape (PERMISSIVE visibility: system ∪
  own tenant; PERMISSIVE tenant-writes for app_role — the 0038-migration
  deny-all lesson; system rows writable only by `tenant_writer`).
  **`lexemes` is the term pre-normalized through
  `to_tsvector('simple')` at write/seed time** — both sides of the
  query-time match are guaranteed the same normalization, and lookup is
  an array-overlap on a GIN index.
- **Expansion wraps — never forks — the sprint-08 builder**
  (`domain/query_expansion.py`): normalize the raw query in one
  roundtrip (`tsvector_to_array(to_tsvector('simple', $1))`, the
  icd10_query.py apostrophe-safe precedent), match lexemes against
  synonym rows, assemble `('ім' | ('інфаркт' & 'міокарда') | 'mi')`
  OR-groups per matched lexeme, AND across query lexemes, and pass the
  string as a **bind parameter** to `to_tsquery('simple', $n)`. One
  `_fts_clause` helper feeds the match predicate, `ts_headline` AND
  `exact_total` — they can never disagree. Unexpanded queries keep the
  plainto path byte-identical; `expand=false` opts out.
- **Caps**: ≤ 8 expanded lexemes per query (the rest stay plain);
  lexemes < 2 chars never expand (the preposition «в» must not drag in
  the в/в = внутрішньовенно group). Same term in two groups unions —
  ГКС (гострий коронарний синдром | глюкокортикостероїди) is seeded
  deliberately; recall over precision, and the response's
  `expanded_terms` field shows the user exactly what happened.
- **Seed**: 171 system groups / 464 terms
  (`infra/seeds/synonyms/uk-medical-synonyms.json` → generated migration
  0064 with deterministic group UUIDs; `make check-synonym-corpus`
  enforces fixture↔migration sync). Authored in-sprint, **flagged for
  clinical-lead review**; growth = fixture entries + a new migration.
- **Not** nlp-service's `abbreviations_global`: that dictionary rewrites
  transcript text at dictation time (direction-aware,
  replay-deterministic); this one broadens search recall at query time
  and is tenant-extendable. Merging them would couple a
  clinical-correctness surface to a recall surface.
- `GET /v1/search/tips` serves the honest what-search-does content
  (typed models, uk/en) so FE copy cannot drift; `/v1/synonyms` CRUD
  (new `synonym.read`/`synonym.write` permissions, tenant_admin writes)
  covers curation until the sprint-17 admin UI.
- **Plan-shape regression** (`tests/integration/
  test_synonyms_and_expansion.py`, first of its kind) — with a finding:
  `@@` (ts_match_vq) is **not leakproof**, so under RLS Postgres refuses
  to push it into an index condition ahead of the security quals —
  **app_role search has never used the GIN index**; the sprint-08
  "canonical GIN plan" was captured as superuser. The production
  app_role plan is tenant-driven (reports tenant index → versions pkey,
  FTS as filter), which is the right shape while tenant slices stay
  small. The test therefore asserts BOTH honestly: the expanded tsquery
  is GIN-indexable (Bitmap Index Scan on
  `report_versions_search_vector_idx`, RLS aside), and the RLS plan
  keeps its tenant-first shape with expansion applied. The ADR-0021
  revisit triggers (p95 > 500 ms) now have a concrete lever: a
  SECURITY DEFINER search function or leakproof-wrapped operator.
- Opportunistic gap fix: `mdx_reports_search_latency_ms` is now actually
  emitted — the sprint-08 dashboard panel and ReportSearchLatencyHigh
  alert had queried an instrument no code ever created.

## Consequences

- Aggregated `search.expanded` audit (count only, never query text);
  `report.searched` unchanged.
- Worst-case tsquery grows to ~8 OR-groups; the GIN bitmap plan absorbs
  OR-of-lexemes, EXPLAIN-verified.
- ADR-0021's "no synonyms" consequence is superseded for recall, not
  for stemming — «гіпертензії» still won't match «гіпертензія» unless a
  synonym group says so; the tips screen says exactly that.
