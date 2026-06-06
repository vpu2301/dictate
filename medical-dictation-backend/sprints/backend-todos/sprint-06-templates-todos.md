# Sprint 06 — Templates & Section-Aware Dictation
## Post-Sprint TODO & Verification Specification (CTO-Level)

**Companion to:** `sprints/backend/sprint-06-templates.md`
**Type:** Engineering specification — work plan, verification protocol, follow-through register
**Owner:** tech lead + clinical content lead
**Reviewers:** every backend engineer, linguist consultant, frontend lead, security lead, SRE/DevOps lead

> Canonical text lives in conversation history. This file is the
> in-repo pointer; regenerate when the spec changes. Do not edit
> piecemeal — supersede with a new sprint TODO.

## 1. Work plan (day-by-day) — see canonical spec.
## 2. Verification protocol — see canonical spec § 2.
## 3. Sign-offs — see `docs/signoffs/sprint-06.md`.
## 4. Operational hygiene — see `docs/runbooks/templates.md`.
## 5. Documentation hygiene
   - JSONB schema: `libs/template_models/`.
   - Architecture: `docs/architecture/templates.md`.
   - ADR: 0016 (JSONB + cosmetic-vs-structural rule + SwitchSection amendment).
   - Authoring guide: `docs/clinical-content/template-authoring.md`.
   - Audit kinds: 6 new in `docs/audit/event-kinds.md` (template.*, dictation.section_switched).
   - Glossary: schema_version, parent_template_id, voice_alias, ASR prompt token budget, structural / cosmetic edit, in-process cache, switch_section, MoH Order 110, FHIR DiagnosticReport.
## 6. Coordination & hand-offs
   - Sprint 07 (HF eval) reuses per-section WER harness.
   - Sprint 08 (reports) FK `template_id` ON DELETE RESTRICT; persists `template_version` at finalize.
   - Sprint 11 (encounters) reads template metadata.
   - Sprint 13 (anamnesis) reads `field_type` per section.
   - Sprint 17 (admin + FHIR) surfaces existing endpoints + adds re-bind UI.
## 7. Retrospective prompts — see `docs/retros/sprint-06.md`.
## 8. Risks — see canonical spec § 8 (E1–E10).
## 9. Out of scope — see canonical spec § 9.
## 10. Definition of done — sprint 6 ships when:
       (a) canonical spec §9 DoD green,
       (b) verification §2 of the canonical spec green incl. WER target ≥ 1 pp,
       (c) sign-offs in `docs/signoffs/sprint-06.md` all checked,
       (d) ADR 0016 + amendment published,
       (e) demo executed per canonical spec § 13.
