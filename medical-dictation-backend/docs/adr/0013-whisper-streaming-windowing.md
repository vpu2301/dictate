# ADR-0013 — Whisper streaming windowing (4 s window + 2 s overlap)

**Date:** 2026-06-05
**Status:** Accepted
**Deciders:** ML/MLOps lead, tech lead

---

## Context

Whisper is a 30-second batch model. To stream from it we slide a window
along incoming audio, transcribe each window independently, and align
across windows. Three knobs:

| Knob              | Smaller → effect                              |
| ----------------- | --------------------------------------------- |
| Window length     | lower latency floor, worse quality at edges   |
| Overlap           | better alignment, more GPU work                |
| Commit policy     | quicker finals, more reversions               |

The constraint set (sprint-04 §9):

- Partial latency p95 ≤ 1100 ms from speech start.
- Final latency p95 ≤ 2500 ms after silence boundary.
- Streaming WER within 1 absolute point of sprint-03 batch WER on the
  same audio.
- Per-window inference p95 ≤ 200 ms on RTX 4080.
- 4 concurrent sessions per worker.

## Decision

Window = **4 s**, overlap = **2 s**, partial-minimum = **1.5 s**.

Commitment policy: a word graduates from PARTIAL to FINAL when ALL of:
1. Its session-absolute end time is older than one full window length.
2. A VAD-detected silence boundary lies between this word and the next.
3. The window's `no_speech_prob` ≤ 0.6.

Token alignment between overlapping windows: Levenshtein word-level
edit alignment; keep the higher-probability transcription per aligned
pair; drop unaligned words below `keep_threshold=0.3` probability.

Prompt biasing: `initial_prompt` is the clinician's specialty prompt
concatenated with the last 150 tokens of finalized text. `<|...|>`
special tokens and voice-command markers stripped.

## Consequences

- 4 s windows × 2 s overlap means each second of audio is transcribed
  twice (once as the trailing-edge of the previous window, once as the
  leading-edge of the next). GPU cost is double a naïve non-overlapping
  scheme; the win is dramatically better boundary tokens.
- Partial latency floor: ~1.5 s of audio + ~200 ms inference + ~100 ms
  transport ≈ 1.8 s naïve. With windowing tricks (don't wait for the
  full 4 s window if 1.5 s already accumulated) we land closer to
  700 ms p50.
- WER cost vs batch: ~0.5–1.0 absolute points on UK/EN reference sets
  measured at sprint-04 day-5. Targets set at 1-point absolute parity.
- Hallucination on silence: Whisper sometimes confidently transcribes
  long silences as common phrases. The `no_speech_prob > 0.6` drop
  rule kills the worst offenders; a final manual scan of the pilot
  week-1 transcripts will confirm.

## Alternatives considered

- **Full-session re-transcribe** at every window (the openai-whisper
  reference style). Quality is highest but latency is unbounded as the
  session grows. Rejected.
- **Smaller windows (2 s + 1 s overlap)**: latency wins but quality at
  word boundaries degrades sharply because Whisper has too little
  context. WER regressed ~3 points on internal corpus. Rejected.
- **Streaming-native model (Conformer-Streaming, Riva)**: would
  eliminate the windowing dance entirely. Not adopted in sprint 4
  because (a) clinical Ukrainian quality is unknown for these models;
  (b) the WhisperEngine abstraction lets us swap later. Backlog.
- **Per-clinician fine-tuned model**: post-pilot, contingent on a DPIA
  + clinician audio + consent.

## Migration path to a streaming-native model

`WhisperEngine.transcribe_window(pcm, language, prompt, prev_text)` is
the abstraction. A streaming-native implementation would:

1. Maintain its own per-session decoder state in process memory.
2. Implement the same Protocol-shaped `transcribe_window` method but
   internally append to its state rather than re-decoding from scratch.
3. Return tokens with timestamps and `no_speech_prob` in the same
   `WindowResult` shape.

No protocol or repository changes needed.

## Trigger conditions for revisiting

- Streaming WER drifts > 1 absolute point above batch on the reference
  set for two consecutive nightly runs.
- A streaming-native model achieves better quality on Ukrainian clinical
  audio (validated by clinical content lead).
- GPU cost of windowing becomes a budget concern (unlikely — sprint 16
  capacity model has headroom).

---

## Amendment (sprint 14, 2026-07-26): the commit policy never committed

Running real audio end-to-end through the production streaming path for
the first time (`scripts/eval/run_conversation_e2e.py`, needed for
conversation mode) exposed **three defects in the as-built commit
policy that between them meant a session could emit partials forever
and finalize an EMPTY transcript.** No test caught them because the
committer's unit tests fed it inputs the windower cannot produce, and
sprint-04's chaos/load suites drive synthetic Opus frames that never
reach a commit decision. (The SPA has been on browser Web Speech, so no
live client exercised this path either.)

**1. The commit horizon was unreachable.** Rule 1 required a word to be
older than one FULL window (4 s), but `StreamingWindower.integrate`
only ever offers candidates drawn from the current 4 s window, so
`now_ms - w.end_ms < 4000` always held and *every* candidate was
rejected as `too_recent`.
*Fix*: the horizon is the OVERLAP (2 s) — the correct LocalAgreement
criterion, since the next window re-transcribes only the trailing
`overlap_s` and anything older can no longer be revised.
`Committer.evaluate` now takes `commit_horizon_ms` instead of
`window_seconds`, and the windower passes `overlap_s`.

**2. The VAD silence gate was unreachable in real speech.** Rule 2
requires a VAD silence boundary, and `VadConfig.min_silence_frames` was
25 frames = **500 ms of contiguous silence**. Measured inter-utterance
pauses in natural clinical dictation and in doctor↔patient turn-taking
run ~200–450 ms, so `last_silence_boundary_ms` returned `None` for
every window of real speech.
*Fix*: 12 frames = **240 ms**, matching the standard inter-pause
threshold and Silero's own turn-splitting default.

**3. Silence-gating had no backstop.** Even correctly tuned, rule 2
made transcript progress *conditional on the speaker pausing*. A
pause-free stretch (fast dictation, an animated consultation) would
hold words provisional indefinitely and lose them at finalize — data
loss in a medical record.
*Fix*: `commit_max_provisional_ms` (default 4000, 2× the horizon):
past it a word commits with reason `stale_commit` even without a
silence boundary. It is already outside the revision horizon, so
holding it back only risks losing it.

### Consequences

- Targets §9 are unchanged and still met on the CPU harness: partial
  p95 400 ms (budget 1100 ms) with both Whisper and the diarizer
  resident; diarization is ~10 % of per-window pipeline cost.
- The normal commit path is still silence-gated, so the "final p95
  ≤ 2500 ms after a silence boundary" target keeps its meaning; the
  backstop only bounds the pathological case.
- Regression coverage now sits at the level where the bug lived:
  `tests/unit/test_windower_commits.py` drives the real
  `StreamingWindower` and asserts finals are produced, so an
  unreachable commit horizon fails CI instead of shipping.
- **The A10G rig must re-verify these numbers with `large-v3`**
  (todo.md, S14): window inference there is ~5× faster than the CPU
  `tiny` used here, which changes the cadence these thresholds sit in.
