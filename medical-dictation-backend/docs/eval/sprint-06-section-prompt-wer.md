# Sprint 06 — Per-Section ASR Prompt WER Evaluation

**Goal:** ≥ 1 percentage point WER improvement on UK cardiology when
using the section-specific prompt vs sprint-03's global cardiology
prompt. Documented in ADR-0016.

## How to run

```sh
make wer-eval-per-section
# or
uv run python scripts/eval/run_per_section_wer.py \
    --fixtures scripts/eval/fixtures/sprint-06-cardiology \
    --template infra/seeds/templates/cardiology_outpatient_uk.json \
    --fail-on-no-improvement
```

## Demo result (2026-06-23)

Reference set: 30 UK cardiology dictations curated by linguist + clinical content lead.

| Section        | n  | Global WER | Section WER | Δ pp |
| -------------- | -- | ---------- | ----------- | ---- |
| anamnesis      |    |            |             |      |
| examination    |    |            |             |      |
| investigations |    |            |             |      |
| diagnosis      |    |            |             |      |
| plan           |    |            |             |      |

(Filled at demo time.)

## Methodology

For each reference utterance:

1. **Global prompt run**: feed Whisper the sprint-03 cardiology prompt
   (the same prompt used by batch ASR sprint-03).
2. **Section-specific prompt run**: feed Whisper the
   `cardiology_outpatient_uk.json` template's per-section ASR prompt.
3. Compute WER (word-level Levenshtein) against the gold transcript.
4. Aggregate per section.

## What "≥ 1 pp improvement" means operationally

If the aggregate UK-cardiology improvement (sum-of-ref-words-weighted)
is ≥ 1 percentage point, the section-aware dictation feature is
**proven to add quality value**. If it's < 1 pp, the section-swap
mechanism still ships (sprint 8 reports need per-section structure
regardless), but the per-section prompts are flagged for re-authoring
by the clinical content lead.

## Nightly cadence

`make wer-eval-per-section` runs at 04:00 UTC; results emit to
Prometheus via the textfile collector. Alert if any section's
improvement drops > 0.5 pp below the demo baseline.
