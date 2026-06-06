# ADR-0015 — Rule-based number normalization

**Date:** 2026-05-13
**Status:** Accepted
**Deciders:** ML/MLOps lead, linguist consultant, clinical content lead

---

## Context

Sprint-05's Stage 3 transforms spelled-out and hybrid number
expressions into canonical short form. The clinical-correctness bar is
high — getting BP, dose, or HR wrong is patient-safety adjacent.

Options:

| Approach                       | Correctness on critical patterns | Determinism | Ops cost   |
| ------------------------------ | --------------------------------- | ----------- | ---------- |
| Rule-based (per-language)      | reviewable; bounded by corpus     | absolute    | none       |
| seq2seq fine-tune              | unknown on edge cases             | weak        | model load |
| Whisper-prompt biasing only    | inconsistent                      | none        | none       |

## Decision

Use a **rule-based per-language normalizer**. Patterns:

- BP: `NUM на/over NUM (UNIT?)` → `NUM/NUM[ UNIT]`.
- HR: `пульс NUM (ударів)? (за хвилину)?` → `пульс NUM/хв`.
- Dose: `NUM UNIT` → `NUM short(UNIT)`.
- Frequency: `NUM раз(ів) на (добу|день)` / `NUM times a day` → `Nx/day`.
- Decimal: `NUM цілих NUM` / `NUM point NUM` → `N,M` / `N.M`.
- Range: `від NUM до NUM` / `from NUM to NUM` → `N–M`.
- Time: `о пів на NUM_HOUR` / `half past N` → `HH:30`.
- Generic: `NUM UNIT` → `NUM short(UNIT)`.

Untagged numbers pass through unchanged — never aggressively rewritten
without a unit/pattern marker.

## Consequences

- **Correctness**: every pattern is reviewable. The clinical content
  lead reviews 50 random samples per language on day 9.
- **Determinism**: no random state, no float ops; same input → same output.
- **Latency**: p95 ≤ 10 ms on a 50-word segment.
- **Coverage**: ≥ 95% on the hand-authored corpus per language. The
  long tail of unusual phrasings passes through unchanged rather than
  wrong.
- **Maintainability**: a new pattern is 10–20 lines of Python + a
  corpus row. The linguist + clinical content lead update the rules
  directly without ML training.
- **Cost of being wrong**: bounded. Any clinical phrasing that surfaces
  in pilot becomes a new corpus row; regression test prevents
  recurrence.

## What's lost

- Idiomatic phrasings outside the corpus pass through unchanged. A
  clinician seeing "сто двадцять одна" with no following unit gets
  "сто двадцять одна" — not converted to "121". This is by design
  (untagged = pass through).
- Cross-language switching mid-text is unsupported.
- Ordinal declensions in Ukrainian are not fully covered; partial
  support for the common forms.

## Alternatives considered

- **seq2seq fine-tune**: black-box behaviour on critical-care inputs
  (BP, dose). Without an exhaustive eval harness we can't verify
  correctness on long-tail clinical phrasings. Rejected for sprint 5.
- **Whisper-prompt biasing only**: relies on the model to output
  already-normalized digits. Inconsistent on long-tail patterns.

## Trigger conditions for revisiting

- Corpus coverage plateau: when rule additions stop closing real-pilot
  errors, the rule-based system has hit its ceiling. At that point,
  evaluate seq2seq on a corpus large enough to validate correctness on
  BP / dose / HR (>5000 examples per language).
- A pilot clinician reports a clinically wrong normalization that
  isn't easily expressible as a rule.
