# ADR-0037: Audio replay — clip-on-demand over the GCM envelope, token-streamed

Date: 2026-08-02
Status: Accepted
Sprint: 15

## Context

Sprint-08 reserved `ReportSection.transcript_segment_ids` "for sprint-15
note review"; per-word timings exist since sprint 04. A clinician
reviewing a note taps a sentence and hears that exact moment — the
trust surface behind ASR + generated text. Constraints discovered:

- **The envelope is whole-object AES-256-GCM** (libs/crypto): one
  IV/tag, no chunked mode — **a GCM envelope cannot be range-read
  without full decrypt**. Verified against `Envelope.decrypt` and
  `EncryptedObjectStore.get` (no `Range=` support). The only seekable
  primitive (AES-CTR, `crypto/stream.py`) is explicitly not for at-rest
  data.
- **Presigned URLs serve ciphertext** (platform rule 3, ADR-0011) — the
  spec's "presigned clip_url" is unusable by an `<audio>` element.
- `transcript_segment_ids` is populated **only** by the sprint-14
  conversation draft; dictation-mode segments carry no `id` at all, and
  the finalize path commits to a byte-identical legacy transcript shape
  (the sprint-14 wire-defect incident class forbids casually adding keys).
- report-service owns every report-read control (purpose gate,
  break-glass, `report.read`) but had no S3/crypto wiring; dictation-
  service has the wiring but is a capacity-constrained inference fleet.

## Decision

**Clip-on-demand in report-service**: fetch encrypted object → decrypt
whole in memory → normalize to s16le/16k/mono PCM (stdlib `wave` fast
path for session WAVs; ffmpeg subprocess for batch containers) → pure
byte-math slice by ms (±300 ms pad) → ffmpeg Ogg/Opus 24 kbps → store
encrypted in `mdx-audio-clips` (AAD = clip_id) → return a **tokenised
stream URL**, not a presign: HMAC token (`{exp}.{hmac}`, domain
`mdx-audio-clip-v1`, bound to tenant+clip, 5-min TTL — the DSAR
download-token idiom, ADR-0028) redeemed at authenticated
`GET /v1/audio-clips/{clip_id}?t=…` which decrypts and streams
`audio/ogg`.

- **Wire deviation from the spec**: `POST /v1/audio-clips` takes
  `{report_id, start_ms, end_ms}`, not `session_or_audio_ref` — the
  purpose gate and author check are report-anchored; a raw session ref
  would bypass both. The server resolves `source_session_id` /
  `source_asr_job_id` → `audio_files`.
- **Ephemeral derivatives, never a second PHI copy**: Redis registry
  (`SETEX 300`) is the real lifetime; 1-day bucket ILM is the ciphertext
  backstop (mc has no sub-day granularity); the tenant-KEK envelope
  means erasure crypto-shreds clips with everything else. No new table →
  no erasure-fanout registration, nothing for DSAR to export.
- **410 honesty taxonomy** (problem `code`): `no_audio_source` (never
  dictated from audio) / `audio_not_retained` (store disabled or
  purge-on-finalize) / `audio_erased` (retention/erasure; the
  `ObjectNotFoundError` idiom) / `audio_partially_retained` (tmpfs ring
  wrapped — the requested range predates the surviving window, computed
  from `total_audio_ms − duration_ms`). Never 416 — that is HTTP-Range
  semantics.
- **Segment listing degrades honestly**: sections with
  `transcript_segment_ids` map 1:1 (conversation drafts, with `speaker`/
  `speaker_role` per segment); everything older returns the whole
  session transcript addressed by `index` + timings, `segment_id: null`.
  **No ids are minted for dictation-mode segments** — replay needs only
  ms ranges, and the byte-identical finalize guarantee stays intact.
- Caps: span ≤ 60 s, 30 clips/user/hour (Redis fixed window, fail-open)
  — replay is review, not export. Every creation audited
  (`report.audio_replayed`; break-glass adds `phi_access.used` with
  `surface="audio_clip"`).
- Cross-service reads: report-service reads `dictation_sessions` /
  `audio_files` / `transcription_jobs` over RLS-scoped connections —
  the core-service timeline/erasure precedent extended.

## Consequences

- A 30-min session WAV is ~58 MB decrypted in memory per clip request —
  acceptable at review volumes (30/user/h cap); a chunked-envelope v2
  would be the escape hatch if it ever isn't.
- report-service gains ffmpeg + S3/crypto wiring (Dockerfile, compose,
  master-key mount) — env names identical to dictation-service.
- Back-filling `transcript_segment_ids` on signed versions is impossible
  without an amendment (it sits inside the JCS-signed bytes); the
  whole-transcript fallback is the permanent answer for old reports.
