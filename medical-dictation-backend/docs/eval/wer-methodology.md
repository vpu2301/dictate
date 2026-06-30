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
