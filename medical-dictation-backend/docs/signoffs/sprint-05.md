# Sprint 05 Sign-off — NLP Post-Processing & Voice Commands

Cycle: 2026-06-09 → 2026-06-23.
Demo: 2026-06-23.
Composite DoD: `sprints/backend-todos/sprint-05-nlp-postprocessing-todos.md` § 10.

## Per-reviewer sign-off

| Role                  | Reviewer | Status   | Notes |
| --------------------- | -------- | -------- | ----- |
| Tech lead             |          | ☐ open   | Code review; pipeline orchestrator stage interface preserved; snapshot pattern correct. |
| Clinical content lead |          | ☐ open   | Voice command vocab medically appropriate; 50-sample number-normalization review; abbreviation directions. |
| Frontend lead         |          | ☐ open   | `operations` enum stable; confidence-span character indexing on rendered text; undo affordance 600 ms toast. |
| ML/MLOps lead         |          | ☐ open   | ADR-0014 punctuation choice; ADR-0015 rule-based numbers; fallback path verified; determinism. |
| Security lead (light) |          | ☐ open   | Input size caps; rate limits; no regex DoS; admin audits. |
| SRE/DevOps            |          | ☐ open   | Dashboards/alerts; cache eviction policy; CI cycle time. |
| Backend eng A         |          | ☐ open   | Pipeline orchestrator + idempotence. |
| Backend eng B         |          | ☐ open   | Date stage + abbreviation snapshot. |
| Backend eng C         |          | ☐ open   | Number normalization corpora. |

## Verification quick-checks

- [ ] `make ci` green (incl. import-linter contracts for `nlp_service`).
- [ ] `make migrate-up` applies 0011 + 0012 cleanly; reverse cycles cleanly.
- [ ] 70 sprint-05 unit tests pass (codec / state / operations / aligner / committer / orchestrator / abbreviation / confidence / number-norm UK + EN / date).
- [ ] Voice command TP ≥ 97%, FP ≤ 2% on the test corpus per language.
- [ ] Punctuation F1 ≥ 90% per language.
- [ ] Number-norm coverage ≥ 95% per language.
- [ ] Date-norm coverage ≥ 95% per language.
- [ ] Latency p95 ≤ 80 ms on a 50-word final.
- [ ] Dictation-service NLP integration: end-to-end dictation latency adds ≤ 100 ms.
- [ ] Pilot session week 2 captured; vocab + abbreviations updated based on real usage.
- [ ] OpenAPI snapshot in sync.

## Notable deviations from spec

(Capture as they arise during review.)
