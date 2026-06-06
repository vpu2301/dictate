# Sprint 03 — Whisper Batch ASR
## Post-Sprint TODO & Verification Specification (CTO-Level)

**Companion to:** `sprints/backend/sprint-03-whisper-batch.md`
**Type:** Engineering specification — work plan, verification protocol, and follow-through register
**Owner:** tech lead + ML/MLOps lead
**Reviewers:** every backend engineer, security lead, DPO, SRE/DevOps lead
**Audience:** the team executing the sprint and the leads signing it off

> This file is the source-of-truth post-sprint TODO. PR reviewers consult
> it; the demo's go/no-go is bound to §10 ("Definition of Done").
>
> The full canonical text lives in conversation history (the user-supplied
> spec). The file is regenerated when the spec changes; do not edit
> piecemeal — instead, open a follow-up sprint TODO and supersede.

## 1. Work plan (day-by-day) — see canonical spec.
## 2. Verification protocol — see canonical spec.
## 3. Sign-offs — see `docs/signoffs/sprint-03.md` (template per role).
## 4. Operational hygiene — see `docs/runbooks/asr-worker.md`.
## 5. Documentation hygiene
   - OpenAPI snapshot: `docs/api/asr-service-openapi.json`.
   - Architecture: `docs/architecture/asr.md`.
   - ADRs: 0009 (faster-whisper), 0010 (Redis Streams), 0011 (envelope).
   - Audit kinds: `docs/audit/event-kinds.md` § asr.*.
   - Glossary: 20+ new entries.
## 6. Coordination & hand-offs
   - Sprint 04 (streaming) reuses `WhisperEngine` and `EncryptedObjectStore`.
   - Sprint 05 (NLP) imports `libs/asr_models.TranscriptionOutput`.
   - Sprint 11 (patients) wires `audio_files.encounter_id` FK + erasure.
   - Sprint 16 (cloud) swaps `FileMasterKeyProvider` → `KmsMasterKeyProvider`.
## 7. Retrospective prompts — see `docs/retros/sprint-03.md`.
## 8. Risks — see canonical spec § 8 (E1–E10).
## 9. Out of scope — see canonical spec § 9.
## 10. Definition of done — sprint 3 ships when, and only when,
       (a) the spec §10 DoD is green,
       (b) all sign-offs in `docs/signoffs/sprint-03.md` are checked,
       (c) the verification protocol §2 of the canonical spec is green,
       (d) WER targets are met,
       (e) PHI never appears at rest plaintext (verified by `mc cat` on
           a stored object and by the new CI gate
           `scripts/ci/check-no-direct-object-storage.py`),
       (f) the master-key chaos test passes (worker refuses startup with
           runbook-pointing error when the key file is missing).
