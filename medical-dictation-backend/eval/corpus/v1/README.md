# WER Eval Corpus — v1

Sprint-07's reference set for the standing WER measurement (ADR-0019).

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
    transcript.txt    # gold (post-NLP-expected) transcript
    metadata.json     # see schema below
```

`metadata.json`:

```json
{
  "utterance_id": "uk-cardio-001",
  "language": "uk",
  "specialty": "cardiology",
  "duration_s": 18.3,
  "dictation_source": "anonymized_real"  // or "authored_by_linguist"
}
```

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
