# Roles

The realm defines five roles. The permission matrix lives at
`docs/auth/permissions.csv`; this file is its prose companion.

| Role           | Holds                                                   | Cannot                       |
| -------------- | ------------------------------------------------------- | ---------------------------- |
| `tenant_admin` | Onboarding, **user read/list, role management, deactivation/reactivation**, MFA reset, audit read/verify, tenant settings | Cross-tenant operations      |
| `clinician`    | Routine clinical user. Tenant-read. Reports (sprint 4+) | User admin (incl. user read); audit |
| `nurse`        | Limited clinical user. Same as clinician minus report sign | Most admin; user read     |
| `auditor`      | Read-only audit + tenant context + **read-only user roster (`user.read`)** | Any write                   |
| `service`      | Machine-to-machine token identity                       | Any user-facing operation today |

The full machine-readable matrix (every role × action × target_kind, with an
explicit `true|false` for each) lives in `docs/auth/permissions.csv`; the
`libs/auth.perms.ALLOW` runtime gate mirrors it and a CI test fails on any
drift or any missing (role × action) combination.

There is **no global / cross-tenant super-admin role**. The DBA superuser
exists in the database but is never used by service code (ADR-0007).

## Picking a role at invite time

- A clinician who also runs the practice → assign **both** `tenant_admin`
  *and* `clinician`. A user can hold multiple realm roles.
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

## How role changes propagate

- Existing access tokens keep their old roles until they expire (up to
  15 minutes). For immediate revocation, follow the runbook's
  "Suspected token theft" path: `POST /admin/users/{sub}/deactivate`
  also calls `/logout` which revokes refresh + active sessions in
  Keycloak.
- Next refresh after a Keycloak-side role change carries the new role
  set in the new access token (Keycloak rebuilds the claim set on
  refresh, not on access-token verify).
