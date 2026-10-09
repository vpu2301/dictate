# WER Eval Methodology (Sprint 07)

The standing-release-gate measurement that protects every downstream
sprint touching Whisper / prompts / NLP / models / audio from silent
regressions.

## Running the eval

```bash
make wer-eval-corpus
# equivalently:
uv run python scripts/eval/run_wer.py --corpus eval/corpus \
    --output eval/reports [--metrics-file <path>] [--dsn "$EVAL_DB_DSN"]
```

**The real gate runs on the Linux/GPU rig** (A10G), where the asr-worker
runtime deps pin `faster-whisper` and the engine loads `large-v3` on CUDA
(`fp16`). Those are the only numbers that count as a release signal.

**Local dev on macOS works out of the box** for plumbing checks:

- `faster-whisper` is excluded from asr-worker's *runtime* deps on macOS
  (it's mocked in tests); the eval pulls it into the shared dev venv via the
  macOS-gated `dev` dependency-group in the workspace `pyproject.toml`.
- `run_wer.py` auto-selects a CPU config on macOS when `MD_ASR_*` are unset:
  `MD_ASR_DEVICE=cpu`, `MD_ASR_COMPUTE_TYPE=int8`, `MD_ASR_MODEL=tiny`
  (an explicit env var always wins). The same defaults can be set by hand on
  any non-GPU box.
- Requires `ffmpeg` on `PATH` to decode `audio.wav` (`brew install ffmpeg`).
- The script needs Python ≥ 3.11 (uses `datetime.UTC`); always run it through
  `uv` (the managed 3.12 venv). A bare system `python3` is guarded with an
  actionable error.

> ⚠️ macOS / CPU / `tiny` numbers are **plumbing-only**, never a release
> signal — different model and precision than the gate. The v1 corpus also
> currently ships **8 placeholder fixtures** (synthetic tones, not speech), so
> every utterance scores WER = 1.0 until real audio is authored. Use the run
> to confirm the harness end-to-end (integrity check → decode → inference →
> WER/CER/RTF → JSON/Markdown/Prometheus/DB outputs), not for accuracy.

## The console's runs (migration 0091) — same contract, different mouth

Since migration 0091 a WER can also be produced without a terminal: record
in `/company/corpus/record`, publish a snapshot, press *Measure WER*. The
run transcribes through **asr-service's ordinary batch path** (same model,
same prompt catalogue, same read-time NLP pass a clinician's dictation
gets), scores each utterance against the snapshot's gold text, and stores
per-utterance WER/CER plus a run summary in `corpus_eval_runs`.

The measurement contract is the one defined below, not a second one:
`autocomplete_service/eval_wer.py` is the service-side twin of
`scripts/eval/wer_lib.py`, and
`services/autocomplete-service/tests/unit/test_corpus_eval_pipeline.py`
asserts token-for-token parity between them. Change one, the test fails
until you change the other.

What the console runs are **not**:

* **Not the release gate.** They score whatever model the reachable
  asr-worker is running — `tiny` on CPU for a laptop stack. Every run row
  stores its `model` (NOT NULL) and the console labels anything that is not
  `large-v3` as not a release signal. The gate stays `make wer-eval-corpus`
  on the rig, over the git-committed corpus.
* **Not a replacement for committing the corpus.** Export a snapshot
  (`GET /corpus/eval/export?snapshot_id=…`) and commit it into
  `eval/corpus/v1/` when you want the nightly standing gate to see those
  utterances. `build_corpus_manifest.py` now recurses into
  `subsets/<subset>/<id>/`, which it previously skipped — subset utterances
  were silently absent from the manifest, and therefore from the integrity
  check and the gate.

