# Runbook — clinical corpus pipeline (corpus-forge, sprint 21)

Governance: ADR-0043 (provenance/tiers/releases), ADR-0044 (LLM review +
PHI boundary). Sign-off state: `docs/signoffs/sprint-21-dpo.md`.

## What this is

An operator CLI (`services/corpus-forge`, entrypoint `corpus-forge`), not a
service. Pipeline: sources → `corpus_candidates` (staging) → tier routing →
review (jury/human) → `promote` → `autocomplete_phrases` → `release`
(immutable artifact + `corpus_releases` row). The serving trie reads only
`review_state = 'accepted'` rows — one predicate in autocomplete-service's
`fetch_corpus()`; there is no other serving path.

## Environment

| Variable | Meaning |
| --- | --- |
| `MDX_CORPUS_DSN` | operator DB DSN. Dev: local superuser. **Prod: a dedicated read-write role limited to `corpus_*` + `autocomplete_phrases`, plus read-only on `reports`/`report_versions`/`patients` for mining. Mining is cross-tenant by design (k-anonymity needs ≥2 tenants) — never a tenant-scoped app DSN.** |
| `MDX_CORPUS_AUDIT_DSN` | audit_writer DSN; unset ⇒ events logged, not chained (dev only) |
| `MDX_CORPUS_LLM_BACKEND` / `_BASE_URL` / `_MODEL` | in-perimeter jury backend (the sprint-15 llama.cpp/Ollama deployment) |
| `MDX_CORPUS_EXTERNAL_API_KEY` / `_MODEL` | optional external API — public-data candidates only; the PHI boundary is enforced in code regardless of this setting |
| `MDX_CORPUS_KENLM_MODEL` | optional kenlm-uk binary; unset ⇒ heuristic fluency filter (recorded in the release manifest) |
| `MDX_LANGUAGETOOL_URL` | optional morphology stage in `validate`; unset ⇒ stage skipped with a warning |
| `MDX_CORPUS_K_MIN_AUTHORS` / `_K_MIN_TENANTS` / `_MIN_FREQUENCY` | mining gates (5 / 2 / 3). Loosening is an ADR amendment. |

## Standard cycle (run from `medical-dictation-backend/`)

```bash
UV="uv run --project services/corpus-forge corpus-forge"

# 1. Sources → staging
$UV mine --language uk                      # needs DPO sign-off before prod
$UV gaps                                    # the work queue (zero-accept prefixes)
$UV import --dataset drlz --dataset-version 2026-08-09 --file drlz.csv \
           --language uk --specialty general
$UV generate --language uk --specialty cardiology --section plan --count 50

# 2. Review
$UV jury --tier 2                           # local model; majority accept, split → tier 3
$UV jury --tier 1 --calibrated              # ONLY after the ≤2% calibration gate
$UV review --candidate-id <uuid> --reviewer <sub> --decision accept --latency-ms 12000

# 3. Ship
$UV promote                                 # staging → live corpus (idempotent)
$UV release --version v1.0.0 --notes "…"    # artifact + corpus_releases row
$UV validate --release-dir infra/seeds/corpus/releases/v1.0.0

# Deploy a published artifact into ANOTHER environment (idempotent seed
# job — releases are never migrations):
$UV release --version v1.0.0 --apply
# Rollback (rows stop serving; register stays immutable):
$UV release --version v1.0.0 --retire
# NOTE: --apply never un-retires rows — an incident-retired harmful phrase
# must not resurrect via a routine re-apply. Restoring a mistakenly-retired
# release is a deliberate operator UPDATE:
#   UPDATE autocomplete_phrases SET review_state='accepted', updated_at=now()
#   WHERE corpus_release='vX' AND review_state='retired' AND source='system';

# 4. Measure (gate: usefulness ≥80%, harmful = 0)
uv run --project services/autocomplete-service python scripts/eval/corpus_coverage.py \
    --release-dir infra/seeds/corpus/releases/v1.0.0 \
    --replay-set eval/replay/replay-set-v1.json \
    --marks eval/replay/marks-v1.0.0.csv \
    --history docs/eval/sprint-21-coverage.md
```

After `promote`, bump the trie cache: the rollup job's version-tag bump
covers it nightly; for immediate effect, `redis-cli DEL` the
`autocomplete:trie:*` keys or bump `autocomplete:tenant_phrase_version:*`.

## ASR prompts

```bash
$UV prompts --language uk --specialty cardiology          # derive candidate (tf-idf, ≤224 tokens)
# record A/B fixtures → scripts/eval/fixtures/sprint-06-cardiology/
# run scripts/eval/run_per_section_wer.py for incumbent and candidate
$UV prompts --promote --incumbent-report a.json --candidate-report b.json
# exit 0 = delta_pp > 0, ship it; exit 1 = incumbent stays
```

## Rollout order (deployment plan)

1. Migrations 0081–0085 + trie predicate → dev; existing corpus still serves.
2. corpus-forge + review UI (`corpus.review`) → internal tenant only.
3. Release v1.0.0 → dev; coverage harness vs pre-release baseline.
4. Staging, 24 h "clinic day" k6 soak; autocomplete p99 ≤ 80 ms unchanged.
5. Production off-peak, one tenant first; watch
   `mdx_autocomplete_trie_size_bytes_histogram` (10k phrases ≈ 20× current corpus).

## Incidents

- **A harmful phrase is serving.** `UPDATE autocomplete_phrases SET
  review_state = 'retired', updated_at = now() WHERE id = …` (as operator),
  bump the trie cache version, then file the retro item — a harmful accept
  means the tier router or the jury version failed; find which via
  `review_engine` and `jury_votes` on the source candidate.
- **A jury version turns out bad.** Its accepts are identifiable:
  `SELECT … FROM autocomplete_phrases WHERE review_engine = 'jury:<model>:<ver>'`.
  Retire them, re-queue the source candidates
  (`UPDATE corpus_candidates SET review_state = 'candidate' WHERE …`), fix
  prompts under a NEW version directory (`infra/seeds/corpus/jury/v2/`).
- **Mining audit shows an unexpected `query_sha256`.** The query text
  drifted from the DPO-signed version — stop mining, diff
  `adapters/mining.py` against the signed SHA, re-sign before the next run.
- **`corpus_reviews` insert fails with insufficient_privilege.** Someone
  attempted UPDATE/DELETE — that's the immutability trigger working, not a
  bug. The chain is append-only; corrections are new rows.

## Metrics to watch

`mdx_corpus_coverage_at_3` / `mdx_corpus_usefulness_at_3` /
`mdx_corpus_harmful_suggestions` (coverage harness textfile), and the
review latency distribution (`corpus_reviews.latency_ms`) — if the median
drifts far above 15 s/decision the review-budget arithmetic in
docs/sprint-21/EXPLORE.md §5 no longer holds and the tier thresholds need
re-cutting.
