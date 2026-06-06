# Sprint 04 — Streaming ASR & Real-Time Dictation
## Post-Sprint TODO & Verification Specification (CTO-Level)

**Companion to:** `sprints/backend/sprint-04-streaming-asr.md`
**Type:** Engineering specification — work plan, verification protocol, follow-through register
**Owner:** ML/MLOps lead + backend engineer #2
**Reviewers:** tech lead, security lead, frontend lead, SRE/DevOps lead

> The canonical text lives in conversation history. This file is the
> in-repo pointer; regenerate when the spec changes. Do not edit
> piecemeal — supersede with a new sprint TODO.

## 1. Work plan (day-by-day) — see canonical spec.
## 2. Verification protocol — see canonical spec § 2.
## 3. Sign-offs — see `docs/signoffs/sprint-04.md`.
## 4. Operational hygiene — see `docs/runbooks/dictation.md` + nightly latency cron.
## 5. Documentation hygiene
   - Protocol spec: `docs/api/dictation-ws-v1.md`.
   - OpenAPI: `docs/api/dictation-service-openapi.json`.
   - ADRs: 0012 (WS vs WebRTC), 0013 (Whisper streaming windowing).
   - Threat model addendum: `docs/security/threat-model.md` § sprint-04.
   - Audit kinds: `docs/audit/event-kinds.md` § dictation.*.
   - Glossary: 25+ new terms.
## 6. Coordination & hand-offs
   - Sprint 05 (NLP) fills `voice_command` on every `final`; field shape locked.
   - Sprint 14 (diarization) forks to `medical-dictation.v2`.
   - Sprint 16 (cloud) wires HPA on `mdx_dictation_active_sessions`.
## 7. Retrospective prompts — see `docs/retros/sprint-04.md`.
## 8. Risks — see canonical spec § 8 (E1–E12).
## 9. Out of scope — see canonical spec § 9.
## 10. Definition of done — sprint 4 ships when:
       (a) canonical spec §12 DoD green,
       (b) verification §2 of the canonical spec green incl. all chaos
           scenarios + streaming-WER targets,
       (c) sign-offs in `docs/signoffs/sprint-04.md` all checked,
       (d) protocol spec `docs/api/dictation-ws-v1.md` matches frontend
           implementation byte-for-byte,
       (e) demo (§16 of canonical spec) executed.
