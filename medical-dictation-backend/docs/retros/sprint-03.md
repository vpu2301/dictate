# Sprint 03 Retrospective — Batch ASR (Whisper)

Date: 2026-05-26.
Facilitator: tech lead.
Participants: backend, ML/MLOps, SRE, security, DPO.

## What worked

(Filled at retro.)

## What didn't

(Filled at retro.)

## Action items

| # | Item | Owner | By |
| - | ---- | ----- | -- |

## Spec retrospective prompts (from sprint-03-whisper-batch-todos.md §7)

1. Did Whisper warmup take longer than 60 s? If yes, plan pre-warm during pod readiness probe (sprint 16).
2. Did the VAD chunking ever produce mid-sentence cuts that hurt WER? Quantify; tune.
3. Did any prompt leak into the transcript output (a Whisper failure mode)? If yes, document mitigation; consider prompt sanitization.
4. Was the 8-step validation enough? Did anything malicious get through in adversarial corpus? Add what's missing.
5. Did Redis Streams handle worker rollouts gracefully? If consumer-group rebalance caused dropped messages, document and fix.
6. Was the master-key alert wired correctly? Test deliberately at retro time.
7. Was the realtime factor (audio_duration / infer_seconds) consistent with what we sized for? If not, capacity model is wrong; revise sprint 16 capacity ADR.
8. Did the encryption envelope cause measurable latency in upload or fetch? If p95 > 200 ms overhead, profile.
9. Did the quota enforcement fire false positives? Tune.
10. Did the pre-signed URL 5-min TTL cause UX problems for clinicians fetching results? If yes, frontend needs retry-on-403-then-refetch logic.
