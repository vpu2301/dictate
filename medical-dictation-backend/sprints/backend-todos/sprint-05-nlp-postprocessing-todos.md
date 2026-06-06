# Sprint 05 — NLP Post-Processing & Voice Commands
## Post-Sprint TODO & Verification Specification (CTO-Level)

**Companion to:** `sprints/backend/sprint-05-nlp-postprocessing.md`
**Type:** Engineering specification — work plan, verification protocol, follow-through register
**Owner:** ML/MLOps lead + linguist consultant (UK + EN medical)
**Reviewers:** tech lead, clinical content lead, frontend lead, security lead, SRE/DevOps lead

> Canonical text lives in conversation history. This file is the
> in-repo pointer; regenerate when the spec changes. Do not edit
> piecemeal — supersede with a new sprint TODO.

## 1. Work plan (day-by-day) — see canonical spec.
## 2. Verification protocol — see canonical spec § 2.
## 3. Sign-offs — see `docs/signoffs/sprint-05.md`.
## 4. Operational hygiene — see `docs/runbooks/nlp.md`.
## 5. Documentation hygiene
   - Voice command catalogue: `docs/nlp/voice-commands.md`.
   - Number normalization: `docs/nlp/number-normalization.md`.
   - Date normalization: `docs/nlp/date-normalization.md`.
   - Abbreviation policy: `docs/nlp/abbreviations.md`.
   - OpenAPI: `docs/api/nlp-service-openapi.json`.
   - ADRs: 0014 (punctuation model), 0015 (rule-based numbers).
   - Audit kinds: `docs/audit/event-kinds.md` § `voice_command.*`, `abbreviation.policy.*`, `dictation.nlp_timeout`.
   - Glossary: 17 new entries.
## 6. Coordination & hand-offs
   - Sprint 06 (templates) reads `template_sections` from `ProcessingContext`; shape locked here.
   - Sprint 07 (eval) replays historical inputs with frozen `pipeline_version` + snapshot fingerprint; byte-equal output is the contract.
   - Sprint 08 (reports) consumes the post-processed transcript JSONB directly.
   - Sprint 13 (anamnesis) hooks anamnesis-specific commands via DB seed; FSM matcher reused.
   - Sprint 14 (conversation) voice commands disabled in conversation mode.
   - Sprint 17 (admin) surfaces abbreviation + voice-command admin UIs.
## 7. Retrospective prompts — see `docs/retros/sprint-05.md`.
## 8. Risks — see canonical spec § 8 (E1–E12).
## 9. Out of scope — see canonical spec § 9.
## 10. Definition of done — sprint 5 ships when:
       (a) canonical spec §14 DoD green,
       (b) verification §2 of the canonical spec green incl. all corpus targets, idempotence checks, security adversarials, and pilot-session feedback loop,
       (c) sign-offs in `docs/signoffs/sprint-05.md` all checked,
       (d) ADRs 0014/0015 published; voice-commands.md + number-normalization.md reviewed by clinical content lead,
       (e) demo executed per spec §18.
