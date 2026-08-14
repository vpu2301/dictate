# Jury calibration — sprint 21 (ADR-0044 §3)

Status: **NOT RUN — auto-accept is OFF.**

`corpus-forge jury --tier 1` refuses to run without `--calibrated`, and
`--calibrated` may only be passed once this document records a passing run.

## Protocol

1. Draw **200 stratified candidates** (by source_kind × tier-shape ×
   language) from `corpus_candidates`.
2. The clinician reviews all 200 **blind** (no jury output visible),
   via `corpus-forge review` — decisions land in `corpus_reviews` with
   latency_ms.
3. The jury (same model + prompt version that will run in production,
   recorded below) reviews the same 200.
4. Compute agreement and, specifically, the **jury false-accept rate on
   items the clinician rejected**, restricted to tier-1-shaped items.

## Gate

Auto-accept (tier 1) is enabled **only if false-accept ≤ 2%** on
tier-1-shaped items. Otherwise tier 1 collapses into tier 2 and the corpus
target shrinks — 3k good phrases beat 10k with 200 bad ones, and
"0 harmful" is not negotiable.

## Results

| Date | Jury engine | Items | Agreement | False-accept (tier-1-shaped) | Gate |
| --- | --- | --- | --- | --- | --- |
| — | — | — | — | — | pending clinician session |

Median human seconds/decision (from `corpus_reviews.latency_ms`): —
(plan assumes 15 s; if the measured median is materially higher, re-cut
the tier thresholds — plan §5.1.)
