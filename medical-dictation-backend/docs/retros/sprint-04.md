# Sprint 04 Retrospective — Streaming Dictation (Whisper)

Date: 2026-06-09.
Facilitator: tech lead.
Participants: backend, ML/MLOps, frontend, SRE, security.

## What worked

(Filled at retro.)

## What didn't

(Filled at retro.)

## Action items

| # | Item | Owner | By |
| - | ---- | ----- | -- |

## Spec retrospective prompts (canonical TODO §7)

1. Did the partial-to-final transition feel smooth or jarring? If clinicians reported jarring revisions, tune the commitment policy.
2. Was the 5-minute reconnection window too short or too long?
3. Was the 4-concurrent-per-worker limit right on the target GPUs?
4. Did Opus → PCM glitches affect ASR quality? Quantify.
5. Did the sliding-window approach produce duplicated text in finals? Where did alignment de-dup miss?
6. Are `Heartbeat` and `TokenExpiring` useful to the frontend or noise?
7. Did the chaos suite catch real regressions, or theatrical?
8. Was the protocol spec stable, or did the frontend find ambiguities?
9. Did `extra="forbid"` create friction during dev, or did it pay off?
10. How often did `gpu_full` fire in dev?
