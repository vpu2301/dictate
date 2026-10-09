# Sprint 21 sign-off — Clinical Corpus Foundation

Date: 2026-08-12 (engineering complete; human-gated items listed below)

## Definition-of-done status (plan §13)

| Item | Status |
| --- | --- |
| (a) EXPLORE.md numbers exist, corpus target set from them | ✅ `docs/sprint-21/EXPLORE.md` — target ~3k (review-budget-derived; dev telemetry = 8 events, honestly unusable) |
| (b) ≥1 corpus release published, versioned, serving | ✅ dev smoke release **v0.0.1** (32 phrases, manifest SHA `9b5141ac…`, rows stamped, trie predicate live). **v1.0.0 awaits real imports + clinician review.** |
| (c) coverage ≥80% useful / 0 harmful | ⏳ harness live (`scripts/eval/corpus_coverage.py`); usefulness needs clinician marks; harmful = 0 on v0.0.1 |
| (d) ASR prompts promoted only where delta_pp > 0 | ✅ gate implemented (`corpus-forge prompts --promote`); no prompt promoted yet — A/B fixtures await clinician recordings |
| (e) eval corpus v1 complete incl. phone-mic subset | ⏳ scaffolding + manifest flow + subset dirs shipped; recordings are the clinician's task (8/120 utterances exist, all placeholders) |
| (f) ADR-0030+0031 accepted | ✅ as **ADR-0043 + ADR-0044** (original numbers were long taken) |
| (g) DPO sign-off on mining query + review pipeline | ⏳ `docs/signoffs/sprint-21-dpo.md` prepared with the signed-text SHA; signature pending |
| (h) clinician-hours consumed recorded in retro | ⏳ instrument live (`corpus_reviews.latency_ms`); zero hours consumed so far |

## Engineering gates

- migrations 0081–**0085** applied; `make check-rls` green (59 tables)
- `make ci` green (lint, mypy-strict incl. corpus-forge, tests, security, grep
  gates + new `check-corpus-releases`, `check-corpus-log-hygiene`)
- import-linter: 22 contracts kept (incl. new corpus-forge layering)
- **110** unit + **8** DB-integration tests for corpus-forge; PHI-boundary
  must-raise test in place; 8 unit tests for the /corpus HTTP surface
- jury auto-accept hard-blocked behind the calibration gate (CLI refuses `--tier 1` without `--calibrated`)

## Deployment slice (docs/sprint-21/DEPLOYMENT-NOTES.md)

- **Refuse-to-start perimeter assertion**: a public in-perimeter-LLM URL
  raises `PHIBoundaryViolation` before any client exists (static judgement,
  no DNS); jury and generation both route through it.
- **`corpus.review` permission** in perms.py + permissions.csv (drift tests
  green); `/corpus` HTTP surface on autocomplete-service for the FE review
  UI (queue, decision w/ spot-`audit` mode, stats incl. quota heatmap +
  `audit_rejections`, releases) + phrase provenance columns + `retire`
  endpoint; OpenAPI snapshot refreshed.
- **Releases are not migrations**: `release --apply` (idempotent,
  SHA-verified seed job) / `release --retire` (rollback; never resurrects
  incident-retired rows) — proven live round-trip on v0.0.1.
- `make corpus-forge ARGS=…`, `make fetch-corpus-sources` (gitignored
  snapshots + committed SHA lockfile), log-hygiene grep gate.
- Contract coordination with the FE session done in-flight (spot-audit
  mode, quota-cell stats, retire scope split) — see the exchange log.

## Reviewer sign-offs

| Role | Name | Date | Signature |
| --- | --- | --- | --- |
| Tech lead | | | |
| Security lead | | | |
| DPO | | | |
| Clinical reviewer | | | |
