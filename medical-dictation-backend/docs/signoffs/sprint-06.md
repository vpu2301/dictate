# Sprint 06 Sign-off — Templates & Section-Aware Dictation

Cycle: 2026-06-23 → 2026-07-07.
Demo: 2026-07-07.
Composite DoD: `sprints/backend-todos/sprint-06-templates-todos.md` § 10.

## Per-reviewer sign-off

| Role | Reviewer | Status | Notes |
| --- | --- | --- | --- |
| Tech lead             |  | ☐ open | Code review; cosmetic-vs-structural rule unambiguous. |
| Clinical content lead |  | ☐ open | 16 templates medically reviewed; MoH-110 alignment. |
| Linguist consultant   |  | ☐ open | UK + EN aliases; no false-positive collisions with content vocab. |
| Frontend lead         |  | ☐ open | `SwitchSection` protocol field + section-cursor UX. |
| ML/MLOps lead         |  | ☐ open | Per-section WER methodology + targets. |
| Security lead (light) |  | ☐ open | Cross-tenant RLS; no untrusted JSONB pathways. |
| SRE/DevOps            |  | ☐ open | Dashboards rendering; performance under load. |
| Backend eng A         |  | ☐ open | Pipeline + endpoint correctness. |
| Backend eng B         |  | ☐ open | Migrations + RLS adversarial. |
| Backend eng C         |  | ☐ open | dictation-service hookup. |

## Verification quick-checks

- [ ] `make ci` green (incl. validate-templates CI gate).
- [ ] `make migrate-up` applies 0013 + 0014 cleanly.
- [ ] `python scripts/validate-templates.py` shows 16 OK.
- [ ] `make seed-templates` upserts 16 system rows.
- [ ] 19 sprint-06 unit tests pass (template schema + classification).
- [ ] WER eval ≥ 1 pp improvement on cardiology UK.
- [ ] Cross-tenant RLS property test (1000 iters) green.
- [ ] Demo executed per canonical spec § 13.

## Notable deviations

(Capture as they arise.)
