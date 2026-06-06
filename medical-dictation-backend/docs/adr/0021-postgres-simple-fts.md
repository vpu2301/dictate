# ADR-0021 — Postgres `simple` FTS config for reports search

- Status: accepted
- Date: 2026-05-13
- Sprint: 08
- Deciders: tech lead, NLP lead, SRE lead, clinical content lead

## Context

The reports search endpoint (`GET /v1/reports/search`) is the most
frequently-used clinical query and the most-demanding performance
budget in the product (p95 ≤ 250 ms with `q`, ≤ 100 ms filter-only,
on 100k reports per pilot clinic).

The content is **bilingual** (Ukrainian + English) with heavy
medical terminology. Three candidates were on the table:

1. **Postgres FTS with `simple` config.** Token = lowercased exact
   match; no stemming.
2. **Postgres FTS with `russian` or a hand-curated Ukrainian config.**
   Stems on Cyrillic root, conflates case endings.
3. **OpenSearch / Elasticsearch sidecar.** Sophisticated analyzers,
   relevance tuning, faceting.

## Decision

Ship **Postgres `simple`** in sprint-08. Carry a fall-back plan to
either a custom UK config or OpenSearch when we see real pilot
queries failing.

Rationale:

- Ukrainian morphology is hard. `russian` config doesn't handle UK
  cases correctly; a hand-curated UK dictionary needs a linguist
  contractor (out of sprint-08 budget).
- `simple` is "find the exact lowercased token". Inflected matches
  (задишку vs задишка) won't match — but for sprint-08 we accept
  this trade-off because pilot clinicians can use truncated stems in
  their queries (we'll document the search-tips screen in sprint-15).
- Avoiding the operational burden of OpenSearch in sprint-08 lets us
  hit other targets (chain integrity, optimistic-lock UX, diff
  caching). A separate cluster is more weight than the demo + early
  pilot phase warrants.

## Triggers for revisiting

Move to a heavier solution **only** when one of the following holds:

| signal                                       | next step                                    |
| -------------------------------------------- | -------------------------------------------- |
| Any tenant > 1M reports                      | Partition + re-eval FTS plan                 |
| Search p95 > 500 ms for 5+ consecutive days  | Profile; consider OpenSearch                 |
| Clinical content lead documents > 10 pilot examples of poor result quality | Begin UK FTS dictionary work |

The dashboard panel `Search latency split by has_q` in the
`sprint-08-reports` Grafana board is the canonical place to watch the
first two signals.

## Consequences

Positive:
- Zero new ops surface.
- Snapshot consistency: search hits exactly what's in `report_versions`.
- ts_headline gives us a free snippet on the same query.

Negative / accepted:
- Cyrillic queries that rely on stemming will miss inflected matches.
- No relevance tuning beyond `ts_rank` (which we don't currently use
  — results are ordered by `encounter_date DESC, id DESC` for stable
  cursor pagination).
- Synonym handling (e.g., "MI" vs "infarct") is not in scope. Sprint-15
  may add a `query_expansion` layer.

## Links

- Sprint-08 spec §4.7.
- `services/report-service/src/report_service/domain/search.py`.
- `docs/eval/sprint-08-loadtest.md`.
- Sprint-15 backlog item: query expansion + search tips UI.
