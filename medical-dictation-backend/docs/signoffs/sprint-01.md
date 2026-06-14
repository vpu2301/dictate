# Sprint 01 — Sign-off Register

**Status:** reconstructed as-built / awaiting countersign
**Sprint window:** paved-road hardening (follows sprint-00 foundation)
**Reconstructed:** 2026-06-14
**Canonical spec:** [`docs/sprints/sprint-01.md`](../sprints/sprint-01.md)

Sprint 01 turns sprint-00's conventions into mechanically-enforced rules:
the pre-commit suite, the two custom AST gates (`no-os-environ`,
`no-direct-asyncpg`), the import-boundary policy, the hardened service
template, and the contributor process. This register is reconstructed
against the as-built repo — each row names the artefact and the
verification that backs it. Mark `✅` only after the linked verification
has been run against the current code; reviewer name + date go in the
columns.

---

## Tech lead (final review)

| Item                                                                                  | Reviewer | Date | Status |
| ------------------------------------------------------------------------------------- | -------- | ---- | ------ |
| `.pre-commit-config.yaml` installs clean; `pre-commit run -a` green on a clean tree    | ________ | ____ | ⬜     |
| Both custom gates reject their anti-pattern AND permit the sanctioned one (both directions) | ________ | ____ | ⬜     |
| import-linter contracts hold (`make lint-imports` green); a planted service→service import fails it | ________ | ____ | ⬜     |
| `cp -r services/_template services/_probe` boots; `/healthz` 200, `/readyz` 200/503; mypy `--strict` + bandit clean | ________ | ____ | ⬜     |
| `make ci` runs the documented gate set and matches CI (no drift)                       | ________ | ____ | ⬜     |
| **Delta §12.1:** decide whether `lint-imports` folds into `make ci` or stays a separate target | ________ | ____ | ⬜     |
| Glossary additions merged; ADR-0001…0005 cross-links resolve                           | ________ | ____ | ⬜     |

---

## Security lead

| Item                                                                                  | Reviewer | Date | Status |
| ------------------------------------------------------------------------------------- | -------- | ---- | ------ |
| gitleaks (`v8.18.4`) blocks a planted fake AWS key pre-commit; `detect-private-key` active | ________ | ____ | ⬜     |
| `no-os-environ-in-services` holds — env reads funnel through `config.py` + `Secret[T]` (excludes correct) | ________ | ____ | ⬜     |
| `no-direct-asyncpg` holds — DB access funnels through `tenant_connection`; `libs/db` exempt | ________ | ____ | ⬜     |
| Sensitive template config fields typed `Secret[…]`; `.env.local`/`*.pem`/`*.key` gitignored | ________ | ____ | ⬜     |
| `AUTH_BYPASS_DEV=true` logs a startup WARNING and is forbidden outside local dev       | ________ | ____ | ⬜     |
| PR template carries the security/PII checklist; security-adjacent diffs require security-lead review | ________ | ____ | ⬜     |
| `SECURITY.md` private-disclosure path is current; hook revs pinned (supply chain)      | ________ | ____ | ⬜     |

---

## SRE / DevOps

| Item                                                                                  | Reviewer | Date | Status |
| ------------------------------------------------------------------------------------- | -------- | ---- | ------ |
| `make pre-commit-install` wires both `commit` and `commit-msg` hooks on a clean machine | ________ | ____ | ⬜     |
| commitizen rejects a non-conventional commit message on `commit-msg`; a Conventional Commit passes | ________ | ____ | ⬜     |
| `make ci` is fast enough that nobody routes around it (cycle-time sanity)              | ________ | ____ | ⬜     |
| `make ci-with-db` variant runs the DB-dependent gates after `dev-up && migrate-up`     | ________ | ____ | ⬜     |
| `docs/onboarding.md` gets a new engineer to a green `make ci` inside 30 minutes        | ________ | ____ | ⬜     |
| **Delta §12.2:** create `docs/onboarding-friction.md` (friction loop) or drop the claim from the spec | ________ | ____ | ⬜     |

---

## Composite

Sprint 01 is **done** when:

- All rows above are ✅.
- §4 of the canonical spec verifies green on a clean machine (pre-commit
  `-a`, env gate, DB gate, gitleaks, commitizen, import-linter, template
  smoke, `make ci == CI`, onboarding 30-min).
- Every custom gate provably rejects its target anti-pattern and permits
  the sanctioned one — both directions tested (DoD §10b).
- A freshly copied service from `services/_template` passes mypy
  `--strict`, bandit, and health checks with zero infra edits (DoD §10d).
- The two as-built deltas in spec §12 are resolved (fold/keep
  `lint-imports`; create/drop `onboarding-friction.md`).
- Retro doc (`docs/retros/sprint-01.md`) is filled in.

Any row left ⬜ blocks the formal sprint-01 close. As of reconstruction
(2026-06-14) every gate, contract, and template artefact exists in the
repo; the rows await a named reviewer countersign against the running
code, and the two §12 deltas await a decision.
