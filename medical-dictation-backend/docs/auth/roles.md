# Roles

The realm defines six roles. The permission matrix lives at
`docs/auth/permissions.csv`; this file is its prose companion.

| Role           | Holds                                                   | Cannot                       |
| -------------- | ------------------------------------------------------- | ---------------------------- |
| `tenant_admin` | Onboarding, **user read/list, role management, deactivation/reactivation**, MFA reset, audit read/verify, tenant settings, templates, the patient roster (redacted to name + id since S15), PHI-free usage stats | Cross-tenant operations; **all clinical content** — notes, dictations, ASR jobs, reports (S14, ADR-0033) — and, since S15, a patient's demographics/timeline without a per-patient break-glass grant |
| `clinician`    | Routine clinical user. Tenant-read. Notes, dictations, ASR, reports (read/write/sign) | User admin (incl. user read); audit |
| `nurse`        | Limited clinical user. Same clinical reads/writes as clinician, minus `asr.cancel` | Most admin; user read     |
| `auditor`      | Read-only audit + tenant context + **read-only user roster (`user.read`)** + **one write: `user.remind_mfa`** (S21 — ask a user to enrol a second factor; changes nothing about the account) | Any write that alters an account: invite, roles, (de)activation, MFA reset |
| `service`      | Machine-to-machine token identity                       | Any user-facing operation today |
| `knowledge_admin` | Evidence-corpus curation only (EVA-S01): upload/review/approve/expire tenant corpus documents (`evidence.corpus.manage`), web-search domain allowlist (`evidence.domains.manage`) | Everything else — every pre-existing action carries an explicit `false` row; no clinical content, no user admin, no audit |

The full machine-readable matrix (every role × action × target_kind, with an
explicit `true|false` for each) lives in `docs/auth/permissions.csv`; the
`libs/auth.perms.ALLOW` runtime gate mirrors it and a CI test fails on any
drift or any missing (role × action) combination.

There is **no global / cross-tenant super-admin role**. The DBA superuser
exists in the database but is never used by service code (ADR-0007).

## Administrators are separated from PHI (S14, ADR-0033)

`tenant_admin` holds **no clinical permission**. `asr.*`, `dictation.*`,
`report.read`/`report.write` and `note.*` are clinician/nurse only. An
administrator sees the patient **roster** (the surface their job needs)
but not any patient's notes, dictations or reports.

This is a matrix over **roles, not people.** A practising doctor who also
runs the clinic holds *both* `tenant_admin` and `clinician`, and a
permission check passes on any granting role — their clinical access is
untouched. It is the admin-ONLY account that is restricted, which is why
the "clinician who also runs the practice → assign both" guidance below
matters more than it used to: assigning `tenant_admin` alone to a
practising doctor now takes their charts away.

Two doors are left open, on purpose:

- **`stats.read`** — PHI-free aggregate reads. Admits an admin to the
  report-search, ASR-job and dictation-session lists in a stripped
  projection (no titles, no snippets, no patient references, no
  transcripts, no result URLs), which is what keeps the business
  dashboard's counts working.
- **Break-glass** (`phi_access.request`) — access to **one** report, after
  a reason from a closed vocabulary and a password re-entry. The grant is
  time-boxed (60 min default), counted on every use, revocable, audited at
  `sec` severity, and the report's authors are notified. See ADR-0033 and
  `docs/runbooks/break-glass.md`.

An `auditor` gets `phi_access.read` — the log of who broke glass and why —
but never the reports themselves.

## Picking a role at invite time

- A clinician who also runs the practice → assign **both** `tenant_admin`
  *and* `clinician`. A user can hold multiple realm roles. Since S14 this
  is **required**, not merely tidy: `tenant_admin` alone carries no
  clinical access, so a doctor given only that role loses their charts.
- Compliance officer / external auditor → `auditor`. Doesn't need
  `tenant_admin`; the audit endpoints are independently role-gated.
- Read-only stakeholder who just needs login → `clinician` for now;
  finer-grained read-only role is a sprint-17 add.
- A machine that calls our API on a partner's behalf → `service`. The
  scope mechanism (Day 7) is wired for service tokens but sprint 02
  doesn't yet enforce per-scope checks.

## Changing a user's role

`PUT /admin/users/{sub}/roles` (tenant_admin only, `user.manage_roles`)
sets a user's realm roles. The body is `{ "roles": ["clinician", …] }`,
validated against the known realm-role catalogue (unknown role → 422). The
endpoint sets the full role set in Keycloak and mirrors the
highest-privilege role into the local `users.role` column (which holds a
single value). It emits a `user.role_changed` audit event (severity `sec`)
recording the old → new role set.

**Guardrail:** the endpoint refuses (409) to remove `tenant_admin` from the
*last* active tenant_admin of a tenant, so a tenant can never be left
without an administrator.

Other user-management endpoints: `GET /admin/users` (list, paginated),
`GET /admin/users/{sub}` (read one), `POST /admin/users/invite`,
`POST /admin/users/{sub}/deactivate`, and
`POST /admin/users/{sub}/reactivate`. All are RLS-scoped to the caller's
tenant; a cross-tenant `sub` returns 404 (no existence leak).

### MFA reminders (S21)

`POST /admin/users/{sub}/mfa-reminder` — `user.remind_mfa`, held by
`tenant_admin` **and `auditor`**. Records a standing request that the target
enrols a second factor:

- One row per (tenant, user) in `mfa_reminders`. A repeat ask bumps
  `reminder_count` and reopens a resolved row rather than inserting a second.
- It closes in exactly one place: `POST /auth/mfa/verify` stamps
  `resolved_at` in the same transaction that sets `users.mfa_enrolled_at`.
  There is no dismiss endpoint, by design — a reminder the subject can wave
  away measures nothing.
- Refusals: 409 already enrolled or deactivated, 422 reminding yourself, 404
  outside the tenant.
- Audited `user.mfa_reminded` at `sec`, and published as the
  `security.mfa_reminder` notification (bell + email) to the subject alone.
- **Not** behind `requires_mfa()`, unlike every other mutation here: it is how
  a clinic with no enrolments bootstraps, and it grants nothing.

`GET /admin/users` and `GET /auth/me` were widened to carry the state these
surfaces read — `mfa_enrolled_at` + `mfa_reminded_at`/`mfa_reminder_count` on
the roster, and `db_user.mfa_reminder` (requester's **role**, never their name)
for the subject's own banner.

## How role changes propagate

- Existing access tokens keep their old roles until they expire (up to
  15 minutes). For immediate revocation, follow the runbook's
  "Suspected token theft" path: `POST /admin/users/{sub}/deactivate`
  also calls `/logout` which revokes refresh + active sessions in
  Keycloak.
- Next refresh after a Keycloak-side role change carries the new role
  set in the new access token (Keycloak rebuilds the claim set on
  refresh, not on access-token verify).
