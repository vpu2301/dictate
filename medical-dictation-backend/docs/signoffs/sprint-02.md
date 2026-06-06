# Sprint 02 — Sign-off Register

**Status:** draft / awaiting review
**Sprint window:** 2026-05-09 → 2026-05-23 (target demo 2026-05-22)

Sprint 02 has a stricter sign-off chain than sprint 01 because its
primitives — identity, isolation, immutability — fail catastrophically.
Each row below names a reviewer, the artefact they reviewed, and the
date of sign-off. Mark `✅` only after the linked verification has
been completed against the running code.

---

## Tech lead (final review)

| Item                                                            | Reviewer | Date | Status |
| --------------------------------------------------------------- | -------- | ---- | ------ |
| Every PR landed cleanly; no force-pushes after review           | _________| ____ | ⬜     |
| Layered architecture intact (routers → domain; libs depend-only on libs they declared) | _________| ____ | ⬜     |
| `os.environ` only inside config.py (pre-commit hook holds)      | _________| ____ | ⬜     |
| `asyncpg.create_pool` only inside libs/db or documented escape (audit_writer) | _________| ____ | ⬜     |
| Every router endpoint uses `requires(action, target_kind)` for authorization | _________| ____ | ⬜     |
| `tenant_connection` is the only path for tenant-scoped DB I/O   | _________| ____ | ⬜     |

---

## Security lead (per § 9 of the sprint-02 spec)

| Item                                                            | Reviewer | Date | Status |
| --------------------------------------------------------------- | -------- | ---- | ------ |
| JWT: no algorithm confusion (RS256-only)                        | _________| ____ | ⬜     |
| JWT: no `kid` confusion (JWKS keyed by `iss`)                   | _________| ____ | ⬜     |
| JWT: clock-skew tolerance is exactly 30 s, configurable         | _________| ____ | ⬜     |
| JWT: `extra="forbid"` reviewed; every accepted claim enumerated | _________| ____ | ⬜     |
| RLS: every user-schema table has `relrowsecurity=true AND relforcerowsecurity=true` | _________| ____ | ⬜     |
| RLS: every table has at least one PERMISSIVE policy and the spec-mandated RESTRICTIVE where applicable | _________| ____ | ⬜     |
| Audit chain: no UPDATE / DELETE possible without DBA + explicit trigger-disable | _________| ____ | ⬜     |
| Audit chain: integrity verified across 1000-event test          | _________| ____ | ⬜     |
| Refresh-replay detection wired; tested against re-used token    | _________| ____ | ⬜     |
| Brute-force lockout enabled in Keycloak realm (5 fail / 60s)    | _________| ____ | ⬜     |
| No long-lived bearer tokens anywhere in code (only the 15-min access tokens) | _________| ____ | ⬜     |
| Cookie attributes correct (HttpOnly, Secure, SameSite=Strict, Path=/auth) for staging/prod env | _________| ____ | ⬜     |
| MFA disabled by pilot decision is **explicitly accepted in writing** by the security lead | _________| ____ | ⬜     |

---

## Data Protection Officer (DPO)

| Item                                                            | Reviewer | Date | Status |
| --------------------------------------------------------------- | -------- | ---- | ------ |
| Audit completeness for auth flows (catalogue at `docs/audit/event-kinds.md`) | _________| ____ | ⬜     |
| Audit retention policy decided (7 years sec; 1 year info — see drafted GDPR/2297-VI policy) | _________| ____ | ⬜     |
| No PHI in audit payloads (convention enforced by review)        | _________| ____ | ⬜     |
| DSAR policy: which audit events are subject-accessible vs sec-internal | _________| ____ | ⬜     |

---

## Per-engineer adoption

Each backend engineer confirms by signing here that they:

| Engineer | Used libs/auth, never rolled own | Used `requires()`, never inline | Audited via AuditWriter only | Never bypassed `tenant_connection` | Understands JCS contract (don't pre-canonicalize) | Date |
| -------- | -------------------------------- | ------------------------------- | ---------------------------- | ---------------------------------- | -------------------------------------------------- | ---- |
| ________ |        ⬜                        |          ⬜                    |        ⬜                   |        ⬜                          |             ⬜                                    | ____ |
| ________ |        ⬜                        |          ⬜                    |        ⬜                   |        ⬜                          |             ⬜                                    | ____ |
| ________ |        ⬜                        |          ⬜                    |        ⬜                   |        ⬜                          |             ⬜                                    | ____ |
| ________ |        ⬜                        |          ⬜                    |        ⬜                   |        ⬜                          |             ⬜                                    | ____ |

---

## SRE / DevOps

| Item                                                            | Reviewer | Date | Status |
| --------------------------------------------------------------- | -------- | ---- | ------ |
| Keycloak realm export is reproducible (`make dev-up` from clean) | _________| ____ | ⬜     |
| Grafana dashboard renders on real data                          | _________| ____ | ⬜     |
| All 6 alert rules fire under synthetic adversarial events       | _________| ____ | ⬜     |
| Nightly verify cron is wired in the target deployment           | _________| ____ | ⬜     |
| CI cycle time ≤ 8 minutes for the full mirror                   | _________| ____ | ⬜     |

---

## Composite

Sprint 02 is **done** when:

- All rows above are ✅.
- `make test` + `make test-integration-db` + `make check-rls` + `make check-audit-insert` + `make openapi-check` all PASS on a clean stack.
- The demo (per § 1 day 10) has been delivered and recorded.
- Retro doc (`docs/retros/sprint-02.md`) is filled in.

Any row left ⬜ at the planned demo date blocks the sprint close.
