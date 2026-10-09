# WER Eval Corpus — v1

Sprint-07's reference set for the standing WER measurement (ADR-0019).

> **v1 IS FROZEN (corpus-v2 §1.2).** As of migration 0092 this set is the
> **holdout**: it is what release numbers are measured on, and it does not
> grow. New replicas go to the **dev** set (`eval/corpus/v2/`), where
> dictionaries, prompts and post-processing may be tuned. Measure test only
> for release snapshots — tuning against the set you report on is how a
> corpus stops measuring anything.
>
> The scoring protocol every number here obeys — raw AND normalised WER,
> dose accuracy, CER, hallucination flags, bootstrap confidence intervals,
> and the per-run record of measurement conditions — is
> **`docs/eval/wer-methodology.md` §The corpus-v2 scoring protocol**. Two
> numbers taken under different `normalizer_version`s or different corpus
> digests are not comparable, and the tooling refuses to compare them.

## Inventory

- **60 UK utterances** distributed: 20 cardiology, 10 endocrinology,
  10 internal medicine, 10 radiology, 10 general.
- **60 EN utterances**, parallel distribution.
- Total duration: ~30 min per language.

## Layout

Each utterance lives in its own directory under
`eval/corpus/v1/<utterance_id>/`:

```
<utterance_id>/
    audio.wav         # 16 kHz mono PCM, 16-bit
    transcript.txt    # gold transcript, in SPOKEN form (../README.md)
    metadata.json     # see schema below
```

`metadata.json`:

```json
{
  "utterance_id": "uk-cardio-001",
  "language": "uk",
  "specialty": "cardiology",
  "duration_s": 18.3,
  "dictation_source": "anonymized_real",  // or "authored_by_linguist" / "authored_by_clinician"
  "subset": "numbers_doses_units"          // optional, sprint-21 adversarial subsets only
}
```

`dictation_source` values: `anonymized_real`, `authored_by_linguist`,
`authored_by_clinician` (sprint 21 — we have no linguist; "authored by
clinician" is the honest value for clinician-recorded utterances).

## Adversarial subsets (sprint 21 §8)

A clean corpus flatters the WER number and hides what actually annoys
clinicians. Sprint 21 adds hostile subsets under
`eval/corpus/v1/subsets/<subset>/` (same per-utterance layout, listed in
the same manifest, optional `subset` metadata field):

| Subset directory | Why |
| --- | --- |
| `numbers_doses_units` | sprint-05 normalizer retro item; highest-harm error class |
| `drug_names` | ДРЛЗ/formulary sampled; Whisper's worst category |
| `abbreviations` | АТ, ЧСС, HbA1c — abbreviation policy vs muscle memory |
| `code_switching` | UA clinician saying Latin/English drug + anatomy names mid-sentence |
| `voice_commands` | commands mid-dictation; regression guard for sprint-05 matcher FPR |
| `phone_mic_noisy` | phone-speaker distance + kitchen noise — **direct sprint-18 dependency**; record 3-5 even if nothing else lands |

Recording is done by the clinician + colleagues through the frontend
recorder, which writes 16 kHz mono PCM straight into this layout. Rebuild
the manifest after adding utterances: `python scripts/eval/build_corpus_manifest.py`.

## Privacy

- `anonymized_real` recordings: names, dates, IPNs replaced with
  linguist-authored stand-ins. Reviewed by clinical content lead +
  DPO.
- `authored_by_linguist` recordings: fully synthetic; no real-patient
  derivation.
- The corpus is part of the repo but the `.wav` files are committed
  to a Git LFS pointer (sprint-07 SRE wires LFS; for the audio assets
  themselves, contact the ML/MLOps lead).

## Manifest

`manifest.json` lists every utterance + SHA-256 of audio + transcript
+ metadata. CI verifies integrity on every PR touching the corpus.

## PII regex sweep

`scripts/eval/check_corpus_pii.py` runs on every PR; flags any
10-digit numbers, 7+ digit numbers near "ІПН"/"ID", or common
Ukrainian name patterns. Any hit blocks the merge until reviewed.

## Sprint-07 deliverable

For sprint-07 demo, this directory ships the manifest + 4 placeholder
fixtures (1 per specialty × 2 languages = 8) so the eval pipeline can
run end-to-end. The full 120-utterance corpus is authored by the
clinical content lead + linguist consultant in parallel and lands
between sprint-07 day-5 and day-9.

## Re-baselining

When the WER baseline shifts due to a deliberate model upgrade
(documented in an ADR), ML/MLOps lead manually updates
`audit.eval_baseline` after 3 consecutive runs at the new level. See
ADR-0019 for the rebasing rules.
