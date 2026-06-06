# ADR-0014 — Punctuation model selection

**Date:** 2026-05-13
**Status:** Accepted
**Deciders:** ML/MLOps lead, tech lead, clinical content lead

---

## Context

Sprint 05's punctuation stage runs after voice commands and before
number normalization. Inputs are lowercase, no-punctuation Whisper
output; outputs need correct sentence punctuation + casing in two
languages (Ukrainian + English) on medical text.

Options evaluated:

| Option                                          | UK quality | EN quality | Latency  | Ops cost          |
| ----------------------------------------------- | ---------- | ---------- | -------- | ----------------- |
| Whisper-native punctuation                      | poor       | OK         | free     | none              |
| Rule-based only                                 | poor       | poor       | <5 ms    | none              |
| `oliverguhr/fullstop-punctuation-multilang-large` | good     | good       | ~40 ms   | one model load    |
| In-house fine-tune                              | best       | best       | ~40 ms   | data + training  |

## Decision

Adopt **`oliverguhr/fullstop-punctuation-multilang-large`** as the
primary punctuation model, with deterministic rule-based post-edits and
a rule-based fallback when:

- model load fails at startup (readiness gate flips to 503),
- a per-call inference exceeds 250 ms,
- the operator sets `MDX_NLP_PUNCTUATION_DISABLED=true`.

The post-edits ALWAYS run on top of either path:
- Force capitalization after `.`, `!`, `?`.
- Capitalize first word of segment.
- Lowercase known units (мг/мл/мм рт. ст. / mg/ml/mmHg) following a number.
- Strip doubled punctuation.

## Consequences

- **Quality**: F1 ≥ 90% on the hand-authored corpus for both languages
  (sprint-05 day-3 target). Pilot session in day-9 confirms with real
  dictation.
- **Latency budget**: p95 ≤ 50 ms on a 50-word final; ~256-token
  budget per inference; long segments split + merge with overlap.
- **Determinism**: `torch.no_grad()`, no sampling, deterministic argmax
  decoding. Same input → same output.
- **Ops cost**: a single model load at process start (~30 s on CPU).
  Models are bundled in the image; no first-call network fetch in prod.
- **Multilingual**: the same model handles UK + EN. Fine-tunes per
  language are a future option.

## Alternatives considered

- **Whisper-native punctuation**: Whisper's punctuated mode is poor on
  Ukrainian medical text and varies by segment length. Rejected.
- **Rule-based only**: insufficient for clinical-feel; clinicians
  notice mis-punctuation in 30 seconds.
- **In-house fine-tune**: triggered only if pilot WER < 85% per
  language; data + DPIA cost rules it out for sprint 5. Backlog.

## Migration path off this model

The `PunctuationStage` is the only call site; a new model swaps in
behind the same async interface. The fallback path stays as a
permanent insurance policy regardless of which model wins later.

## Trigger conditions for revisiting

- UK F1 < 85% in production (pilot data).
- The model upstream is deprecated by Hugging Face.
- A streaming-capable punctuation model lands (would let partials
  receive punctuation; sprint-05 deliberately leaves partials
  punctuation-free per spec §2.4).
