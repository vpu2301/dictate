# Sprint 21 Retrospective — Clinical Corpus Foundation

Date: (to be held after the clinician review cycle)
Facilitator: tech lead.
Participants: backend, ML/MLOps, clinical reviewer, DPO, SRE, security.

## The one number that decides whether the process is sustainable

**Clinician-hours actually consumed:** ___ (budget: ~8 h ≈ 1,900 decisions
at 15 s each; measured median s/decision from `corpus_reviews.latency_ms`: ___).
Plan §13(h): if it took 30 hours, the process failed even if the corpus is
good.

## What worked

(Filled at retro.)

## What didn't

(Filled at retro.)

## Action items

| # | Item | Owner | By |
| - | ---- | ----- | -- |

## Spec retrospective prompts

1. Was the tier split at real candidate volumes anywhere near the assumed
   65/25/10? What did re-cutting thresholds cost?
2. Jury calibration: what was the measured false-accept rate, and did
   auto-accept ever turn on? (docs/eval/sprint-21-jury-calibration.md)
3. Did the risk flagger's over-flag bias swamp tier 3, or catch real
   dose/laterality/negation hazards?
4. Was 15 s/decision real once the review flow was keyboard-driven?
5. Did the k-anonymity gate (≥5 authors, ≥2 tenants) leave ANY mined
   candidates at pilot scale, or is mining premature until more tenants
   dictate?
6. Did any ASR prompt actually clear the delta_pp > 0 gate? Was the
   per-section fixture recording burden acceptable?
7. Coverage honesty: once real telemetry accumulated, how far apart were
   synthetic and telemetry coverage@3?
8. Was the heuristic fluency fallback good enough, or does kenlm-uk need
   pinning as a real model artifact?
9. Quota matrix: which cells were hardest to fill, and did the 70/130
   tolerance band fire for the right reasons?
10. Phone-mic subset: were the 3-5 recordings made, and what did they do to
    WER? (Direct sprint-18 dependency.)
