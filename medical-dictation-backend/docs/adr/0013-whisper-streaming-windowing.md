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
