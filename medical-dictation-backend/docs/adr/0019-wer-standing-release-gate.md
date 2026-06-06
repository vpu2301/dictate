# ADR-0019 — WER eval is a standing release gate

- Status: accepted
- Date: 2026-05-12
- Sprint: 07
- Deciders: ML/MLOps lead, NLP lead, product lead

## Context

Up to sprint-06 we shipped Whisper/Prompts/NLP changes without an
automated, cross-sprint quality metric. Every sprint's tests pass on
unit corpora, but cross-sprint regressions (a number-norm change that
worsens overall WER, a prompts tweak that degrades cardiology
specifically) are easy to miss until someone notices in production.

## Decision

Stand up a nightly WER + RTF + number-norm eval that runs against a
versioned reference corpus (`eval/corpus/v1`) on the same GPU class
the demo uses, persists results to `audit.eval_runs` /
`audit.eval_utterances`, and compares the latest run to a rolling
baseline in `audit.eval_baseline`.

Regression thresholds (sprint-07 calibrated):

- Per-language WER may not increase by more than **1.0 pp absolute**.
- RTF p95 may not drop by more than **0.05**.
- Number-norm accuracy may not fall below **95%** on any category.

A regression alerts `#eval-regressions` Slack with the
nightly-wer GitHub Actions run link. The ML/MLOps lead triages
within one business day. Re-baselining is permitted **only** when a
new model version, NLP pipeline version, or corpus version ships
(documented in an ADR).

The corpus is part of the repo (LFS-backed). PII sweep
(`scripts/eval/check_corpus_pii.py`) runs on every PR.

## Consequences

Positive:
- Quality changes are now measurable, not anecdotal.
- Per-specialty breakdown catches regressions invisible at the
  aggregate level.
- Determinism contract (model + prompts + pipeline_version hashed
  into every run) lets us bisect.

Negative:
- Nightly GPU runner is a non-zero cost (~30 min/day on A10G).
- Corpus authorship is expensive — clinical content lead + linguist
  consultant. Funded for v1; v2 expansion is sprint-08+.

## Out of scope

- Latency under load (sprint-08 will add a load-test harness).
- Adversarial / accent eval — v2 corpus.

## Links

- `scripts/eval/run_wer.py`.
- `scripts/eval/compare_to_baseline.py`.
- `.github/workflows/nightly-wer.yml`.
- `docs/eval/wer-methodology.md`.
- Sprint-07 spec §4 (eval pipeline) + §5 (baseline + alerts).