Aggregation matches the gate's: corpus WER is **weighted by reference
words**, and utterances that could not be scored (ASR failure, or German —
outside batch ASR's `uk|en`) are excluded from numerator and denominator
alike rather than counted as 1.0 or 0.0.

## Scoring

### WER (Word Error Rate)

- Tokenization: whitespace split after lowercase + non-alphanumeric strip
  (Unicode-aware for Cyrillic).
- Cost: Levenshtein on word sequences (substitution = insertion = deletion = 1).
- Normalization: WER = edit_distance / reference_length.

Ukrainian-specific notes:
- We **do not** strip case endings. "Інфаркту" vs "інфаркт" is a real
  error (the model produced the wrong case). jiwer's English-style
  tokenization would mask this; our custom tokenizer doesn't.
- Apostrophe (`'`) preserved.
- Diacritics not normalised.

### CER (Character Error Rate)

Same Levenshtein over character lists; case-insensitive; preserves
spaces. CER serves as a tiebreaker for utterances where WER is dominated
by a single substitution.

### RTF (Realtime Factor)

`RTF = audio_seconds / inference_wall_seconds`. RTF > 1 means
faster-than-realtime.

Sprint-07 target: **RTF p95 ≥ 5** on the eval rig (A10G GPU). The
nightly eval emits both p50 and p95; alert if p95 drops below 5.

### Number-norm score

For each category (BP / dose / frequency / date):

1. Extract category instances from reference (regex in `run_wer.py`).
2. Extract category instances from hypothesis (same regex).
3. Score = `|ref ∩ hyp| / |ref|`. Score 1.0 = every reference number
   surfaced correctly in the hypothesis.

Sprint-07 target: **≥ 95% on each category**.

## The corpus-v2 scoring protocol

Sprint-07's scoring answers "how many words did the model get wrong". That
is one number, and corpus-v2 §1.1 sets out why it is not yet evidence: it is
computed on samples of 4–7 utterances per category, it counts a
transcription-convention difference as a recognition error, and it averages
Whisper's silence hallucinations in with the real results. The six rules
below are the protocol that makes it evidence. They are additive — nothing
here changes what §Scoring above computes, and the nightly gate still gates
on raw WER.

### 1. Two WERs, reported side by side

**Raw** is §WER above, unchanged. **Normalised** applies a versioned rule set
(`autocomplete_service/data/eval_normalization_v1.json`) before scoring:

    lowercase → fold apostrophes/dashes/slashes → decimal comma to point →
    strip punctuation → collapse multi-word units ("мілімоль на літр" →
    mmol_l) → number words to digits in both languages, decimals and
    clinical idiom included ("сто сорок на дев'яносто" → "140 90",
    "one thirty eight" → "138") → drop the connector between two numbers →
    canonicalise units (міліграмів / мг / mg → mg).

Neither replaces the other. Raw alone over-reports numeric error and hides
real drug-name error behind it; normalised alone forgives formatting the
report generator does have to get right, and can be tuned into
meaninglessness by adding rules. **The gap between them is the interesting
number**: large means the model heard correctly and the writing convention
differs, small means it genuinely misheard.

What normalisation deliberately does NOT do: stem, lemmatise or strip
Ukrainian case endings (§WER's rule stands), and it does not touch
abbreviations — "А Те" vs "АТ" is what the abbreviations subset measures.

`normalizer_version` is stored on every run. Two runs under different rule
versions are not comparable on the normalised metric, and `/corpus/eval/
compare` refuses the pair rather than reporting the difference.

### 2. Dose accuracy

The share of utterances where EVERY numeric token — value, unit and
frequency — survived, compared for exact equality including order. Computed
only over utterances whose reference contains a number.

A dose is right or it is a different dose: 40 mg and 4 mg differ by one
character and by a factor of ten, and a metric that gave partial credit for
"close" would be the wrong metric for a prescription. For clinical safety
this matters more than mean WER.

### 3. CER alongside WER for numbers and drugs

Long rare words (drug names) punish WER disproportionately — one miss in a
five-word line is WER 0.2 whether the model produced a near-miss or a
different drug. CER is reported per bucket for the same reason it exists in
§CER: as the tiebreaker.

### 4. Hallucination flags

An utterance is quarantined when any of these hold:

| Flag | Condition |
| --- | --- |
| `wer_over_100` | raw WER > 1.0 — more errors than reference words, unreachable by mishearing |
| `known_hallucination` | the hypothesis contains a stoplist phrase ("Дякую за перегляд!", "Thanks for watching", subtitle credits) |
| `speech_too_short` | under 1 s of speech by the engine's VAD |
| `mostly_silence` | over 50% of the take is silence |

A flagged utterance **keeps its score and loses its vote**: excluded from
every average, reported in full in `summary.flagged` so it gets re-recorded.
Nothing is deleted. The last two flags need `vad_seconds_speech` from the
ASR metadata; where the engine does not report it they cannot fire, and the
item is unchecked rather than clean.

### 5. Confidence intervals

Every reported rate carries a 95% CI from a bootstrap over utterances (1000
iterations, seeded — see §Determinism). **The resampling unit is the
utterance, not the word**: words within one utterance are not independent
draws, and resampling words would report an interval several times too
narrow. Buckets under 10 utterances are marked `insufficient_data` — the
number stays visible, the flag is what stops it being quoted as a result.

### 6. Measurement conditions, recorded per run

`model` + revision, `beam_size`, language hints, prompt ids,
`nlp_pipeline_version`, `normalizer_version`, the corpus digest
(`corpus_sha256`) and the bootstrap seed. Anything the engine does not
report is stored as null with a reason — `temperature` is null today
because no ASR surface in this system exposes it, and a plausible default
written into an evidence field is worse than a visible gap.

Snapshot-to-snapshot comparison uses a **paired** bootstrap over the
utterances both runs scored, reporting Δ WER with a 95% CI. A CI that
straddles zero means the change is indistinguishable from resampling noise,
however good the point estimate looks.

### dev / test discipline

The corpus is split (migration 0092). v1 is frozen as the **test** set;
everything added since is **dev**. Tuning — dictionaries, initial prompts,
post-processing — is measured on dev. The official number comes only from
test. A run scores one set, the comparison endpoint refuses a dev-vs-test
pair, and importing into the test set requires an explicit confirmation.

## Determinism

The eval is deterministic by construction:

- Whisper `fp16` inference + greedy decoding (beam=5, no sampling).
- Sprint-05 NLP pipeline_version captured in `eval_runs.prompts_hash`.
- Sprint-03 prompts corpus SHA-256 captured.
- Whisper model name captured (`large-v3`).

If the same `(audio, model, prompts_hash, pipeline_version)` tuple
produces different output across runs, that's a determinism bug —
report to ML/MLOps lead.

## Baselining

After 3 consecutive runs at the new code state, ML/MLOps lead writes
the rolling average to `audit.eval_baseline`. Subsequent runs alert if:

- WER per language regresses by ≥ 1.0 percentage point absolute.
- RTF p95 regresses by ≥ 0.05.
- Number-norm accuracy drops below 95% on any category.

## Re-baselining rules (ADR-0019)

Re-baselining is permitted ONLY when:

- A new Whisper model version ships (recorded in ADR).
- A new NLP pipeline version ships (recorded in ADR).
- A corpus version bump documents an authorship change.

In every other case, a regression alert is a real signal — investigate
and fix, do not silence.

## Output formats

- JSON: `eval/reports/{run_id}.json` — full per-utterance breakdown.
- Markdown: `docs/eval/wer-history/{YYYY-MM-DD}.md` — appended daily.
- Prometheus textfile: `mdx_wer_overall{language}`,
  `mdx_wer_specialty{language,specialty}`, `mdx_cer_overall{language}`,
  `mdx_rtf_p95`, `mdx_number_norm_accuracy{category}`.
- DB: `audit.eval_runs` + `audit.eval_utterances` (sprint-07
  migration 0015).

## Edge cases

- **No-speech utterance (silence)**: WER = 0 if hypothesis is also
  empty; WER = 1 (full substitution) if hypothesis contains words.
- **Single-word reference**: substitution → WER = 1.0; high variance.
  Such utterances kept in the corpus for completeness but excluded from
  the per-specialty mean (counted in the overall mean).
- **Hyphenated compounds**: kept as single tokens.
- **Numbers in reference but not in regex categories**: counted at the
  word level but not in number-norm accuracy.
